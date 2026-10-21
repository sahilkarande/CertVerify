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

## Full file set (Drive folder export, 2026-10-19)

Folder `PGCP-BDA  - FEB -26 (File responses)/` (git-ignored) with one sub-folder per upload question.

### Structure and file-to-student mapping

- 7 folders × 82 files = **574 files**, the same count as the Drive links in the CSV.
- File name = `<student's original file name> - <respondent name>.<ext>`.
  - The respondent name is the student's **Google account name**, not the name typed into the form.
  - Each of the 82 respondent names appears **exactly once in every folder**, so the 7 files of a student group cleanly.
- The CSV links (Drive IDs) cannot be matched to these files, and file times were not preserved (all are the copy time).
  Mapping to form rows must use names:
  - 8 exact, 71 where all words of the account name appear in the form name, 2 spelling/format variants
    (also confirmed by the email address), 1 nickname with no overlap (resolvable only by elimination
    or by the name printed on the documents).
- The 14 files downloaded earlier into `Data/` are byte-identical to their copies here.

### File formats

| Slot | Formats (by content) | Notes |
|---|---|---|
| 10th, 12th/Diploma, semesters, provisional, degree | 410 PDF | Sizes 41 KB–2.1 MB (2 degree files exceed the form's 1 MB limit) |
| Photo | 69 JPEG, 12 PNG, **1 HEIC** (iPhone) | 1 file named `.png` is actually JPEG → trust content, not extension |
| Signature | 77 JPEG, 5 PNG | Extensions include `.jfif` |

- Text layer: almost none. 12–16 per slot have one, and only **4 of 82** semester PDFs → OCR is required.
- Embedded image resolution: median 150–200 ppi for certificates, but **semester PDFs median 96 ppi**
  (132 of 232 images below 100 ppi) → low resolution is the main OCR risk.
- Pages: 10th/12th almost always 1; provisional/degree slots 1–4; **semester PDFs 1–14 pages** (34 have 8).
- **17 PDFs (11 students) fail to open in standard readers.** Each is a complete PDF followed by zero-byte
  padding (to a 1 KB boundary), probably added by a phone app. Cutting at the last `%%EOF` recovers
  **all 17** (verified by rendering). The original bytes should be kept as evidence; repair belongs in intake.

### Content variety (first page of every file in 3 slots, inspected visually)

**12th / Diploma slot.** Mostly the Maharashtra State Board (HSC statement of marks), plus:
- the HSC passing *certificate* (a different document with no marks)
- CBSE
- MSBTE Diploma, about 12 files
- autonomous Government Polytechnics (Mumbai, Amravati, Aurangabad)
- Karnataka PUE (Kannada), UP Board (Hindi), MP Board, Jharkhand JAC, Telangana BIE, Rajasthan Board

At least one **10th marksheet was uploaded in the 12th slot**.

**Semester slot.** Roughly 25+ universities or autonomous institutes:
- most common: SPPU, University of Mumbai, DBATU
- also VTU, RTMNU (incl. G H Raisoni, RCOEM, St. Vincent Pallotti), KBC NMU, Dr. BAMU, MGM University,
  Shivaji University, RGPV, SAGE, AKTU, GLA, Graphic Era, Chandigarh, JECRC, Teerthanker Mahaveer,
  Sanjay Ghodawat, Usha Martin, MPKV (agriculture), VIT Pune, Tatyasaheb Kore, DY Patil

Layouts include:
- colour security-print grade cards
- old dot-matrix SPPU statements, often **rotated 90°**
- **mobile screenshots of online result portals**
- photos of documents lying on desks or folders
- **supplementary/backlog** result sheets, so one semester can have several sheets

Degrees include B.E./B.Tech (most), B.Sc., M.Sc. and MCA, so **postgraduate records occur too**.

**Degree slot.** Contents:
- decorative degree certificates (SPPU, Mumbai, DBATU, Shivaji, RTMNU, VTU, SGBAU, Graphic Era, Sharda, …)
- provisional certificates and "passing certificates"
- 7th/8th semester grade cards
- PG statements of marks
- at least one **10th marksheet**

Many are landscape or rotated, some bilingual or Hindi/Marathi-first.

### Import result (Phase 3)

- 82 students × 7 documents = 574 files imported. Every stored file decrypts to bytes identical to the folder.
- Mapping methods:
  - 8 exact, 71 all-words, 2 similar-plus-email: confirmed automatically
  - 1 by elimination: confirmed by a person
- **Exact duplicates:** none across students. 44 students uploaded the same file in more than one of their own slots:
  - 42 × provisional slot + degree slot
  - 1 × those two plus the semester PDF
  - 1 × provisional slot + semester PDF

  This follows from the form's fallback instructions, so it should be reported as *info*, not as an error.

### Page quality measurement (Phase 4, 2026-10-19)

All 969 PDF pages (410 files) and 164 images were measured. Pages were rendered at 100 dpi; scripts are in the
session scratchpad and not part of the codebase.

| Slot | Pages | Effective resolution | Notes |
|---|---|---|---|
| Semester PDFs | 592 | short side median **793 px ≈ 96 dpi**; 313 pages < 100 dpi, 569 < 150 dpi | the main OCR risk |
| 10th / 12th / provisional / degree | 83–104 each | median 150–180 ppi, ~11–15 pages each < 100 ppi | mostly fine |

- **Sharpness** (variance of the Laplacian, at a normalised width):
  - useful, but it mixes several causes
  - the lowest-scoring pages are blank back sides of degree certificates ("back to back" uploads),
    genuinely blurred or faded phone photos, and documents that fill only a small part of the page
  - so quality checks need separate tests for each cause: blank page, document area, blur
- **Rotation:** a projection-profile heuristic flagged 78 pages, but only about 1 in 3 were really rotated (tables
  with strong vertical rules fool it). Estimate: about 25 sideways pages, mostly semester sheets. Reliable detection
  needs Tesseract's orientation detection (OSD).
- **Photo of a document on a desk:** dark-border detection found almost none at page level, because PDF tools add white
  margins. Document-area cropping must look for the paper region, not dark borders.
- **Images:** 144 JPEG, 17 PNG, 2 MPO (multi-picture JPEG from phones), 1 HEIC that Pillow cannot read without a
  plugin. Only 1 image relies on its EXIF orientation tag. Photos are as small as 340 × 420 px.
- **Speed:** the pure-Python baseline takes a median of 0.85 s per page to render and measure at 100 dpi.

### Quality calibration and Phase 4 results (2026-10-19)

The first thresholds were tuned on synthetic pages and failed on real photos: nothing was found blank or
blurry. Recalibrated by inspecting the extremes of each metric:

- **Resolution:** a 595 px (≈72 dpi A4) SPPU sheet is legible at native size. New thresholds: retake below 450 px,
  warn below 650 px.
- **Blur:** sharpness must be measured at native resolution, because upscaling softens low-resolution pages.
  Even then, blurry phone photos and readable washed-out scans overlap, so only extreme blur is flagged.
  Readability is judged in Phase 5 from OCR confidence.
- **Blank:** paper texture gives near-empty pages 3–5% "ink"; a true blank is below 1%. Certificate back sides
  with a postal label are not blank; recognising them is a Phase 6 task.
- **EXIF:** 10 images carry the invalid orientation `0`; only one is really rotated.
- **pdfium** opens all 17 zero-padded PDFs without repair (poppler could not); the repair path remains as a fallback.

Result (processing version 2): 574 documents → 1,133 pages, 0 failures, 0.48 s/page on this laptop.

| Verdict | Pages | Main reasons |
|---|---|---|
| ok | 852 | |
| warn | 270 | low resolution (233; 221 of them semester pages), low contrast (42, mostly light security paper) |
| retake | 10 | resolution 329–430 px (6 from one student's statements) |
| blank | 1 | back side of a certificate |

### OCR evaluation (Phase 5, 2026-10-19)

Ground truth: 40 pages sampled by group and quality verdict, about 20 institutions, 237 key strings
(name tokens, IDs, SGPA/CGPA/percentage/totals, institution words), hand-labelled from the page images.
It is stored in git-ignored `backend/data/ocr_eval/` and re-measured with `make ocr-eval`.

| Setting (Tesseract 5.3.4, eng) | Key recall | Rotation correct | Time/page |
|---|---|---|---|
| Plain, full-page layout (psm 3) | 62% | 28/40 with Tesseract OSD | 0.8 s |
| Upscaled to 2480 px | 60% | n/a | 1.5 s |
| **Background removal + sparse text (psm 11), 1240 px, rotation by OCR confidence** (chosen) | **75.5%** | **40/40** | 2.4–3.8 s |
| Same + plain pass, keep better | 76.4% | 40/40 | 4.4 s |
| Same at 1800 px | 74.3% | 39/40 | 6.2 s |

- **Security-print backgrounds** made plain Tesseract return *nothing* on 7 clear pages; even Otsu
  binarisation did not help. Background removal (dilate + median-blur estimate, keep the difference)
  fixed most of them.
- **Tesseract OSD** skips short certificates ("too few characters") and was wrong on 3 of the 4 sideways
  dot-matrix sheets. Reading at each rotation and keeping the most confident result is reliable.
- **By group:** school/diploma ~90%, degree/provisional ~83%, good semester sheets 83%,
  low-resolution semester sheets 57%, retake pages 36% (confirms the Phase 4 verdict).
- **By kind:** words 92%, IDs 58%, numbers 53%. Digits in tables are the weak spot, which matters for Phase 7.
- **Readability:** pages marked `low_confidence` (mean word confidence < 50) recover 27% of keys, against 79% for
  `ok` pages. Confidence is a useful review flag (correlation with recall 0.53), not proof of correctness.

### Full OCR run (Phase 5)

- 968 document pages (photos, signatures and the blank page are excluded): 896 `ok`, 72 `low_confidence`,
  0 `no_text`, 0 failed; about 3.4 s per page on this laptop.
- 54 pages were turned upright (49 × 270°, 5 × 90°). This is far more than the earlier ~25 estimate: Amravati
  dot-matrix sheets, landscape grade cards and sideways certificate photos. A visual check of all 54 showed
  them upright afterwards; the few doubtful ones have confidence below 50 and are already `low_confidence`.
- 9 of the 10 Phase 4 `retake` pages are `low_confidence` here, so the two independent checks agree.

### Page classification (Phase 6, 2026-10-20)

Labels: 80 pages in git-ignored `backend/data/ocr_eval/classification.json`. Set A is the 40 OCR-evaluation pages,
used to tune the rules. Set B is 40 further pages sampled by slot and kept out of tuning.
Types: 36 semester, 14 provisional/passing, 10 HSC, 9 SSC, 6 degree, 4 diploma, 1 back side.

| | Set A (tuning) | Set B (held out) |
|---|---|---|
| Type correct (stored result, including fallbacks) | 39/40 | 38/40 |
| Semester number exact | 26/36 across both sets | |

- All 3 wrong labels are low or medium confidence (2 continuation at 0.5, 1 fuzzy at 0.7). Every high-confidence (exact)
  label was correct, so the review rule is: confidence < 0.9 means a person checks the page.
- Lessons:
  - The Maharashtra board's name contains "Higher Secondary", so 40% of 10th sheets mention it.
    Only exam titles count, and only in the page header.
  - OCR damages titles ("SECONDARY SCHOO! CERTIFICATE"), so long titles use approximate matching. Comparing both
    families is required, because the SSC and HSC titles are about 80% similar to each other.
  - Roman numerals get garbled (VIII→VILL, IV→LV, II→IL).
  - "ENTRY POINT: 10+2 (HSC)" on an SPPU passing certificate once made it a 12th sheet.
- Full run, 968 pages:

  | Method | Pages |
  |---|---|
  | exact content match | 854 |
  | fuzzy content match | 11 |
  | continuation (later unreadable page takes the previous page's type) | 38 |
  | slot hint (unreadable page in the 10th slot) | 28 |
  | unknown → review | 37 (3.8%) |

- Wrong-slot uploads made visible by content:
  - degree slot: 25 provisional/passing certificates and 16 semester sheets
  - semester PDFs: 6 provisional and 2 degree certificates
  - 12th slot: 19 diploma sheets (expected) and 1 10th sheet
- **Migration incident.** SQLite batch migrations rebuild a table (copy, drop, rename). With foreign keys
  enforced, the drop cascaded:
  - the Phase 6 migration emptied `page_ocr` (restored from the pre-migration backup)
  - the Phase 4 migration had nulled all 574 `import_file_refs.document_id` (relinked unambiguously by submission
    and slot)

  Migrations now run with foreign keys off and finish with `PRAGMA foreign_key_check`. A regression test covers it.

### OCR engine comparison (Phase 7, 2026-10-21)

Extraction experiments showed Tesseract was the bottleneck: the SGPA value appeared anywhere in its text on only
3 of 15 labelled semester pages, so no extraction rule could find the rest. PaddleOCR 3.7 (CPU only, oneDNN off
because it crashes in paddlepaddle 3.3.1) was measured on the same 40 labelled pages, read upright:

| Engine | All keys | Words | IDs | Numbers | SGPA present in text | Time per page |
|---|---|---|---|---|---|---|
| Tesseract 5.3.4 (Phase 5 pipeline) | 76% | 92% | 58% | 53% | 3/15 | 3.5 s |
| PaddleOCR PP-OCRv6 medium | 96% | 99% | 83% | 96% | — | 53 s |
| PaddleOCR PP-OCRv6 small | 95% | 98% | 83% | 95% | 12/15 | 16 s |
| **PaddleOCR PP-OCRv6 tiny (chosen)** | **91%** | **93%** | **83%** | **90%** | **10/15** | **5.2 s** |

- Tiny is the default: nearly as fast as Tesseract and the numbers recall rises from 53% to 90%. The models can be
  changed by configuration for a stronger server.
- Integrated (`make ocr-eval engine=paddle`, Tesseract's stored rotation, PaddleOCR reading once): 90.7% key recall
  (ids 83%, numbers 89%, words 93%), rotation 40/40, 4.9 s/page.
- With the better text, the remaining extraction gaps are in the rules: e.g. the total marks value is present in the
  text on 13/15 pages but the rough rule found it on 3.

### Implications

1. Manual download replaces Drive API access for the seed data. A **local-folder importer** is simpler and
   needs no Google Cloud setup.
2. The intake stage (Phase 4) must handle:
   - detecting the real file type
   - HEIC conversion
   - padded-PDF repair
   - rotation detection
   - low-resolution upscaling
   - screenshots
3. Rule-based extraction per layout will not scale to 25+ layouts. Classification can stay rule-based (the header
   phrases are distinctive), but extraction needs a generic approach (OCR + key–value / table parsing, local
   LLM fallback) with per-layout rules only for the most common boards and universities (MSBSHSE, CBSE, MSBTE,
   SPPU, Mumbai, DBATU).
4. The document-type registry needs **`hsc_passing_certificate`** and **`pg_marksheet`/`pg_degree`** (or a
   level attribute), and semester records need a **supplementary/attempt** notion.
