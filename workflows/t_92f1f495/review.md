# Review

## Verdict

CHANGES_REQUESTED

## Scope

Independent review of `firmware/src/pipeline.py` (1792 lines, commit created by task t_dddea607) against:
- `docs/software/SPEC_YA.md`
- `docs/architecture/architecture.md`
- `docs/architecture/implementation-plan.md`
- `workflows/t_dddea607/implementation-report.md`

Verified by: reading full source (all 1792 lines), `python3 -c "import pipeline"` smoke test (passes), `git status` (untracked-file layout check), grep for secrets/hardcoded keys.

## Requirements (SPEC_YA.md)

| # | Requirement | Verdict | Notes |
|---|---|---|---|
| Вход: файл/папка PDF/DOCX | PASS | `find_input_files()` L134-146 |
| Выход: `Markdown/<file>/<file>.md` + `image/` | PASS | `process_file()` L1641-1646, L1704 |
| Промежуточные → `tmp/<file>/` | PARTIAL/FAIL | See Finding 4 — converted DOCX→PDF is deleted, not kept in tmp |
| Лог: `tmp/Create_Markdown_VisionOCR.log` | PASS | `main()` L1751-1752 |
| OCR-артефакты (разбитые слова, пробелы) | PASS | `fix_ocr_artifacts()` L1273-1292 |
| Формулы `equation.text` → `$$` | N/A (justified deviation) | ADR-6: Yandex already returns inline `$...$`; `wrap_equations()` correctly omitted |
| LaTeX-чистка (пробелы в числах, `\mathsf`→text, `90^{\circ}`→`90 ^{\circ}`) | PASS | `cleanup_latex()`/`_simplify_math_commands()`/`_fix_latex_ocr_artifacts()` L739-852 |
| HTML→Markdown таблицы | PASS | `convert_html_tables()` L999-1021 |
| Объединённые заголовочные ячейки → текст в каждый столбец | PASS | `_parse_table_html()` colspan expansion L885-909 fills every spanned cell |
| Объединение смежных таблиц (Продолжение/Окончание/одинаковый заголовок) | PASS | `merge_tables()` L1079-1155 |
| Примечания/сноски под таблицами → цитаты | PASS | `fix_notes()` L1239-1270 |
| Подписи `Таблица N`, `Рис. N` | PASS | `fix_table_fig_labels()` L1218-1232 |
| Вырезание изображений по координатам из json | **FAIL** | See Finding 1 — no pixel→point scaling, likely wrong crop region on real scans |
| Переименование изображений → fig_N | PASS (mechanically) | `rename_images()` L1162-1195, but only rewrites links that don't exist yet — see Finding 2 |
| Вставка ссылок на изображения в итоговый md по координатам | **FAIL** | See Finding 2 — never implemented, only logged |
| Подписи под изображениями (курсив) | PARTIAL | Logic present (`fix_image_captions()` L1198-1215) but unreachable without Finding 2 fix, since no `![...]` links are ever inserted for OCR-derived pictures |
| AI-постобработка (Provod, gemini-3.5-flash / claude-sonnet-5, config_ai.yaml, `--ai` flag) | PASS | `ai_postprocess()`, `_call_ai_api()`, `parse_args()` — but see Finding 5 (missing `config_ai.yaml` template) |

## Architecture Compliance

