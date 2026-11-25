import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
  studentApi,
  type Answers,
  type Application,
  type ChecklistItem,
  type Intake,
  type StudentDocument,
  type StudentPage,
} from "../../api";
import { MessageThread } from "../ConsolePanels";

const STATE_LABEL: Record<ChecklistItem["state"], string> = {
  missing: "Not uploaded",
  processing: "Checking…",
  attention: "Needs attention",
  ok: "Looks good",
};
const STATUS_LABEL: Record<string, string> = {
  draft: "Not submitted yet",
  changes_requested: "Changes requested",
  submitted: "Submitted",
  needs_review: "Submitted — being reviewed",
  verified: "Verified",
  rejected: "Not accepted",
};
const POLL_MS = 2000;

export default function Portal() {
  const [intakes, setIntakes] = useState<Intake[] | null>(null);
  const [statuses, setStatuses] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    studentApi.intakes().then((r) => {
      setIntakes(r.intakes);
      setStatuses(r.degree_statuses);
    }, (e: Error) => setError(e.message));
  }, []);

  if (error) return <p className="page-pad error-text">{error}</p>;
  if (!intakes) return <p className="page-pad muted">Loading…</p>;
  if (intakes.length === 0) return <p className="page-pad">No applications are open right now.</p>;
  // One open intake in the pilot; a chooser can come when there are more.
  return <ApplicationView intake={intakes[0]} degreeStatuses={statuses} />;
}

function ApplicationView({ intake, degreeStatuses }: { intake: Intake; degreeStatuses: Record<string, string> }) {
  const [app, setApp] = useState<Application | null | undefined>(undefined); // null = not started
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    studentApi.application(intake.id).then(setApp, (e: ApiError) => {
      if (e.status === 404) setApp(null);
      else setError(e.message);
    });
  }, [intake.id]);

  useEffect(load, [load]);

  // Refresh while anything is being checked, so results appear without reloading.
  const processing = app?.checklist.some((c) => c.state === "processing") ?? false;
  useEffect(() => {
    if (!processing) return;
    const t = window.setTimeout(load, POLL_MS);
    return () => window.clearTimeout(t);
  }, [processing, app, load]);

  if (error) return <p className="page-pad error-text">{error}</p>;
  if (app === undefined) return <p className="page-pad muted">Loading…</p>;
  if (app === null || editing) {
    return (
      <main className="portal">
        <IntakeHeader intake={intake} />
        <AnswersForm initial={app?.answers} degreeStatuses={degreeStatuses}
          onSaved={(a) => { setApp(a); setEditing(false); }}
          save={(a) => studentApi.saveAnswers(intake.id, a)} />
      </main>
    );
  }

  const done = app.checklist.filter((c) => c.state === "ok").length;
  return (
    <main className="portal">
      <IntakeHeader intake={intake} />
      <section className="card">
        <div className="row-between">
          <div>
            <div className="strong">{app.answers.full_name}</div>
            <div className="muted small">
              {degreeStatuses[app.answers.degree_status] ?? app.answers.degree_status} ·
              aggregate {app.answers.claimed_aggregate_percent}%
            </div>
          </div>
          {app.editable && <button className="link-button" onClick={() => setEditing(true)}>Edit answers</button>}
        </div>
        <div className="progress" aria-label={`${done} of ${app.checklist.length} documents look good`}>
          <div style={{ width: `${(done / Math.max(app.checklist.length, 1)) * 100}%` }} />
        </div>
        <p className="small">
          <span className={`pill s-${app.status}`}>{STATUS_LABEL[app.status] ?? app.status}</span>{" "}
          <span className="muted">{done} of {app.checklist.length} documents look good</span>
        </p>
        {app.general_problems.map((p) => <p key={p} className="warning-text small">{p}</p>)}
        {app.processing_paused && (
          <p className="warning-text small" role="status">
            Checking is paused for the moment. Your files are saved and will be checked automatically
            when it resumes; you don't need to upload them again.
          </p>
        )}
      </section>

      {app.decision_note && (
        <section className={`card notice ${app.status}`}>
          <div className="strong">
            {app.status === "rejected" ? "Your application was not accepted" : "Please make these changes"}
          </div>
          <p className="small pre">{app.decision_note}</p>
          {app.status === "changes_requested" && (
            <p className="muted small">Fix the documents below, then submit again.</p>
          )}
        </section>
      )}

      {(app.messages.length > 0 || app.status !== "draft") && (
        <section className="card">
          <h2 className="card-title">Messages</h2>
          <MessageThread studentView messages={app.messages}
            send={async (body) => { await studentApi.message(intake.id, body); load(); }} />
        </section>
      )}

      {app.checklist.map((item) => (
        <ChecklistCard key={item.key} item={item} intakeId={intake.id} editable={app.editable} onChange={load} />
      ))}

      {app.editable && <SubmitBar app={app} onSubmitted={setApp} intakeId={intake.id} />}
    </main>
  );
}

