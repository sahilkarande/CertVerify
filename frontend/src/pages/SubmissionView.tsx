import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  adminApi,
  console_,
  fetchSubmission,
  pageImageUrl,
  type DocumentInfo,
  type Field,
  type Issue,
  type Me,
  type Page,
  type SubmissionDetail,
} from "../api";
import UploadPanel from "./UploadPanel";
import { DecisionPanel, IssueActions, MessageThread } from "./ConsolePanels";
import {
  DEGREE_STATUS,
  FIELD_LABELS,
  HIGH_CONFIDENCE,
  ISSUE_TITLES,
  SLOT_LABELS,
  VERDICT_LABELS,
  humanize,
  label,
} from "../labels";

interface Selected {
  doc: DocumentInfo;
  page: Page;
}

/** Pages an issue points at, within its file; null = the whole file. */
function issuePages(i: Issue): number[] | null {
  if (typeof i.details.page === "number") return [i.details.page];
  if (Array.isArray(i.details.pages)) return i.details.pages.filter((n): n is number => typeof n === "number");
  return null;
}

const isOn = (i: Issue, doc: DocumentInfo, page: Page) => {
  const pages = issuePages(i);
  return i.document_id === doc.id && (pages === null || pages.includes(page.number));
};

/** Typing in a field must never flip pages. */
const typing = (t: EventTarget | null) =>
  t instanceof HTMLElement && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName));

