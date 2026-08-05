# Implementation report — t_5e60b7c3

## Summary

Two changes to `process_file()` in `firmware/src/pipeline.py`:

1. **Chunking for gap-filling** — the gap-filling stage no longer sends the
   whole `gap_input` (prompt + full markdown + all `table_*.md`) in a single
   `_call_ai_api()` call. If `len(gap_input) > AI_MAX_CHARS` (24000), the input
   is split with the existing `_chunk_text()` (same helper `ai_postprocess()`
   uses), each chunk is processed with a per-chunk context label
   `"{file_stem} [gap ч.{i+1}]"`, and successful results are joined with
   `"\n\n"`. Small inputs keep the single-call path, now also passing the
   `file_stem` context label.

2. **Stage reorder** — gap-filling now runs **before** `ai_postprocess()`
   (Этап 6 → gap-filling, Этап 7 → ai_postprocess). Rationale per task: first
   bring tables up to par (gap-filling), then run the final AI cleanup over the
   whole document. The output of gap-filling feeds `ai_postprocess`.

## Files changed

- `firmware/src/pipeline.py`
  - `process_file()`: moved the `use_ai and use_ai_table` gap-filling block
    before the `use_ai` `ai_postprocess` block; renumbered comments
    (`# Этап 6: AI-постобработка ТАБЛИЦ / Gap-filling (если --ai + --ai-table)`,
    `# Этап 7: AI-постобработка ИТОГОВОГО MD (если --ai)`).
  - Added chunking inside gap-filling exactly per task pseudocode:
    ```python
    if len(gap_input) > AI_MAX_CHARS:
        chunks = _chunk_text(gap_input, AI_MAX_CHARS)
        results = []
        for i, chunk in enumerate(chunks):
            result = _call_ai_api(chunk, ai_cfg, f"{file_stem} [gap ч.{i + 1}]")
            if result:
                results.append(result)
        gap_result = "\n\n".join(results) if results else None
    else:
        gap_result = _call_ai_api(gap_input, ai_cfg, file_stem)
    ```
- `firmware/src/test_gap_filling.py`
  - `_run_process_file()` helper extended with `md_text` and `order` kwargs
    (call-order recording between `_call_ai_api` and `ai_postprocess`).
  - New tests:
    - `test_gap_filling_single_call_context` — small input → single call,
      context = `file_stem`.
    - `test_gap_filling_chunks_large_input` — big input → multiple calls, each
      chunk ≤ `AI_MAX_CHARS`, per-chunk context labels, no content loss
      (headers/paragraphs/table text present across chunks), joined results
      written to final `.md`.
    - `test_gap_filling_chunk_partial_results` — failed chunks are skipped,
      only successful results are glued.
    - `test_gap_filling_runs_before_ai_postprocess` — order assertion:
      `["gap_filling", "ai_postprocess"]`.
    - `test_gap_filling_output_feeds_ai_postprocess` — `ai_postprocess`
      receives the gap-filled text, not the original md.

## Validation

- `python3 -m pytest test_gap_filling.py -v` → 9 passed
- Full suite: `python3 -m pytest -v` (test_ai_table, test_gap_filling,
  test_latex_caret_spaces, test_no_hardcoded_prompts) → **44 passed**
- `python3 -m py_compile pipeline.py test_gap_filling.py` → OK

## Known limitations

- `_chunk_text()` keeps tables and code fences intact, but chunk boundaries may
  fall between the section headers (`=== Markdown-файл ===`) and their content;
  this is inherent to the requested reuse of `_chunk_text()` and does not lose
  content — each chunk still carries the shared prompt prefix in the first
  chunk only (same behaviour as the existing `ai_postprocess()` chunking).
- No API key / real model calls were made during testing; `_call_ai_api` is
  mocked. Live behaviour is unchanged for small files (single call).

## Constraints honored

- `config_ai.yaml` NOT modified (working-tree diff for it predates this task,
  from t_00995ea0).
- Gap-filling and `ai_postprocess` logic not rewritten — only chunking wrapper
  and stage order changed.
- No production code placed in `workflows/` (report only).