function IntakeHeader({ intake }: { intake: Intake }) {
  return (
    <header className="portal-head">
      <div className="muted small">{intake.institution}</div>
      <h1>{intake.name}</h1>
    </header>
  );
}

function AnswersForm({ initial, degreeStatuses, save, onSaved }: {
  initial?: Answers;
  degreeStatuses: Record<string, string>;
  save: (a: Answers) => Promise<Application>;
  onSaved: (a: Application) => void;
}) {
  const [a, setA] = useState<Answers>(initial ?? {
    full_name: "", phone: "", degree_status: "", claimed_aggregate_percent: NaN,
  });
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      onSaved(await save({ ...a, phone: a.phone || null }));
    } catch (err) {
      setError((err as Error).message);
    }
  };
  return (
    <form className="card auth-form wide" onSubmit={submit}>
      <h2 className="card-title">About you</h2>
      <label>
        Full name (as on your marksheets)
        <input required minLength={2} value={a.full_name} autoComplete="name"
          onChange={(e) => setA({ ...a, full_name: e.target.value })} />
      </label>
      <label>
        Mobile number
        <input type="tel" autoComplete="tel" value={a.phone ?? ""}
          onChange={(e) => setA({ ...a, phone: e.target.value })} />
      </label>
      <fieldset className="choices">
        <legend>Your degree</legend>
        {Object.entries(degreeStatuses).map(([k, v]) => (
          <label key={k} className="choice">
            <input type="radio" name="degree" required value={k} checked={a.degree_status === k}
              onChange={() => setA({ ...a, degree_status: k })} />
            {v}
          </label>
        ))}
      </fieldset>
      <label>
        Aggregate percentage of your degree
        <input type="number" required min={0} max={100} step="0.01" inputMode="decimal"
          value={Number.isNaN(a.claimed_aggregate_percent) ? "" : a.claimed_aggregate_percent}
          onChange={(e) => setA({ ...a, claimed_aggregate_percent: e.target.valueAsNumber })} />
      </label>
      {error && <p className="error-text small" role="alert">{error}</p>}
      <button className="primary">{initial ? "Save" : "Continue to documents"}</button>
    </form>
  );
}

