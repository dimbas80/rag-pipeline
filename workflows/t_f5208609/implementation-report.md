# Implementation Report: t_f5208609

## Summary

Implemented all 5 review fixes in `firmware/src/pipeline.py` covering CRITICAL, HIGH, and MEDIUM issues.

## Files Changed

- `firmware/src/pipeline.py` — all changes

## Changes

### Fix #1 (CRITICAL): Coordinate scaling in `extract_images_from_pdf()` (ADR-2)

**Problem:** Image extraction used raw Yandex pixel coordinates as PyMuPDF points, producing wrong crop regions on scanned PDFs.

**Solution:**
- Added optional `pages` parameter to `extract_images_from_pdf()` to access `textAnnotation.width`/`height` per page
- Added `page_dims` lookup extracting `ta["width"]` and `ta["height"]` from the full Yandex response
- Compute `scale_x = ta["width"] / page.rect.width`, `scale_y = ta["height"] / page.rect.height`
- Convert pixel coords to points: `point = pixel / scale`
- Fallback to A4@300DPI (2480×3508 px) when page dimensions unavailable
- Updated `process_file()` to pass `pages=pages` to `extract_images_from_pdf()`

### Fix #2 (CRITICAL): Insert images into output Markdown

**Problem:** Images were extracted and saved to `image/` but never referenced in the output `.md` file.

**Solution:**
- Added new function `_insert_images_into_md(md_text, extracted)` after `_crop_and_save_image()`
- Sorts images by page and figure number
- Appends `![fig_N](image/fig_N.ext)` references at the end of the markdown text with a `---` separator
- Called in `process_file()` immediately after `extract_images_from_pdf()`

### Fix #3 (HIGH): Retry with exponential backoff for OCR API

**Problem:** `send_to_yandex_ocr()` only retried on HTTP 429 with a fixed 5s delay. Per architecture §7, needs 3 retries with exponential backoff for all 4xx/5xx.

**Solution:**
- Replaced the 429-only retry with a proper `for attempt in range(max_retries)` loop (max_retries=3)
- Retry conditions: httpx.TimeoutException, httpx.HTTPError, and any HTTP 4xx/5xx response
- Exponential backoff: `delay = (4 ** attempt) + 1` → ~1s, ~5s, ~17s
- Falls through to `RuntimeError` after all retries exhausted

### Fix #4 (HIGH): Table deduplication

**Problem:** `parse_yandex_json_to_md()` concatenates the `markdown` field text and structured `tables` without checking for overlap, producing duplicate tables.

**Solution:**
- Added `existing_md: str = ""` parameter to `_parse_tables_from_json()`
- Before appending each parsed table, normalizes and checks if the first 2 lines (header + separator) already exist in `existing_md`
- Skips duplicate tables with a debug log message
- Updated `parse_yandex_json_to_md()` to pass `existing_md=md_text`

### Fix #5 (MEDIUM): Keep intermediate DOCX→PDF

**Problem:** The `finally` block in `process_file()` deleted the intermediate PDF from DOCX conversion. Per SPEC_YA and architecture §9, it should be preserved in `tmp/<file_name>/`.

**Solution:**
- Removed `Path(pdf_path).unlink()` call from the `finally` block
- Added comment explaining the PDF stays in `tmp/<file>/` per SPEC_YA

## Validation

- `python3 -c "py_compile.compile(...)"` — syntax OK, no errors
- 10 automated checks pass (2 per fix)
- No existing tests in the project
- Architecture unchanged: all signatures remain within documented constraints

## Verification

- Fix #1: ADR-2 formula (`scale_x = ta["width"] / page.rect.width`) is now applied
- Fix #2: `_insert_images_into_md()` called with extracted images in `process_file()`
- Fix #3: `4 ** attempt` backoff formula present, retries on non-200 responses
- Fix #4: `existing_md` parameter with `key in existing_md` dedup logic
- Fix #5: No `.unlink()` call for the intermediate PDF

## Known Limitations

- Image insertion (Fix #2) adds images at the end of the document rather than at page-specific positions. Full coordinate-based insertion requires correlating Yandex text block positions with image bounding boxes, which is a separate feature beyond the scope of this fix.
- Exponential backoff uses simple `time.sleep()` which blocks the event loop — acceptable for a synchronous CLI tool.