export default function SubmissionView({ me }: { me: Me }) {
  const { id } = useParams();
  const [data, setData] = useState<SubmissionDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pageId, setPageId] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [help, setHelp] = useState(false);

  const load = useCallback(
    (focusDocument?: string) => {
      if (!id) return;
      fetchSubmission(id).then(
        (d) => {
          setData(d);
          const doc = d.documents.find((x) => x.id === focusDocument);
          const first = (doc ? doc.pages : d.documents.flatMap((x) => x.pages)).find(
            (p) => p.has_image,
          );
          setPageId((current) => (focusDocument || !current ? (first?.id ?? null) : current));
        },
        (e: Error) => setError(e.message),
      );
    },
    [id],
  );

  useEffect(() => {
    setData(null);
    setError(null);
    setPageId(null);
    load();
  }, [load]);

  const allPages = useMemo(
    () => data?.documents.flatMap((doc) => doc.pages.map((page) => ({ doc, page }))) ?? [],
    [data],
  );
  const openIssues = useMemo(() => data?.issues.filter((i) => i.status === "open") ?? [], [data]);
  // Pages that still have something to look at (warnings and errors; notes do not count).
  const flagged = useMemo(() => {
    const set = new Set<string>();
    for (const i of openIssues) {
      if (i.severity === "info") continue;
      const doc = data?.documents.find((d) => d.id === i.document_id);
      if (!doc) continue;
      const pages = issuePages(i);
      const hits = pages === null ? doc.pages.slice(0, 1) : doc.pages.filter((p) => pages.includes(p.number));
      (hits.length ? hits : doc.pages.slice(0, 1)).forEach((p) => set.add(p.id));
    }
    return set;
  }, [openIssues, data]);
  const selected: Selected | undefined = allPages.find((x) => x.page.id === pageId);
  const index = allPages.findIndex((x) => x.page.id === pageId);

  const step = useCallback(
    (delta: number) => {
      const next = allPages[index + delta];
      if (next) setPageId(next.page.id);
    },
    [allPages, index],
  );
  const nextFlagged = useCallback(
    (delta: 1 | -1) => {
      const n = allPages.length;
      for (let k = 1; k <= n; k++) {
        const x = allPages[(((index + delta * k) % n) + n) % n];
        if (flagged.has(x.page.id)) return setPageId(x.page.id);
      }
    },
    [allPages, flagged, index],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (typing(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === "ArrowRight" || e.key === "j") step(1);
      else if (e.key === "ArrowLeft" || e.key === "k") step(-1);
      else if (e.key === "n") nextFlagged(1);
      else if (e.key === "p") nextFlagged(-1);
      else if (e.key === "?") setHelp((h) => !h);
      else if (e.key === "Escape") setHelp(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step, nextFlagged]);

  if (error) return <p className="page-pad error-text">Could not load this applicant ({error}).</p>;
  if (!data) return <p className="page-pad muted">Loading…</p>;

  const closedIssues = data.issues.filter((i) => i.status !== "open");
  const general = openIssues.filter((i) => !i.document_id);
  const docCount = (doc: DocumentInfo) =>
    openIssues.filter((i) => i.document_id === doc.id && i.severity !== "info");
  const toCheck = openIssues.filter((i) => i.severity !== "info").length;
  const lastMessage = data.messages[data.messages.length - 1];

  const attrs = data.attributes;
  return (
    <main className="review-layout">
      <aside className="sidebar">
        <div className="sidebar-scroll">
          {data.test_run ? (
            <Link to={`/testing/${data.test_run.id}`} className="back">← Test run: {data.test_run.name}</Link>
          ) : (
            <Link to="/" className="back">
              ← All applicants
            </Link>
          )}
          <h1 className="applicant-name">
            {data.applicant.name}
            {data.test_run && <span className="pill test-pill">Test student</span>}
          </h1>
          <div className="todo-bar">
            <span className={`verdict ${data.verdict}`}>{VERDICT_LABELS[data.verdict]}</span>
            {flagged.size > 0 ? (
              <button className="secondary small-button" onClick={() => nextFlagged(1)} title="Shortcut: N">
                Next issue ›
              </button>
            ) : (
              toCheck === 0 && <span className="muted small">Nothing left to check</span>
            )}
          </div>

          {general.length > 0 && (
            <section>
              <h2>
                About the application <span className="count">{general.length}</span>
              </h2>
              <ul className="issues">
                {general.map((i) => (
                  <IssueItem key={i.id} issue={i} reload={() => load()} />
                ))}
              </ul>
            </section>
          )}

          <h2>Documents</h2>
          <ul className="docs">
            {data.documents.map((doc) => {
              const issues = docCount(doc);
              return (
                <li key={doc.id}>
                  <div className="doc-head">
                    <span>{label(SLOT_LABELS, doc.slot)}</span>
                    {issues.length > 0 && (
                      <span className={`flag ${issues.some((i) => i.severity === "error") ? "error" : "warning"}`}
                        title={issues.map((i) => label(ISSUE_TITLES, i.code)).join("\n")}>
                        {issues.length}
                      </span>
                    )}
                  </div>
                  {doc.processing_error && <p className="error-text small">{doc.processing_error}</p>}
                  <div className="page-chips">
                    {doc.pages.map((p) => (
                      <button
                        key={p.id}
                        className={`chip ${p.id === pageId ? "active" : ""} ${p.type ? "" : "unknown"} ${flagged.has(p.id) ? "flagged" : ""}`}
                        onClick={() => setPageId(p.id)}
                        title={`${p.type_name ?? "Not recognised"}${p.semesters.length ? ` · sem ${p.semesters.join(", ")}` : ""}${flagged.has(p.id) ? " · has an issue" : ""}`}
                      >
                        {p.number}
                      </button>
                    ))}
                  </div>
                </li>
              );
            })}
          </ul>

          <details className="fold" open={!!lastMessage && !lastMessage.from_staff}>
            <summary>
              Messages <span className="count">{data.messages.length}</span>
              {lastMessage && !lastMessage.from_staff && <span className="pill small-pill">Student replied</span>}
            </summary>
            <MessageThread messages={data.messages}
              send={async (body) => { await console_.message(data.id, body); load(); }} />
          </details>
          <details className="fold">
            <summary>Student details</summary>
            <dl className="facts">
              <dt>Form no.</dt>
              <dd className="mono">{data.applicant.form_number ?? "—"}</dd>
              <dt>Degree status</dt>
              <dd>{label(DEGREE_STATUS, attrs.degree_status as string)}</dd>
              <dt>Claimed aggregate</dt>
              <dd>{attrs.claimed_aggregate_percent != null ? `${attrs.claimed_aggregate_percent}%` : "—"}</dd>
              <dt>Course</dt>
              <dd>{(attrs.course as string) ?? "—"}</dd>
              <dt>Email</dt>
              <dd className="wrap">{data.applicant.email ?? "—"}</dd>
            </dl>
          </details>
          <details className="fold">
            <summary>Add a file for the student</summary>
            <UploadPanel submissionId={data.id} onProcessed={load} />
          </details>
          {closedIssues.length > 0 && (
            <details className="fold">
              <summary>
                Resolved or dismissed <span className="count">{closedIssues.length}</span>
              </summary>
              <ul className="issues closed">
                {closedIssues.map((i) => (
                  <IssueItem key={i.id} issue={i} reload={() => load()} />
                ))}
              </ul>
            </details>
          )}
          {(me.role === "platform_admin" || me.role === "institution_admin") && <DeleteRecord data={data} />}
        </div>
        <div className="sidebar-footer">
          <DecisionPanel data={data} reload={() => load()} />
        </div>
      </aside>

      {selected ? (
        <>
          <PageViewer page={selected.page} hovered={hovered} />
          <PagePanel
            sel={selected}
            issues={openIssues.filter((i) => isOn(i, selected.doc, selected.page))}
            hovered={hovered}
            onHover={setHovered}
            position={`${index + 1} / ${allPages.length}`}
            onStep={step}
            onHelp={() => setHelp(true)}
            reload={() => load()}
            types={data.document_types}
          />
        </>
      ) : (
        <p className="page-pad muted">This applicant has no pages to show.</p>
      )}
      {help && <ShortcutHelp onClose={() => setHelp(false)} />}
    </main>
  );
}

