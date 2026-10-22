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

  return (
    <section className={`card item ${item.state}`}>
      <div className="row-between">
        <h2 className="card-title">{item.label}</h2>
        <span className={`pill st-${item.state}`}>{STATE_LABEL[item.state]}</span>
      </div>
      {item.expected.length > 0 && (
        <p className="muted small">Accepted: {item.expected.join(" or ")}</p>
      )}
      {item.problems.map((p) => <p key={p} className="warning-text small">⚠ {p}</p>)}
      {item.documents.map((d) => (
        <DocumentRow key={d.id} doc={d} editable={editable} onChange={onChange} />
      ))}
      {editable && (
        <div className="upload-buttons">
          <input ref={camera} type="file" accept="image/*" capture="environment" hidden
            onChange={(e) => upload(e.target.files?.[0])} />
          <input ref={picker} type="file" accept="application/pdf,image/*" hidden
            onChange={(e) => upload(e.target.files?.[0])} />
          <button className="secondary" disabled={busy} onClick={() => camera.current?.click()}>📷 Take photo</button>
          <button className="secondary" disabled={busy} onClick={() => picker.current?.click()}>📄 Choose file</button>
          {busy && <span className="muted small">Uploading…</span>}
        </div>
      )}
      {error && <p className="error-text small" role="alert">{error}</p>}
    </section>
  );
}

function DocumentRow({ doc, editable, onChange }: { doc: StudentDocument; editable: boolean; onChange: () => void }) {
  const remove = async () => {
    if (!window.confirm("Remove this file?")) return;
    await studentApi.remove(doc.id);
    onChange();
  };
  return (
    <div className="doc-row">
      <div className="thumbs">
        {doc.pages.filter((p) => p.has_image).slice(0, 6).map((p) => (
          <figure key={p.id} className={`thumb q-${p.quality}`}>
            <img src={studentApi.pageImageUrl(p.id)} alt={`Page ${p.number}`} loading="lazy" />
            <figcaption>{p.number}</figcaption>
          </figure>
        ))}
        {doc.processing && doc.pages.length === 0 && <span className="muted small">Checking the file…</span>}
      </div>
      {doc.failed && <p className="error-text small">{doc.error_message ?? "This file could not be opened."}</p>}
      {doc.pages.flatMap((p) => p.problems.map((m) => (
        <p key={`${p.id}${m}`} className="warning-text small">Page {p.number}: {m}</p>
      )))}
      {editable && doc.pages.filter((p) => p.confirm).map((p) => (
        <ConfirmForm key={`${p.id}-confirm`} page={p} onDone={onChange} />
      ))}
      {editable && <button className="link-button small" onClick={remove}>Remove this file</button>}
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
      <div className="strong small">Page {page.number}: please type these exactly as printed</div>
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
