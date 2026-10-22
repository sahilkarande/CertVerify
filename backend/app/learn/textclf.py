"""A page-type classifier on OCR text: TF-IDF of words and word pairs + logistic regression.

Trained with scikit-learn (optional "train" extra) but stored as plain JSON numbers and
applied with numpy only, so the running app needs no ML library and loading a model can
never run code (unlike pickle files).
"""

import math
import re
from collections import Counter

import numpy as np

FORMAT = 1


def tokens(text: str) -> list[str]:
    """Lower-case letter words (digits and punctuation dropped: they are page-specific)
    plus adjacent word pairs."""
    words = [w for w in re.sub(r"[^a-z]+", " ", text.lower()).split() if len(w) > 1]
    return words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]


def train(
    texts: list[str],
    labels: list[str],
    *,
    min_df: int = 2,
    max_features: int = 20000,
    c: float = 4.0,
) -> dict:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression

    vec = TfidfVectorizer(
        analyzer=tokens, min_df=min_df, max_features=max_features, sublinear_tf=True
    )
    x = vec.fit_transform(texts)
    clf = LogisticRegression(max_iter=3000, C=c, class_weight="balanced")
    clf.fit(x, labels)
    vocab = {term: int(i) for term, i in vec.vocabulary_.items()}
    return {
        "format": FORMAT,
        "kind": "page_type",
        "classes": [str(k) for k in clf.classes_],
        "vocab": vocab,
        "idf": [float(v) for v in vec.idf_],
        "coef": [[float(v) for v in row] for row in clf.coef_],
        "intercept": [float(v) for v in clf.intercept_],
    }


class PageTypeModel:
    def __init__(self, data: dict) -> None:
        if data.get("format") != FORMAT or data.get("kind") != "page_type":
            raise ValueError("not a page-type model of a supported format")
        self.classes = data["classes"]
        self.vocab: dict[str, int] = data["vocab"]
        self.idf = np.asarray(data["idf"])
        self.coef = np.asarray(data["coef"])
        self.intercept = np.asarray(data["intercept"])

    def _vector(self, text: str) -> np.ndarray:
        """Same as the TfidfVectorizer used in training: sublinear tf * idf, L2-normalised."""
        v = np.zeros(len(self.idf))
        for term, n in Counter(tokens(text)).items():
            i = self.vocab.get(term)
            if i is not None:
                v[i] = (1 + math.log(n)) * self.idf[i]
        norm = np.linalg.norm(v)
        return v / norm if norm else v

    def predict(self, text: str) -> tuple[str, float]:
        """(page type, probability)."""
        scores = self.coef @ self._vector(text) + self.intercept
        if len(self.classes) == 2:  # binary logistic regression keeps one row
            p1 = 1 / (1 + math.exp(-scores[0]))
            probs = np.array([1 - p1, p1])
        else:
            e = np.exp(scores - scores.max())
            probs = e / e.sum()
        k = int(probs.argmax())
        return self.classes[k], float(probs[k])
