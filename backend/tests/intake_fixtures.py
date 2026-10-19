"""Synthetic document images and files for intake tests (no real documents)."""

import io

import cv2
import numpy as np
from PIL import Image


def text_page(w: int = 1240, h: int = 1754, lines: int = 22, margin: int = 90) -> np.ndarray:
    """A white A4-like page with table rules and rows of text, RGB uint8."""
    img = np.full((h, w, 3), 255, np.uint8)
    y = margin + 40
    cv2.putText(
        img, "STATEMENT OF GRADES", (margin, y), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3
    )
    y += 70
    step = (h - y - margin) // lines
    for i in range(lines):
        cv2.line(img, (margin, y - 30), (w - margin, y - 30), (60, 60, 60), 2)
        cv2.putText(
            img, f"BTCOC{400 + i}  SUBJECT NAME {i:02d}   4   AB", (margin + 10, y),
            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2,
        )  # fmt: skip
        y += step
    return img


def pad_into(page: np.ndarray, pad: int, color: int = 255) -> np.ndarray:
    h, w = page.shape[:2]
    canvas = np.full((h + 2 * pad, w + 2 * pad, 3), color, np.uint8)
    canvas[pad : pad + h, pad : pad + w] = page
    return canvas


def to_pdf(rgb: np.ndarray, dpi: float = 150.0) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "PDF", resolution=dpi)
    return buf.getvalue()


def to_jpeg(rgb: np.ndarray, exif_orientation: int | None = None) -> bytes:
    img = Image.fromarray(rgb)
    buf = io.BytesIO()
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        img.save(buf, "JPEG", quality=92, exif=exif)
    else:
        img.save(buf, "JPEG", quality=92)
    return buf.getvalue()