function ChecklistCard({ item, intakeId, editable, onChange }: {
  item: ChecklistItem; intakeId: string; editable: boolean; onChange: () => void;
}) {
  const camera = useRef<HTMLInputElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [viewing, setViewing] = useState<{ doc: StudentDocument; index: number } | null>(null);

  const upload = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      await studentApi.upload(intakeId, item.key, file);
      onChange();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
      if (camera.current) camera.current.value = "";
      if (picker.current) picker.current.value = "";
    }
  };

  const pagesToFix = item.documents.flatMap((d) =>
    d.pages.map((p, index) => ({ d, p, index })).filter(({ p }) => p.status === "fix" || p.status === "check"),
  );
  const hasFiles = item.documents.length > 0;
  // Keep the open page in step with refreshed data (e.g. after typing the details).
  const viewDoc = viewing && item.documents.find((d) => d.id === viewing.doc.id);

  return (
    <section className={`card item ${item.state}`}>
      <div className="row-between">
        <h2 className="card-title">{item.label}</h2>
        <span className={`pill st-${item.state}`}>{STATE_LABEL[item.state]}</span>
      </div>
      {item.expected.length > 0 && (
        <p className="muted small">What to upload: {item.expected.join(" or ")}</p>
      )}

      {(item.problems.length > 0 || pagesToFix.length > 0) && (
        <div className="todo">
          <div className="todo-title">What needs your attention</div>
          <ul>
            {item.problems.map((p) => <li key={p}>{p}</li>)}
            {pagesToFix.map(({ d, p, index }) => (
              <li key={p.id} className={p.status}>
                <button className="link-button" onClick={() => setViewing({ doc: d, index })}>
                  Page {p.number}
                </button>
                : {p.problems[0]}
                {p.problems.length > 1 && ` (+${p.problems.length - 1} more)`}
              </li>
            ))}
          </ul>
          {pagesToFix.some(({ p }) => p.photo_tips) && (
            <p className="small">Open a page to see exactly what to fix and how to take a clear picture.</p>
          )}
        </div>
      )}

      {item.documents.map((d) => (
        <FileCard key={d.id} doc={d} editable={editable} onChange={onChange}
          onOpen={(index) => setViewing({ doc: d, index })} />
      ))}

      {editable && (
        <div className="upload-buttons">
          <input ref={camera} type="file" accept="image/*" capture="environment" hidden
            onChange={(e) => upload(e.target.files?.[0])} />
          <input ref={picker} type="file" accept="application/pdf,image/*" hidden
            onChange={(e) => upload(e.target.files?.[0])} />
          <button className="secondary" disabled={busy} onClick={() => camera.current?.click()}>
            📷 {hasFiles ? "Take another photo" : "Take photo"}
          </button>
          <button className="secondary" disabled={busy} onClick={() => picker.current?.click()}>
            📄 {hasFiles ? "Upload another file" : "Choose file"}
          </button>
          {busy && <span className="muted small">Uploading…</span>}
        </div>
      )}
      {hasFiles && editable && item.state === "attention" && (
        <p className="muted small">
          To replace a file: upload the new one, then remove the old one.
        </p>
      )}
      {error && <p className="error-text small" role="alert">{error}</p>}

      {viewing && viewDoc && (
        <PageViewer doc={viewDoc} index={Math.min(viewing.index, viewDoc.pages.length - 1)}
          editable={editable} onIndex={(index) => setViewing({ doc: viewDoc, index })}
          onClose={() => setViewing(null)} onChange={onChange} />
      )}
    </section>
  );
}

const PAGE_BADGE: Record<StudentPage["status"], string> = { ok: "✓", check: "!", fix: "✕", waiting: "…" };
const PAGE_STATUS_TEXT: Record<StudentPage["status"], string> = {
  ok: "Looks good",
  check: "Worth a look",
  fix: "Needs fixing",
  waiting: "Being checked",
};

function pageCaption(p: StudentPage): string {
  if (p.semesters.length) return `Semester ${p.semesters.join(" & ")}`;
  return p.recognised ?? `Page ${p.number}`;
}

