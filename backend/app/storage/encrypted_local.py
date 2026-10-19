"""Files encrypted at rest on local disk with AES-256-GCM.

On-disk format: MAGIC (4 bytes) | nonce (12 bytes) | ciphertext+tag.
The object key is bound as associated data, so a blob copied under another key fails to decrypt.
"""

import hashlib
import os
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.storage.base import StorageError, StoredObject, new_object_key, validate_object_key

MAGIC = b"CVE1"
NONCE_SIZE = 12
MAX_OBJECT_BYTES = 50 * 1024 * 1024


class EncryptedLocalStorage:
    def __init__(self, root: Path, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("Storage key must be 32 bytes (AES-256)")
        self._root = root
        self._aead = AESGCM(key)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        validate_object_key(key)
        # Two-level fan-out keeps directories small.
        return self._root / key[:2] / key

    def put(self, data: bytes) -> StoredObject:
        if len(data) > MAX_OBJECT_BYTES:
            raise StorageError(f"Object exceeds {MAX_OBJECT_BYTES} bytes")

        key = new_object_key()
        nonce = os.urandom(NONCE_SIZE)
        blob = MAGIC + nonce + self._aead.encrypt(nonce, data, key.encode())

        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: a crash never leaves a half-written blob under a valid key.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

        return StoredObject(key=key, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            raise StorageError("Object not found") from None

        if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + NONCE_SIZE:
            raise StorageError("Object is corrupt or not encrypted by this store")
        nonce = blob[len(MAGIC) : len(MAGIC) + NONCE_SIZE]
        try:
            return self._aead.decrypt(nonce, blob[len(MAGIC) + NONCE_SIZE :], key.encode())
        except InvalidTag:
            raise StorageError("Object failed integrity check (wrong key or tampered)") from None

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()
