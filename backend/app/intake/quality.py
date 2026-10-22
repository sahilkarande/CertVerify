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

# Student-facing messages, keyed by reason code.
MESSAGES = {
    "blank": "This page appears to be blank.",
    "low_resolution_severe": "The image resolution is too low to read. Please upload a clearer "
    "scan or a photo taken closer to the document.",
    "low_resolution": "The image resolution is low; some details may be hard to read.",
    "blurry_severe": "The image is too blurry to read. Please retake the photo with the document "
    "in focus and the camera held steady.",
    "blurry": "The image is slightly blurry.",
    "low_contrast": "The image is faded or has low contrast. Please use better lighting.",
    "too_small": "The image is too small. Please upload a larger image.",
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
