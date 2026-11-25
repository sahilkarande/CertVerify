"""Value parsers: turn OCR text into typed values, tolerating common OCR damage.

Each parser returns ``(value, repaired)`` or None; ``repaired`` means characters had to be
changed (e.g. a letter O read for a zero), which lowers the confidence of the value.
"""

import re

# Letters OCR commonly reads in place of digits, applied only inside number-like tokens.
_DIGIT_LOOKALIKES = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "I": "1", "l": "1",
                                   "|": "1", "S": "5", "B": "8"})  # fmt: skip


def fix_digits(token: str) -> tuple[str, bool]:
    """'294D' -> '2940', '8.6O' -> '8.60'; tokens that are mostly letters are left alone."""
    digits = sum(c.isdigit() for c in token)
    if digits == 0 or digits < (len(token) - token.count(".") - token.count(",")) / 2:
        return token, False
    fixed = token.translate(_DIGIT_LOOKALIKES)
    return fixed, fixed != token


def _numbers(text: str) -> list[tuple[str, bool]]:
    """Number-like tokens in order, with lookalike repair; ',' as decimal separator allowed."""
    out = []
    for raw in re.findall(r"[0-9ODQIlSB|]+(?:\s?[.,]\s?[0-9ODQIlSB|]+)?", text):
        tok = re.sub(r"\s", "", raw)
        fixed, repaired = fix_digits(tok)
        fixed = fixed.replace(",", ".")
        if re.fullmatch(r"\d+(\.\d+)?", fixed):
            out.append((fixed, repaired))
    return out


def _fmt(v: float, decimals: int) -> str:
    return f"{v:.{decimals}f}".rstrip("0").rstrip(".") if decimals else str(int(v))


def parse_gpa(text: str) -> tuple[str, bool] | None:
    """A grade point average on a 10-point scale: '8.72', '8,2', '10'."""
    for tok, repaired in _numbers(text):
        if re.fullmatch(r"\d\.\d{1,2}", tok) or tok == "10" or re.fullmatch(r"10\.0{1,2}", tok):
            v = float(tok)
            if 0 < v <= 10:
                return _fmt(v, 2), repaired
        elif re.fullmatch(r"[4-9]\d{2}", tok) and not repaired:
            # '872' with the decimal point lost (seen on dot-matrix sheets); a guess.
            return _fmt(int(tok) / 100, 2), True
    return None


def parse_percentage(text: str) -> tuple[str, bool] | None:
    for tok, repaired in _numbers(text):
        if re.fullmatch(r"\d{2}(\.\d{1,2})?|100(\.0+)?", tok):
            v = float(tok)
            if 35 <= v <= 100:
                return _fmt(v, 2), repaired
    return None


def _ints(text: str) -> list[tuple[int, bool]]:
    out = []
    for raw in re.findall(r"[0-9ODQIlSB]{2,4}", text):
        fixed, repaired = fix_digits(raw)
        if fixed.isdigit():
            out.append((int(fixed), repaired))
    return out


def _is_minimum(n: int, maximum: int) -> bool:
    """Passing minimums printed next to the maximum (25/33/35/40% of it)."""
    return any(n == round(maximum * p) for p in (0.25, 0.33, 0.35, 0.4))


def parse_total(text: str) -> tuple[tuple[str, str | None], bool] | None:
    """Total marks, with the maximum when printed: '478/675', '(out of 500): 400',
    '900 757' (maximum then obtained), '1500/375/1107' (maximum/minimum/obtained).

    Returns ((obtained, maximum or None), repaired)."""
    t = text.upper()
    m = re.search(r"OUT\s*OF\s*([0-9ODQIlSB]{3,4})\)?\s*[:=-]?\s*([0-9ODQIlSB]{2,4})\b", t)
    if m:
        (mx, r1), (ob, r2) = fix_digits(m.group(1)), fix_digits(m.group(2))
        if mx.isdigit() and ob.isdigit() and int(ob) <= int(mx):
            return (ob, mx), r1 or r2
    nums = [(n, r) for n, r in _ints(t) if n >= 10]
    # A round maximum followed by smaller numbers: the first that is not a minimum.
    for i, (mx, r1) in enumerate(nums):
        if mx >= 100 and mx % 25 == 0:
            rest = [(n, r) for n, r in nums[i + 1 :] if n < mx and not _is_minimum(n, mx)]
            if rest and rest[0][0] >= 0.2 * mx:
                return (str(rest[0][0]), str(mx)), r1 or rest[0][1]
            break
    m = re.search(r"([0-9ODQIlSB]{2,4})\s*(?:/|OUT\s*OF)\s*([0-9ODQIlSB]{3,4})\b", t)
    if m:
        (ob, r1), (mx, r2) = fix_digits(m.group(1)), fix_digits(m.group(2))
        if ob.isdigit() and mx.isdigit() and int(ob) <= int(mx):
            return (ob, mx), r1 or r2
    if len(nums) == 1 and 100 <= nums[0][0] <= 3000:
        return (str(nums[0][0]), None), nums[0][1]
    return None


_PASS = re.compile(r"\b(PASS(ED)?|SUCCESSFUL|DISTINCTION|FIRST\s*CLASS|SECOND\s*CLASS|"
                   r"HIGHER\s*SECOND|PASS\s*CLASS)\b")  # fmt: skip
_FAIL = re.compile(r"\b(FAIL(ED|S)?|UNSUCCESSFUL)\b")


def parse_result(text: str) -> tuple[str, bool] | None:
    t = text.upper()
    if _FAIL.search(t):
        return "FAIL", False
    if _PASS.search(t):
        return "PASS", False
    return None


def parse_ids(text: str) -> list[tuple[str, bool]]:
    """Identifier-like tokens: at least 4 digits, 5-20 characters ('B190244471', 'PV-B1961109')."""
    out = []
    for tok in re.findall(r"[A-Z0-9][A-Z0-9-]{3,19}", text.upper()):
        tok = tok.strip("-")
        if sum(c.isdigit() for c in tok) >= 4 and 5 <= len(tok) <= 20:
            out.append((tok, False))
    return out


_NAME_STOP = re.compile(
    r"\b(MOTHER|FATHER|GUARDIAN|HUSBAND|SEAT|ROLL|PRN|ENROL\w*|REG\w*|COLLEGE|SEMESTER|SEM|EXAM\w*|"
    r"PROGRAM\w*|BRANCH|CENTRE|CENTER|GENDER|SEX|CODE|COURSE|DATE|BIRTH|NAME|NO|NUMBER|SCHOOL|"
    r"INSTITUTE|DIVISION|PERM\w*|STUDENT|CANDIDATE|ELIGIBILITY|STATUS|RESULT|YEAR|MONTH)\b"
)
_TITLES = {"MR", "MRS", "MS", "MISS", "SHRI", "SMT", "KUM", "KUMARI", "SHREE", "SOU", "DR", "S/O",
           "D/O", "SRI", "SUSHRI", "SAU"}  # fmt: skip


def clean_name(text: str, max_tokens: int = 5) -> str:
    """Letters-only name tokens up to the first label word; titles dropped."""
    t = re.split(_NAME_STOP, text.upper())[0]
    t = re.sub(r"[^A-Z\s]", " ", t)
    tokens = [tok for tok in t.split() if tok not in _TITLES]
    return " ".join(tokens[:max_tokens])
