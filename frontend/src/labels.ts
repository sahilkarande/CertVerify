// Human-readable labels for codes coming from the backend.

export const SLOT_LABELS: Record<string, string> = {
  photo: "Photo",
  signature: "Signature",
  ssc: "10th marksheet",
  hsc_or_diploma: "12th / Diploma",
  semester_marksheets: "Semester marksheets",
  provisional_or_latest_sem: "Provisional certificate",
  degree_or_fallback: "Degree certificate",
};

export const DEGREE_STATUS: Record<string, string> = {
  has_degree: "Has degree certificate",
  has_provisional: "Has provisional certificate",
  awaiting_provisional: "Awaiting provisional",
  awaiting_final_semester: "Awaiting final semester",
};

export const FIELD_LABELS: Record<string, string> = {
  candidate_name: "Name",
  mother_name: "Mother's name",
  seat_number: "Seat number",
  student_id: "PRN / enrolment",
  semester: "Semester",
  sgpa: "SGPA",
  cgpa: "CGPA",
  percentage: "Percentage",
  total_marks_obtained: "Total marks",
  total_marks_max: "Out of",
  result: "Result",
};

export const ISSUE_TITLES: Record<string, string> = {
  missing_document: "Document missing",
  expected_document_substituted: "Different document uploaded",
  unconfirmed_document: "Document not recognised",
  wrong_slot: "Uploaded in another slot",
  semester_marksheets_missing: "Semester marksheets missing",
  duplicate_upload: "Same file uploaded twice",
  file_shared_with_other_applicant: "File also used by another applicant",
  identifier_shared_with_other_applicant: "ID also used by another applicant",
  name_mismatch: "Name does not match",
  name_partial_match: "Name partly matches",
  marks_inconsistent: "Marks do not add up",
  claimed_percentage_mismatch: "Claimed percentage differs",
  claimed_percentage_unverified: "Claimed percentage not checked",
  document_unreadable: "File unreadable",
  page_quality_poor: "Poor page quality",
  page_text_unreadable: "Text could not be read",
  page_needs_confirmation: "Waiting for the student to confirm values",
  student_confirmed_values: "Values typed by the student",
};

export const VERDICT_LABELS = { clear: "Clear", review: "Needs review", problems: "Problems" };

export const label = (map: Record<string, string>, key: string | null | undefined) =>
  key ? (map[key] ?? key.replace(/_/g, " ")) : "—";

/** "has_provisional" -> "Has provisional", for detail keys without a dedicated label. */
export const humanize = (key: string) =>
  key.charAt(0).toUpperCase() + key.slice(1).replace(/_/g, " ");

export const HIGH_CONFIDENCE = 0.9;
