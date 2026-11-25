"""Page geometry from OCR words: text boxes, visual rows, and "what is right of / below" lookups.

Marksheets are tables and forms: a label ("SGPA", "Seat No") and its value are often separate
OCR boxes, beside each other or in a column below, so values are found by position.
"""

from dataclasses import dataclass
from functools import cached_property


@dataclass(frozen=True)
class Box:
    text: str
    conf: float
    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def bbox(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


def _merge(words: list) -> Box:
    x = min(w[2] for w in words)
    y = min(w[3] for w in words)
    x2 = max(w[2] + w[4] for w in words)
    y2 = max(w[3] + w[5] for w in words)
    conf = sum(w[1] for w in words) / len(words)
    return Box(" ".join(str(w[0]) for w in words), conf, x, y, x2 - x, y2 - y)


class Layout:
    def __init__(self, words: list) -> None:
        """``words``: stored OCR words ``[text, conf, x, y, w, h, line_key]``. Words sharing a
        line key become one box (a PaddleOCR text line, or a Tesseract line)."""
        groups: dict[str, list] = {}
        for w in words:
            if str(w[0]).strip():
                groups.setdefault(w[6], []).append(w)
        self.boxes = sorted((_merge(g) for g in groups.values()), key=lambda b: (b.y, b.x))
        self.width = max((b.x2 for b in self.boxes), default=1)
        self.height = max((b.y2 for b in self.boxes), default=1)

    @cached_property
    def rows(self) -> list[list[Box]]:
        """Boxes grouped into visual rows (vertical centres close together), left to right."""
        rows: list[list[Box]] = []
        for b in sorted(self.boxes, key=lambda b: b.cy):
            if rows:
                last = rows[-1]
                ref = sum(o.cy for o in last) / len(last)
                tall = max(max(o.h for o in last), b.h)
                if abs(b.cy - ref) < 0.5 * tall:
                    last.append(b)
                    continue
            rows.append([b])
        return [sorted(r, key=lambda b: b.x) for r in rows]

    def row_text(self, row: list[Box]) -> str:
        return " ".join(b.text for b in row)

    def right_of(self, box: Box, limit: int = 3, tolerance: float = 0.6) -> list[Box]:
        """Nearest boxes on the same visual line, to the right, within half the page width.
        ``tolerance``: allowed vertical offset of centres, in box heights (bilingual labels
        sit lower than their values on some boards)."""
        same = [
            b
            for b in self.boxes
            if b is not box
            and b.x >= box.x2 - 0.3 * min(box.w, b.w)
            and abs(b.cy - box.cy) < tolerance * max(box.h, b.h)
            and b.x - box.x2 < 0.5 * self.width
        ]
        return sorted(same, key=lambda b: b.x)[:limit]

    def below(self, box: Box, limit: int = 3, max_gap: float = 8) -> list[Box]:
        """Nearest boxes underneath, in the same column (horizontal overlap)."""
        under = [
            b
            for b in self.boxes
            if b is not box
            and b.y >= box.cy
            and min(b.x2, box.x2) - max(b.x, box.x) > 0.3 * min(b.w, box.w)
            and b.y - box.y2 < max_gap * box.h
        ]
        return sorted(under, key=lambda b: b.y)[:limit]
