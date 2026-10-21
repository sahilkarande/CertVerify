import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Route, Routes } from "react-router-dom";
import SubmissionList from "./pages/SubmissionList";
import SubmissionView from "./pages/SubmissionView";
import "./styles.css";

function App() {
  return (
    <BrowserRouter>
      <header className="topbar">
        <Link to="/" className="brand">
          CertVerify <span>Review</span>
        </Link>
        <span className="topbar-note">Local only · read-only</span>
      </header>
      <Routes>
        <Route path="/" element={<SubmissionList />} />
        <Route path="/submissions/:id" element={<SubmissionView />} />
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
