import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
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
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab | null) ?? "students";
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

  const setTab = (t: Tab) => setParams(t === "students" ? {} : { tab: t }, { replace: true });
  const otherName = compareTo === "baseline" ? "Baseline" : runs.find((r) => r.id === compareTo)?.name ?? "Other";

  return (
    <main className="lab">
      <Link to="/testing" className="back">← All test runs</Link>
      <header className="row-between">
        <div>
          <h1>{data.run.name}</h1>
          <p className="muted small">
            {data.students.length} student{data.students.length === 1 ? "" : "s"} · {pages} pages · reviewed {reviewed}
            {data.run.notes && <> · {data.run.notes}</>}
          </p>
        </div>
        {processing && <span className="pill st-processing">Processing…</span>}
      </header>

      <nav className="lab-tabs" role="tablist">
        {(["students", "results", "add"] as Tab[]).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
            {TAB_LABELS[t]}{t === "students" && failed > 0 && <span className="tab-badge">{failed}</span>}
          </button>
        ))}
      </nav>

      {tab === "add" && <BulkAdd data={data} onDone={() => { load(); setTab("students"); }} />}

      {tab === "students" && (
        <section className="card">
          {failed > 0 && (
            <div className="lab-failed" role="alert">
              <span>
                <strong>{failed} file{failed === 1 ? "" : "s"} could not be processed</strong> (the worker gave up after
                retrying). Their pages were not read, so documents look missing.
              </span>
              <button className="secondary" onClick={async () => { await labApi.retry(data.run.id); load(); }}>
                Retry {failed === 1 ? "it" : "them"}
              </button>
            </div>
          )}
          {data.students.length === 0 ? (
            <div className="lab-empty">
              <p>No test students yet.</p>
              <button className="primary" onClick={() => setTab("add")}>Add students</button>
            </div>
          ) : (
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
                To measure accuracy, open a student and review it: ✓ right values, correct wrong ones, fix page types.
              </p>
            </>
          )}
          <DeleteRun name={data.run.name} onDelete={async (typed) => { await labApi.remove(data.run.id, typed); navigate("/testing"); }} />
        </section>
      )}

      {tab === "results" && (
        <section className="card">
          <div className="row-between">
            <label className="small compare">Compare with
              <select value={compareTo} onChange={(e) => setCompareTo(e.target.value)}>
                <option value="baseline">Real applicants (baseline)</option>
                {runs.filter((r) => r.id !== data.run.id).map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
              </select>
            </label>
            <a className="secondary-link" href={labApi.exportUrl(data.run.id)}>⬇ Export CSV</a>
          </div>
          <Headlines mine={data.metrics} other={compare} otherName={otherName} />
          <details className="all-metrics">
            <summary>Show all metrics</summary>
            <MetricsTable mine={data.metrics} other={compare} otherName={otherName} />
            <div className="lab-lists">
              <CountList title="Document types recognised (pages)" counts={data.metrics.document_types} />
              <CountList title="Open issues (students)" counts={data.metrics.issues} labels={ISSUE_TITLES} />
            </div>
          </details>
        </section>
      )}
    </main>
  );
}

type Tab = "students" | "results" | "add";
const TAB_LABELS: Record<Tab, string> = { students: "Students", results: "Results", add: "Add students" };

// The numbers to look at first; everything else is under "Show all metrics".
const HEADLINES = [
  "recognised", "read_clear", "found_name", "sure_id", "found_grade", "verdict_clear", "students_asked", "acc_all",
];

function Headlines({ mine, other, otherName }: { mine: Metrics; other: Metrics | null; otherName: string }) {
  const by = (m: Metrics | null) => new Map(m?.sections.flatMap((s) => s.metrics.map((x) => [x.key, x] as const)) ?? []);
  const a = by(mine), b = by(other);
  return (
    <div className="headlines">
      {HEADLINES.map((k) => {
        const m = a.get(k);
        if (!m) return null;
        const o = b.get(k);
        const diff = m.value != null && o?.value != null ? m.value - o.value : null;
        const good = diff === null || Math.abs(diff) < 0.05 || !m.better ? "" : (diff > 0) === (m.better === "higher") ? "better" : "worse";
        return (
          <div key={k} className="headline">
            <div className="headline-label">{m.label}</div>
            <div className="headline-value">{fmt(m)}</div>
            <div className="muted small">
              {m.n != null && `of ${m.n}`}
              {o && <> · {otherName} {fmt(o)}{diff !== null && <span className={good}> ({diff > 0 ? "+" : ""}{Math.round(diff * 10) / 10}{m.unit === "%" ? " pts" : ""})</span>}</>}
            </div>
          </div>
        );
      })}
    </div>
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

const ACCEPTED = /\.(pdf|jpe?g|png|heic|heif|webp)$/i;

/** "1000203296 - Mohasin Nadaf" -> "Mohasin Nadaf": folder names often carry form numbers. */
function nameFromFolder(folder: string): string {
  return folder.replace(/[_.\-]+/g, " ").split(/\s+/).filter((w) => w && !/\d/.test(w)).join(" ");
}

interface Planned { key: string; name: string; files: File[]; include: boolean }

/** Group files into students: a batch folder holds one folder per student; a single
 * student's folder (or dropped files) is one student. */
function plan(files: File[]): { students: Planned[]; skipped: number } {
  const groups = new Map<string, File[]>();
  let skipped = 0;
  for (const f of files) {
    if (!ACCEPTED.test(f.name) || f.name.startsWith(".")) { skipped++; continue; }
    const parts = (f.webkitRelativePath || f.name).split("/");
    const key = parts.length >= 3 ? parts[1] : parts.length === 2 ? parts[0] : "";
    groups.set(key, [...(groups.get(key) ?? []), f]);
  }
  const students = [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, list]) => ({ key: key || "dropped", name: nameFromFolder(key), files: list, include: true }));
  return { students, skipped };
}

