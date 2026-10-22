// Types and fetchers for the read-only review API (backend/app/api/routes/review.py).

export type Severity = "error" | "warning" | "info";
export type Verdict = "clear" | "review" | "problems";

export interface SubmissionRow {
  id: string;
  name: string;
  form_number: string | null;
  status: string;
  degree_status: string | null;
  claimed_percent: number | null;
  submitted_at: string | null;
  documents: number;
  issues: Record<Severity, number>;
  verdict: Verdict;
}

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Field {
  key: string;
  value: string | null;
  machine_value: string | null;
  confidence: number | null;
  source: "machine" | "human";
  box: Box | null;
}

export interface Page {
  id: string;
  number: number;
  type: string | null;
  type_name: string | null;
  classification_confidence: number | null;
  classification_method: string | null;
  semesters: number[];
  level: string | null;
  quality: string | null;
  quality_reasons: string[];
  ocr: { engine: string; status: string; confidence: number | null; rotation: number } | null;
  has_image: boolean;
  fields: Field[];
}

export interface DocumentInfo {
  id: string;
  slot: string | null;
  status: string;
  type: string | null;
  page_count: number;
  processing_error: string | null;
  pages: Page[];
}

export interface Issue {
  id: string;
  code: string;
  severity: Severity;
  status: "open" | "resolved" | "dismissed";
  message: string;
  document_id: string | null;
  details: Record<string, unknown>;
}

export interface SubmissionDetail {
  id: string;
  applicant: { name: string; email: string | null; phone: string | null; form_number: string | null };
  status: string;
  submitted_at: string | null;
  attributes: Record<string, unknown>;
  verdict: Verdict;
  issues: Issue[];
  documents: DocumentInfo[];
}

const API = "/api/v1";
const BASE = `${API}/review`;

/** An API error; ``status`` 401 means "sign in again". */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

// Fired on any 401 so the app can return to the sign-in screen (session expired/revoked).
export const SIGNED_OUT_EVENT = "certverify:signed-out";

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers["X-Requested-With"] = "CertVerify"; // CSRF guard (backend)
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    if (res.status === 401 && !url.startsWith(`${API}/auth/`)) {
      window.dispatchEvent(new Event(SIGNED_OUT_EVENT));
    }
    throw new ApiError(res.status, detail);
  }
  return (res.status === 204 ? undefined : await res.json()) as T;
}

const get = <T>(path: string) => request<T>("GET", BASE + path);

export const fetchSubmissions = () => get<SubmissionRow[]>("/submissions");
export const fetchSubmission = (id: string) => get<SubmissionDetail>(`/submissions/${id}`);
export const pageImageUrl = (pageId: string) => `${BASE}/pages/${pageId}/image`;

// --- sign-in ---------------------------------------------------------------------

export interface Me {
  id: string;
  email: string;
  name: string;
  role: string;
  institution: string | null;
  mfa_passed: boolean;
  mfa_enrolled: boolean;
  mfa_required: boolean;
}

export interface TotpSetup {
  secret: string;
  otpauth_uri: string;
  qr_svg: string;
}

export const auth = {
  me: () => request<Me>("GET", `${API}/auth/me`),
  login: (email: string, password: string) =>
    request<Me>("POST", `${API}/auth/login`, { email, password }),
  setupTotp: () => request<TotpSetup>("POST", `${API}/auth/totp/setup`),
  confirmTotp: (code: string) => request<Me>("POST", `${API}/auth/totp/confirm`, { code }),
  verifyTotp: (code: string) => request<Me>("POST", `${API}/auth/totp/verify`, { code }),
  logout: () => request<void>("POST", `${API}/auth/logout`),
  changePassword: (current_password: string, new_password: string) =>
    request<{ ok: boolean }>("POST", `${API}/auth/password`, { current_password, new_password }),
};
