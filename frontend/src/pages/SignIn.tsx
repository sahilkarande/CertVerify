import { useEffect, useRef, useState, type FormEvent } from "react";
import { auth, type Me, type TotpSetup } from "../api";

/** Email + password, then the authenticator step (first-time setup or a code). */
export default function SignIn({ me, onDone }: { me: Me | null; onDone: (me: Me) => void }) {
  if (me && me.mfa_required && !me.mfa_passed) {
    return me.mfa_enrolled ? (
      <CodeStep onDone={onDone} />
    ) : (
      <SetupStep email={me.email} onDone={onDone} />
    );
  }
  return <PasswordStep onDone={onDone} />;
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <main className="auth-page">
      <div className="auth-card">
        <div className="brand auth-brand">
          CertVerify <span>Review</span>
        </div>
        <h1>{title}</h1>
        {children}
      </div>
    </main>
  );
}

function useSubmit(action: () => Promise<Me>, onDone: (me: Me) => void) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onDone(await action());
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return { error, busy, submit };
}

function PasswordStep({ onDone }: { onDone: (me: Me) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const { error, busy, submit } = useSubmit(() => auth.login(email, password), onDone);
  return (
    <Card title="Sign in">
      <form onSubmit={submit} className="auth-form">
        <label>
          Email
          <input type="email" autoComplete="username" required autoFocus value={email}
            onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>
          Password
          <input type="password" autoComplete="current-password" required value={password}
            onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && <p className="error-text small" role="alert">{error}</p>}
        <button className="primary" disabled={busy}>{busy ? "Signing in…" : "Continue"}</button>
      </form>
    </Card>
  );
}

function CodeInput({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <label>
      6-digit code
      <input inputMode="numeric" autoComplete="one-time-code" pattern="[0-9 ]{6,7}" required
        autoFocus maxLength={7} value={value} className="code-input"
        onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

function CodeStep({ onDone }: { onDone: (me: Me) => void }) {
  const [code, setCode] = useState("");
  const { error, busy, submit } = useSubmit(() => auth.verifyTotp(code), onDone);
  return (
    <Card title="Authenticator code">
      <p className="muted">Open your authenticator app and enter the code for CertVerify.</p>
      <form onSubmit={submit} className="auth-form">
        <CodeInput value={code} onChange={setCode} />
        {error && <p className="error-text small" role="alert">{error}</p>}
        <button className="primary" disabled={busy}>Verify</button>
      </form>
      <SignOutLink />
    </Card>
  );
}

function SetupStep({ email, onDone }: { email: string; onDone: (me: Me) => void }) {
  const [setup, setSetup] = useState<TotpSetup | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const { error, busy, submit } = useSubmit(() => auth.confirmTotp(code), onDone);
  const requested = useRef(false);
  useEffect(() => {
    // Each call issues a new secret: never request twice (React may run effects twice in dev).
    if (requested.current) return;
    requested.current = true;
    auth.setupTotp().then(setSetup, (e: Error) => setLoadError(e.message));
  }, []);
  return (
    <Card title="Set up your authenticator">
      <ol className="steps muted">
        <li>Install an authenticator app (Google Authenticator, Microsoft Authenticator, …).</li>
        <li>Scan this code with it, or type the key below.</li>
        <li>Enter the 6-digit code it shows.</li>
      </ol>
      {loadError && <p className="error-text small">{loadError}</p>}
      {setup && (
        <div className="qr">
          <img src={setup.qr_svg} alt={`QR code to add CertVerify (${email}) to an authenticator app`} />
          <code className="secret">{setup.secret.match(/.{1,4}/g)?.join(" ")}</code>
        </div>
      )}
      <form onSubmit={submit} className="auth-form">
        <CodeInput value={code} onChange={setCode} />
        {error && <p className="error-text small" role="alert">{error}</p>}
        <button className="primary" disabled={busy || !setup}>Confirm</button>
      </form>
      <SignOutLink />
    </Card>
  );
}

function SignOutLink() {
  return (
    <button className="link-button" onClick={() => auth.logout().finally(() => window.location.reload())}>
      Use a different account
    </button>
  );
}
