import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  labApi,
  type Metric,
  type Metrics,
  type RunDetail,
  type RunSummary,
} from "../api";
import { ISSUE_TITLES, VERDICT_LABELS, label } from "../labels";

const POLL_MS = 5000;

/** Testing Lab: batches of real student documents, processed like live uploads (after
 * them), measured against the real applicants (baseline) or another batch. */
export function TestingLab() {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [checklists, setChecklists] = useState<{ id: string; name: string }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    labApi.runs().then((r) => { setRuns(r.runs); setChecklists(r.checklists); }, (e: Error) => setError(e.message));
  }, []);

  if (error) return <p className="page-pad error-text">{error}</p>;
  if (!runs) return <p className="page-pad muted">Loading…</p>;
  return (
    <main className="lab">
      <header>
        <h1>Testing Lab</h1>
        <p className="muted">
          Upload real students' documents as test students. They are processed exactly like live uploads (after
          them in the queue), never appear in the verifier queue or real statistics, and the results are measured
          here so you can compare batches and settings.
        </p>
      </header>
      <NewRun checklists={checklists} onCreated={(id) => navigate(`/testing/${id}`)} />
      <section className="card">
        <h2 className="card-title">Test runs</h2>
        {runs.length === 0 ? <p className="muted small">No test runs yet.</p> : (
          <table className="lab-table">
            <thead><tr><th>Name</th><th className="num">Students</th><th className="num">Pages</th><th>Created</th></tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id}>
                  <td><Link to={`/testing/${r.id}`}>{r.name}</Link>{r.notes && <div className="muted small">{r.notes}</div>}</td>
                  <td className="num">{r.students}</td>
                  <td className="num">{r.pages}</td>
                  <td className="small">{new Date(r.created_at).toLocaleString()}{r.created_by && <span className="muted"> · {r.created_by}</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}

function NewRun({ checklists, onCreated }: { checklists: { id: string; name: string }[]; onCreated: (id: string) => void }) {
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [checklist, setChecklist] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      onCreated((await labApi.create(name, notes, checklist || null)).id);
    } catch (err) {
      setError((err as Error).message);
    }
  };
  return (
    <form className="card lab-form" onSubmit={submit}>
      <h2 className="card-title">New test run</h2>
      <label>Name<input required maxLength={255} value={name} placeholder="e.g. Pune batch, 29 Oct"
        onChange={(e) => setName(e.target.value)} /></label>
      <label>Notes (what you are testing)<input maxLength={4000} value={notes}
        placeholder="e.g. after switching to Paddle 3.2.2" onChange={(e) => setNotes(e.target.value)} /></label>
      {checklists.length > 1 && (
        <label>Checklist
          <select value={checklist} onChange={(e) => setChecklist(e.target.value)}>
            <option value="">{checklists[0].name}</option>
            {checklists.slice(1).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </label>
      )}
      {error && <p className="error-text small">{error}</p>}
      <button className="primary">Create test run</button>
    </form>
  );
}

