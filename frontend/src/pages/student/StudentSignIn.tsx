import { useState, type FormEvent } from "react";
import { studentApi, type Me } from "../../api";

/** Email -> 6-digit code. No password. */
export default function StudentSignIn({ onDone }: { onDone: (me: Me) => void }) {
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async (e: FormEvent, action: () => Promise<void>) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!sent) {
    return (
      <form className="auth-form" onSubmit={(e) => run(e, async () => {
        setSent((await studentApi.requestCode(email)).message);
      })}>
        <label>
          Your email
          <input type="email" autoComplete="email" required autoFocus value={email}
            onChange={(e) => setEmail(e.target.value)} />
        </label>
        <p className="muted small">We'll email you a 6-digit code. No password needed.</p>
        {error && <p className="error-text small" role="alert">{error}</p>}
        <button className="primary" disabled={busy}>{busy ? "Sending…" : "Email me a code"}</button>
      </form>
    );
  }
  return (
    <form className="auth-form" onSubmit={(e) => run(e, async () => {
      onDone(await studentApi.verifyCode(email, code));
    })}>
      <p className="small">{sent} <span className="muted">({email})</span></p>
      <label>
        6-digit code
        <input inputMode="numeric" autoComplete="one-time-code" required autoFocus maxLength={7}
          className="code-input" value={code} onChange={(e) => setCode(e.target.value)} />
      </label>
      {error && <p className="error-text small" role="alert">{error}</p>}
      <button className="primary" disabled={busy}>Sign in</button>
      <button type="button" className="link-button" onClick={() => { setSent(null); setCode(""); }}>
        Use a different email or send a new code
      </button>
    </form>
  );
}
