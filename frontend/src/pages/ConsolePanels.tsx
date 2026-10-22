// Verifier actions: decision, issue handling, messages. Each action reloads the applicant.
import { useState, type FormEvent } from "react";
import { console_, type Decision, type Issue, type Message, type SubmissionDetail } from "../api";

const STATUS_LABEL: Record<string, string> = {
  draft: "Draft (not submitted)",
  submitted: "Submitted",
  needs_review: "Needs review",
  changes_requested: "Changes requested",
  verified: "Verified",
  rejected: "Rejected",
};

function useAction(reload: () => void) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      reload();
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  };
  return { error, busy, run };
}

export function DecisionPanel({ data, reload }: { data: SubmissionDetail; reload: () => void }) {
  const [mode, setMode] = useState<Decision | null>(null);
  const [note, setNote] = useState("");
  const { error, busy, run } = useAction(reload);
  const blocking = data.issues.filter((i) => i.status === "open" && i.severity !== "info").length;
  const decidable = data.status === "submitted" || data.status === "needs_review";
  const decided = data.status === "verified" || data.status === "rejected";

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!mode) return;
    if (await run(() => console_.decide(data.id, mode, data.status, note || undefined))) {
      setMode(null);
      setNote("");
    }
  };

  return (
    <section className="decision">
      <div className="row-between">
        <span className={`pill s-${data.status}`}>{STATUS_LABEL[data.status] ?? data.status}</span>
        {data.decision.by && (
          <span className="muted small">by {data.decision.by}</span>
        )}
      </div>
      {data.decision.note && <p className="small quote">“{data.decision.note}”</p>}
      {decidable && !mode && (
        <div className="decision-buttons">
          <button className="approve" disabled={busy || blocking > 0} onClick={() => run(() => console_.decide(data.id, "approve", data.status))}
            title={blocking ? "Resolve or dismiss the open errors and warnings first" : "Mark as verified"}>
            ✓ Approve
          </button>
          <button className="secondary" onClick={() => setMode("request_changes")}>Request changes</button>
          <button className="secondary danger" onClick={() => setMode("reject")}>Reject</button>
        </div>
      )}
      {decidable && blocking > 0 && !mode && (
        <p className="muted small">{blocking} open error/warning issue(s) must be resolved or dismissed before approving.</p>
      )}
      {decided && !mode && (
        <button className="link-button small" onClick={() => setMode("reopen")}>Reopen this decision</button>
      )}
      {data.status === "draft" && <p className="muted small">The student has not submitted yet.</p>}
      {mode && (
        <form onSubmit={submit} className="decision-form">
          <label className="small">
            {mode === "request_changes" ? "What should the student change? (sent to them)" :
              mode === "reject" ? "Reason for rejecting (sent to the student)" : "Why reopen?"}
            <textarea required rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          <div className="decision-buttons">
            <button className="primary" disabled={busy}>
              {mode === "request_changes" ? "Send request" : mode === "reject" ? "Reject application" : "Reopen"}
            </button>
            <button type="button" className="link-button" onClick={() => { setMode(null); setNote(""); }}>Cancel</button>
          </div>
        </form>
      )}
      {error && <p className="error-text small" role="alert">{error}</p>}
    </section>
  );
}

export function IssueActions({ issue, reload }: { issue: Issue; reload: () => void }) {
  const [dismissing, setDismissing] = useState(false);
  const [note, setNote] = useState("");
  const { error, busy, run } = useAction(reload);
  if (issue.status !== "open") {
    return (
      <div className="issue-actions">
        {issue.resolution_note && <p className="small quote">“{issue.resolution_note}”</p>}
        <button className="link-button small" disabled={busy} onClick={() => run(() => console_.issueStatus(issue.id, "open"))}>Reopen</button>
        {error && <p className="error-text small">{error}</p>}
      </div>
    );
  }
  return (
    <div className="issue-actions">
      {!dismissing ? (
        <>
          <button className="link-button small" disabled={busy} onClick={() => run(() => console_.issueStatus(issue.id, "resolved"))}>Mark resolved</button>
          <button className="link-button small" onClick={() => setDismissing(true)}>Dismiss…</button>
        </>
      ) : (
        <form className="decision-form" onSubmit={async (e) => {
          e.preventDefault();
          if (await run(() => console_.issueStatus(issue.id, "dismissed", note))) setDismissing(false);
        }}>
          <textarea required rows={2} placeholder="Why does this not apply? (kept on record)" value={note}
            onChange={(e) => setNote(e.target.value)} />
          <div className="decision-buttons">
            <button className="secondary" disabled={busy}>Dismiss</button>
            <button type="button" className="link-button small" onClick={() => setDismissing(false)}>Cancel</button>
          </div>
        </form>
      )}
      {error && <p className="error-text small">{error}</p>}
    </div>
  );
}

export function MessageThread({ messages, send, studentView = false }: {
  messages: Message[];
  send: (body: string) => Promise<unknown>;
  studentView?: boolean;
}) {
  const [body, setBody] = useState("");
  const { error, busy, run } = useAction(() => setBody(""));
  return (
    <div className="messages">
      {messages.length === 0 && <p className="muted small">No messages yet.</p>}
      {messages.map((m) => (
        <div key={m.id} className={`message ${m.from_staff ? "staff" : "student"}`}>
          <div className="muted small">
            {m.from_staff ? (studentView ? "Verification team" : m.author ?? "Staff") : (studentView ? "You" : "Student")}
            {" · "}{new Date(m.at).toLocaleString()}
          </div>
          <div className="small pre">{m.body}</div>
        </div>
      ))}
      <form className="decision-form" onSubmit={(e) => { e.preventDefault(); run(() => send(body)); }}>
        <textarea rows={2} required placeholder={studentView ? "Reply to the verification team" : "Message the student (also emailed)"}
          value={body} onChange={(e) => setBody(e.target.value)} />
        <button className="secondary" disabled={busy}>Send</button>
      </form>
      {error && <p className="error-text small">{error}</p>}
    </div>
  );
}
