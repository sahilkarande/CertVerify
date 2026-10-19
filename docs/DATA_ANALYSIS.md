# Data Analysis — `data_feb_26.csv`

Google Form response export for C-DAC PG course admission document submission.
Contains PII; it is git-ignored and must never be committed. Figures below are aggregates only.

## Shape

- 82 responses (one per student), 22 columns, no ragged rows.
- Submitted 2026-02-12 → 2026-02-25.
- Single course: `PGCP-BDA` (all 82).

## Columns

| # | Column (short) | Role | Notes |
|---|---|---|---|
| 0 | Timestamp | metadata | `M/D/YYYY HH:MM:SS` |
| 1 | Form Number | **student key** | 9 digits, unique |
| 2 | Name | identity | 73 have 3 words, 8 have 2, 1 has 4; 20 have stray spaces; 5 are ALL CAPS |
| 3 | Course | identity | constant `PGCP-BDA` |
| 4 | Form No. | duplicate | identical to col 1 in all rows → drop |
| 5 | Mobile | contact | 1 value has only 9 digits |
| 6 | Guardian mobile | contact | all 10 digits |
| 7 | Email | contact | all gmail; 1 needs trim/lowercase |
| 8 | Hostel | admin | Yes 58 / No 24 — not document-related |
| 9 | Photo | **file** | image expected |
| 10 | Signature | **file** | image expected |
| 11 | 10th marksheet | **file** | PDF expected |
| 12 | 12th **or Diploma** marksheet | **file** | either type; content decides |
| 13 | Degree option | **expectation** | see below |
| 14 | All semester marksheets in one PDF | **file** | multi-page; needs page-level split/classification |
| 15, 17 | Undertaking (free text) | noise | mostly empty; random text → ignore |
| 16, 18 | Undertaking (checkbox) | consent | always "I agree to this Undertaking." |
| 19 | Provisional degree (fallback: 8th/7th sem) | **file** | label ≠ guaranteed content |
| 20 | Degree certificate (fallback chain: provisional → 8th → 7th sem) | **file** | label ≠ guaranteed content |
| 21 | Aggregate % (self-reported, CGPA converted by student) | **claim to verify** | 55.89–88.0, all look like percentages |

## Files

- 7 Drive links per student → **574 files**, all `https://drive.google.com/open?id=<ID>`, all distinct IDs.
- Distinct IDs ≠ distinct content: the same file uploaded twice gets a new ID → duplicate detection must use content hashes.
- File type/size not visible in the CSV; the form *asks* for PDF/image ≤1 MB but this must be verified after download.

## Degree option → what cols 19/20 should actually contain

| Degree option (col 13) | Count | Expected col 19 | Expected col 20 |
|---|---|---|---|
| Already having Degree Certificate | 45 | Provisional (or 8th/7th sem) | Degree certificate |
| Having Provisional Degree Certificate | 25 | Provisional | Provisional (same doc again) |
| Awaiting Provisional Degree Certificate | 11 | 8th/7th sem marksheet | 8th/7th sem marksheet |
| Awaiting 8th Sem Result | 1 | 7th sem marksheet | 7th sem marksheet |

## Implications for the design

1. **Classify by content, not by upload slot.** Slots are hints; the fallback rules mean a slot can contain 3–4 different document types.
2. **Expected-document rules depend on col 13.** "Missing document" checks must be computed per degree option.
3. **Some duplicates are expected by design** (e.g. provisional uploaded in both 19 and 20; latest-semester sheet also inside col 14). These should be reported as info, not errors.
4. **Page-level processing** for col 14: one PDF → N semester marksheets.
5. **Col 21 is a verification target:** compare against the aggregate computed from the extracted marks/CGPA.
6. **Normalise identity fields** (trim/collapse spaces, case-fold) before comparing names with names on certificates.
7. **Photo/signature** are stored and shown in the dashboard but not OCR'd.

## Sample documents (2 students, 23 files, in git-ignored `Data/`)

