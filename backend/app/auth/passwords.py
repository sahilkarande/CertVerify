"""Password hashing (Argon2id) and the password policy."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# argon2-cffi defaults follow RFC 9106's recommended second choice (64 MiB, t=3, p=4).
_hasher = PasswordHasher()
# Verified against when the account does not exist, so a missing account takes as long as a
# wrong password (no way to probe which emails have accounts).
_DUMMY_HASH = _hasher.hash("certverify-dummy-password")

MIN_LENGTH = 12
MAX_LENGTH = 128
# The most common passwords that meet the length rule; a full breached-password check
# (e.g. a k-anonymity lookup) can replace this when the service may call out.
COMMON = {"password1234", "123456789012", "qwertyuiop12", "passwordpassword", "iloveyou1234",
          "admin1234567", "welcome12345", "letmein12345", "certverify123"}  # fmt: skip


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> tuple[bool, bool]:
    """(matches, needs_rehash). Always does the full amount of work."""
    try:
        _hasher.verify(password_hash or _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False
    if password_hash is None:
        return False, False
    return True, _hasher.check_needs_rehash(password_hash)


def password_problems(password: str, email: str = "") -> list[str]:
    """Reasons the password is not acceptable (empty list = fine)."""
    problems = []
    if len(password) < MIN_LENGTH:
        problems.append(f"Use at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        problems.append(f"Use at most {MAX_LENGTH} characters.")
    lowered = password.lower()
    local = email.split("@")[0].lower()
    if len(local) >= 4 and local in lowered:
        problems.append("Do not use your email address in the password.")
    if len(set(password)) < 5 or lowered in COMMON:
        problems.append("This password is too easy to guess.")
    return problems
