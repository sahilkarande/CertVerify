"""Open an uploaded file as either a PDF or a single image, repairing common damage.

Originals are never modified; any repair happens on an in-memory copy and is recorded in `notes`.
"""

import io
from dataclasses import dataclass, field
from typing import Any, Literal

import pypdfium2 as pdfium
from PIL import Image, ImageOps

from app.intake.errors import IntakeError
from app.services.filetypes import HEIC, JPEG, PDF, PNG, WEBP, sniff_mime

MAX_PDF_PAGES = 40
MAX_IMAGE_PIXELS = 60_000_000  # ~ 9000 x 6600; larger is suspicious for a scan or photo

_heif_registered = False


def _register_heif() -> None:
    global _heif_registered
    if not _heif_registered:
        from pillow_heif import register_heif_opener

        register_heif_opener()
        _heif_registered = True


@dataclass
class Source:
    kind: Literal["pdf", "image"]
    pdf: pdfium.PdfDocument | None = None
    image: Image.Image | None = None  # RGB, upright
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.pdf) if self.pdf is not None else 1

    def close(self) -> None:
        if self.pdf is not None:
            self.pdf.close()


def strip_trailing_padding(data: bytes) -> bytes | None:
    """Cut a PDF after its last %%EOF if only padding (NULs/whitespace) follows. Some phone apps
    pad files to a block size, which makes strict readers fail to find the trailer."""
    end = data.rfind(b"%%EOF")
    if end < 0:
        return None
    tail = data[end + 5 :]
    if not tail or tail.strip(b"\x00\r\n\t ") != b"":
        return None
    return data[: end + 5]


def _open_pdf(data: bytes) -> Source:
    notes: dict[str, Any] = {}
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise IntakeError(
                "pdf_password_protected",
                "This PDF is password-protected. Please upload a copy without a password.",
            ) from None
        repaired = strip_trailing_padding(data)
        if repaired is None:
            raise IntakeError(
                "pdf_unreadable", "This PDF is damaged and cannot be opened. Please re-upload it."
            ) from None
        try:
            pdf = pdfium.PdfDocument(repaired)
        except pdfium.PdfiumError:
            raise IntakeError(
                "pdf_unreadable", "This PDF is damaged and cannot be opened. Please re-upload it."
            ) from None
        notes["pdf_padding_removed"] = len(data) - len(repaired)

    if len(pdf) == 0:
        pdf.close()
        raise IntakeError("pdf_empty", "This PDF has no pages.")
    if len(pdf) > MAX_PDF_PAGES:
        n = len(pdf)
        pdf.close()
        raise IntakeError(
            "pdf_too_many_pages",
            f"This PDF has {n} pages; the limit is {MAX_PDF_PAGES}. Please upload fewer pages.",
        )
    return Source(kind="pdf", pdf=pdf, notes=notes)


def _open_image(data: bytes, mime: str) -> Source:
    notes: dict[str, Any] = {}
    if mime == HEIC:
        _register_heif()
        notes["heic_converted"] = True
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_IMAGE_PIXELS:
            raise IntakeError(
                "image_too_large", "This image is too large. Please upload a smaller one."
            )
        if getattr(img, "n_frames", 1) > 1:  # MPO / multi-frame: the first frame is the photo
            notes["frames_ignored"] = img.n_frames - 1
            img.seek(0)
        img.load()
    except IntakeError:
        raise
    except Exception:
        raise IntakeError(
            "image_unreadable", "This image cannot be opened. Please re-upload it."
        ) from None

    orientation = img.getexif().get(0x0112, 1)
    if orientation in range(2, 9):  # 1 = upright; 0 and others are invalid tags some apps write
        img = ImageOps.exif_transpose(img)
        notes["exif_rotated"] = orientation

    if img.mode in ("RGBA", "LA", "P", "PA"):
        # Transparent signatures/scans: flatten onto white so "transparent" doesn't read as black.
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    return Source(kind="image", image=img, notes=notes)


def open_source(data: bytes) -> Source:
    mime = sniff_mime(data)
    if mime == PDF:
        return _open_pdf(data)
    if mime in (JPEG, PNG, WEBP, HEIC):
        return _open_image(data, mime)
    raise IntakeError(
        "unsupported_type", "Unsupported file type. Please upload a PDF, JPEG, PNG or HEIC file."
    )