Student A: Dr. Babasaheb Ambedkar Technological University (DBATU), B.Tech CSE.
Student B: Vishwakarma Institute of Technology (autonomous, affiliated to SPPU), B.Tech Mechanical.
Both: "Already having Degree Certificate"; both did 10th/12th with the Maharashtra State Board (MSBSHSE).

### Files

- All PDFs are **image-only scans** (0 text characters) → OCR is mandatory for every document.
- Mostly **phone photos** wrapped into PDFs (iLovePDF, iText, pdf-lib, ReportLab): perspective skew, background
  edges, hologram glare, "COPY" watermarks, security-print backgrounds. Embedded resolution 72–150 ppi.
- Sizes 50 KB – 1.0 MB; all within the form's 1 MB limit.
- Photo and signature are JPEGs.
- Download file names are `<original name> - <respondent name>.<ext>`. The original name is arbitrary.

### Document types seen and key fields

| Type | Layout | Key fields | Language |
|---|---|---|---|
| SSC (10th) statement of marks, MSBSHSE | Fixed board template | Seat no., month/year, name (surname first), mother's name, subject marks, total/500, percentage, result | English + Marathi (key fields in English) |
| HSC (12th) statement of marks, MSBSHSE | Fixed board template | Stream, seat no., month/year, name, mother's name, marks, total/650, percentage, result | English + Marathi |
| Semester grade report, DBATU | University template | Semester (word: FIRST…EIGHTH), exam session, PRN, name, subjects/credits/grades, SGPA, CGPA | English |
| Statement of grades, VIT Pune | Institute template | Semester (Roman: I…VIII), GR no., name, mother's name, grades, SPI, CPI | English |
| Degree certificate, DBATU | Decorative (blackletter font) | Name, college, degree + branch, session, class, **CGPA** | Marathi half + English half |
| Degree certificate, SPPU/VIT | Decorative (script font) | Name, mother's name, degree + branch, session. **No CGPA.** | English + Marathi |

### Findings that drive the design

1. **Slot ≠ content, confirmed.** Student A uploaded the *degree certificate* in the provisional slot (byte-identical
   file, same SHA-256 as the degree slot). Student B uploaded the *8th-semester grade sheet* in the provisional slot.
   That page is also inside the all-semesters PDF, so it is a content duplicate with a different file hash.
2. **Pages are not in order.** Student A's all-semesters PDF has page order 1, 6, 5, 8, 7, 3, 2, 4. The semester
   number must be read from each page.
3. **Identity fields vary within one student's documents.**
   - Name order: "Surname First Middle" (board, early semesters) vs "First Middle Surname" (later semesters, form).
   - The same ID appears as `2021…` and `T2021…`.
   - The institute name is abbreviated on some sheets and written out in full on others.
   → Name matching must be order-insensitive and token-based; IDs need normalisation.
4. **Mother's name** appears on SSC, HSC, VIT sheets and the SPPU degree. Useful as a second identity check.
5. **Grading terms differ per university** (SGPA/CGPA vs SPI/CPI; letter grades AA/AB/BB/…).
6. **The CGPA → % conversion is university-specific, and students already apply it.**
   - A: degree CGPA 8.00, form says 75 → (CGPA − 0.5) × 10 ✔
   - B: final CPI 7.06, form says 63.54 → CPI × 9 ✔ (no CGPA on the degree certificate; it comes from the sem 8 sheet)
   → Verifying column 21 needs a per-university formula table (an admin-editable config), not one global formula.
7. Each page has distinctive English header text ("SECONDARY SCHOOL CERTIFICATE", "HIGHER SECONDARY",
   "SEMESTER GRADE REPORT", "Statement of Grades", "degree of"). That makes rule-based classification from OCR
   text a realistic first approach.

## Still unknown

- How many other universities/boards appear among the 82 students. Only 2 seen so far.
- Diploma marksheets (12th slot) and provisional certificates: none seen yet.
- Worst-case image quality across the full set.
