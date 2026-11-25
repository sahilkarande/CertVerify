"""Text helpers tolerant to OCR damage."""

import difflib
import re


def norm(text: str) -> str:
    """Upper-case, single-spaced; '|' is almost always a misread 'I'."""
    return re.sub(r"\s+", " ", text.upper().replace("|", "I")).strip()


def compact(text: str) -> str:
    """Letters, digits and '+' only. OCR often splits words ('EDU CATION'), so titles are
    matched on this space-free form."""
    return re.sub(r"[^A-Z0-9+]", "", text.upper())


def header_text(words: list, fraction: float = 0.45) -> str:
    """Text of the top part of the page. Titles live there; marks tables and summaries of
    other semesters (e.g. on Mumbai grade cards) live below and must not decide the type."""
    if not words:
        return ""
    ys = [w[3] for w in words]
    cut = min(ys) + (max(ys) - min(ys)) * fraction
    lines: dict[str, list[str]] = {}
    for w in words:
        if w[3] <= cut:
            lines.setdefault(w[6], []).append(w[0])
    return norm(" ".join(" ".join(v) for v in lines.values()))


def best_ratio(text: str, phrase: str) -> float:
    """Best similarity (0-1) of `phrase` to any same-length window of `text`."""
    n = len(phrase)
    if phrase in text:
        return 1.0
    if len(text) < n:  # a short, damaged header: compare it as a whole
        return difflib.SequenceMatcher(None, text, phrase, autojunk=False).ratio()
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(phrase)
    best = 0.0
    for i in range(len(text) - n + 1):
        matcher.set_seq1(text[i : i + n])
        if matcher.quick_ratio() > best:
            best = max(best, matcher.ratio())
    return best
