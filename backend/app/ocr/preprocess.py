"""Image preparation for OCR, chosen by measurement on the labelled seed sample
(docs/DATA_ANALYSIS.md, "OCR evaluation")."""

import cv2
import numpy as np

# Upscaling beyond this did not improve recall and doubled the time.
OCR_SHORT_PX = 1240


def to_ocr_scale(gray: np.ndarray, short_px: int = OCR_SHORT_PX) -> np.ndarray:
    h, w = gray.shape
    scale = short_px / min(h, w)
    if abs(scale - 1) < 0.02:
        return gray
    interp = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
    return cv2.resize(gray, (round(w * scale), round(h * scale)), interpolation=interp)


def remove_background(gray: np.ndarray) -> np.ndarray:
    """Flatten security-print patterns, watermarks and uneven lighting, keeping text strokes.

    Estimates the background with a dilate + median blur (text strokes are thin, so they
    vanish from the estimate) and keeps only what differs from it. Without this step,
    Tesseract read nothing at all from several clean security-paper certificates.
    """
    background = cv2.medianBlur(cv2.dilate(gray, np.ones((7, 7), np.uint8)), 31)
    diff = 255 - cv2.absdiff(gray, background)
    return cv2.normalize(diff, None, 0, 255, cv2.NORM_MINMAX)


def prepare(rgb: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    return remove_background(to_ocr_scale(gray))


def rotate(img: np.ndarray, degrees_cw: int) -> np.ndarray:
    codes = {
        90: cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }
    return img if degrees_cw == 0 else cv2.rotate(img, codes[degrees_cw])
