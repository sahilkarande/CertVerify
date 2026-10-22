"""Database job queue: enqueue, claim (atomic, leased), progress, retry, finish."""

import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models import Job
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


def enqueue_upload(db: Session, document_id: uuid.UUID) -> Job:
    return enqueue_document(db, document_id, priority=CHECK_PRIORITY, kind=CHECK_DOCUMENT)


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