- All 11 sections present and labeled with `# === N. ... ===` style headers matching ADR-4. PASS.
- `math-markdown` model used for OCR request. PASS.
- LibreOffice used for DOCX→PDF. PASS.
- PyMuPDF used for image extraction. PASS, but algorithm deviates from architecture — see Finding 1.
- ADR-6 (no `wrap_equations`) respected. PASS.
- ADR-2 (pixel→point coordinate scaling, `scale_x = width/page.rect.width`) **NOT implemented** — Finding 1.
- Architecture §3 Module Decomposition promised `_match_images_to_pictures(doc, pictures, page_dims)` using IoU against embedded PDF images — **not implemented**; the code instead treats the OCR bounding box directly as the crop rectangle with a fixed `dpi_scale=2.0`, with no matching step and no page dimensions passed in at all. This is an undocumented architectural deviation (not flagged in the implementation report as a design decision — it's only mentioned as a "Known Limitation").
- Data-flow step "Вставка ссылок ![fig_N] в .md на позиции по координатам" (architecture.md §4) is not implemented — Finding 2 (same gap, also called out as a known limitation in the implementation report, but it is a required data-flow step, not optional).
- Error handling: architecture §7 requires "HTTP 4xx/5xx: 3 ретрая с экспоненциальной задержкой (1с, 4с, 16с)" for Yandex OCR — **not implemented**; only HTTP 429 has any retry (single retry, 5s). Finding 3.

## Tests

No automated test suite exists under `tests/` (still just `.gitkeep`). The implementation report documents manual validation against one real Yandex OCR JSON (7 pages) and manual invocation of individual postprocessing functions — this is acceptable smoke testing but not regression-safe. `python3 -c "import pipeline"` was re-verified independently and passes. No test was performed against a real PDF end-to-end (`process_file()`/`main()`), so the image-extraction and OCR-retry code paths were never actually exercised. I did not have Yandex/Provod credentials available to run a real end-to-end pipeline; this could not be independently re-verified beyond code inspection.

## Findings

Severity: CRITICAL
Location: `firmware/src/pipeline.py:662-713` (`extract_images_from_pdf`)
Problem: Yandex OCR returns picture bounding boxes in pixel coordinates (per ADR-2), but the code passes these raw pixel values straight into `fitz.Rect(x0, y0, x1, y1)` (points) with no scale conversion. The architecture's documented formula (`scale_x = width/page.rect.width`, `scale_y = height/page.rect.height`) is never computed — the function doesn't even receive page pixel width/height. A `# TODO: получать width/height из textAnnotation` comment at L674-696 confirms this is known-incomplete, not a deliberate simplification.
Expected: Compute `scale_x`/`scale_y` per page from `textAnnotation.width`/`height` vs `page.rect.width`/`height`, and divide/scale the pixel bbox into points before building `fitz.Rect`.
Recommended correction: Thread `page_dims` (from `textAnnotation`) into `extract_images_from_pdf()`/`_crop_and_save_image()` and apply the ADR-2 scaling formula.
Reason: On any real-world scanned PDF (which OCR is designed for) the pixel-to-point ratio is typically 2–4x (150–300 DPI vs 72 DPI), so crops will be wrong-sized/wrong-positioned or exceed page bounds. This defeats a core requirement of SPEC_YA §"Вставка изображений".

Severity: CRITICAL
Location: `firmware/src/pipeline.py:1682-1694` (`process_file`, image extraction step)
Problem: Extracted images are saved to `image/fig_N.png` but no `![...]|(image/fig_N.png)` reference is ever inserted into `md_text`. The code only logs the count (`log.info(f"  Извлечено изображений: {len(extracted)}")`) with a `# TODO: вставка ссылок по координатам (сложная логика)` comment.
Expected: SPEC_YA.md line 27 explicitly requires "в итоговой md вставить ссылки на изображения согласно координатам из json"; architecture.md §4 lists this as a mandatory data-flow step, not optional.
Recommended correction: Implement the image-insertion step (sort pictures by page/Y-coordinate, insert `![Рисунок N](image/fig_N.png)` at the corresponding location in the assembled markdown, or at minimum append them in reading order if precise positional insertion is deferred).
Reason: Without this, image extraction is functionally useless to the end user — the final `.md` file will never reference any of the extracted images, and downstream postprocessing functions that depend on image links (`rename_images()`, `fix_image_captions()`) become dead code for OCR-sourced images (they only fire on pre-existing `![image](...)` markup, which never exists for this pipeline's own extracted figures).

Severity: HIGH
Location: `firmware/src/pipeline.py:220-317` (`send_to_yandex_ocr`)
Problem: architecture.md §7 (Error Handling Strategy) and implementation-plan.md Unit 2 both specify "HTTP 4xx/5xx: 3 ретрая с экспоненциальной задержкой (1с, 4с, 16с)". The implementation only special-cases HTTP 429 with one retry after 5s; all other 4xx/5xx responses immediately `raise RuntimeError` with no retry.
Expected: Wrap the POST call in a retry loop (3 attempts, backoff 1s/4s/16s) for general 4xx/5xx per the approved architecture.
Recommended correction: Add a small retry loop (or reuse the pattern already present in `_call_ai_api()` L1478-1501, which does implement multi-attempt retry correctly) around the initial POST in `send_to_yandex_ocr()`.
Reason: Transient Yandex API errors (5xx, momentary network issues) will abort the whole file's processing instead of recovering, contrary to the documented reliability requirement.

Severity: HIGH
Location: `firmware/src/pipeline.py:394-437` (`parse_yandex_json_to_md`)
Problem: The function always appends `_parse_tables_from_json()` output (`tables_md`) after the `markdown` field text (`md_text = md_text + "\n\n" + tables_md`), with no de-duplication check. Per ADR-1, the `math-markdown` model's `markdown` field already contains tables rendered inline (as HTML or MD). Unconditionally appending the separately-parsed structured `tables` field a second time will duplicate every table that appears in both sources.
Expected: Either use `tables` as the sole source of truth for tables (stripping/replacing table blocks already present in `md_text`), or only append `tables_md` for tables that are not already represented in `md_text` (e.g. when `markdown` field is empty/fallback).
Recommended correction: Reconcile against the real Yandex JSON output (which the coder already has access to, per Test Infrastructure section of the implementation report) to determine whether `markdown` already embeds tables, and adjust the merge logic accordingly. At minimum, add a test asserting no duplicate table content in a realistic sample.
Reason: Confirmed on the implementation report's own numbers: "`parse_yandex_json_to_md()` → 33 991 символ markdown" combines a markdown field and a rendered 12Kb table blob (per the two preceding rows in the same table) with no reconciliation step; this strongly suggests duplicated table content in the actual test output, which was not checked in the report.

Severity: MEDIUM
Location: `firmware/src/pipeline.py:1711-1717` (`process_file`, `finally` block)
Problem: The `finally` block unconditionally deletes the DOCX→PDF-converted intermediate file (`Path(pdf_path).unlink()`) instead of leaving it in `tmp/<file>/`. `convert_docx_to_pdf()` already places the PDF inside `tmp/<file>/` (matching SPEC_YA's "промежуточные файлы" location), so deleting it afterward removes an intermediate artifact SPEC_YA §3.2 says should be preserved: "Все промежуточные файлы генерируемые Vision OCR и скриптами постобработки должны быть перемещены в папку tmp/<file>".
Expected: Keep the converted PDF under `tmp/<file>/` like the other intermediates (`yandex_result.json`, `raw.md`).
Recommended correction: Remove the `unlink()` call, or make deletion configurable/opt-in.
Reason: Debuggability — if OCR quality is poor, being able to inspect the exact PDF sent to Yandex (post-LibreOffice-conversion) is valuable, and SPEC_YA implies it should be retained.

Severity: LOW
Location: repository — `config_ai.yaml` referenced by `parse_args()` default (`--config ./config_ai.yaml`) and by `load_config()`
Problem: No `config_ai.yaml` template file was created anywhere in the repo (checked via file search — zero matches for `config_ai.yaml` under the project). `load_config()` degrades gracefully to a hardcoded default if the file is missing, so this is not a functional blocker, but architecture.md §8 documents `config_ai.yaml` as a first-class configuration artifact (with model/prompt content) that end users are expected to be able to edit.
Expected: Ship a `firmware/src/config_ai.yaml` (or project-root) template per architecture §8, consistent with "production files in firmware/src/" repository rule.
Recommended correction: Coder should add the template config file, or explicitly note in the implementation report that this was intentionally deferred.
Reason: Minor completeness/DX gap; low severity because `load_config()` has a working fallback.

Severity: LOW
Location: `firmware/src/pipeline.py` (whole file)
Problem: No unit/integration tests were added under `tests/` (project convention per `.hermes/orchestration.yml` repository.tests rule), despite the file containing substantial, independently testable pure functions (`cleanup_latex`, `merge_tables`, `_parse_table_html`, `_chunk_text`, etc.).
Expected: At minimum a `tests/test_pipeline.py` covering the postprocessing functions with the ad-hoc test scripts already written during implementation (`/tmp/test_parser.py`, `/tmp/test_postprocess.py` per the implementation report) — those were left in `/tmp` and not committed anywhere in the repo.
Recommended correction: Move/adapt the temporary test scripts referenced in the implementation report into `tests/`.
Reason: Regression risk — the two CRITICAL findings above (image coordinate scaling, missing image insertion) would likely have been caught by a real end-to-end test against a scanned PDF with embedded images, rather than only the JSON-parsing-only test that was actually run.

## Required Changes

1. Fix image coordinate scaling in `extract_images_from_pdf()`/`_crop_and_save_image()` (Finding 1 — CRITICAL).
2. Implement image-link insertion into the final markdown per SPEC_YA (Finding 2 — CRITICAL).
3. Add retry/backoff for Yandex OCR HTTP 4xx/5xx per architecture §7 (Finding 3 — HIGH).
4. Resolve table duplication between `markdown` field and structured `tables` field in `parse_yandex_json_to_md()` (Finding 4 — HIGH).
5. Stop deleting the intermediate DOCX→PDF file, or document the deviation (Finding 5 — MEDIUM).
6. (Recommended, not blocking) Add `config_ai.yaml` template and commit a minimal test suite.

## Risks

- Findings 1 and 2 together mean the image pipeline is effectively non-functional end-to-end for any real document with embedded figures — this is the single biggest risk to accepting this implementation for integration, since image handling was called out as ADR-2/ADR-4/§4 core functionality, not an edge case.
- Finding 4 (table duplication) risks silently corrupting output on any document with tables — a very common case for the GOST-style technical documents this pipeline appears to target (per `!База_ГОСТ` reference in the implementation report's Key Design Decisions).
- No end-to-end run against a real PDF/DOCX with images and tables was performed by either the coder or this review (I lack Yandex/Provod credentials in this environment), so real-world severity of Findings 1 and 4 is inferred from code inspection, not confirmed by execution. Strongly recommend the coder run one real end-to-end test with a document containing both embedded images and tables before the next review pass.

## Notes

- File placement, repository hygiene, code quality (imports, constants, logging), and absence of hardcoded secrets are all satisfactory — no findings in those areas.
- `wrap_equations()` omission (ADR-6) and the CLI's lack of `--backend` flag are correctly implemented per the approved architecture; these are not deviations, they are documented and intentional.
- Module imports cleanly (`python3 -c "import pipeline"` reverified independently during this review, matches implementation report).
