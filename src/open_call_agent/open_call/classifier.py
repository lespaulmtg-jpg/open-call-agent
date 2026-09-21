"""
Local LLM classifiers (Qwen2.5-3B-Instruct via llama.cpp) that decide whether page text announces an open call.

Two backends share the same prompt and output checks:
- LocalClassifier runs the model in-process with llama-cpp-python. Calls must not overlap (use one worker).
- ServerClassifier calls a llama-server started by LlamaServer, which batches concurrent requests across parallel
  slots (continuous batching), so it can be called from several threads at once.

Benchmark on 88 page chunks from 20 Zurich festival websites (M1 Max): in-process with a JSON-schema grammar took 245s;
in-process without it 153s (llama-cpp-python's grammar sampling is slow); llama-server 154s with or without the grammar;
llama-server with 4 slots 130s, and 8 slots gave no further gain (prompt processing, not generation, is the limit).
So LocalClassifier generates unconstrained and retries with the grammar only when output doesn't parse, while
ServerClassifier always uses the grammar.
"""
import json
import logging
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import requests

from .dates import format_date, normalize_deadline
from .text import normalize_text

logger = logging.getLogger(__name__)

# (Hugging Face repo, file name or glob for split files). The small model checks every site; the large model reviews
# sites the small model found open or needing review.
SMALL_MODEL = ("Qwen/Qwen2.5-3B-Instruct-GGUF", "qwen2.5-3b-instruct-q4_k_m.gguf")
LARGE_MODEL = ("Qwen/Qwen2.5-7B-Instruct-GGUF", "qwen2.5-7b-instruct-q4_k_m-*.gguf")
CONTEXT_TOKENS = 8192  # Per sequence (per server slot)
MAX_OUTPUT_TOKENS = 600
CHUNK_CHARS = 12000  # Roughly 3-4k tokens, leaving room for the prompt and answer
CHUNK_OVERLAP = 500
MAX_CHUNKS_PER_PAGE = 4  # Text beyond this is not classified, and flagged in the report
MIN_QUOTE_CHARS = 15  # Shorter quotes match too easily to be evidence
STATUS_RANK = ["open", "not_yet_open", "unclear", "closed", "none"]

OPPORTUNITY_TYPES = ["music", "dj", "live_performance", "visuals", "video_film", "installation", "workshops",
                     "talks", "residency", "exhibition", "vendors_food", "volunteers", "other"]

CLASSIFIER_SYSTEM_PROMPT = f"""You review the text of one web page from an event, venue, festival or arts organisation website \
and decide whether it announces an open call: an invitation for outside people to apply, submit or propose something \
(e.g. to play music, DJ, perform, show visuals or art, run a workshop, give a talk, join a residency, run a stall, volunteer).

The goal is to avoid false positives. These are NOT open calls:
- A menu, footer or link label that merely says "Open Call" without describing the opportunity on this page.
- Job or employment adverts for paid staff positions.
- Generic contact details, booking enquiries, press contacts, newsletter sign-ups, ticket sales, or guest-list forms.
- Descriptions of past programmes that were selected through a call, unless the call itself is described.
- Staff credits such as "Open Call Adviser".
- Applications to join as an audience member: memberships, fan or community clubs, loyalty programmes, ticket \
presales, or guest lists.

An invitation to send proposals, applications or submissions DOES count, even if it is short and gives no deadline \
or details. For example "If you want to send a proposal for next year, use the following link. Send us your proposal." \
is an open call with status "open". A link to an application or submission form is strong evidence.

Set status:
- "open": the page invites people to apply or submit now. If a deadline is stated it must be on or after today's date. \
A call that accepts submissions now for a future festival, edition or year is "open".
- "not_yet_open": the page says applications cannot be submitted yet, because the call opens at a later date.
- "closed": the call is described but its deadline has passed or the page says it is closed.
- "unclear": a call is described but you cannot tell whether it is open.
- "none": the page does not describe an open call.

For every status other than "none", copy one to three quotes from the page text that prove it. Quotes must be copied \
exactly, character for character, and be short (under 200 characters). Use opportunity_types only from this list: \
{", ".join(OPPORTUNITY_TYPES)}. Use an empty list when status is "none".

Respond with only a JSON object, no other text, with these keys in this order. Write the reasoning first, then choose \
the status that matches it:
{{"reasoning": "<one or two sentences>", "status": "<open|not_yet_open|closed|unclear|none>", "opportunity_types": ["<type>", ...], \
"quotes": ["<exact quote>", ...], "deadline": "<deadline or empty>", "how_to_apply": "<how to apply or empty>"}}"""

