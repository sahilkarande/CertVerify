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

const BASE = "/api/v1/review";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(BASE + path, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

export const fetchSubmissions = () => get<SubmissionRow[]>("/submissions");
export const fetchSubmission = (id: string) => get<SubmissionDetail>(`/submissions/${id}`);
export const pageImageUrl = (pageId: string) => `${BASE}/pages/${pageId}/image`;
