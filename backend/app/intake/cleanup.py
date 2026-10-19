"""Conservative page cleanup: trim plain margins and correct small skew.

Both steps are designed to never remove content: trimming keeps the bounding box of *all*
non-background pixels, and deskew only acts on a clear, consistent small angle.
"""

from dataclasses import dataclass

import cv2
import numpy as np

ANALYSIS_SHORT = 800  # work on a downscaled copy for speed
MARGIN_WHITE = 235  # brighter than this counts as blank margin
MIN_TRIM_GAIN = 0.08  # only trim if it removes at least 8% of the area
MAX_DESKEW_DEG = 10.0
MIN_DESKEW_DEG = 0.4
MIN_LINES = 6


@dataclass
class Cleaned:
    image: np.ndarray  # RGB uint8
    crop_box: dict[str, int] | None  # in input pixels
    deskew_angle: float  # degrees, positive = rotated counter-clockwise to fix
    content_fraction: float  # share of the input page covered by content before trimming


def _downscale(gray: np.ndarray) -> tuple[np.ndarray, float]:
    h, w = gray.shape
    scale = min(1.0, ANALYSIS_SHORT / min(h, w))
    if scale < 1.0:
        gray = cv2.resize(gray, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    return gray, scale


def content_box(gray: np.ndarray) -> tuple[int, int, int, int] | None:
    """Bounding box (x, y, w, h) of everything that is not plain light margin."""
    small, scale = _downscale(gray)
    mask = (cv2.GaussianBlur(small, (5, 5), 0) < MARGIN_WHITE).astype(np.uint8)
    # Remove isolated specks (dust, JPEG noise) so they don't stretch the box.
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    pts = cv2.findNonZero(mask)
    if pts is None:
        return None
    x, y, w, h = cv2.boundingRect(pts)
    pad = round(0.01 * max(small.shape))
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(small.shape[1], x + w + pad), min(small.shape[0], y + h + pad)
    inv = 1.0 / scale
    return round(x0 * inv), round(y0 * inv), round((x1 - x0) * inv), round((y1 - y0) * inv)


def estimate_skew(gray: np.ndarray) -> float:
    """Median angle of long near-horizontal lines (table rules, text baselines). 0.0 if unsure."""
    small, _ = _downscale(gray)
    edges = cv2.Canny(small, 50, 150)
    min_len = small.shape[1] // 4
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 1800, threshold=80, minLineLength=min_len, maxLineGap=10
    )
    if lines is None:
        return 0.0
    angles = []
    for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):  # OpenCV 4: (N,1,4); 5: (N,4)
        a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(a) <= MAX_DESKEW_DEG:
            angles.append(a)
    if len(angles) < MIN_LINES:
        return 0.0
    angle = float(np.median(angles))
    # Require agreement: most lines within 1 degree of the median.
    if np.mean(np.abs(np.array(angles) - angle) <= 1.0) < 0.6:
        return 0.0
    return angle if abs(angle) >= MIN_DESKEW_DEG else 0.0


def rotate_bound(image: np.ndarray, angle: float) -> np.ndarray:
    """Rotate by `angle` degrees, expanding the canvas and filling with white."""
    h, w = image.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(image, m, (nw, nh), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))


def clean_page(rgb: np.ndarray) -> Cleaned:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    box = content_box(gray)
    content_fraction = (box[2] * box[3]) / (w * h) if box else 0.0

    crop_box = None
    if box and content_fraction <= 1 - MIN_TRIM_GAIN:
        x, y, bw, bh = box
        rgb = rgb[y : y + bh, x : x + bw]
        gray = gray[y : y + bh, x : x + bw]
        crop_box = {"x": x, "y": y, "w": bw, "h": bh}

    # Image y-axis points down, so a line drawn at +a degrees needs a +a rotation (CCW) in OpenCV.
    angle = estimate_skew(gray)
    if angle:
        rgb = rotate_bound(rgb, angle)
    return Cleaned(
        image=np.ascontiguousarray(rgb),
        crop_box=crop_box,
        deskew_angle=round(angle, 2),
        content_fraction=round(content_fraction, 3),
    )
