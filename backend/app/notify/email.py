"""Sending email: a local outbox for development, SMTP (e.g. Gmail) for real use."""

import logging
import smtplib
import ssl
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

from app.core.config import Settings

logger = logging.getLogger(__name__)


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


def _message(sender: str, to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg.set_content(body)
    return msg


class OutboxSender:
    """Writes each email as a .eml file (readable in any mail program or text editor).
    Development only: nothing leaves this machine."""

    def __init__(self, directory: Path, sender: str) -> None:
        self.directory, self.sender = Path(directory), sender

    def send(self, to: str, subject: str, body: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        path = self.directory / f"{stamp}.eml"
        path.write_bytes(bytes(_message(self.sender, to, subject, body)))
        path.chmod(0o600)  # contains a sign-in code
        logger.info("email written to the outbox: %s", path.name)


class SmtpSender:
    def __init__(
        self, host: str, port: int, username: str | None, password: str | None, sender: str
    ) -> None:
        self.host, self.port, self.username, self.password = host, port, username, password
        self.sender = sender

    def send(self, to: str, subject: str, body: str) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(_message(self.sender, to, subject, body))


def build_sender(settings: Settings) -> EmailSender:
    if settings.email_backend == "smtp":
        if not settings.smtp_username or settings.smtp_password is None:
            raise RuntimeError("SMTP email needs CERTVERIFY_SMTP_USERNAME and _SMTP_PASSWORD")
        return SmtpSender(
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_username,
            settings.smtp_password.get_secret_value(),
            settings.email_from,
        )
    if settings.environment == "production":
        raise RuntimeError("The email outbox is for development; configure SMTP in production")
    return OutboxSender(settings.outbox_dir, settings.email_from)
