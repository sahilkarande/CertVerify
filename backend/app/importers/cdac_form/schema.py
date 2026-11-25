"""Static description of the C-DAC form: its columns, upload slots and requirement set."""

from dataclasses import dataclass

INSTITUTION_SLUG = "cdac-kharghar"
INSTITUTION_NAME = "C-DAC Kharghar"
REQUIREMENT_SET_CODE = "pg-feb-2026"
REQUIREMENT_SET_NAME = "PG course admission documents (Feb 2026)"

# Columns are located by the start of their header text, so the long question wording can
# change slightly without breaking the import. Each prefix must match exactly one column.
COL_TIMESTAMP = "Timestamp"
COL_FORM_NUMBER = "Form Number"
COL_FORM_NO_DUP = "Form No."
COL_NAME = "Name (First Name"
COL_COURSE = "Course"
COL_MOBILE = "Mobile No"
COL_GUARDIAN_MOBILE = "Emergency Contact No"
COL_EMAIL = "Email ID"
COL_HOSTEL = "Opted for CDAC Kharghar Hostel"
COL_DEGREE_OPTION = "Select Your Degree Options"
COL_AGGREGATE = "Aggregate Percentage of Marks"

TIMESTAMP_FORMAT = "%m/%d/%Y %H:%M:%S"
FORM_TIMEZONE = "Asia/Kolkata"


@dataclass(frozen=True)
class UploadSlot:
    key: str
    header_prefix: str
    label: str
    accepted_type_codes: tuple[str, ...]
    # Which types satisfy the slot can depend on a form answer (see the validation engine).
    condition: dict | None = None


_SEM = "semester_marksheet"
_PROV = "provisional_degree_certificate"
_DEG = "degree_certificate"  # a degree also satisfies "provisional" (it supersedes it)


# Order follows the form. Accepted types reflect the form's own fallback instructions:
# the slot is only a hint, the content decides the actual type (Phase 6).
UPLOAD_SLOTS: tuple[UploadSlot, ...] = (
    UploadSlot(
        "photo",
        "Upload Your Formal Passport Size Photo",
        "Passport-size photo",
        ("passport_photo",),
    ),
    UploadSlot("signature", "Upload Your Sign", "Signature", ("signature",)),
    UploadSlot("ssc", "Upload 10th Marksheet", "10th marksheet", ("ssc_marksheet",)),
    UploadSlot(
        "hsc_or_diploma",
        "Upload 12th or Diploma Marksheet",
        "12th or Diploma marksheet",
        ("hsc_marksheet", "diploma_marksheet"),
    ),
    UploadSlot(
        "semester_marksheets",
        "Upload all semester marksheets",
        "All semester marksheets (one PDF)",
        ("semester_marksheet",),
    ),
    UploadSlot(
        "provisional_or_latest_sem",
        "Upload Provisional Degree certificate",
        "Provisional degree certificate (else 8th/7th sem marksheet)",
        (_PROV, _SEM),
        {
            "attribute": "degree_status",
            "accepted_by_value": {
                "has_degree": [_PROV, _SEM, _DEG],
                "has_provisional": [_PROV, _DEG],
                "awaiting_provisional": [_SEM],
                "awaiting_final_semester": [_SEM],
            },
        },
    ),
    UploadSlot(
        "degree_or_fallback",
        "Upload Degree Certificate",
        "Degree certificate (else provisional, else 8th/7th sem marksheet)",
        ("degree_certificate", _PROV, _SEM),
        {
            "attribute": "degree_status",
            "accepted_by_value": {
                "has_degree": ["degree_certificate"],
                "has_provisional": [_PROV, _DEG],
                "awaiting_provisional": [_SEM],
                "awaiting_final_semester": [_SEM],
            },
        },
    ),
)

# Answer to "Select Your Degree Options" -> stable code stored in submission attributes.
# The validation engine (Phase 8) uses it to decide which documents to expect.
DEGREE_STATUS = {
    "Already having Degree Certificate": "has_degree",
    "Having Provisional Degree Certificate": "has_provisional",
    "Awaiting Provisional Degree Certificate": "awaiting_provisional",
    "Awaiting 8th Sem Result": "awaiting_final_semester",
}
