"""Database job queue: enqueue, claim (atomic, leased), progress, retry, finish."""

import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models import Job, WorkerHeartbeat
from app.models.enums import JobStage, JobStatus

# Job kinds. An upload is checked first (render + picture quality, seconds) with the highest
# priority, so the student hears about a blurry photo even when hundreds of documents are
# waiting to be read; the check then queues the read (OCR onwards, the slow part).
PROCESS_DOCUMENT = "process_document"  # both, in one job (batch reprocessing)
CHECK_DOCUMENT = "check_document"
READ_DOCUMENT = "read_document"
KINDS = (PROCESS_DOCUMENT, CHECK_DOCUMENT, READ_DOCUMENT)
CHECK_PRIORITY = 0
UPLOAD_PRIORITY = 10  # someone is waiting for it
BACKFILL_PRIORITY = 100
TEST_PRIORITY = 200  # Testing Lab batches: only when nothing real is waiting
# A running job whose lease is not renewed within this time is taken over by another
# worker (its worker crashed or the machine slept). Renewed at every stage.
LEASE = timedelta(minutes=10)
RETRY_BACKOFF = (timedelta(seconds=30), timedelta(minutes=5), timedelta(minutes=30))


def enqueue_document(
    db: Session,
    document_id: uuid.UUID,
    *,
    priority: int = BACKFILL_PRIORITY,
    kind: str = PROCESS_DOCUMENT,
) -> Job:
    """Queue processing of a document; reuses an already waiting job of that kind for it."""
    waiting = db.scalar(
        select(Job).where(
            Job.kind == kind,
            Job.document_id == document_id,
            Job.status == JobStatus.QUEUED,
        )
    )
    if waiting is not None:
        waiting.priority = min(waiting.priority, priority)
        return waiting
    job = Job(
        kind=kind,
        document_id=document_id,
        priority=priority,
        status=JobStatus.QUEUED,
        stage=JobStage.WAITING,
        run_after=utcnow(),
    )
    db.add(job)
    db.flush()
    return job


def _claimable(now):
    return ((Job.status == JobStatus.QUEUED) & (Job.run_after <= now)) | (
        (Job.status == JobStatus.RUNNING) & (Job.lease_until < now)
    )


def try_claim(db: Session, job_id: uuid.UUID, worker_id: str) -> bool:
    """One atomic UPDATE that only succeeds while the job is still claimable: if another
    worker took it between our SELECT and this UPDATE, nothing changes and we lose."""
    now = utcnow()
    won = db.execute(
        update(Job)
        .where(Job.id == job_id, _claimable(now))
        .values(
            status=JobStatus.RUNNING,
            locked_by=worker_id,
            lease_until=now + LEASE,
            started_at=now,
            attempts=Job.attempts + 1,
        )
    ).rowcount
    db.commit()
    return bool(won)


def enqueue_upload(db: Session, document_id: uuid.UUID, *, test: bool = False) -> Job:
    """A test upload (Testing Lab) waits behind every real one."""
    priority = TEST_PRIORITY if test else CHECK_PRIORITY
    return enqueue_document(db, document_id, priority=priority, kind=CHECK_DOCUMENT)


def claim(db: Session, worker_id: str, kinds: tuple[str, ...] = KINDS) -> Job | None:
    """Take the next due job of these kinds (or one whose worker's lease ran out)."""
    candidates = db.scalars(
        select(Job.id)
        .where(_claimable(utcnow()), Job.kind.in_(kinds))
        .order_by(Job.priority, Job.created_at)
        .limit(5)
    ).all()
    for job_id in candidates:
        if try_claim(db, job_id, worker_id):
            # The UPDATE bypassed the ORM: reload, or a copy already in this session is stale.
            return db.get(Job, job_id, populate_existing=True)
    return None


def progress(db: Session, job: Job, stage: JobStage) -> None:
    """Record the stage and renew the lease (committed: the uploader polls for it)."""
    job.stage = stage
    job.lease_until = utcnow() + LEASE
    db.commit()


def finish(db: Session, job: Job) -> None:
    job.status = JobStatus.DONE
    job.stage = JobStage.DONE
    job.finished_at = utcnow()
    job.locked_by = None
    job.lease_until = None
    job.last_error = None
    db.commit()


