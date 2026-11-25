import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { auth } from "../api";

export default function ChangePassword() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (next !== repeat) return setMessage({ ok: false, text: "The new passwords differ." });
    try {
      await auth.changePassword(current, next);
      setCurrent("");
      setNext("");
      setRepeat("");
      setMessage({ ok: true, text: "Password changed. Other browsers have been signed out." });
    } catch (err) {
      setMessage({ ok: false, text: (err as Error).message });
    }
  };

  return (
    <main className="page-pad narrow">
      <Link to="/" className="back">← All applicants</Link>
      <h1>Change password</h1>
      <form onSubmit={submit} className="auth-form">
        <label>
          Current password
          <input type="password" autoComplete="current-password" required value={current}
            onChange={(e) => setCurrent(e.target.value)} />
        </label>
        <label>
          New password <span className="muted small">(12 or more characters)</span>
          <input type="password" autoComplete="new-password" required minLength={12} value={next}
            onChange={(e) => setNext(e.target.value)} />
        </label>
        <label>
          Repeat new password
          <input type="password" autoComplete="new-password" required value={repeat}
            onChange={(e) => setRepeat(e.target.value)} />
        </label>
        {message && <p className={`small ${message.ok ? "ok-text" : "error-text"}`} role="status">{message.text}</p>}
        <button className="primary">Change password</button>
      </form>
    </main>
  );
}
