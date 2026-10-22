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
  id: string;
  key: string;
  value: string | null;
  machine_value: string | null;
  confidence: number | null;
  source: "machine" | "human" | "student";
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
  addable_fields: { key: string; label: string }[];
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
  resolution_note: string | null;
  resolved_at: string | null;
  details: Record<string, unknown>;
}

export interface Message {
  id: string;
  from_staff: boolean;
  body: string;
  author: string | null;
  at: string;
}

export interface SubmissionDetail {
  id: string;
  applicant: { name: string; email: string | null; phone: string | null; form_number: string | null };
  status: string;
  submitted_at: string | null;
  attributes: Record<string, unknown>;
  verdict: Verdict;
  decision: { by: string | null; at: string | null; note: string | null };
  messages: Message[];
  document_types: { code: string; name: string }[];
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
  const isForm = body instanceof FormData;
  if (body !== undefined && !isForm) headers["Content-Type"] = "application/json";
  const res = await fetch(url, {
    method,
    headers,
    body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
    credentials: "same-origin",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    if (res.status === 401 && !url.startsWith(`${API}/auth/`) && !url.startsWith(`${API}/student/auth/`)) {
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

export type Decision = "approve" | "reject" | "request_changes" | "reopen";

export const console_ = {
  correctField: (fieldId: string, value: string, expected_value: string | null) =>
    request<{ value: string }>("PATCH", `${BASE}/fields/${fieldId}`, { value, expected_value }),
  addField: (pageId: string, key: string, value: string) =>
    request<{ id: string }>("POST", `${BASE}/pages/${pageId}/fields`, { key, value }),
  issueStatus: (issueId: string, status: "open" | "resolved" | "dismissed", note?: string) =>
    request<{ status: string }>("POST", `${BASE}/issues/${issueId}/status`, { status, note }),
  decide: (submissionId: string, decision: Decision, expected_status: string, note?: string) =>
    request<{ status: string }>("POST", `${BASE}/submissions/${submissionId}/decision`,
      { decision, note, expected_status }),
  message: (submissionId: string, body: string) =>
    request<{ messages: Message[] }>("POST", `${BASE}/submissions/${submissionId}/messages`, { body }),
  setPageType: (pageId: string, type: string) =>
    request<{ type: string }>("POST", `${BASE}/pages/${pageId}/type`, { type }),
};

// --- administration ----------------------------------------------------------------

export interface FieldSpec {
  key: string;
  label?: string | null;
  type?: string | null;
  required?: boolean;
}

export interface DocType {
  id: string;
  code: string;
  name: string;
  category: string;
  builtin: boolean;
  active: boolean;
  fields: FieldSpec[];
  title_phrases: string[];
  pages: number;
}

export interface RequirementSetInfo {
  id: string;
  code: string;
  name: string;
  active: boolean;
  items: { id: string; key: string; label: string; required: boolean; accepted_types: string[];
           condition: Record<string, unknown> | null }[];
}

const ADMIN = `${API}/admin`;
export const adminApi = {
  types: () => request<{ types: DocType[]; extractable: Record<string, string> }>("GET", `${ADMIN}/document-types`),
  createType: (body: { code: string; name: string; category: string; fields: FieldSpec[]; title_phrases: string[] }) =>
    request<{ id: string }>("POST", `${ADMIN}/document-types`, body),
  updateType: (id: string, body: Partial<{ name: string; title_phrases: string[]; fields: FieldSpec[]; active: boolean }>) =>
    request<{ id: string }>("PATCH", `${ADMIN}/document-types/${id}`, body),
  sets: () => request<{ requirement_sets: RequirementSetInfo[] }>("GET", `${ADMIN}/requirement-sets`),
  updateItem: (id: string, body: Partial<{ label: string; required: boolean; accepted_types: string[] }>) =>
    request<{ id: string }>("PATCH", `${ADMIN}/requirement-items/${id}`, body),
  addItem: (setId: string, body: { key: string; label: string; accepted_types: string[]; required: boolean }) =>
    request<{ id: string }>("POST", `${ADMIN}/requirement-sets/${setId}/items`, body),
};

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


// --- uploads & background processing ---------------------------------------------

export type JobStage =
  | "waiting" | "quality" | "reading" | "classifying" | "extracting" | "validating" | "done";

export interface DocumentStatus {
  document_id: string;
  document_status: string;
  processing_error: string | null;
  job: { status: string; stage: JobStage; attempts: number; error: string | null; finished: boolean } | null;
  pages: { number: number; quality: string | null; reasons: string[] }[];
}

export const uploadDocument = (submissionId: string, slot: string, file: File) => {
  const form = new FormData();
  form.append("slot", slot);
  form.append("file", file);
  return request<{ document_id: string; job_id: string }>(
    "POST", `${BASE}/submissions/${submissionId}/documents`, form,
  );
};

export const fetchDocumentStatus = (documentId: string) =>
  get<DocumentStatus>(`/documents/${documentId}/status`);

// --- student portal --------------------------------------------------------------

const SAPI = `${API}/student`;

export interface Intake {
  id: string;
  name: string;
  institution: string;
}

export interface ConfirmField {
  key: string;
  label: string;
  kind: "name" | "id" | "number";
}

export interface StudentPage {
  id: string;
  number: number;
  quality: string | null;
  problems: string[];
  has_image: boolean;
  text_unreadable: boolean; // the checklist item's problems say what to do
  // Values we could not read clearly: the student types them as printed (null = not printed).
  confirm: { fields: ConfirmField[]; mismatched: string[]; attempts_left: number } | null;
}

export interface StudentDocument {
  id: string;
  uploaded_at: string | null;
  processing: boolean;
  stage: string | null;
  failed: boolean;
  error_message: string | null;
  recognised_as: string[];
  pages: StudentPage[];
}

export interface ChecklistItem {
  key: string;
  label: string;
  required: boolean;
  expected: string[];
  state: "missing" | "processing" | "attention" | "ok";
  problems: string[];
  documents: StudentDocument[];
}

export interface Answers {
  full_name: string;
  phone: string | null;
  degree_status: string;
  claimed_aggregate_percent: number;
}

export interface Application {
  id: string;
  intake: { id: string; name: string; institution: string | null };
  status: string;
  editable: boolean;
  answers: Answers;
  checklist: ChecklistItem[];
  general_problems: string[];
  ready_to_submit: boolean;
  decision_note: string | null;
  messages: Message[];
}

export const studentApi = {
  requestCode: (email: string) =>
    request<{ message: string }>("POST", `${SAPI}/auth/request-code`, { email }),
  verifyCode: (email: string, code: string) =>
    request<Me>("POST", `${SAPI}/auth/verify-code`, { email, code }),
  intakes: () =>
    request<{ intakes: Intake[]; degree_statuses: Record<string, string> }>("GET", `${SAPI}/intakes`),
  application: (intakeId: string) => request<Application>("GET", `${SAPI}/applications/${intakeId}`),
  saveAnswers: (intakeId: string, a: Answers) =>
    request<Application>("PUT", `${SAPI}/applications/${intakeId}`, a),
  upload: (intakeId: string, slot: string, file: File) => {
    const form = new FormData();
    form.append("slot", slot);
    form.append("file", file);
    return request<{ document_id: string }>("POST", `${SAPI}/applications/${intakeId}/documents`, form);
  },
  remove: (documentId: string) => request<void>("DELETE", `${SAPI}/documents/${documentId}`),
  submit: (intakeId: string) => request<Application>("POST", `${SAPI}/applications/${intakeId}/submit`),
  pageImageUrl: (pageId: string) => `${SAPI}/pages/${pageId}/image`,
  confirmPage: (pageId: string, values: Record<string, string | null>) =>
    request<Application>("POST", `${SAPI}/pages/${pageId}/confirm`, { values }),
  message: (intakeId: string, body: string) =>
    request<{ messages: Message[] }>("POST", `${SAPI}/applications/${intakeId}/messages`, { body }),
};
