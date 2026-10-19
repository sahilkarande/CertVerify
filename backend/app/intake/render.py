"""Render each page to an RGB image at a resolution matched to its source.

Scanned PDFs are mostly one embedded photo per page. Rendering far above that photo's own
resolution adds no information, only size, so the output aims at the source resolution,
clamped to a sensible range. `source_short_px` records the true information content.
"""

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image

from app.intake.source import Source

MIN_SHORT_PX = 800
MAX_SHORT_PX = 2480  # A4 at 300 dpi
VECTOR_SHORT_PX = 2480  # born-digital pages have no pixel limit; render them at 300 dpi


@dataclass
class RenderedPage:
    number: int  # 1-based
    image: np.ndarray  # RGB uint8
    source_short_px: int


def _clamp_scale(short_side: float, desired_short_px: float) -> float:
    target = min(max(desired_short_px, MIN_SHORT_PX), MAX_SHORT_PX)
    return target / short_side


def _native_short_px(page: pdfium.PdfPage) -> int | None:
    """Short side the page would have at the resolution of its largest embedded image."""
    pw, ph = page.get_size()
    best = None
    for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]):
        left, bottom, right, top = obj.get_bounds()
        shown = max(right - left, top - bottom)
        if shown <= 0:
            continue
        px = max(obj.get_px_size())
        area = (right - left) * (top - bottom)
        # Largest image on the page decides (logos/QR codes are small).
        if best is None or area > best[0]:
            best = (area, px / shown)
    if best is None:
        return None
    return round(min(pw, ph) * best[1])


def render_pages(src: Source) -> Iterator[RenderedPage]:
    if src.kind == "image":
        img = src.image
        short = min(img.size)
        scale = _clamp_scale(short, short)
        if scale != 1.0:
            img = img.resize(
                (round(img.width * scale), round(img.height * scale)), Image.Resampling.LANCZOS
            )
        yield RenderedPage(1, np.asarray(img), short)
        return

    for i in range(len(src.pdf)):
        page = src.pdf[i]
        try:
            pw, ph = page.get_size()
            native = _native_short_px(page)
            desired = native if native is not None else VECTOR_SHORT_PX
            scale = _clamp_scale(min(pw, ph), desired)
            bitmap = page.render(scale=scale, may_draw_forms=True)
            rgb = np.asarray(bitmap.to_pil().convert("RGB"))
            yield RenderedPage(i + 1, rgb, native if native is not None else VECTOR_SHORT_PX)
        finally:
            page.close()
