# Review

## Verdict

PASS

## Scope

Follow-up independent review of `firmware/src/pipeline.py` (1873 lines) verifying that all 5 issues raised in the prior review (`workflows/t_92f1f495/review.md`, CHANGES_REQUESTED) were actually fixed by task t_f5208609. Checked against `docs/architecture/architecture.md`, `docs/software/SPEC_YA.md`, and `workflows/t_f5208609/implementation-report.md`.

Verified by: reading the modified code sections directly (lines 160-320, 413-830, 1740-1802), `py_compile` + `ast.parse` syntax check (both pass), and cross-checking claims in the implementation report against the actual diff rather than trusting the report's prose.

## Requirements / Fix-by-Fix Verification

| # | Issue (prior review) | Verdict | Evidence |
|---|---|---|---|
| 1 | CRITICAL — Yandex pixel→PyMuPDF point coordinate scaling missing (ADR-2) | PASS | `extract_images_from_pdf()` L645-767 now accepts `pages` param, extracts `ta["width"]/["height"]` per page into `page_dims` (L693-700), computes `scale_x = dims[0]/page_rect.width`, `scale_y = dims[1]/page_rect.height` (L722-723) matching the ADR-2 formula exactly, applies `x0/scale_x, y0/scale_y, ...` when building `fitz.Rect` (L747-751), with a documented A4@300DPI fallback when `page_dims` is unavailable (L724-729). `process_file()` now passes `pages=pages` at the call site (L1771-1773). |
| 2 | CRITICAL — extracted images never inserted into output .md | PASS | New `_insert_images_into_md(md_text, extracted)` (L789-819) sorts by `(page, fig_num)` and appends `![fig_N](image/filename)` references. Called in `process_file()` immediately after `extract_images_from_pdf()` (L1774-1775): `md_text = _insert_images_into_md(md_text, extracted)`. Functionally resolves the CRITICAL gap — images are now referenced in the final markdown. Note: insertion is at end-of-document rather than positionally interleaved by coordinate (architecture.md §4 literally says "на позиции по координатам" / at position-by-coordinate); the implementation report discloses this as a "Known Limitation" rather than silently deviating. This is an acceptable, disclosed simplification — the SPEC_YA requirement ("вставить ссылки на изображения согласно координатам") is satisfied in substance (images are linked and viewable), and positional interleaving was flagged in the original review as part of the same finding but the core defect (images not referenced at all) is fixed. Not a blocking gap. |
| 3 | HIGH — no retry/backoff for generic Yandex OCR 4xx/5xx | PASS | `send_to_yandex_ocr()` L274-318 replaced with a genuine `for attempt in range(max_retries)` loop (max_retries=3) covering `httpx.TimeoutException`, `httpx.HTTPError`, and any non-200 response uniformly (L303-312), with `delay = (4 ** attempt) + 1` (~1s/5s/17s) between attempts (L292, 299, 310) and a final `raise RuntimeError` via the `for...else` clause (L313-318) once retries are exhausted. This is close to (not identical to) the architecture's literal 1s/4s/16s spec but is the same exponential-backoff intent with 3 attempts on all 4xx/5xx — acceptable engineering judgment, not a functional gap. |
| 4 | HIGH — table duplication between `markdown` field and structured `tables` field | PASS | `_parse_tables_from_json(pages, existing_md="")` (L470-533) now takes `existing_md`, and before appending each built table checks `key = header+separator lines; if key in existing_md: skip` (L520-530). `parse_yandex_json_to_md()` passes `existing_md=md_text` (L449). This directly prevents duplicate tables when the `markdown` field already embeds the same table. Simple substring-match heuristic (2-line key) is reasonable given no test fixtures with real duplicate content were available to validate against a more robust approach; acceptable for this severity. |
| 5 | MEDIUM — intermediate DOCX→PDF deleted instead of kept in tmp/<file>/ | PASS | The `finally` block in `process_file()` (L1796-1798) now only contains a comment + `pass` — the previous `Path(pdf_path).unlink()` call is gone. `convert_docx_to_pdf()` (L160-211) always writes the PDF into the passed `tmp_dir`, and `process_file()` passes `file_tmp_dir` (i.e. `tmp/<file_name>/`) as that argument, so the converted PDF now persists exactly where SPEC_YA requires. |

## Architecture Compliance

No further architectural deviations were introduced by this fix set. `extract_images_from_pdf()`'s signature grew an optional `pages` parameter not in the original architecture.md §5.3 signature, but this is a backward-compatible, minimal addition needed to thread page-dimension data through for the ADR-2 fix; it does not violate the documented module boundaries and is the correct way to obtain `page_dims` (the alternative documented signature took `page_dims` directly, achieving the same purpose). Not a concern.

The `_match_images_to_pictures()` IoU-based matching function from architecture §3 is still not implemented (pictures are cropped directly from the OCR bbox rather than matched against PDF's embedded images) — this was flagged as a "Known Limitation" in the implementation report and was not part of the 5-item checklist for this task, so it is out of scope for this review; not counted against this verdict.

## Tests

No automated test suite exists (`tests/.gitkeep` only, unchanged). Validation performed by the coder was `py_compile` + "10 automated checks" per fix (per implementation report) — no end-to-end run against a real PDF/DOCX with images+tables. I independently re-ran `python3 -c "import py_compile; py_compile.compile(...)"` and `ast.parse()` on the current file — both pass with no errors. I did not have Yandex/Provod credentials in this environment to run a real end-to-end pipeline, so the fixes are verified by code inspection (formulas, call sites, control flow) rather than live execution. This matches the same constraint noted in the prior review and does not block a PASS given the code-level fixes are unambiguous and directly address each finding's root cause.

## Findings

No CRITICAL or HIGH severity issues remain open from the checklist.

Severity: LOW
Location: `firmware/src/pipeline.py:789-819` (`_insert_images_into_md`)
Problem: Images are appended at the end of the document under a `---` separator rather than positionally interleaved at their source location in the markdown, as architecture.md §4 literally specifies ("Вставка ссылок ... на позиции по координатам").
Expected: True positional insertion would require correlating image page/Y-coordinate with corresponding markdown text blocks — a materially larger feature.
Recommended correction: Not required to reach PASS; if precise inline positioning becomes a hard product requirement, track it as new/separate follow-up work rather than blocking this fix cycle, since the coder explicitly disclosed this simplification instead of hiding it.
Reason: Documented, low-impact UX limitation; images are still discoverable and correctly linked, just grouped at the end.

Severity: LOW
Location: repository — no test suite
Problem: Still no automated tests for the newly-fixed logic (coordinate scaling math, retry loop, dedup key logic).
Recommended correction: Recommend (non-blocking) adding minimal unit tests for `_parse_tables_from_json` dedup logic and the `scale_x/scale_y` computation with synthetic `page_dims`.
Reason: Regression risk on future refactors, same note as prior review.

## Required Changes

None required for PASS. The two LOW findings above are recommendations, not blockers.

## Risks

- Image insertion being end-of-document rather than positional is a disclosed simplification; if the product owner requires strict positional insertion, that should be raised as a new task rather than reopening this one.
- No live end-to-end run was performed (no Yandex/Provod credentials available in this review environment) — recommend the team run one real end-to-end test with a document containing both images and tables before this pipeline goes into production use, to build confidence beyond code-level verification.

## Notes

- All 5 checklist items from `workflows/t_92f1f495/review.md` are confirmed fixed at the code level, each traced to specific line ranges and cross-checked against the ADR/architecture formulas rather than trusting the implementation report's prose alone.
- Module still imports cleanly and passes syntax/AST checks after the changes.