# Reasoning comes first so the model decides the status after explaining it. With the status first, the 7B model
# labelled an information event "upcoming" while its own reasoning said nobody was invited to apply. The label is
# "not_yet_open" rather than "upcoming" because the 7B model read "upcoming" as "for an upcoming festival".
CLASSIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string", "description": "One or two sentences explaining the decision."},
        "status": {"type": "string", "enum": ["open", "not_yet_open", "closed", "unclear", "none"]},
        "opportunity_types": {"type": "array", "items": {"type": "string", "enum": OPPORTUNITY_TYPES}},
        "quotes": {"type": "array", "items": {"type": "string"}},
        "deadline": {"type": "string", "description": "Deadline as written on the page, or empty string."},
        "how_to_apply": {"type": "string", "description": "Application method (form URL, email, ...), or empty string."},
    },
    "required": ["reasoning", "status", "opportunity_types", "quotes", "deadline", "how_to_apply"],
    "additionalProperties": False,
}


def split_text(text):
    """
    Split page text into overlapping chunks the model can take in one call.

    :param text: The page text.
    :return: A list of chunks, at least one. Callers decide how many of them to classify (MAX_CHUNKS_PER_PAGE).
    """
    return [text[start:start + CHUNK_CHARS] for start in range(0, len(text), CHUNK_CHARS - CHUNK_OVERLAP)] or [""]


def most_positive(results):
    """
    :param results: Classification dicts.
    :return: The most positive one, by STATUS_RANK.
    """
    return min(results, key=lambda result: STATUS_RANK.index(result["status"]))


def verify(result, evidence_text, where="the page"):
    """
    Check the model's evidence in code, the same way for both models: a quote it gave must appear verbatim in the text
    it was given, and a deadline that has passed means the call is closed, whatever the model said.

    :param result: A classification dict, updated in place.
    :param evidence_text: The normalised text the model classified.
    :param where: How to refer to that text in the reason, e.g. "the page" or "the pages".
    :return: The result.
    """
    result["verified_quotes"] = [quote for quote in result["quotes"]
                                 if len(quote) >= MIN_QUOTE_CHARS and normalize_text(quote) in evidence_text]
    if result["status"] != "none" and not result["verified_quotes"]:
        # The model's evidence isn't in the text, so don't trust the positive
        result["reasoning"] = f"Downgraded: no quote found verbatim on {where}. Model said: {result['reasoning']}"
        result["status"] = "unclear"

    # Deadlines are stored as YYYY-MM-DD; the text as written on the page is kept in deadline_text
    result["deadline_text"] = result["deadline"]
    deadline = normalize_deadline(result["deadline"])
    result["deadline"] = format_date(deadline)
    if result["status"] == "open" and deadline and deadline < date.today():
        result["reasoning"] = f"Downgraded: deadline {deadline} has passed. Model said: {result['reasoning']}"
        result["status"] = "closed"
    return result


def build_messages(url, title, text):
    """
    Build the chat messages for one piece of page text. The system prompt is identical for every call, so llama.cpp
    reuses its cached prompt processing.

    :param url: The page URL.
    :param title: The page title.
    :param text: The page text (or a chunk of it).
    :return: A list of chat messages.
    """
    return [{"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": f"Today's date: {date.today().isoformat()}\nPage URL: {url}\n"
                                        f"Page title: {title}\n\n<page_text>\n{text}\n</page_text>"}]