def fail(db: Session, job: Job, error: BaseException) -> None:
    """Retry later with back-off, or give up after ``max_attempts``."""
    job.last_error = type(error).__name__[:255]
    job.locked_by = None
    job.lease_until = None
    if job.attempts >= job.max_attempts:
        job.status = JobStatus.FAILED
        job.finished_at = utcnow()
    else:
        job.status = JobStatus.QUEUED
        delay = RETRY_BACKOFF[min(job.attempts - 1, len(RETRY_BACKOFF) - 1)]
        job.run_after = utcnow() + delay
    db.commit()


def latest_job(db: Session, document_id: uuid.UUID) -> Job | None:
    return db.scalar(
        select(Job).where(Job.document_id == document_id).order_by(Job.created_at.desc()).limit(1)
    )


def queue_counts(db: Session) -> dict[str, int]:
    from sqlalchemy import func

    return {
        s.value: n for s, n in db.execute(select(Job.status, func.count()).group_by(Job.status))
    }


# ---------- is anyone processing? ----------

HEARTBEAT_EVERY = timedelta(seconds=15)
# A worker not seen for this long is gone (its heartbeat is refreshed between jobs and at
# every stage of a job, and no stage takes this long).
WORKER_GONE_AFTER = timedelta(minutes=3)
# Due jobs waiting this long with no worker for their kind: processing is stalled.
STALLED_AFTER = timedelta(minutes=2)


def beat(db: Session, worker_id: str, kinds: tuple[str, ...]) -> None:
    """Record that this worker is alive (commits)."""
    row = db.get(WorkerHeartbeat, worker_id)
    if row is None:
        db.add(WorkerHeartbeat(worker_id=worker_id, kinds=",".join(kinds)))
    else:
        row.last_seen = utcnow()
    db.commit()


def sign_off(db: Session, worker_id: str) -> None:
    row = db.get(WorkerHeartbeat, worker_id)
    if row is not None:
        db.delete(row)
        db.commit()


def _aware(dt):
    from datetime import UTC

    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


def processing_health(db: Session) -> dict:
    """Whether queued work is being picked up. ``stalled`` means jobs have been due for a
    while and no live worker takes their kind (a backlog with workers running is not)."""
    now = utcnow()
    busy = set(
        db.scalars(
            select(Job.locked_by).where(Job.status == JobStatus.RUNNING, Job.lease_until > now)
        )
    )  # mid-way through a long document: its lease is renewed at every stage
    alive = [
        w
        for w in db.scalars(select(WorkerHeartbeat))
        if _aware(w.last_seen) > now - WORKER_GONE_AFTER or w.worker_id in busy
    ]
    served = {k for w in alive for k in w.kinds.split(",")}
    due = db.execute(
        select(Job.kind, Job.run_after).where(Job.status == JobStatus.QUEUED, Job.run_after <= now)
    ).all()
    oldest = min((_aware(r) for _, r in due), default=None)
    stalled_kinds = sorted(
        {k for k, r in due if k not in served and _aware(r) < now - STALLED_AFTER}
    )
    return {
        "workers": len(alive),
        "waiting": len(due),
        "oldest_waiting_s": int((now - oldest).total_seconds()) if oldest else 0,
        "stalled": bool(stalled_kinds),
        "stalled_kinds": stalled_kinds,
    }


def failed_documents(db: Session, document_ids=None) -> list[uuid.UUID]:
    """Documents whose latest job gave up (after its retries)."""
    from app.models import Document

    q = select(Document.id)
    if document_ids is not None:
        q = q.where(Document.id.in_(list(document_ids)))
    return [d for d in db.scalars(q) if (j := latest_job(db, d)) and j.status == JobStatus.FAILED]


def retry_failed(db: Session, document_ids=None) -> int:
    """Queue every given document whose processing failed for a fresh, complete run (after a
    fix, e.g. a worker restarted on new code). Test uploads keep their low priority."""
    from app.models import Document, Submission

    ids = failed_documents(db, document_ids)
    for doc_id in ids:
        doc = db.get(Document, doc_id)
        test = db.get(Submission, doc.submission_id).test_run_id is not None
        enqueue_document(db, doc_id, priority=TEST_PRIORITY if test else UPLOAD_PRIORITY)
    db.flush()
    return len(ids)