function ShortcutHelp({ onClose }: { onClose: () => void }) {
  const keys: [string, string][] = [
    ["→  or  J", "Next page"],
    ["←  or  K", "Previous page"],
    ["N", "Next page with an issue"],
    ["P", "Previous page with an issue"],
    ["?", "Show or hide this list"],
  ];
  return (
    <div className="shortcut-backdrop" onClick={onClose}>
      <div className="shortcut-card" role="dialog" aria-label="Keyboard shortcuts" onClick={(e) => e.stopPropagation()}>
        <h2>Keyboard shortcuts</h2>
        <dl>
          {keys.map(([k, v]) => (
            <div key={k}>
              <dt><kbd>{k}</kbd></dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
        <button className="secondary" onClick={onClose}>Close</button>
      </div>
    </div>
  );
}

/** Same comparison as the server: case, spacing and punctuation do not matter. */
const sameName = (a: string, b: string) => {
  const norm = (s: string) => s.normalize("NFKD").toLowerCase().replace(/[^a-z]+/g, " ").trim();
  return norm(a) !== "" && norm(a) === norm(b);
};

/** Admins only. Irreversible, so: two clearly different scopes, and the student's name
 * typed in full before the button works (the server checks it again). */
function DeleteRecord({ data }: { data: SubmissionDetail }) {
  const navigate = useNavigate();
  const [scope, setScope] = useState<"application" | "person">("application");
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const others = data.applicant.applications - 1;
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      if (scope === "application") await adminApi.deleteApplication(data.id, typed);
      else await adminApi.eraseApplicant(data.applicant.id, typed);
      navigate("/");
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };
  return (
    <details className="danger-zone">
      <summary>Delete this student's data</summary>
      <fieldset className="choices">
        <label className="choice">
          <input type="radio" name="scope" checked={scope === "application"} onChange={() => setScope("application")} />
          <span>
            <strong>Delete this application</strong>
            <span className="muted small"> — its documents, files, results and messages. The student keeps their
              account and can start again.</span>
          </span>
        </label>
        <label className="choice">
          <input type="radio" name="scope" checked={scope === "person"} onChange={() => setScope("person")} />
          <span>
            <strong>Erase this student completely</strong>
            <span className="muted small"> — {others > 0 ? `all ${others + 1} applications` : "the application"}, every
              file, their name, email, phone, form number and sign-in account. They will not come back from a later
              form import.</span>
          </span>
        </label>
      </fieldset>
      <p className="small warning-text">This cannot be undone. Backups made before today still hold the data.</p>
      <label className="small">
        Type <strong>{data.applicant.name}</strong> to confirm
        <input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" aria-label="Student's full name" />
      </label>
      <button className="erase-button" disabled={busy || !sameName(typed, data.applicant.name)} onClick={run}>
        {busy ? "Deleting…" : scope === "application" ? "Delete this application" : "Erase this student"}
      </button>
      {error && <p className="error-text small" role="alert">{error}</p>}
    </details>
  );
}

/** Bookkeeping, or already shown by where the issue appears. */
const HIDDEN_DETAILS = new Set(["item", "key", "rules_version", "page", "pages"]);

function IssueItem({ issue, reload }: { issue: Issue; reload: () => void }) {
  const details = Object.entries(issue.details).filter(([k]) => !HIDDEN_DETAILS.has(k));
  return (
    <li className={`issue ${issue.severity} ${issue.status}`}>
      <div className="issue-title">
        <span className={`sev ${issue.severity}`}>{issue.severity}</span>
        {label(ISSUE_TITLES, issue.code)}
        {issue.status !== "open" && <span className="muted small"> · {issue.status}</span>}
      </div>
      <p>{issue.message}</p>
      {details.length > 0 && (
        <dl className="issue-details">
          {details.map(([k, v]) => (
            <div key={k}>
              <dt>{humanize(k)}</dt>
              <dd>{formatValue(v)}</dd>
            </div>
          ))}
        </dl>
      )}
      <IssueActions issue={issue} reload={reload} />
    </li>
  );
}

function formatValue(v: unknown): string {
  if (Array.isArray(v)) return v.map((x) => (typeof x === "string" ? x.replace(/_/g, " ") : String(x))).join(", ");
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (v === null || v === undefined) return "—";
  return String(v).replace(/_/g, " ");
}

function PageViewer({ page, hovered }: { page: Page; hovered: string | null }) {
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    setLoaded(false);
    setFailed(false);
  }, [page.id]);

  if (!page.has_image) return <section className="page-view muted">No image for this page.</section>;
  return (
    <section className="page-view">
      <div className="image-wrap">
        {!loaded && !failed && <div className="image-loading muted">Loading page…</div>}
        {failed && <div className="image-loading error-text">Could not load the page image.</div>}
        <img
          key={page.id}
          src={pageImageUrl(page.id)}
          alt={`Page ${page.number}: ${page.type_name ?? "unrecognised document"}`}
          onLoad={() => setLoaded(true)}
          onError={() => setFailed(true)}
          style={{ visibility: loaded ? "visible" : "hidden" }}
        />
        {loaded &&
          page.fields
            .filter((f) => f.box)
            .map((f) => (
              <div
                key={f.key}
                className={`field-box ${hovered === f.key ? "hot" : ""} ${confClass(f.confidence)}`}
                style={{
                  left: `${f.box!.x * 100}%`,
                  top: `${f.box!.y * 100}%`,
                  width: `${f.box!.w * 100}%`,
                  height: `${f.box!.h * 100}%`,
                }}
                title={`${label(FIELD_LABELS, f.key)}: ${f.value}`}
              />
            ))}
      </div>
    </section>
  );
}

