import { StrictMode, useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Route, Routes } from "react-router-dom";
import { SIGNED_OUT_EVENT, auth, type Me } from "./api";
import ChangePassword from "./pages/ChangePassword";
import SignIn from "./pages/SignIn";
import SubmissionList from "./pages/SubmissionList";
import SubmissionView from "./pages/SubmissionView";
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
          <Link to="/account/password" className="small">Change password</Link>
          <button className="link-button small" onClick={signOut}>Sign out</button>
        </div>
      </header>
      <Routes>
        <Route path="/" element={<SubmissionList />} />
        <Route path="/submissions/:id" element={<SubmissionView />} />
        <Route path="/account/password" element={<ChangePassword />} />
        <Route path="*" element={<p className="page-pad">Page not found.</p>} />
      </Routes>
    </BrowserRouter>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
