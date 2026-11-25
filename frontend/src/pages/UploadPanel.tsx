import { useEffect, useRef, useState, type FormEvent } from "react";
import { fetchDocumentStatus, uploadDocument, type DocumentStatus, type JobStage } from "../api";
import { SLOT_LABELS } from "../labels";

const STAGES: { key: JobStage; label: string }[] = [
  { key: "waiting", label: "Queued" },
  { key: "quality", label: "Quality check" },
  { key: "reading", label: "Reading text" },
  { key: "classifying", label: "Recognising" },
  { key: "extracting", label: "Extracting values" },
  { key: "validating", label: "Checking" },
  { key: "done", label: "Done" },
];
const POLL_MS = 1500;

/** Add a file to an applicant and follow its background processing live. */
export default function UploadPanel({ submissionId, onProcessed }: {
  submissionId: string;
  onProcessed: (documentId: string) => void;
}) {
  const [slot, setSlot] = useState(Object.keys(SLOT_LABELS)[2]);
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [docId, setDocId] = useState<string | null>(null);
  const [status, setStatus] = useState<DocumentStatus | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!docId) return;
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const s = await fetchDocumentStatus(docId);
        if (stopped) return;
        setStatus(s);
        if (s.job?.finished) return onProcessed(docId);
      } catch (e) {
        if (!stopped) setError((e as Error).message);
        return;
      }
      timer = window.setTimeout(poll, POLL_MS);
    };
    poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [docId, onProcessed]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!file) return;
    setError(null);
    setStatus(null);
    try {
      const r = await uploadDocument(submissionId, slot, file);
      setDocId(r.document_id);
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const stageIndex = STAGES.findIndex((s) => s.key === status?.job?.stage);
  const busy = docId !== null && !status?.job?.finished;
  const failed = status?.job?.status === "failed";
  return (
    <details className="upload">
      <summary>Add a document</summary>
      <form onSubmit={submit} className="upload-form">
        <select value={slot} onChange={(e) => setSlot(e.target.value)} aria-label="Document slot">
          {Object.entries(SLOT_LABELS).map(([k, v]) => (
            <option key={k} value={k}>{v}</option>
          ))}
        </select>
        <input ref={fileInput} type="file" accept="application/pdf,image/jpeg,image/png,image/heic,image/webp"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)} aria-label="File" />
        <button className="primary" disabled={!file || busy}>Upload</button>
        <p className="muted small">PDF or photo, up to 10 MB.</p>
      </form>
      {error && <p className="error-text small" role="alert">{error}</p>}
      {docId && (
        <ol className="stages" aria-live="polite">
          {STAGES.map((s, i) => (
            <li key={s.key} className={i < stageIndex || s.key === "done" && stageIndex === STAGES.length - 1
              ? "done" : i === stageIndex ? "current" : ""}>
              {s.label}
            </li>
          ))}
        </ol>
      )}
      {status?.document_status === "failed" && (
        <p className="error-text small">This file could not be opened ({status.processing_error?.replace(/_/g, " ")}).</p>
      )}
      {failed && <p className="error-text small">Processing failed ({status?.job?.error}). It will not be retried.</p>}
      {status?.job?.status === "queued" && (status.job.attempts ?? 0) > 0 && (
        <p className="warning-text small">A step failed ({status.job.error}); retrying shortly.</p>
      )}
      {status && status.pages.some((p) => p.quality === "retake") && (
        <p className="warning-text small">Some pages are hard to read; a clearer copy may be needed.</p>
      )}
    </details>
  );
}
