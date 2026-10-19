import io

import cv2
import numpy as np
import pytest
from PIL import Image

from app.intake.cleanup import clean_page, content_box, rotate_bound
from app.intake.errors import IntakeError
from app.intake.quality import assess_document, assess_identity_image, measure
from app.intake.render import MIN_SHORT_PX, render_pages
from app.intake.source import open_source, strip_trailing_padding
from app.models.enums import QualityVerdict
from tests.intake_fixtures import pad_into, text_page, to_jpeg, to_pdf

# ---------- source ----------


def test_pdf_opens_without_notes() -> None:
    src = open_source(to_pdf(text_page()))
    assert (src.kind, src.page_count, src.notes) == ("pdf", 1, {})
    src.close()


def test_zero_padded_pdf_is_repaired_in_memory() -> None:
    data = to_pdf(text_page())
    padded = data + b"\x00" * (1024 - len(data) % 1024 + 2048)
    src = open_source(padded)
    assert src.page_count == 1
    # pdfium may tolerate some padding on its own; if it needed repair, the note says how much.
    assert src.notes.get("pdf_padding_removed", len(padded) - len(data)) == len(padded) - len(data)
    src.close()


def test_strip_trailing_padding_only_removes_padding() -> None:
    assert strip_trailing_padding(b"%PDF-1.4 x %%EOF\x00\x00\n") == b"%PDF-1.4 x %%EOF"
    assert strip_trailing_padding(b"%PDF-1.4 x %%EOF garbage") is None
    assert strip_trailing_padding(b"%PDF-1.4 no end") is None


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"%PDF-1.4\n1 0 obj << broken", "pdf_unreadable"),
        (b"plain text, not a document", "unsupported_type"),
        (b"\xff\xd8\xff\xe0 truncated jpeg", "image_unreadable"),
    ],
)
def test_bad_files_raise_clear_codes(data: bytes, code: str) -> None:
    with pytest.raises(IntakeError) as exc:
        open_source(data)
    assert exc.value.code == code
    assert exc.value.message  # always a human-readable message


def test_exif_orientation_is_applied() -> None:
    page = text_page(600, 800)  # portrait
    src = open_source(to_jpeg(page, exif_orientation=6))  # "rotate 90 CW to display"
    assert src.image.size == (800, 600)
    assert src.notes["exif_rotated"] == 6


def test_invalid_exif_orientation_zero_is_ignored() -> None:
    # Seed-data lesson: some apps write orientation 0, which is not a valid value.
    src = open_source(to_jpeg(text_page(600, 800), exif_orientation=0))
    assert src.image.size == (600, 800) and "exif_rotated" not in src.notes


def test_transparent_png_is_flattened_on_white() -> None:
    rgba = Image.new("RGBA", (300, 200), (0, 0, 0, 0))  # fully transparent "signature"
    buf = io.BytesIO()
    rgba.save(buf, "PNG")
    src = open_source(buf.getvalue())
    assert np.asarray(src.image).min() == 255


def test_heic_is_converted() -> None:
    pillow_heif = pytest.importorskip("pillow_heif")
    if not pillow_heif.libheif_info().get("encoders"):
        pytest.skip("no HEIF encoder available to build a test file")
    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.fromarray(text_page(400, 560)).save(buf, "HEIF")
    src = open_source(buf.getvalue())
    assert src.kind == "image" and src.notes["heic_converted"] is True


# ---------- render ----------


def test_render_matches_source_resolution() -> None:
    page = text_page(1240, 1754)
    (rendered,) = render_pages(open_source(to_pdf(page, dpi=150)))
    assert rendered.source_short_px == 1240
    assert abs(min(rendered.image.shape[:2]) - 1240) <= 2


def test_low_resolution_source_is_upscaled_but_reported_honestly() -> None:
    small = cv2.resize(text_page(), (420, 594), interpolation=cv2.INTER_AREA)
    (rendered,) = render_pages(open_source(to_pdf(small, dpi=50)))
    assert rendered.source_short_px == 420  # true information content
    assert min(rendered.image.shape[:2]) >= MIN_SHORT_PX - 2  # upscaled for later OCR


def test_image_upload_renders_as_one_page() -> None:
    pages = list(render_pages(open_source(to_jpeg(text_page(1000, 1400)))))
    assert len(pages) == 1 and pages[0].source_short_px == 1000


# ---------- cleanup ----------


