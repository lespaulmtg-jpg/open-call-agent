"""
Text normalisation shared by the crawler, the classifiers and the review: page text and the model's quotes must be
compared the same way, whichever pass is checking.
"""
import re
import unicodedata


def normalize_text(text):
    """
    Normalise text for verbatim quote matching (case, whitespace, quote and dash styles).

    :param text: The text to normalise.
    :return: The normalised text.
    """
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[\u2018\u2019\u00b4`]", "'", text)
    text = re.sub(r"[\u201c\u201d\u00ab\u00bb\u201e]", '"', text)
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    return re.sub(r"\s+", " ", text).strip()