def parse_classification(content):
    """
    Parse and validate model output against CLASSIFIER_SCHEMA.

    :param content: The model's text output, possibly wrapped in a Markdown code fence.
    :return: A dict matching CLASSIFIER_SCHEMA.
    :raises ValueError: If the output isn't a valid classification.
    """
    match = re.search(r"\{.*\}", content or "", re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object in model output: {content[:200]!r}")
    try:
        result = json.loads(match.group(0))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in model output: {error}")

    if not isinstance(result, dict) or result.get("status") not in STATUS_RANK:
        raise ValueError(f"Invalid status in model output: {str(result)[:200]}")
    for key in ("opportunity_types", "quotes"):
        if not isinstance(result.get(key), list) or not all(isinstance(item, str) for item in result[key]):
            raise ValueError(f"'{key}' must be a list of strings")
    for key in ("deadline", "how_to_apply", "reasoning"):
        if not isinstance(result.get(key, ""), str):
            raise ValueError(f"'{key}' must be a string")

    return {
        "status": result["status"],
        "opportunity_types": [kind for kind in result["opportunity_types"] if kind in OPPORTUNITY_TYPES],
        "quotes": result["quotes"],
        "deadline": result.get("deadline", ""),
        "how_to_apply": result.get("how_to_apply", ""),
        "reasoning": result.get("reasoning", ""),
    }


def resolve_model_path(model_path=None, model=SMALL_MODEL):
    """
    :param model_path: Path to a GGUF file, or None to use model from the Hugging Face cache.
    :param model: A (repo, file name or glob) tuple, e.g. SMALL_MODEL or LARGE_MODEL.
    :return: The local model path (the first shard for split models), downloading the model if needed.
    """
    if model_path:
        return str(model_path)
    repo, pattern = model
    if "*" not in pattern:
        from huggingface_hub import hf_hub_download
        return hf_hub_download(repo_id=repo, filename=pattern)
    from huggingface_hub import snapshot_download
    # llama.cpp loads the remaining shards of a split model from the same directory
    return str(sorted(Path(snapshot_download(repo, allow_patterns=[pattern])).glob(pattern))[0])


class GPUInUseError(RuntimeError):
    pass


_gpu_lock = threading.Lock()
_gpu_holder = None


def acquire_gpu(holder):
    """
    Claim the GPU for one model. Only one model may be loaded at a time.

    :param holder: A description of the model, for error messages.
    :raises GPUInUseError: If another model still holds the GPU.
    """
    global _gpu_holder
    with _gpu_lock:
        if _gpu_holder is not None:
            raise GPUInUseError(f"Cannot load {holder}: {_gpu_holder} is still loaded")
        _gpu_holder = holder


def release_gpu(holder):
    """
    :param holder: The description passed to acquire_gpu.
    """
    global _gpu_holder
    with _gpu_lock:
        if _gpu_holder == holder:
            _gpu_holder = None


class ClassifierStats:
    """
    Thread-safe counters for classifier calls.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = 0
        self.grammar_fallbacks = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def record(self, usage, fallback=False):
        with self.lock:
            self.calls += 1
            self.grammar_fallbacks += int(fallback)
            self.prompt_tokens += (usage or {}).get("prompt_tokens", 0)
            self.completion_tokens += (usage or {}).get("completion_tokens", 0)

    def as_dict(self):
        with self.lock:
            return {"calls": self.calls, "grammar_fallbacks": self.grammar_fallbacks,
                    "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens}


class BaseClassifier:
    """
    Shared unconstrained-first generation with a grammar-constrained retry.
    """

    # Maximum concurrent classify() calls this classifier supports
    max_concurrency = 1

    def __init__(self, constrained=False):
        """
        :param constrained: Always constrain output with the JSON-schema grammar instead of only on retry.
        """
        self.constrained = constrained
        self.stats = ClassifierStats()

    def complete(self, messages, constrained):
        """
        :return: A tuple of (output text, usage dict).
        """
        raise NotImplementedError

    def classify(self, url, title, text):
        """
        Classify one piece of page text.

        :param url: The page URL.
        :param title: The page title.
        :param text: The page text (or a chunk of it).
        :return: A dict matching CLASSIFIER_SCHEMA.
        :raises Exception: If generation fails, or constrained output still doesn't validate.
        """
        messages = build_messages(url, title, text)
        if not self.constrained:
            content, usage = self.complete(messages, constrained=False)
            try:
                result = parse_classification(content)
            except ValueError as error:
                logger.debug("%s: unconstrained output invalid (%s), retrying with grammar", url, error)
                self.stats.record(usage)
            else:
                self.stats.record(usage)
                return result

        content, usage = self.complete(messages, constrained=True)
        self.stats.record(usage, fallback=not self.constrained)
        return parse_classification(content)

    def close(self):
        pass


class LocalClassifier(BaseClassifier):
    """
    Runs the model in-process with llama-cpp-python. Not safe for concurrent calls.
    """

    def __init__(self, model_path=None, constrained=False, model=SMALL_MODEL):
        """
        :param model_path: Path to a GGUF file, or None to use model from the Hugging Face cache.
        :param constrained: Always constrain output with the JSON-schema grammar.
        :param model: A (repo, file) tuple used when model_path is None.
        """
        super().__init__(constrained)
        from llama_cpp import Llama

        path = resolve_model_path(model_path, model)
        self.gpu_holder = f"in-process model {Path(path).name}"
        acquire_gpu(self.gpu_holder)
        logger.info("Loading model %s", path)
        try:
            self.llm = Llama(model_path=path, n_ctx=CONTEXT_TOKENS, n_gpu_layers=-1, verbose=False)
        except Exception:
            release_gpu(self.gpu_holder)
            raise

    def close(self):
        if getattr(self, "llm", None) is not None:
            self.llm.close()
            self.llm = None
        release_gpu(self.gpu_holder)

    def complete(self, messages, constrained):
        extra = {"response_format": {"type": "json_object", "schema": CLASSIFIER_SCHEMA}} if constrained else {}
        response = self.llm.create_chat_completion(messages=messages, temperature=0, max_tokens=MAX_OUTPUT_TOKENS,
                                                   **extra)
        return response["choices"][0]["message"]["content"], response.get("usage")


def find_llama_server():
    """
    Find the llama-server binary, on the PATH or next to the running Python, so it is also found when the conda
    environment isn't activated, e.g. for a job the dashboard starts.

    :return: The path to llama-server.
    :raises RuntimeError: If it isn't installed.
    """
    binary = shutil.which("llama-server") or shutil.which("llama-server", path=str(Path(sys.executable).parent))
    if binary is None:
        raise RuntimeError("llama-server not found. Install it into the conda env: "
                           "conda install 'pkgs/main::llama.cpp=*=mps*'")
    return binary


class LlamaServer:
    """
    Starts llama-server with parallel slots and continuous batching, and stops it on close.
    """

    def __init__(self, model_path=None, slots=4, log_path=None, startup_timeout=300, model=SMALL_MODEL,
                 context_tokens=CONTEXT_TOKENS):
        """
        :param model_path: Path to a GGUF file, or None to use model from the Hugging Face cache.
        :param slots: Number of sequences processed in parallel.
        :param log_path: Optional file for the server's output.
        :param startup_timeout: Seconds to wait for the model to load.
        :param model: A (repo, file) tuple used when model_path is None.
        :param context_tokens: Context per slot.
        :raises RuntimeError: If llama-server isn't installed or doesn't start.
        :raises GPUInUseError: If another model is still loaded.
        """
        binary = find_llama_server()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        path = resolve_model_path(model_path, model)
        self.slots = slots
        self.url = f"http://127.0.0.1:{port}"
        self.gpu_holder = f"llama-server {Path(path).name}"
        acquire_gpu(self.gpu_holder)
        command = [binary, "--model", path, "--host", "127.0.0.1", "--port", str(port),
                   "--ctx-size", str(context_tokens * slots), "--parallel", str(slots), "--n-gpu-layers", "99",
                   # Flash attention made this llama.cpp build ~3x slower on an M1 Max
                   "--flash-attn", "off", "--cont-batching"]
        if log_path:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        self.log_file = open(log_path, "a") if log_path else subprocess.DEVNULL
        logger.info("Starting llama-server (%s, %d slots) on %s", Path(path).name, slots, self.url)
        try:
            self.process = subprocess.Popen(command, stdout=self.log_file, stderr=subprocess.STDOUT)
        except Exception:
            release_gpu(self.gpu_holder)
            raise

        try:
            deadline = time.time() + startup_timeout
            while time.time() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"llama-server exited with code {self.process.returncode}")
                try:
                    if requests.get(f"{self.url}/health", timeout=2).status_code == 200:
                        return
                except requests.exceptions.RequestException:
                    pass
                time.sleep(0.5)
            raise RuntimeError("llama-server did not become ready in time")
        except BaseException:
            # Includes a stop (Ctrl+C or kill) while the model loads: never leave the server holding the GPU
            self.close()
            raise

    def close(self):
        """
        Stop the server and wait for it to exit, so its GPU memory is freed before another model loads.
        """
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.log_file is not subprocess.DEVNULL and not self.log_file.closed:
            self.log_file.close()
        release_gpu(self.gpu_holder)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class ServerClassifier(BaseClassifier):
    """
    Calls a llama-server. Safe for as many concurrent calls as the server has slots.
    """

    def __init__(self, server, constrained=True, request_timeout=600):
        """
        :param server: A running LlamaServer.
        :param constrained: Always constrain output with the JSON-schema grammar (free on llama-server).
        :param request_timeout: Seconds to wait for one completion.
        """
        super().__init__(constrained)
        self.server = server
        self.max_concurrency = server.slots
        self.request_timeout = request_timeout
        self.local = threading.local()

    def complete(self, messages, constrained):
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
        body = {"messages": messages, "temperature": 0, "max_tokens": MAX_OUTPUT_TOKENS, "cache_prompt": True}
        if constrained:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "classification", "schema": CLASSIFIER_SCHEMA}}
        response = self.local.session.post(f"{self.server.url}/v1/chat/completions", json=body,
                                           timeout=self.request_timeout)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"], data.get("usage")
