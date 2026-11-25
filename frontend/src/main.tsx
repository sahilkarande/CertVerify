import { StrictMode, useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Route, Routes } from "react-router-dom";
import { SIGNED_OUT_EVENT, auth, console_, type Me } from "./api";
import ChangePassword from "./pages/ChangePassword";
import Settings from "./pages/Settings";
import Portal from "./pages/student/Portal";
import SignIn from "./pages/SignIn";
import SubmissionList from "./pages/SubmissionList";
import SubmissionView from "./pages/SubmissionView";
import { TestRunView, TestingLab } from "./pages/TestingLab";
import "./styles.css";

const ROLE_LABELS: Record<string, string> = {
  verifier: "Verifier",
  institution_admin: "Institution admin",
  platform_admin: "Platform admin",
};

function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined); // undefined = checking

  const refresh = useCallback(() => {
    auth.me().then(setMe, () => setMe(null));
  }, []);

  useEffect(() => {
    refresh();
    const onSignedOut = () => setMe(null);
    window.addEventListener(SIGNED_OUT_EVENT, onSignedOut);
    return () => window.removeEventListener(SIGNED_OUT_EVENT, onSignedOut);
  }, [refresh]);

  if (me === undefined) return <p className="page-pad muted">Loading…</p>;
  const signedIn = me !== null && (!me.mfa_required || me.mfa_passed);
  if (!signedIn) return <SignIn me={me} onDone={setMe} />;

  const signOut = () => auth.logout().finally(() => setMe(null));
  if (me.role === "student") {
    return (
      <BrowserRouter>
        <header className="topbar">
          <span className="brand">CertVerify</span>
          <div className="user-menu">
            <span className="small muted wrap">{me.email}</span>
            <button className="link-button small" onClick={signOut}>Sign out</button>
          </div>
        </header>
        <Routes>
          <Route path="*" element={<Portal />} />
        </Routes>
      </BrowserRouter>
    );
  }
  return (
    <BrowserRouter>
      <header className="topbar">
        <Link to="/" className="brand">
          CertVerify <span>Review</span>
        </Link>
        <div className="user-menu">
          <span className="small">
            {me.name} · <span className="muted">{ROLE_LABELS[me.role] ?? me.role}{me.institution ? `, ${me.institution}` : ""}</span>
          </span>
          {me.role === "platform_admin" && <Link to="/testing" className="small">Testing Lab</Link>}
          {(me.role === "platform_admin" || me.role === "institution_admin") && (
            <Link to="/settings" className="small">Settings</Link>
          )}
          <Link to="/account/password" className="small">Change password</Link>
          <button className="link-button small" onClick={signOut}>Sign out</button>
        </div>
      </header>
      <ProcessingBanner />
      <Routes>
        <Route path="/" element={<SubmissionList />} />
        <Route path="/submissions/:id" element={<SubmissionView me={me} />} />
        <Route path="/account/password" element={<ChangePassword />} />
        <Route path="/settings" element={<Settings me={me} />} />
        {me.role === "platform_admin" && <Route path="/testing" element={<TestingLab />} />}
        {me.role === "platform_admin" && <Route path="/testing/:id" element={<TestRunView />} />}
        <Route path="*" element={<p className="page-pad">Page not found.</p>} />
      </Routes>
    </BrowserRouter>
  );
}

const HEALTH_POLL_MS = 30_000;

/** Uploads wait forever when no background worker is running: tell staff, with the fix. */
function ProcessingBanner() {
  const [stalled, setStalled] = useState<{ waiting: number; minutes: number } | null>(null);
  useEffect(() => {
    const check = () =>
      console_.processing().then(
        (h) => setStalled(h.stalled ? { waiting: h.waiting, minutes: Math.round(h.oldest_waiting_s / 60) } : null),
        () => undefined, // a failed check is not news; try again next time
      );
    check();
    const t = window.setInterval(check, HEALTH_POLL_MS);
    return () => window.clearInterval(t);
  }, []);
  if (!stalled) return null;
  return (
    <div className="stall-banner" role="alert">
      <strong>Processing is paused:</strong> {stalled.waiting} upload{stalled.waiting === 1 ? "" : "s"} waiting
      {stalled.minutes > 0 && ` (oldest ${stalled.minutes} min)`} and no background worker is running.
      Start it on the server with <code>make run</code> (or <code>make worker</code>).
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
