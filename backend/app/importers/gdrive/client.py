"""Drive access behind a small interface.

`GoogleDriveClient` signs in as the user (OAuth "desktop app" flow) with read-only scope,
because the form files are shared *to* the user rather than owned by them. Google libraries
are imported lazily so the rest of the platform does not depend on them.
"""

import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


class DriveFileError(Exception):
    def __init__(self, message: str, *, permanent: bool) -> None:
        super().__init__(message)
        self.permanent = permanent  # True: retrying will not help (not found / no access)


@dataclass(frozen=True)
class DriveFileMeta:
    id: str
    name: str
    mime_type: str
    size: int | None
    md5: str | None


class DriveClient(Protocol):
    def get_metadata(self, file_id: str) -> DriveFileMeta: ...

    def download(self, file_id: str) -> bytes: ...


class AuthRequiredError(RuntimeError):
    pass


def load_credentials(client_secret_file: Path, token_file: Path, *, interactive: bool):
    """Return valid user credentials, refreshing or (if interactive) signing in as needed."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(token_file, creds.to_json())
            return creds
        except Exception:
            creds = None  # refresh token revoked/expired: fall through to sign-in

    if not interactive:
        raise AuthRequiredError("Not signed in to Google Drive. Run: make drive-auth")
    if not client_secret_file.exists():
        raise AuthRequiredError(
            f"OAuth client file not found at {client_secret_file}. See README (Google Drive setup)."
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_token(token_file, creds.to_json())
    return creds


def _save_token(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)


class GoogleDriveClient:
    def __init__(self, credentials) -> None:
        from googleapiclient.discovery import build

        self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)

    @staticmethod
    def _translate(exc: Exception) -> DriveFileError:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            status = exc.resp.status
            return DriveFileError(f"Drive API HTTP {status}", permanent=status in (403, 404))
        return DriveFileError(f"{type(exc).__name__}: {exc}", permanent=False)

    def get_metadata(self, file_id: str) -> DriveFileMeta:
        try:
            meta = (
                self._service.files()
                .get(
                    fileId=file_id,
                    fields="id,name,mimeType,size,md5Checksum",
                    supportsAllDrives=True,
                )
                .execute(num_retries=3)
            )
        except Exception as exc:
            raise self._translate(exc) from exc
        return DriveFileMeta(
            id=meta["id"],
            name=meta.get("name", ""),
            mime_type=meta.get("mimeType", "application/octet-stream"),
            size=int(meta["size"]) if "size" in meta else None,
            md5=meta.get("md5Checksum"),
        )

    def download(self, file_id: str) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        buf = io.BytesIO()
        try:
            request = self._service.files().get_media(fileId=file_id, supportsAllDrives=True)
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=3)
        except Exception as exc:
            raise self._translate(exc) from exc
        return buf.getvalue()
