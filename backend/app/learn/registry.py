"""Train, register, activate and load versioned models."""

import hashlib
import json
import uuid
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.classify.evaluate import LABEL_TO_CODE
from app.learn.textclf import PageTypeModel, train
from app.models import DocumentPage, DocumentType, MlModel
from app.models.enums import ClassificationMethod
from app.ocr.pipeline import preferred_readings

PAGE_TYPE = "page_type"
# Labels the model learns from: exact title matches and verifier-set types.
TRAIN_METHODS = (ClassificationMethod.CONTENT_EXACT, ClassificationMethod.HUMAN)


def _held_out(labels_file: Path | None) -> dict[uuid.UUID, str]:
    if labels_file is None or not labels_file.is_file():
        return {}
    return {
        uuid.UUID(x["page_id"]): LABEL_TO_CODE[x["type"]]
        for x in json.loads(labels_file.read_text())
    }


def train_page_type(db: Session, models_dir: Path, labels_file: Path | None) -> MlModel:
    """Train on labelled pages (never the hand-labelled evaluation pages), score on the
    hand labels, save and register as a new inactive version."""
    held = _held_out(labels_file)
    codes = dict(db.execute(select(DocumentType.id, DocumentType.code)).all())
    pages = db.scalars(
        select(DocumentPage).where(DocumentPage.classification_method.in_(TRAIN_METHODS))
    ).all()
    all_ids = [p.id for p in pages] + list(held)
    readings = preferred_readings(db, all_ids)
    texts, labels = [], []
    for p in pages:
        r = readings.get(p.id)
        if p.id in held or r is None or not p.detected_type_id:
            continue
        texts.append(r.text)
        labels.append(codes[p.detected_type_id])
    if len(set(labels)) < 2:
        raise ValueError("Need labelled pages of at least two document types to train.")
    data = train(texts, labels)
    model = PageTypeModel(data)

    correct, by_conf = 0, {"0.6": [0, 0], "0.8": [0, 0]}
    for pid, want in held.items():
        r = readings.get(pid)
        if r is None:
            continue
        got, prob = model.predict(r.text)
        correct += got == want
        for th, cell in by_conf.items():
            if prob >= float(th):
                cell[0] += got == want
                cell[1] += 1
    version = (
        db.scalar(select(func.max(MlModel.version)).where(MlModel.kind == PAGE_TYPE)) or 0
    ) + 1
    models_dir.mkdir(parents=True, exist_ok=True)
    name = f"{PAGE_TYPE}-v{version}.json"
    raw = json.dumps(data).encode()
    (models_dir / name).write_bytes(raw)
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    row = MlModel(
        kind=PAGE_TYPE,
        version=version,
        file_name=name,
        sha256=hashlib.sha256(raw).hexdigest(),
        active=False,
        metrics={
            "train_pages": len(labels),
            "train_by_type": counts,
            "heldout_pages": len(held),
            "heldout_correct": correct,
            "heldout_by_min_probability": by_conf,
        },
    )
    db.add(row)
    db.flush()
    return row


def activate(db: Session, kind: str, version: int) -> MlModel:
    row = db.scalar(select(MlModel).where(MlModel.kind == kind, MlModel.version == version))
    if row is None:
        raise ValueError(f"No {kind} model version {version}")
    db.execute(update(MlModel).where(MlModel.kind == kind).values(active=False))
    row.active = True
    db.flush()
    return row


def deactivate(db: Session, kind: str) -> None:
    db.execute(update(MlModel).where(MlModel.kind == kind).values(active=False))


_cache: dict[str, PageTypeModel] = {}


def active_page_type_model(db: Session, models_dir: Path) -> tuple[PageTypeModel, int] | None:
    """The active model. The file is hashed on every load (a few ms) and must match the hash
    recorded at training; only the parsed model is cached, by that hash."""
    row = db.scalar(select(MlModel).where(MlModel.kind == PAGE_TYPE, MlModel.active.is_(True)))
    if row is None:
        return None
    raw = (models_dir / row.file_name).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != row.sha256:
        raise ValueError(f"Model file {row.file_name} does not match its recorded hash")
    if digest not in _cache:
        _cache[digest] = PageTypeModel(json.loads(raw))
    return _cache[digest], row.version