function FileCard({ doc, editable, onChange, onOpen }: {
  doc: StudentDocument; editable: boolean; onChange: () => void; onOpen: (index: number) => void;
}) {
  const remove = async () => {
    if (!window.confirm(`Remove ${doc.file_name ?? "this file"}? You can upload it again later.`)) return;
    await studentApi.remove(doc.id);
    onChange();
  };
  const uploaded = doc.uploaded_at ? new Date(doc.uploaded_at).toLocaleDateString() : null;
  return (
    <div className="file-card">
      <div className="file-head">
        <div className="file-name" title={doc.file_name ?? undefined}>📎 {doc.file_name ?? "Uploaded file"}</div>
        <div className="muted small">
          {doc.pages.length > 0 && `${doc.pages.length} page${doc.pages.length === 1 ? "" : "s"}`}
          {uploaded && ` · uploaded ${uploaded}`}
          {doc.processing && " · checking…"}
        </div>
      </div>
      {doc.failed && <p className="error-text small">{doc.error_message ?? "This file could not be opened. Please upload it again."}</p>}
      <div className="page-grid">
        {doc.pages.map((p, i) => (
          <button key={p.id} className={`page-tile ps-${p.status}`} onClick={() => onOpen(i)}
            title={`Page ${p.number}: ${PAGE_STATUS_TEXT[p.status]} — click to view`}>
            {p.has_image
              ? <img src={studentApi.pageImageUrl(p.id)} alt={`Page ${p.number}`} loading="lazy" />
              : <span className="no-image">No preview</span>}
            <span className="page-badge" aria-label={PAGE_STATUS_TEXT[p.status]}>{PAGE_BADGE[p.status]}</span>
            <span className="page-caption">{p.number}. {pageCaption(p)}</span>
          </button>
        ))}
        {doc.processing && doc.pages.length === 0 && <span className="muted small">Checking the file…</span>}
      </div>
      <div className="file-actions">
        {doc.pages.length > 0 && <button className="link-button small" onClick={() => onOpen(0)}>View pages</button>}
        {editable && <button className="link-button small danger-link" onClick={remove}>Remove file</button>}
      </div>
    </div>
  );
}

