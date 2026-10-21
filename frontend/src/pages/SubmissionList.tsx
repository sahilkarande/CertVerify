import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { fetchSubmissions, type SubmissionRow, type Verdict } from "../api";
import { DEGREE_STATUS, VERDICT_LABELS, label } from "../labels";

const FILTERS: (Verdict | "all")[] = ["all", "problems", "review", "clear"];
const VERDICT_ORDER: Record<Verdict, number> = { problems: 0, review: 1, clear: 2 };

export default function SubmissionList() {
  const [rows, setRows] = useState<SubmissionRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [params, setParams] = useSearchParams();
  const filter = (params.get("verdict") as Verdict | "all") ?? "all";
  const query = params.get("q") ?? "";
  const navigate = useNavigate();

  useEffect(() => {
    fetchSubmissions().then(setRows, (e: Error) => setError(e.message));
  }, []);

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: rows?.length ?? 0, problems: 0, review: 0, clear: 0 };
    rows?.forEach((r) => (c[r.verdict] += 1));
    return c;
  }, [rows]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (rows ?? [])
      .filter((r) => filter === "all" || r.verdict === filter)
      .filter((r) => !q || r.name.toLowerCase().includes(q) || (r.form_number ?? "").includes(q))
      .sort(
        (a, b) =>
          VERDICT_ORDER[a.verdict] - VERDICT_ORDER[b.verdict] ||
          b.issues.error - a.issues.error ||
          b.issues.warning - a.issues.warning ||
          a.name.localeCompare(b.name),
      );
  }, [rows, filter, query]);

  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value && value !== "all") next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  if (error) return <p className="page-pad error-text">Could not load submissions ({error}).</p>;
  if (!rows) return <p className="page-pad muted">Loading…</p>;

  return (
    <main className="page-pad">
      <div className="list-head">
        <h1>Applicants</h1>
        <input
          className="search"
          type="search"
          placeholder="Search name or form number"
          value={query}
          onChange={(e) => setParam("q", e.target.value)}
          aria-label="Search applicants"
        />
      </div>
      <div className="filters" role="tablist">
        {FILTERS.map((f) => (
          <button
            key={f}
            role="tab"
            aria-selected={filter === f}
            className={`filter ${filter === f ? "active" : ""} ${f}`}
            onClick={() => setParam("verdict", f)}
          >
            {f === "all" ? "All" : VERDICT_LABELS[f]} <span className="count">{counts[f]}</span>
          </button>
        ))}
      </div>
      <table className="list">
        <thead>
          <tr>
            <th>Applicant</th>
            <th>Form no.</th>
            <th>Degree status</th>
            <th className="num">Claimed %</th>
            <th className="num">Files</th>
            <th>Open issues</th>
            <th>Verdict</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((r) => (
            <tr
              key={r.id}
              tabIndex={0}
              onClick={() => navigate(`/submissions/${r.id}`)}
              onKeyDown={(e) => e.key === "Enter" && navigate(`/submissions/${r.id}`)}
            >
              <td className="strong">{r.name}</td>
              <td className="mono">{r.form_number ?? "—"}</td>
              <td>{label(DEGREE_STATUS, r.degree_status)}</td>
              <td className="num">{r.claimed_percent?.toFixed(2) ?? "—"}</td>
              <td className="num">{r.documents}</td>
              <td>
                <IssueCounts issues={r.issues} />
              </td>
              <td>
                <span className={`verdict ${r.verdict}`}>{VERDICT_LABELS[r.verdict]}</span>
              </td>
            </tr>
          ))}
          {shown.length === 0 && (
            <tr>
              <td colSpan={7} className="muted center">
                No applicants match.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </main>
  );
}

function IssueCounts({ issues }: { issues: SubmissionRow["issues"] }) {
  const parts = (["error", "warning", "info"] as const).filter((s) => issues[s] > 0);
  if (parts.length === 0) return <span className="muted">none</span>;
  return (
    <span className="issue-counts">
      {parts.map((s) => (
        <span key={s} className={`dot-count ${s}`} title={`${issues[s]} ${s}`}>
          {issues[s]} {s}
        </span>
      ))}
    </span>
  );
}