/** Add students in bulk: pick a batch folder (one folder per student) or drop one student's
 * files. Files are sorted into the checklist automatically once read; photo and signature
 * by their file name ("photo", "sign"). */
function BulkAdd({ data, onDone }: { data: RunDetail; onDone: () => void }) {
  const folderInput = useRef<HTMLInputElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [students, setStudents] = useState<Planned[]>([]);
  const [skipped, setSkipped] = useState(0);
  const [degree, setDegree] = useState("has_degree");
  const [progress, setProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const take = (files: File[]) => {
    const p = plan(files);
    setStudents(p.students);
    setSkipped(p.skipped);
    setError(null);
  };
  const chosen = students.filter((s) => s.include);
  const fileCount = chosen.reduce((a, s) => a + s.files.length, 0);

  const upload = async () => {
    setError(null);
    let done = 0;
    try {
      for (const [i, s] of chosen.entries()) {
        setProgress(`Student ${i + 1} of ${chosen.length}: creating…`);
        const { id } = await labApi.addStudent(data.run.id, { name: s.name, degree_status: degree, claimed_aggregate_percent: null });
        for (const f of s.files) {
          setProgress(`Student ${i + 1} of ${chosen.length} · file ${++done} of ${fileCount}: ${f.name}`);
          await labApi.upload(data.run.id, id, "auto", f);
        }
      }
      setStudents([]);
      onDone();
    } catch (err) {
      setError(`${(err as Error).message} (stopped after ${done} file${done === 1 ? "" : "s"}; students already added are kept)`);
      onDone();
    } finally {
      setProgress(null);
    }
  };

  return (
    <section className="card lab-form">
      {students.length === 0 ? (
        <div
          className={`drop-zone ${dragging ? "over" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => { e.preventDefault(); setDragging(false); take(Array.from(e.dataTransfer.files)); }}
        >
          <div className="strong">Drop one student's files here</div>
          <div className="muted small">or</div>
          <div className="drop-buttons">
            <button className="primary" onClick={() => folderInput.current?.click()}>Choose a batch folder</button>
            <button className="secondary" onClick={() => fileInput.current?.click()}>Choose files</button>
          </div>
          <p className="muted small">
            A batch folder holds one folder per student (folder name = student name; form numbers in it are ignored).
            Files are sorted into the checklist automatically after reading. Name photo and signature files with
            "photo" and "sign" so they are recognised.
          </p>
          <input ref={folderInput} type="file" hidden multiple
            {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
            onChange={(e) => take(Array.from(e.target.files ?? []))} />
          <input ref={fileInput} type="file" hidden multiple accept="application/pdf,image/*"
            onChange={(e) => take(Array.from(e.target.files ?? []))} />
        </div>
      ) : (
        <>
          <div className="row-between">
            <div className="strong">{chosen.length} student{chosen.length === 1 ? "" : "s"}, {fileCount} files
              {skipped > 0 && <span className="muted small"> · {skipped} other file{skipped === 1 ? "" : "s"} skipped (not PDF or image)</span>}
            </div>
            <label className="small compare">Degree status
              <select value={degree} onChange={(e) => setDegree(e.target.value)}>
                {Object.entries(data.degree_statuses).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
            </label>
          </div>
          <table className="lab-table">
            <thead><tr><th></th><th>Student name (check it: used for the name check)</th><th className="num">Files</th></tr></thead>
            <tbody>
              {students.map((s, i) => (
                <tr key={s.key}>
                  <td><input type="checkbox" checked={s.include} aria-label={`Include ${s.name}`}
                    onChange={(e) => setStudents(students.map((x, j) => j === i ? { ...x, include: e.target.checked } : x))} /></td>
                  <td><input className="name-input" value={s.name} placeholder="Name as on the documents"
                    onChange={(e) => setStudents(students.map((x, j) => j === i ? { ...x, name: e.target.value } : x))} /></td>
                  <td className="num" title={s.files.map((f) => f.name).join("\n")}>{s.files.length}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {error && <p className="error-text small" role="alert">{error}</p>}
          <div className="row-between">
            <button className="link-button" disabled={!!progress} onClick={() => setStudents([])}>Start over</button>
            <span className="muted small">{progress}</span>
            <button className="primary" disabled={!!progress || fileCount === 0} onClick={upload}>
              Upload {chosen.length} student{chosen.length === 1 ? "" : "s"}
            </button>
          </div>
        </>
      )}
    </section>
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