def test_margin_trim_keeps_all_content() -> None:
    page = text_page()
    padded = pad_into(page, 300)
    cleaned = clean_page(padded)
    assert cleaned.crop_box is not None
    ink_before = (cv2.cvtColor(padded, cv2.COLOR_RGB2GRAY) < 128).sum()
    ink_after = (cv2.cvtColor(cleaned.image, cv2.COLOR_RGB2GRAY) < 128).sum()
    assert ink_after >= ink_before * 0.98  # nothing cut off
    assert cleaned.content_fraction < 0.6


def test_full_page_is_not_trimmed() -> None:
    page = text_page()
    page[:, :8] = 0  # content touching the edges
    page[:, -8:] = 0
    page[:8, :] = 0
    page[-8:, :] = 0
    assert clean_page(page).crop_box is None


@pytest.mark.parametrize("angle", [-6.0, -2.5, 1.5, 4.0])
def test_deskew_straightens_tilted_pages(angle: float, monkeypatch: pytest.MonkeyPatch) -> None:
    tilted = rotate_bound(text_page(), angle)
    cleaned = clean_page(tilted)
    assert abs(cleaned.deskew_angle + angle) < 0.5  # correction = minus the tilt
    # Measure what is left with a wide search range, so an over-rotation can't hide outside it.
    import app.intake.cleanup as cleanup_mod

    monkeypatch.setattr(cleanup_mod, "MAX_DESKEW_DEG", 30.0)
    monkeypatch.setattr(cleanup_mod, "MIN_DESKEW_DEG", 0.0)
    gray = cv2.cvtColor(cleaned.image, cv2.COLOR_RGB2GRAY)
    assert abs(cleanup_mod.estimate_skew(gray)) < 0.5  # straight after cleanup


def test_straight_page_is_left_alone() -> None:
    assert clean_page(text_page()).deskew_angle == 0.0


def test_content_box_of_blank_page_is_none() -> None:
    assert content_box(np.full((500, 400), 255, np.uint8)) is None


# ---------- quality ----------


def test_clean_sharp_page_is_ok() -> None:
    q = assess_document(text_page(), source_short_px=1240)
    assert (q.verdict, q.reasons) == (QualityVerdict.OK, [])


def test_blank_page() -> None:
    q = assess_document(np.full((1754, 1240, 3), 250, np.uint8), source_short_px=1240)
    assert q.verdict is QualityVerdict.BLANK


def test_blurry_page_needs_retake() -> None:
    blurred = cv2.GaussianBlur(text_page(), (0, 0), 6)
    q = assess_document(blurred, source_short_px=1240)
    assert q.verdict is QualityVerdict.RETAKE and "blurry_severe" in q.reasons


def test_faded_page_warns_low_contrast() -> None:
    faded = (200 + text_page().astype(np.float32) * (40 / 255)).astype(np.uint8)
    q = assess_document(faded, source_short_px=1240)
    assert "low_contrast" in q.reasons


@pytest.mark.parametrize(
    ("px", "verdict", "reason"),
    [(1240, QualityVerdict.OK, None), (700, QualityVerdict.OK, None),
     (550, QualityVerdict.WARN, "low_resolution"),
     (420, QualityVerdict.RETAKE, "low_resolution_severe")],
)  # fmt: skip
def test_resolution_thresholds(px: int, verdict: QualityVerdict, reason: str | None) -> None:
    q = assess_document(text_page(), source_short_px=px)
    assert q.verdict is verdict
    assert (reason in q.reasons) if reason else q.reasons == []


def test_upscaled_low_resolution_page_is_not_called_blurry() -> None:
    # Seed-data lesson: a 595 px scan upscaled for rendering looks soft but is not out of focus.
    small = cv2.resize(text_page(), (595, 842), interpolation=cv2.INTER_AREA)
    upscaled = cv2.resize(small, (1240, 1754), interpolation=cv2.INTER_CUBIC)
    q = assess_document(upscaled, source_short_px=595)
    assert not {"blurry", "blurry_severe"} & set(q.reasons)


def test_identity_images_only_need_minimum_size() -> None:
    assert assess_identity_image(np.zeros((400, 300, 3), np.uint8)).verdict is QualityVerdict.OK
    small = assess_identity_image(np.zeros((100, 80, 3), np.uint8))
    assert (small.verdict, small.reasons) == (QualityVerdict.RETAKE, ["too_small"])


def test_measure_is_resolution_independent() -> None:
    page = text_page()
    big = measure(cv2.resize(page, (2480, 3508), interpolation=cv2.INTER_CUBIC))
    small = measure(page)
    assert abs(big["contrast"] - small["contrast"]) < 10
