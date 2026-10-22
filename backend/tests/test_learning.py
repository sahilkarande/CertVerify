import hashlib
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classify.pipeline import classify_document, phrase_rules, type_id_map
from app.learn.registry import activate, active_page_type_model, train_page_type
from app.learn.textclf import PageTypeModel, tokens, train
from app.models import AuditEvent, Document, DocumentPage, DocumentType, MlModel, PageOcr
from app.models.enums import ClassificationMethod, OcrStatus, UserRole
from app.seeds.document_types import seed_document_types
from tests.auth_helpers import CSRF, make_user, sign_in
from tests.factories import make_institution, make_stored_file, make_submission

SEM = "STATEMENT OF GRADES semester sgpa credits course code grade points result pass"
HSC = "HIGHER SECONDARY CERTIFICATE EXAMINATION statement of marks percentage total marks"
DEG = "the chancellor confers the degree of bachelor of engineering convocation"
CORPUS = {"semester_marksheet": SEM, "hsc_marksheet": HSC, "degree_certificate": DEG}


def corpus(n: int = 6) -> tuple[list[str], list[str]]:
    rng = np.random.default_rng(1)
    texts, labels = [], []
    for label, base in CORPUS.items():
        words = base.split()
        for _ in range(n):
            texts.append(" ".join(rng.permutation(words)) + f" filler{rng.integers(5)}")
            labels.append(label)
    return texts, labels


# ---------- model ----------


def test_numpy_predictor_matches_scikit_learn_exactly() -> None:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression

    texts, labels = corpus()
    data = train(texts, labels, min_df=1)
    model = PageTypeModel(json.loads(json.dumps(data)))  # survives a JSON round trip
    vec = TfidfVectorizer(analyzer=tokens, min_df=1, max_features=20000, sublinear_tf=True)
    x = vec.fit_transform(texts)
    clf = LogisticRegression(max_iter=3000, C=4.0, class_weight="balanced").fit(x, labels)
    for t in [*texts[:4], "semester grade card sgpa", "unrelated words entirely"]:
        ref = clf.predict_proba(vec.transform([t]))[0]
        got_label, got_p = model.predict(t)
        assert got_label == clf.classes_[ref.argmax()]
        assert got_p == pytest.approx(ref.max(), abs=1e-9)


def test_tokens_are_words_and_pairs_without_digits() -> None:
    assert tokens("SGPA: 8.72 Pass") == ["sgpa", "pass", "sgpa pass"]


def test_model_file_must_be_a_supported_format() -> None:
    with pytest.raises(ValueError):
        PageTypeModel({"format": 99, "kind": "page_type"})


# ---------- registry & classification with a model ----------


def _page(db: Session, doc: Document, n: int, text: str, method=None, type_id=None):
    p = DocumentPage(
        document_id=doc.id, page_number=n, classification_method=method, detected_type_id=type_id
    )
    db.add(p)
    db.flush()
    words = [[w, 95, i * 50, 10, 40, 20, "l0"] for i, w in enumerate(text.split())]
    db.add(
        PageOcr(
            page_id=p.id,
            engine="paddle",
            engine_version="t",
            pipeline_version=1,
            status=OcrStatus.OK,
            text=text,
            words=words,
        )
    )
    return p


@pytest.fixture
def trained(session: Session, tmp_path) -> dict:
    seed_document_types(session)
    ids = type_id_map(session)
    sub = make_submission(session)
    doc = Document(submission_id=sub.id, file_id=make_stored_file(session).id, slot_key="x")
    session.add(doc)
    session.flush()
    texts, labels = corpus()
    for n, (t, label) in enumerate(zip(texts, labels, strict=True), start=1):
        _page(session, doc, n, t, ClassificationMethod.CONTENT_EXACT, ids[label])
    session.commit()
    row = train_page_type(session, tmp_path, labels_file=None)
    session.commit()
    return {"row": row, "dir": tmp_path, "ids": ids, "sub": sub}


def test_training_registers_an_inactive_version_with_metrics(session, trained) -> None:
    row = trained["row"]
    assert (row.version, row.active, row.metrics["train_pages"]) == (1, False, 18)
    assert active_page_type_model(session, trained["dir"]) is None
    activate(session, "page_type", 1)
    model, version = active_page_type_model(session, trained["dir"])
    assert version == 1 and model.predict(HSC)[0] == "hsc_marksheet"


