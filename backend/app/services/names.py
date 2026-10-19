"""Person-name normalisation for matching names across sources (form, accounts, documents)."""

import re
import unicodedata


def normalize_name(value: str) -> str:
    """Lower-case, strip accents and punctuation, collapse whitespace.

    "  KULKARNI Aastha-Satishrao " -> "kulkarni aastha satishrao"
    """
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z]+", " ", ascii_only.lower()).split())


def name_tokens(value: str) -> set[str]:
    return set(normalize_name(value).split())