function confClass(c: number | null) {
  if (c == null) return "low";
  return c >= HIGH_CONFIDENCE ? "high" : c >= 0.7 ? "mid" : "low";
}

function PagePanel(props: {
  sel: Selected;
  issues: Issue[];
  hovered: string | null;
  onHover: (k: string | null) => void;
  position: string;
  onStep: (d: number) => void;
  onHelp: () => void;
  reload: () => void;
  types: { code: string; name: string }[];
}) {
  const { doc, page } = props.sel;
  const fieldOrder = Object.keys(FIELD_LABELS);
  const fields = [...page.fields].sort(
    (a, b) => fieldOrder.indexOf(a.key) - fieldOrder.indexOf(b.key),
  );
  const problems = props.issues.filter((i) => i.severity !== "info");
  const notes = props.issues.filter((i) => i.severity === "info");
  return (
    <aside className="panel">
      <div className="pager">
        <button onClick={() => props.onStep(-1)} aria-label="Previous page">
          ‹
        </button>
        <span className="muted small">Page {props.position}</span>
        <button onClick={() => props.onStep(1)} aria-label="Next page">
          ›
        </button>
      </div>
      <h2 className="page-title">{page.type_name ?? "Not recognised"}</h2>
      <p className="muted small">
        {label(SLOT_LABELS, doc.slot)} · page {page.number} of {doc.page_count}
        {page.semesters.length > 0 && ` · semester ${page.semesters.join(", ")}`}
      </p>

      {problems.length > 0 ? (
        <ul className="issues page-issues">
          {problems.map((i) => (
            <IssueItem key={i.id} issue={i} reload={props.reload} />
          ))}
        </ul>
      ) : (
        <p className="all-clear small">Nothing flagged on this page.</p>
      )}
      {notes.length > 0 && (
        <details className="fold notes">
          <summary className="small">
            {notes.length} note{notes.length > 1 ? "s" : ""} (no action needed)
          </summary>
          <ul className="issues">
            {notes.map((i) => (
              <IssueItem key={i.id} issue={i} reload={props.reload} />
            ))}
          </ul>
        </details>
      )}

      <h2>Values read from this page</h2>
      {fields.length === 0 ? (
        <p className="muted small">No values read from this page.</p>
      ) : (
        <table className="fields">
          <tbody>
            {fields.map((f) => (
              <FieldRow key={f.id} f={f} hovered={props.hovered} onHover={props.onHover} reload={props.reload} />
            ))}
          </tbody>
        </table>
      )}
      {page.addable_fields.length > 0 && <AddField page={page} reload={props.reload} />}

      <details className="fold" open={!page.type}>
        <summary className="small">{page.type ? "Page details and type" : "Set the page type"}</summary>
        <PageTypePicker page={page} types={props.types} reload={props.reload} />
        <dl className="facts">
          <dt>Recognised by</dt>
          <dd>
            {page.classification_method ? humanize(page.classification_method) : "—"}
            {page.classification_confidence != null && (
              <ConfidenceTag value={page.classification_confidence} />
            )}
          </dd>
          <dt>Quality</dt>
          <dd className={page.quality && page.quality !== "ok" ? "warning-text" : ""}>
            {page.quality ?? "—"}
            {page.quality_reasons.length > 0 && ` (${page.quality_reasons.join(", ").replace(/_/g, " ")})`}
          </dd>
          <dt>Text reading</dt>
          <dd>
            {page.ocr
              ? `${page.ocr.engine}, ${page.ocr.status.replace(/_/g, " ")}${page.ocr.confidence != null ? `, ${Math.round(page.ocr.confidence)}%` : ""}`
              : "—"}
          </dd>
        </dl>
      </details>
      <p className="muted small legend">
        <span className="swatch high" /> confident <span className="swatch mid" /> check{" "}
        <span className="swatch low" /> unsure
      </p>
      <button className="link-button small" onClick={props.onHelp}>
        Keyboard shortcuts <kbd>?</kbd>
      </button>
    </aside>
  );
}

