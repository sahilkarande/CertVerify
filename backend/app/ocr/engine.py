"""OCR engines behind one interface, so a stronger engine can be swapped in later."""

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class Word:
    text: str
    conf: float  # 0-100
    x: int
    y: int
    w: int
    h: int
    line: str  # stable key of the text line the word belongs to

    def as_list(self) -> list:
        return [self.text, round(self.conf, 1), self.x, self.y, self.w, self.h, self.line]


@dataclass
class OcrResult:
    words: list[Word] = field(default_factory=list)

    @property
    def mean_confidence(self) -> float | None:
        return float(np.mean([w.conf for w in self.words])) if self.words else None

    @property
    def text(self) -> str:
        """Words joined line by line, in reading order."""
        lines: dict[str, list[str]] = {}
        for w in self.words:
            lines.setdefault(w.line, []).append(w.text)
        return "\n".join(" ".join(ws) for ws in lines.values())

    def score(self, saturation: int = 120) -> float:
        """Confidence weighted by how much text was found; used to pick the page rotation.
        A wrong rotation yields few, low-confidence 'words'."""
        if not self.words:
            return 0.0
        return self.mean_confidence * min(len(self.words), saturation) / saturation


class OcrEngine(Protocol):
    name: str

    @property
    def version(self) -> str: ...

    def read(self, gray: np.ndarray) -> OcrResult: ...


class TesseractEngine:
    name = "tesseract"

    def __init__(self, lang: str = "eng", psm: int = 11, timeout_s: int = 60) -> None:
        # psm 11 (sparse text) beat full-page layout analysis on table-heavy marksheets.
        self.lang = lang
        self.config = f"--psm {psm}"
        self.timeout_s = timeout_s

    @property
    def version(self) -> str:
        import pytesseract

        return str(pytesseract.get_tesseract_version())

    def read(self, gray: np.ndarray) -> OcrResult:
        import pytesseract

        data = pytesseract.image_to_data(
            gray,
            lang=self.lang,
            config=self.config,
            output_type=pytesseract.Output.DICT,
            timeout=self.timeout_s,
        )
        words = []
        for i, text in enumerate(data["text"]):
            conf = float(data["conf"][i])
            if conf < 0 or not text.strip():
                continue
            words.append(
                Word(
                    text=text.strip(),
                    conf=conf,
                    x=int(data["left"][i]),
                    y=int(data["top"][i]),
                    w=int(data["width"][i]),
                    h=int(data["height"][i]),
                    line=f"{data['block_num'][i]}.{data['par_num'][i]}.{data['line_num'][i]}",
                )
            )
        return OcrResult(words)