export function TestRunView() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [data, setData] = useState<RunDetail | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [compareTo, setCompareTo] = useState("baseline");
  const [compare, setCompare] = useState<Metrics | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    if (id) labApi.run(id).then(setData, (e: Error) => setError(e.message));
  }, [id]);
  useEffect(load, [load]);
  useEffect(() => { labApi.runs().then((r) => setRuns(r.runs), () => undefined); }, []);
  useEffect(() => {
    setCompare(null);
    labApi.metrics(compareTo).then(setCompare, () => setCompare(null));
  }, [compareTo]);

  const processing = data?.students.some((s) => s.processing > 0) ?? false;
  useEffect(() => {
    if (!processing) return;
    const t = window.setTimeout(load, POLL_MS);
    return () => window.clearTimeout(t);
  }, [processing, data, load]);

  if (error) return <p className="page-pad error-text">{error}</p>;
  if (!data) return <p className="page-pad muted">Loading…</p>;
  const reviewed = data.students.reduce((a, s) => a + s.reviewed_pages, 0);
  const failed = data.students.reduce((a, s) => a + s.failed, 0);
  const pages = data.students.reduce((a, s) => a + s.pages, 0);

  return (
    <main className="lab">
      <Link to="/testing" className="back">← All test runs</Link>
      <header className="row-between">
        <div>
          <h1>{data.run.name}</h1>
          {data.run.notes && <p className="muted">{data.run.notes}</p>}
        </div>
        <a className="secondary-link" href={labApi.exportUrl(data.run.id)}>⬇ Export metrics (CSV)</a>
      </header>

      <AddStudent data={data} onDone={load} />

      <section className="card">
        <div className="row-between">
          <h2 className="card-title">Test students ({data.students.length})</h2>
          {processing && <span className="pill st-processing">Processing…</span>}
        </div>
        {failed > 0 && (
          <div className="lab-failed" role="alert">
            <span>
              <strong>{failed} file{failed === 1 ? "" : "s"} could not be processed</strong> (the worker gave up after
              retrying). Their pages were not read, so they show as "not recognised" and documents look missing.
            </span>
            <button className="secondary" onClick={async () => { await labApi.retry(data.run.id); load(); }}>
              Retry {failed === 1 ? "it" : "them"}
            </button>
          </div>
        )}
        {data.students.length === 0 ? <p className="muted small">Add a test student above.</p> : (
          <>
            <table className="lab-table">
              <thead><tr><th>Student</th><th className="num">Files</th><th className="num">Pages</th><th>Result</th><th className="num">Reviewed</th></tr></thead>
              <tbody>
                {data.students.map((s) => (
                  <tr key={s.id}>
                    <td><Link to={`/submissions/${s.id}`}>{s.name}</Link></td>
                    <td className="num">{s.documents}</td>
                    <td className="num">{s.pages}</td>
                    <td>{s.processing > 0
                      ? <span className="muted small">processing {s.processing} file{s.processing === 1 ? "" : "s"}…</span>
                      : <span className={`verdict ${s.verdict}`}>{VERDICT_LABELS[s.verdict]}</span>}
                      {s.failed > 0 && <span className="error-text small"> · {s.failed} failed</span>}</td>
                    <td className="num">{s.reviewed_pages} / {s.pages}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted small">
              For accuracy, open a student and review it like a verifier: ✓ values that are right, correct the
              wrong ones, add missing ones, and fix page types. Reviewed so far: {reviewed} of {pages} pages.
            </p>
          </>
        )}
      </section>

      <section className="card">
        <div className="row-between">
          <h2 className="card-title">Metrics</h2>
          <label className="small compare">Compare with
            <select value={compareTo} onChange={(e) => setCompareTo(e.target.value)}>
              <option value="baseline">Real applicants (baseline)</option>
              {runs.filter((r) => r.id !== data.run.id).map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
            </select>
          </label>
        </div>
        <MetricsTable mine={data.metrics} other={compare}
          otherName={compareTo === "baseline" ? "Baseline" : runs.find((r) => r.id === compareTo)?.name ?? "Other"} />
        <div className="lab-lists">
          <CountList title="Document types recognised (pages)" counts={data.metrics.document_types} />
          <CountList title="Open issues (students)" counts={data.metrics.issues} labels={ISSUE_TITLES} />
        </div>
      </section>

      <DeleteRun name={data.run.name} onDelete={async (typed) => { await labApi.remove(data.run.id, typed); navigate("/testing"); }} />
    </main>
  );
}

function fmt(m: Metric | undefined): string {
  if (!m || m.value === null || m.value === undefined) return "—";
  if (m.unit === "%") return `${m.value}%`;
  if (m.unit === "s") return `${m.value} s`;
  return String(m.value);
}

/** This run next to the comparison, with the difference coloured by whether it is better. */
function MetricsTable({ mine, other, otherName }: { mine: Metrics; other: Metrics | null; otherName: string }) {
  const otherBy = new Map(other?.sections.flatMap((s) => s.metrics.map((m) => [m.key, m] as const)) ?? []);
  return (
    <div className="metrics">
      {mine.sections.map((s) => (
        <table key={s.key} className="lab-table metrics-table">
          <thead>
            <tr><th>{s.title}</th><th className="num">This run</th><th className="num">{otherName}</th><th className="num">Difference</th></tr>
          </thead>
          <tbody>
            {s.metrics.map((m) => {
              const o = otherBy.get(m.key);
              const diff = m.value !== null && o?.value != null && m.unit !== "count" ? m.value - o.value : null;
              const good = diff === null || diff === 0 || !m.better ? "" : (diff > 0) === (m.better === "higher") ? "better" : "worse";
              return (
                <tr key={m.key}>
                  <td>{m.label}</td>
                  <td className="num">{fmt(m)}{m.n != null && m.unit !== "count" && <span className="muted small"> of {m.n}</span>}</td>
                  <td className="num">{fmt(o)}{o?.n != null && o.unit !== "count" && <span className="muted small"> of {o.n}</span>}</td>
                  <td className={`num ${good}`}>
                    {diff === null ? "" : `${diff > 0 ? "+" : ""}${Math.round(diff * 10) / 10}${m.unit === "%" ? " pts" : m.unit === "s" ? " s" : ""}`}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ))}
      <p className="muted small">
        "of N" is what a rate is out of: treat rates over a handful of pages with care. Accuracy counts only what
        you have reviewed.
      </p>
    </div>
  );
}

function CountList({ title, counts, labels }: { title: string; counts: Record<string, number>; labels?: Record<string, string> }) {
  const entries = Object.entries(counts);
  return (
    <div>
      <div className="strong small">{title}</div>
      {entries.length === 0 ? <p className="muted small">None.</p> : (
        <ul className="count-list">
          {entries.map(([k, n]) => <li key={k}><span>{labels ? label(labels, k) : k}</span><span className="num">{n}</span></li>)}
        </ul>
      )}
    </div>
  );
}

/** A test student: name and answers like a real one, then files per checklist item. */
function AddStudent({ data, onDone }: { data: RunDetail; onDone: () => void }) {
  const [name, setName] = useState("");
  const [degree, setDegree] = useState("has_degree");
  const [claimed, setClaimed] = useState("");
  const [files, setFiles] = useState<Record<string, File[]>>({});
  const [progress, setProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const form = useRef<HTMLFormElement>(null);
  const count = Object.values(files).reduce((a, f) => a + f.length, 0);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      setProgress("Creating the test student…");
      const { id } = await labApi.addStudent(data.run.id, {
        name, degree_status: degree, claimed_aggregate_percent: claimed === "" ? null : Number(claimed),
      });
      let n = 0;
      for (const [slot, list] of Object.entries(files)) {
        for (const f of list) {
          setProgress(`Uploading ${++n} of ${count}: ${f.name}`);
          await labApi.upload(data.run.id, id, slot, f);
        }
      }
      setName(""); setClaimed(""); setFiles({});
      form.current?.reset();
      onDone();
    } catch (err) {
      setError((err as Error).message);
      onDone();
    } finally {
      setProgress(null);
    }
  };

  return (
    <form ref={form} className="card lab-form" onSubmit={submit}>
      <h2 className="card-title">Add a test student</h2>
      <div className="lab-grid">
        <label>Full name (as on the documents)<input value={name} maxLength={255}
          placeholder="Leave empty to skip the name check" onChange={(e) => setName(e.target.value)} /></label>
        <label>Degree status
          <select value={degree} onChange={(e) => setDegree(e.target.value)}>
            {Object.entries(data.degree_statuses).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label>Claimed aggregate % (optional)<input type="number" min={0} max={100} step="0.01" value={claimed}
          onChange={(e) => setClaimed(e.target.value)} /></label>
      </div>
      <div className="lab-files">
        {data.checklist.map((item) => (
          <label key={item.key} className="lab-file">
            <span>{item.label}</span>
            <input type="file" multiple accept="application/pdf,image/*"
              onChange={(e) => setFiles({ ...files, [item.key]: Array.from(e.target.files ?? []) })} />
          </label>
        ))}
      </div>
      {error && <p className="error-text small" role="alert">{error}</p>}
      <div className="row-between">
        <span className="muted small">{progress ?? `${count} file${count === 1 ? "" : "s"} selected`}</span>
        <button className="primary" disabled={!!progress || count === 0}>Add student and upload</button>
      </div>
    </form>
  );
}

function DeleteRun({ name, onDelete }: { name: string; onDelete: (typed: string) => Promise<void> }) {
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  return (
    <details className="danger-zone">
      <summary>Delete this test run</summary>
      <p className="small">Removes every test student in it, their files and results. This cannot be undone.</p>
      <label className="small">Type <strong>{name}</strong> to confirm
        <input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
      </label>
      <button className="erase-button" disabled={typed.trim() !== name}
        onClick={() => onDelete(typed).catch((e: Error) => setError(e.message))}>Delete test run</button>
      {error && <p className="error-text small">{error}</p>}
    </details>
  );
}
