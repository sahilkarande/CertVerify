import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  fetchSubmission,
  pageImageUrl,
  type DocumentInfo,
  type Field,
  type Issue,
  type Page,
  type SubmissionDetail,
} from "../api";
import UploadPanel from "./UploadPanel";
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

export default function SubmissionView() {
  const { id } = useParams();
  const [data, setData] = useState<SubmissionDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pageId, setPageId] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [showClosed, setShowClosed] = useState(false);

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
  const selected: Selected | undefined = allPages.find((x) => x.page.id === pageId);

  const step = useCallback(
    (delta: number) => {
      const i = allPages.findIndex((x) => x.page.id === pageId);
      const next = allPages[i + delta];
      if (next) setPageId(next.page.id);
    },
    [allPages, pageId],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return;
      if (e.key === "ArrowRight" || e.key === "j") step(1);
      if (e.key === "ArrowLeft" || e.key === "k") step(-1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step]);

  if (error) return <p className="page-pad error-text">Could not load this applicant ({error}).</p>;
  if (!data) return <p className="page-pad muted">Loading…</p>;

  const openIssues = data.issues.filter((i) => i.status === "open");
  const closedIssues = data.issues.filter((i) => i.status !== "open");
  const issuesByDoc = new Map<string, Issue[]>();
  openIssues.forEach((i) => {
    if (i.document_id && i.severity !== "info")
      issuesByDoc.set(i.document_id, [...(issuesByDoc.get(i.document_id) ?? []), i]);
  });

  const goToIssue = (issue: Issue) => {
    const doc = data.documents.find((d) => d.id === issue.document_id);
    if (!doc) return;
    const wanted = typeof issue.details.page === "number" ? issue.details.page : null;
    const page = doc.pages.find((p) => p.number === wanted) ?? doc.pages[0];
    if (page) setPageId(page.id);
  };

  const attrs = data.attributes;
  return (
    <main className="review-layout">
      <aside className="sidebar">
        <Link to="/" className="back">
          ← All applicants
        </Link>
        <h1 className="applicant-name">{data.applicant.name}</h1>
        <span className={`verdict ${data.verdict}`}>{VERDICT_LABELS[data.verdict]}</span>
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

        <h2>
          Issues <span className="count">{openIssues.length}</span>
        </h2>
        {openIssues.length === 0 && <p className="muted small">No open issues.</p>}
        <ul className="issues">
          {openIssues.map((i) => (
            <IssueItem key={i.id} issue={i} onShow={i.document_id ? () => goToIssue(i) : undefined} />
          ))}
        </ul>
        {closedIssues.length > 0 && (
          <button className="link-button" onClick={() => setShowClosed(!showClosed)}>
            {showClosed ? "Hide" : "Show"} {closedIssues.length} resolved/dismissed
          </button>
        )}
        {showClosed && (
          <ul className="issues closed">
            {closedIssues.map((i) => (
              <IssueItem key={i.id} issue={i} />
            ))}
          </ul>
        )}

        <h2>Documents</h2>
        <UploadPanel submissionId={data.id} onProcessed={load} />
        <ul className="docs">
          {data.documents.map((doc) => (
            <li key={doc.id}>
              <div className="doc-head">
                <span>{label(SLOT_LABELS, doc.slot)}</span>
                {issuesByDoc.has(doc.id) && (
                  <span
                    className={`flag ${issuesByDoc.get(doc.id)!.some((i) => i.severity === "error") ? "error" : "warning"}`}
                    title={issuesByDoc.get(doc.id)!.map((i) => i.message).join("\n")}
                  >
                    !
                  </span>
                )}
              </div>
              {doc.processing_error && <p className="error-text small">{doc.processing_error}</p>}
              <div className="page-chips">
                {doc.pages.map((p) => (
                  <button
                    key={p.id}
                    className={`chip ${p.id === pageId ? "active" : ""} ${p.type ? "" : "unknown"} q-${p.quality}`}
                    onClick={() => setPageId(p.id)}
                    title={`${p.type_name ?? "Not recognised"}${p.semesters.length ? ` · sem ${p.semesters.join(", ")}` : ""}${p.quality && p.quality !== "ok" ? ` · quality: ${p.quality}` : ""}`}
                  >
                    {p.number}
                  </button>
                ))}
              </div>
            </li>
          ))}
        </ul>
      </aside>

      {selected ? (
        <>
          <PageViewer page={selected.page} hovered={hovered} />
          <PagePanel
            sel={selected}
            hovered={hovered}
            onHover={setHovered}
            position={`${allPages.findIndex((x) => x.page.id === pageId) + 1} / ${allPages.length}`}
            onStep={step}
          />
        </>
      ) : (
        <p className="page-pad muted">This applicant has no pages to show.</p>
      )}
    </main>
  );
}

