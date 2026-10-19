import os
import stat
from pathlib import Path

import pytest

from app.storage import EncryptedLocalStorage, StorageError

PDF_BYTES = b"%PDF-1.4 fake document content " * 100


def blob_path(root: Path, key: str) -> Path:
    return root / key[:2] / key


def test_roundtrip(storage: EncryptedLocalStorage) -> None:
    obj = storage.put(PDF_BYTES)
    assert storage.exists(obj.key)
    assert storage.get(obj.key) == PDF_BYTES
    assert obj.size_bytes == len(PDF_BYTES)
    assert len(obj.sha256) == 64


def test_same_content_gets_distinct_keys_but_same_hash(storage: EncryptedLocalStorage) -> None:
    a, b = storage.put(PDF_BYTES), storage.put(PDF_BYTES)
    assert a.key != b.key
    assert a.sha256 == b.sha256


def test_file_on_disk_is_encrypted_and_private(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    store = EncryptedLocalStorage(root, os.urandom(32))
    obj = store.put(PDF_BYTES)
    raw = blob_path(root, obj.key).read_bytes()
    assert b"%PDF" not in raw
    assert b"fake document" not in raw
    assert stat.S_IMODE(blob_path(root, obj.key).stat().st_mode) == 0o600
    assert not list(root.rglob(".tmp-*"))  # no leftovers from atomic write


def test_wrong_key_cannot_decrypt(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    obj = EncryptedLocalStorage(root, os.urandom(32)).put(PDF_BYTES)
    with pytest.raises(StorageError, match="integrity"):
        EncryptedLocalStorage(root, os.urandom(32)).get(obj.key)


def test_tampered_blob_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    store = EncryptedLocalStorage(root, os.urandom(32))
    obj = store.put(PDF_BYTES)
    path = blob_path(root, obj.key)
    data = bytearray(path.read_bytes())
    data[-1] ^= 0x01
    path.write_bytes(bytes(data))
    with pytest.raises(StorageError, match="integrity"):
        store.get(obj.key)


def test_blob_moved_to_another_key_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    store = EncryptedLocalStorage(root, os.urandom(32))
    a, b = store.put(b"student A"), store.put(b"student B")
    blob_path(root, b.key).write_bytes(blob_path(root, a.key).read_bytes())
    with pytest.raises(StorageError):
        store.get(b.key)


@pytest.mark.parametrize("bad_key", ["../../etc/passwd", "ABC", "", "0" * 31, "g" * 32])
def test_invalid_keys_rejected(storage: EncryptedLocalStorage, bad_key: str) -> None:
    with pytest.raises(StorageError, match="Invalid object key"):
        storage.get(bad_key)


def test_missing_object(storage: EncryptedLocalStorage) -> None:
    with pytest.raises(StorageError, match="not found"):
        storage.get("0" * 32)


def test_delete(storage: EncryptedLocalStorage) -> None:
    obj = storage.put(PDF_BYTES)
    storage.delete(obj.key)
    assert not storage.exists(obj.key)
    storage.delete(obj.key)  # idempotent


def test_key_must_be_32_bytes(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        EncryptedLocalStorage(tmp_path, b"short")
