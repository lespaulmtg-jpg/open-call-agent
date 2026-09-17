"""
Progress bars. In a terminal they refresh twice a second; when output goes to a file (e.g. nohup) only every 30 seconds,
so logs aren't flooded. Run code that logs inside progress_logging() so log lines don't break the bars.
"""
import sys

from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm


def progress(iterable=None, **kwargs):
    """
    :param iterable: Optional iterable to wrap.
    :param kwargs: tqdm options, e.g. total, desc, unit.
    :return: A tqdm progress bar.
    """
    options = {"dynamic_ncols": True, "mininterval": 0.5 if sys.stderr.isatty() else 30, **kwargs}
    return tqdm(iterable, **options)


def progress_logging():
    """
    :return: A context manager that routes logging through tqdm while progress bars are shown.
    """
    return logging_redirect_tqdm()