function IssueItem({ issue, onShow }: { issue: Issue; onShow?: () => void }) {
  const details = Object.entries(issue.details).filter(([k]) => k !== "item");
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
      {onShow && (
        <button className="link-button" onClick={onShow}>
          Show document →
        </button>
      )}
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

  if (!page.has_image) return <section className="viewer muted">No image for this page.</section>;
  return (
    <section className="viewer">
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
  hovered: string | null;
  onHover: (k: string | null) => void;
  position: string;
  onStep: (d: number) => void;
}) {
  const { doc, page } = props.sel;
  const fieldOrder = Object.keys(FIELD_LABELS);
  const fields = [...page.fields].sort(
    (a, b) => fieldOrder.indexOf(a.key) - fieldOrder.indexOf(b.key),
  );
  return (
    <aside className="panel">
      <div className="pager">
        <button onClick={() => props.onStep(-1)} aria-label="Previous page">
          ‹
        </button>
        <span className="muted small">
          Page {props.position} <kbd>←</kbd>
          <kbd>→</kbd>
        </span>
        <button onClick={() => props.onStep(1)} aria-label="Next page">
          ›
        </button>
      </div>
      <h2 className="page-title">{page.type_name ?? "Not recognised"}</h2>
      <p className="muted small">
        {label(SLOT_LABELS, doc.slot)} · page {page.number} of {doc.page_count}
      </p>
      <dl className="facts">
        <dt>Recognised by</dt>
        <dd>
          {page.classification_method ? humanize(page.classification_method) : "—"}
          {page.classification_confidence != null && (
            <ConfidenceTag value={page.classification_confidence} />
          )}
        </dd>
        {page.semesters.length > 0 && (
          <>
            <dt>Semester</dt>
            <dd>{page.semesters.join(", ")}</dd>
          </>
        )}
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

      <h2>Extracted values</h2>
      {fields.length === 0 ? (
        <p className="muted small">No values extracted from this page.</p>
      ) : (
        <table className="fields">
          <tbody>
            {fields.map((f) => (
              <FieldRow key={f.key} f={f} hovered={props.hovered} onHover={props.onHover} />
            ))}
          </tbody>
        </table>
      )}
      <p className="muted small legend">
        <span className="swatch high" /> confident (≥ 0.9) <span className="swatch mid" /> check{" "}
        <span className="swatch low" /> unsure
      </p>
    </aside>
  );
}

function FieldRow({ f, hovered, onHover }: { f: Field; hovered: string | null; onHover: (k: string | null) => void }) {
  return (
    <tr
      className={hovered === f.key ? "hot" : ""}
      onMouseEnter={() => onHover(f.key)}
      onMouseLeave={() => onHover(null)}
    >
      <th>{label(FIELD_LABELS, f.key)}</th>
      <td>
        <span className="value">{f.value ?? "—"}</span>
        {f.source === "human" && f.machine_value !== f.value && (
          <span className="muted small"> (read as {f.machine_value})</span>
        )}
      </td>
      <td className="num">{f.confidence != null && <ConfidenceTag value={f.confidence} />}</td>
    </tr>
  );
}

function ConfidenceTag({ value }: { value: number }) {
  return <span className={`conf ${confClass(value)}`}>{value.toFixed(2)}</span>;
}
