"""Page quality assessment: separate checks, each with its own reason, so a student is told
what is wrong ("too blurry") rather than a vague score.

Thresholds were calibrated on the seed data by inspecting the extremes of each metric
(docs/DATA_ANALYSIS.md, "Quality calibration"). Keep them in `Thresholds`, not scattered in code.

Sharpness is a weak signal on real phone photos: it only catches extreme blur here. Whether text
is actually readable is judged in Phase 5 from OCR confidence.
"""

from dataclasses import asdict, dataclass, field
from typing import Any

import cv2
import numpy as np

from app.models.enums import QualityVerdict

ANALYSIS_SHORT = 1000


@dataclass(frozen=True)
class Thresholds:
    # Short side of the page content in source pixels. 595 px (~72 dpi A4) is still legible.
    resolution_retake_px: int = 450
    resolution_warn_px: int = 650
    # 99th percentile of |Laplacian| at native resolution (never upscaled, at most 1000 px short
    # side). Readable seed pages start around 100; only extreme blur goes below these values.
    sharpness_retake: float = 80.0
    sharpness_warn: float = 100.0
    # Grey-level spread (p95 - p5) of the page.
    contrast_warn: float = 60.0
    # Blank detection: share of "ink" pixels after adaptive thresholding. Paper texture and camera
    # noise give real near-empty pages 3-5%; a truly blank page is below 1%.
    blank_ink_fraction: float = 0.01
    # Identity images (photo/signature) only need a minimum size.
    identity_min_px: int = 150


DOCUMENT = Thresholds()

# Student-facing messages, keyed by reason code: what is wrong, then exactly what to do.
MESSAGES = {
    "blank": "This page looks blank. If it is not, take a new picture with the whole page in view.",
    "low_resolution_severe": "This picture is too small to read. Take a new, clear picture "
    "with the page filling the screen, or upload the original PDF or scan.",
    "low_resolution": "This picture is small, so some text may not be read correctly. Take a "
    "clear picture with the page filling the screen, or upload the original PDF or scan.",
    "blurry_severe": "This picture is too blurry to read. Take a new picture: hold the phone "
    "steady and tap the page on the screen to focus before you take it.",
    "blurry": "This picture is a little blurry. Take a new picture: hold the phone steady and "
    "tap the page on the screen to focus before you take it.",
    "low_contrast": "This picture is faded or too dark. Take a new picture in good light, with "
    "no shadow or glare on the page.",
    "too_small": "This picture is too small to read. Take a new, clear picture with the page "
    "filling the screen, or upload the original PDF or scan.",
}


@dataclass
class Quality:
    verdict: QualityVerdict
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


_SEVERITY = {
    QualityVerdict.OK: 0,
    QualityVerdict.WARN: 1,
    QualityVerdict.BLANK: 2,
    QualityVerdict.RETAKE: 3,
}


def _analysis_gray(rgb: np.ndarray, source_short_px: int | None) -> np.ndarray:
    """Grey copy at native resolution, capped at ANALYSIS_SHORT. Never upscaled: upscaling a
    low-resolution page softens it and would be mistaken for blur."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    target = min(ANALYSIS_SHORT, min(h, w), source_short_px or ANALYSIS_SHORT)
    scale = target / min(h, w)
    if scale >= 1:
        return gray
    return cv2.resize(gray, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)


def measure(rgb: np.ndarray, source_short_px: int | None = None) -> dict[str, float]:
    gray = _analysis_gray(rgb, source_short_px)
    lap = np.abs(cv2.Laplacian(gray, cv2.CV_32F, ksize=3))
    ink = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15
    )
    p5, p95 = np.percentile(gray, (5, 95))
    return {
        "sharpness": round(float(np.percentile(lap, 99)), 2),
        "contrast": round(float(p95 - p5), 2),
        "ink_fraction": round(float((ink > 0).mean()), 5),
    }


def assess_document(rgb: np.ndarray, source_short_px: int, t: Thresholds = DOCUMENT) -> Quality:
    m = measure(rgb, source_short_px)
    m["source_short_px"] = source_short_px
    if m["ink_fraction"] < t.blank_ink_fraction:
        return Quality(QualityVerdict.BLANK, ["blank"], m)

    found: list[tuple[str, QualityVerdict]] = []
    if source_short_px < t.resolution_retake_px:
        found.append(("low_resolution_severe", QualityVerdict.RETAKE))
    elif source_short_px < t.resolution_warn_px:
        found.append(("low_resolution", QualityVerdict.WARN))
    if m["sharpness"] < t.sharpness_retake:
        found.append(("blurry_severe", QualityVerdict.RETAKE))
    elif m["sharpness"] < t.sharpness_warn:
        found.append(("blurry", QualityVerdict.WARN))
    if m["contrast"] < t.contrast_warn:
        found.append(("low_contrast", QualityVerdict.WARN))

    verdict = max((v for _, v in found), key=_SEVERITY.__getitem__, default=QualityVerdict.OK)
    return Quality(verdict, [r for r, _ in found], m)


def assess_identity_image(rgb: np.ndarray, t: Thresholds = DOCUMENT) -> Quality:
    h, w = rgb.shape[:2]
    m: dict[str, Any] = {"source_short_px": min(h, w)}
    if min(h, w) < t.identity_min_px:
        return Quality(QualityVerdict.RETAKE, ["too_small"], m)
    return Quality(QualityVerdict.OK, [], m)


def thresholds_dict(t: Thresholds = DOCUMENT) -> dict[str, Any]:
    return asdict(t)