function FieldRow({ f, hovered, onHover, reload }: {
  f: Field; hovered: string | null; onHover: (k: string | null) => void; reload: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(f.value ?? "");
  const [error, setError] = useState<string | null>(null);
  const save = async (v: string) => {
    setError(null);
    try {
      await console_.correctField(f.id, v, f.value);
      setEditing(false);
      reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const human = f.source === "human";
  const student = f.source === "student";
  return (
    <tr className={hovered === f.key ? "hot" : ""} onMouseEnter={() => onHover(f.key)} onMouseLeave={() => onHover(null)}>
      <th>{label(FIELD_LABELS, f.key)}</th>
      <td>
        {editing ? (
          <form className="inline-edit" onSubmit={(e) => { e.preventDefault(); save(value); }}>
            <input autoFocus value={value} onChange={(e) => setValue(e.target.value)} aria-label={`Correct ${f.key}`} />
            <button className="secondary">Save</button>
            <button type="button" className="link-button small" onClick={() => { setEditing(false); setValue(f.value ?? ""); }}>Cancel</button>
          </form>
        ) : (
          <>
            <button className="value value-button" title="Click to correct" onClick={() => setEditing(true)}>
              {f.value ?? "—"}
            </button>
            {(human || student) && f.machine_value !== f.value && (
              <span className="muted small"> (read as {f.machine_value ?? "nothing"})</span>
            )}
            {student && <span className="muted small" title="Typed by the student and matched against the page"> · typed by student</span>}
          </>
        )}
        {error && <div className="error-text small">{error}</div>}
      </td>
      <td className="num">
        {human ? (
          <span className="conf high" title="Checked by a verifier">✓ checked</span>
        ) : (
          <>
            {f.confidence != null && <ConfidenceTag value={f.confidence} />}
            <button className="confirm" title="The value is right" onClick={() => save(f.value ?? "")}>✓</button>
          </>
        )}
      </td>
    </tr>
  );
}

const METHOD_LABELS: Record<string, string> = {
  content_exact: "Title found",
  content_fuzzy: "Title found (approximate)",
  continuation: "Same as previous page",
  slot_hint: "Guessed from upload slot",
  model: "Model suggestion",
  phrase: "Admin title phrase",
  human: "Set by a verifier",
};

function PageTypePicker({ page, types, reload }: {
  page: Page; types: { code: string; name: string }[]; reload: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="type-picker small">
      <span className="muted">{page.classification_method ? METHOD_LABELS[page.classification_method] ?? page.classification_method : "Not recognised"}</span>
      <select aria-label="Change page type" value={page.type ?? ""} onChange={async (e) => {
        setError(null);
        try { await console_.setPageType(page.id, e.target.value); reload(); } catch (err) { setError((err as Error).message); }
      }}>
        <option value="" disabled>Set the type…</option>
        {types.map((t) => <option key={t.code} value={t.code}>{t.name}</option>)}
      </select>
      {error && <span className="error-text">{error}</span>}
    </div>
  );
}

function AddField({ page, reload }: { page: Page; reload: () => void }) {
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  return (
    <details className="add-field">
      <summary className="small">Add a value the machine missed</summary>
      <form className="inline-edit" onSubmit={async (e) => {
        e.preventDefault();
        setError(null);
        try {
          await console_.addField(page.id, key, value);
          setKey("");
          setValue("");
          reload();
        } catch (err) {
          setError((err as Error).message);
        }
      }}>
        <select required value={key} onChange={(e) => setKey(e.target.value)} aria-label="Which value">
          <option value="">Choose…</option>
          {page.addable_fields.map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
        </select>
        <input required value={value} onChange={(e) => setValue(e.target.value)} placeholder="Value" aria-label="Value" />
        <button className="secondary">Add</button>
      </form>
      {error && <p className="error-text small">{error}</p>}
    </details>
  );
}

function ConfidenceTag({ value }: { value: number }) {
  return <span className={`conf ${confClass(value)}`}>{value.toFixed(2)}</span>;
}
