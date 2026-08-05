# Review — t_f2ea1b74

## Verdict
**PASS**

## Scope
Review of parent task t_b77b058f: per-chunk vision tables + AI_MAX_CHARS=80K + post-verification of ID markers. File: `firmware/src/pipeline.py` plus tests.

## Requirements

| # | Requirement | Expected | Actual | Status |
|---|------------|----------|--------|--------|
| 1 | `AI_MAX_CHARS = 80000` | `AI_MAX_CHARS = 80000` | Line 53: `AI_MAX_CHARS = 80000` | PASS |
| 2 | `_table_id_sort_key` exists | >=2 occurrences | 3: definition (2513) + 2 uses (3593, 3638) | PASS |
| 3 | Log "Чанков с эталонами" | 1 occurrence in f-string | Line 3631 | PASS |
| 4 | Post-check "Неснятые ID-маркеры" | 1 occurrence | Lines 3637-3639 | PASS |
| 5 | `re.findall.*t_p` for ID extraction | >=2 occurrences | 2: chunk matching (3589) + post-check (3634) | PASS |
| 6 | `vision_tables[` dict access | >=1 occurrence | 2: population (3553) + lookup (3595) | PASS |
| 7 | Syntax valid | `ast.parse` OK | OK | PASS |
| 8 | Import works | `import pipeline` OK | OK | PASS |
| 9 | Tests: table ID markers + chunk/bbox | 24 tests pass | 24 passed in 0.21s | PASS |

## Architecture

Implementation follows the approved design:
- `vision_tables` dict populated from `table_*.md` files by extracting `<!-- t_pN_M -->` from first line.
- `_table_id_sort_key` sorts IDs by (page, index) numerically.
- md_text chunked via `_chunk_text(md_text, AI_MAX_CHARS)`.
- For each chunk, IDs extracted via `re.findall(r"<!--\s*(t_p\d+_\d+)\s*-->")` — same regex used for chunk matching and post-check.
- Only matching vision tables (by ID) are appended to each chunk as "=== Эталонные таблицы ===".
- Post-check scans final output for remaining `<!-- t_pN_M -->` markers and logs a warning if any remain.
- Checkpointing preserved from previous implementation.
- Context overlap (3 lines from previous chunk) intact.

## Tests

- `test_table_id_markers.py`: 12 tests covering `_table_id_sort_key` (ordering, multidigit, unknown format), `_inject_table_ids` (various scenarios), `run_script_postprocess` integration.
- `test_chunk_overlap_table_bbox.py`: 12 tests covering chunk overlap, bbox extension, section-aware chunking, oversized section split.
- All 24 tests PASS.
- Pre-existing `test_gap_filling.py` failure (8/9) is unrelated (gap-filling feature not yet implemented) and pre-dates this task — documented by parent.

## Findings

No issues found. Implementation is clean and correct:

1. **AI_MAX_CHARS** properly raised to 80000 (line 53), used consistently across `_split_oversized_section`, `_chunk_text`, and chunk assembly.
2. **_table_id_sort_key** correctly parses `t_p{page}_{index}` with numeric sort, fallback (0,0) for unknown formats.
3. **Per-chunk matching** efficiently pairs only relevant vision tables with each chunk — avoids bloating chunks with unnecessary table data.
4. **Post-check** is a safety net catching any ID markers the AI failed to resolve — logs warning with sorted marker list.
5. **Regex consistency** — identical pattern used for extraction and post-check.
6. **Edge cases handled**: table files without ID markers skipped, chunks without markers get no table data, missing vision_tables entries gracefully skipped.

## Known Risks

- Pre-existing `test_gap_filling.py` 8/9 failures (gap-filling feature not implemented) — unrelated, documented in parent task metadata.

## Notes

Parent task t_b77b058f summary confirms all checks independently. This review is consistent with the parent's self-reported acceptance criteria.