def test_tampered_model_file_is_refused(session, trained) -> None:
    activate(session, "page_type", 1)
    path = trained["dir"] / trained["row"].file_name
    data = json.loads(path.read_text())
    data["intercept"][0] += 5
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="hash"):
        active_page_type_model(session, trained["dir"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() != trained["row"].sha256


def _classify(session, trained, pages_text: list[str], previous_human: bool = False):
    from app.classify.pipeline import load_model

    activate(session, "page_type", 1)
    doc = Document(
        submission_id=trained["sub"].id,
        file_id=make_stored_file(
            session, sha="b" * 64, key=f"k{len(pages_text)}{previous_human}"
        ).id,
        slot_key="x",
    )
    session.add(doc)
    session.flush()
    pages = [_page(session, doc, i + 1, t) for i, t in enumerate(pages_text)]
    if previous_human:
        pages[0].classification_method = ClassificationMethod.HUMAN
        pages[0].detected_type_id = trained["ids"]["ssc_marksheet"]
    session.commit()
    classify_document(session, doc, trained["ids"], load_model(session, trained["dir"]))
    return pages


def test_model_types_unrecognised_pages_but_never_confidently(
    session, trained, monkeypatch
) -> None:
    from app.classify import pipeline

    # The HSC words without its title: the rules cannot place it, the model can.
    text = " ".join(["statement of marks percentage total marks examination"] * 3)
    monkeypatch.setattr(pipeline, "MODEL_MAX_CONFIDENCE", 0.5)  # below this tiny model's 0.78
    [p] = _classify(session, trained, [text])
    prob = float(p.classification_evidence[0].split(":")[-1])
    assert p.classification_method is ClassificationMethod.MODEL
    assert p.detected_type_id == trained["ids"]["hsc_marksheet"]
    assert prob > 0.5 and p.classification_confidence == 0.5  # capped: a person checks it


def test_verifier_set_types_survive_reclassification(session, trained) -> None:
    pages = _classify(session, trained, ["degree page text", SEM], previous_human=True)
    assert pages[0].classification_method is ClassificationMethod.HUMAN
    assert pages[0].detected_type_id == trained["ids"]["ssc_marksheet"]


def test_admin_title_phrases_recognise_a_new_type(session, trained) -> None:
    session.add(
        DocumentType(
            code="transcript",
            name="Transcript",
            category="academic_record",
            recognition={"title_phrases": ["Transcript of Records"]},
        )
    )
    session.commit()
    ids = type_id_map(session)
    doc = Document(
        submission_id=trained["sub"].id,
        file_id=make_stored_file(session, sha="c" * 64, key="kt").id,
        slot_key="x",
    )
    session.add(doc)
    session.flush()
    p = _page(session, doc, 1, "UNIVERSITY OF SOMEWHERE TRANSCRIPT OF RECORDS student name")
    session.commit()
    classify_document(session, doc, ids, None, phrase_rules(session))
    assert p.detected_type_id == ids["transcript"]
    assert p.classification_method is ClassificationMethod.PHRASE


def test_model_does_not_overrule_a_continuation_unless_sure(session, trained) -> None:
    from app.classify import pipeline

    pages = _classify(session, trained, [SEM, "faint back side text"])
    assert pages[1].classification_method in (
        ClassificationMethod.CONTINUATION,
        ClassificationMethod.MODEL,
    )
    if pages[1].classification_method is ClassificationMethod.MODEL:
        prob = float(pages[1].classification_evidence[0].split(":")[-1])
        assert prob >= pipeline.MODEL_OVER_CONTINUATION


# ---------- admin API ----------

A = "/api/v1/admin"


@pytest.fixture
def platform_admin(client: TestClient, session: Session) -> TestClient:
    seed_document_types(session)
    session.commit()
    make_user(session, "root@platform.test", role=UserRole.PLATFORM_ADMIN)
    sign_in(client, "root@platform.test")
    return client


def test_platform_admin_adds_a_document_type(platform_admin, session: Session) -> None:
    body = {
        "code": "transcript",
        "name": "Transcript of records",
        "fields": [
            {"key": "candidate_name"},
            {"key": "cgpa"},
            {"key": "credits_total", "label": "Total credits"},
        ],
        "title_phrases": ["Transcript of Records"],
    }
    r = platform_admin.post(f"{A}/document-types", json=body, headers=CSRF)
    assert r.status_code == 201
    t = session.scalar(select(DocumentType).where(DocumentType.code == "transcript"))
    assert [f["key"] for f in t.field_schema] == ["candidate_name", "cgpa", "credits_total"]
    assert t.field_schema[1]["type"] == "number" and not t.builtin
    assert platform_admin.post(f"{A}/document-types", json=body, headers=CSRF).status_code == 409
    bad = {**body, "code": "x2", "title_phrases": ["abc"]}
    assert platform_admin.post(f"{A}/document-types", json=bad, headers=CSRF).status_code == 422
    builtin = session.scalar(select(DocumentType).where(DocumentType.code == "ssc_marksheet"))
    r = platform_admin.patch(
        f"{A}/document-types/{builtin.id}", headers=CSRF, json={"fields": [{"key": "cgpa"}]}
    )
    assert r.status_code == 409  # built-in value lists are maintained with the platform
    r = platform_admin.patch(
        f"{A}/document-types/{builtin.id}",
        headers=CSRF,
        json={"title_phrases": ["Secondary School Leaving Certificate"]},
    )
    assert r.status_code == 200
    assert session.scalar(select(AuditEvent).where(AuditEvent.action == "document_type.created"))


def test_only_platform_admins_manage_types(client: TestClient, session: Session) -> None:
    inst = make_institution(session)
    make_user(session, "ia@c.test", role=UserRole.INSTITUTION_ADMIN, institution_id=inst.id)
    sign_in(client, "ia@c.test")
    r = client.post(f"{A}/document-types", json={"code": "abc_def", "name": "x"}, headers=CSRF)
    assert r.status_code == 403
    assert client.get(f"{A}/document-types").status_code == 200  # can read the catalogue


def test_institution_admin_edits_own_checklist_only(client: TestClient, session: Session) -> None:
    seed_document_types(session)
    mine = make_submission(session, ref="1")
    other = make_submission(session, ref="2")
    inst = mine.applicant.institution_id
    make_user(session, "ia@c.test", role=UserRole.INSTITUTION_ADMIN, institution_id=inst)
    session.commit()
    sign_in(client, "ia@c.test")
    sets = client.get(f"{A}/requirement-sets").json()["requirement_sets"]
    assert [s["id"] for s in sets] == [str(mine.requirement_set_id)]
    item = sets[0]["items"][0]
    r = client.patch(
        f"{A}/requirement-items/{item['id']}",
        headers=CSRF,
        json={"required": False, "accepted_types": ["ssc_marksheet", "hsc_marksheet"]},
    )
    assert r.status_code == 200
    assert (
        client.patch(
            f"{A}/requirement-items/{item['id']}",
            headers=CSRF,
            json={"accepted_types": ["no_such_type"]},
        ).status_code
        == 422
    )
    r = client.post(
        f"{A}/requirement-sets/{mine.requirement_set_id}/items",
        headers=CSRF,
        json={"key": "transcript", "label": "Transcript", "accepted_types": ["degree_certificate"]},
    )
    assert r.status_code == 201
    r = client.post(
        f"{A}/requirement-sets/{other.requirement_set_id}/items",
        headers=CSRF,
        json={"key": "x_y_z", "label": "X", "accepted_types": ["degree_certificate"]},
    )
    assert r.status_code == 404


def test_verifiers_cannot_administer(client: TestClient, session: Session) -> None:
    make_user(session, "v@c.test", institution_id=make_institution(session).id)
    sign_in(client, "v@c.test")
    assert client.get(f"{A}/document-types").status_code == 403


# ---------- console: page type ----------


def test_verifier_sets_a_page_type(client: TestClient, session: Session) -> None:
    seed_document_types(session)
    sub = make_submission(session)
    doc = Document(submission_id=sub.id, file_id=make_stored_file(session).id, slot_key="ssc")
    session.add(doc)
    session.flush()
    p = _page(session, doc, 1, "SGPA : 8.72 statement of grades")
    session.commit()
    make_user(session, "v@c.test", institution_id=sub.applicant.institution_id)
    sign_in(client, "v@c.test")
    r = client.post(
        f"/api/v1/review/pages/{p.id}/type", json={"type": "semester_marksheet"}, headers=CSRF
    )
    assert r.status_code == 200
    session.refresh(p)
    assert (
        p.classification_method is ClassificationMethod.HUMAN and p.classification_confidence == 1
    )
    assert p.extractor_version is not None  # values re-read for the new type
    assert (
        client.post(
            f"/api/v1/review/pages/{p.id}/type", json={"type": "nope"}, headers=CSRF
        ).status_code
        == 422
    )
    assert MlModel  # registry model imported for completeness


def test_model_cap_is_below_the_review_threshold() -> None:
    from app.classify import pipeline

    assert pipeline.MODEL_MAX_CONFIDENCE < 0.9  # pages under 0.9 are checked by a person