/** A page at full size, with what we recognised and what (if anything) to fix. */
function PageViewer({ doc, index, editable, onIndex, onClose, onChange }: {
  doc: StudentDocument; index: number; editable: boolean;
  onIndex: (i: number) => void; onClose: () => void; onChange: () => void;
}) {
  const page = doc.pages[index];
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    close.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return;
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowRight" && index < doc.pages.length - 1) onIndex(index + 1);
      if (e.key === "ArrowLeft" && index > 0) onIndex(index - 1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [index, doc.pages.length, onIndex, onClose]);
  if (!page) return null;
  return (
    <div className="viewer-backdrop" onClick={onClose}>
      <div className="viewer" role="dialog" aria-modal="true" aria-label={`Page ${page.number}`}
        onClick={(e) => e.stopPropagation()}>
        <div className="viewer-bar">
          <span className="small">
            <strong>{doc.file_name ?? "Uploaded file"}</strong> · page {page.number} of {doc.pages.length}
          </span>
          <button ref={close} className="link-button" onClick={onClose} aria-label="Close">✕ Close</button>
        </div>
        <div className="viewer-body">
          <div className="viewer-image">
            {page.has_image
              ? <img src={studentApi.pageImageUrl(page.id)} alt={`Page ${page.number}`} />
              : <p className="muted">No preview for this page.</p>}
          </div>
          <div className="viewer-side">
            <span className={`pill ps-pill-${page.status}`}>{PAGE_BADGE[page.status]} {PAGE_STATUS_TEXT[page.status]}</span>
            <div>
              <div className="muted small">We recognised this page as</div>
              <div className="strong">
                {page.recognised ?? (page.status === "waiting" ? "Still checking…" : "Not recognised")}
                {page.semesters.length > 0 && ` · semester ${page.semesters.join(" & ")}`}
              </div>
            </div>
            {page.problems.length > 0 ? (
              <div>
                <div className="muted small">What to fix</div>
                <ul className="viewer-problems">
                  {page.problems.map((m) => <li key={m}>{m}</li>)}
                </ul>
              </div>
            ) : page.status !== "waiting" && <p className="small">Nothing to fix on this page.</p>}
            {page.photo_tips && <PhotoTips />}
            {editable && page.confirm && <ConfirmForm page={page} onDone={onChange} />}
            <div className="viewer-nav">
              <button className="secondary" disabled={index === 0} onClick={() => onIndex(index - 1)}>← Previous</button>
              <button className="secondary" disabled={index >= doc.pages.length - 1} onClick={() => onIndex(index + 1)}>Next →</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/** Exactly how to take a picture we can read: shown wherever the picture is the problem. */
function PhotoTips() {
  return (
    <div className="photo-tips">
      <div className="strong small">How to take a clear picture</div>
      <ol>
        <li>Put the page flat on a table, in daylight or under a bright light.</li>
        <li>Hold your phone straight above the page, not at an angle.</li>
        <li>Move closer until the page fills the screen. Do not zoom.</li>
        <li>Tap the page on the screen to focus, hold still, then take the picture.</li>
        <li>Check there is no shadow, glare or finger on the page, and every corner is visible.</li>
      </ol>
      <div className="muted small">Have the original PDF or a scan? Upload that instead: it is always clearer.</div>
    </div>
  );
}

const PLACEHOLDER = { name: "e.g. as printed after NAME", id: "e.g. 1234567", number: "e.g. 536" };

/** We could not read some values clearly: the student types them as printed (we never show
 * what we read, so the check means something). They are kept only if they match the page. */
function ConfirmForm({ page, onDone }: { page: StudentPage; onDone: () => void }) {
  const confirm = page.confirm!;
  const [values, setValues] = useState<Record<string, string>>({});
  const [notPrinted, setNotPrinted] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const out: Record<string, string | null> = {};
      for (const f of confirm.fields) out[f.key] = notPrinted[f.key] ? null : (values[f.key] ?? "").trim();
      await studentApi.confirmPage(page.id, out);
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="confirm-form" onSubmit={submit}>
      <div className="strong small">Type these exactly as printed on this page</div>
      {confirm.mismatched.length > 0 && (
        <p className="error-text small" role="alert">
          What you typed for {confirm.mismatched.join(", ")} does not match what we could read.
          Check it against the document, or upload a clearer copy.
          {confirm.attempts_left === 1 && " You have one more try."}
        </p>
      )}
      {confirm.fields.map((f) => (
        <div key={f.key} className="confirm-field">
          <label>
            {f.label}
            <input required={!notPrinted[f.key]} disabled={notPrinted[f.key]} autoComplete="off"
              inputMode={f.kind === "number" ? "decimal" : undefined} maxLength={200}
              placeholder={PLACEHOLDER[f.kind]} value={values[f.key] ?? ""}
              onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
          </label>
          <label className="check-line small">
            <input type="checkbox" checked={!!notPrinted[f.key]}
              onChange={(e) => setNotPrinted({ ...notPrinted, [f.key]: e.target.checked })} />
            Not printed on this page
          </label>
        </div>
      ))}
      <button className="secondary" disabled={busy}>{busy ? "Checking…" : "Check"}</button>
      {error && <p className="error-text small" role="alert">{error}</p>}
    </form>
  );
}

function SubmitBar({ app, intakeId, onSubmitted }: { app: Application; intakeId: string; onSubmitted: (a: Application) => void }) {
  const [error, setError] = useState<string | null>(null);
  const submitted = app.status !== "draft" && app.status !== "changes_requested";
  const submit = async () => {
    setError(null);
    try {
      onSubmitted(await studentApi.submit(intakeId));
    } catch (err) {
      setError((err as Error).message);
    }
  };
  return (
    <section className="submit-bar">
      {submitted ? (
        <p className="small">Your application is submitted. You can still replace documents until it is reviewed.</p>
      ) : (
        <>
          <button className="primary" disabled={!app.ready_to_submit} onClick={submit}>
            {app.status === "changes_requested" ? "Submit again" : "Submit application"}
          </button>
          {!app.ready_to_submit && <p className="muted small">Upload every required document to submit.</p>}
        </>
      )}
      {error && <p className="error-text small" role="alert">{error}</p>}
    </section>
  );
}
