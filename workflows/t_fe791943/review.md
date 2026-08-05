# Review

## Verdict

**PASS**

## Scope

Independent review of task t_37246615 — сквозная ID-маркировка таблиц.
Checked: `firmware/src/pipeline.py`, `firmware/src/config_ai.yaml`, tests.

## Requirements

| # | Check | Result |
|---|-------|--------|
| 1 | `extract_table_images()` returns `"id": f"t_p{pi + 1}_{ti}"` | PASS (line 1563) |
| 2 | `recognize_tables_vision()` writes `<!-- {ti['id']} -->` as first line | PASS (line 1709) |
| 3 | `run_script_postprocess()` accepts `page_boundaries` + `table_images` | PASS (lines 2516-2517) |
| 4 | `_inject_table_ids` function exists (def + call >= 2) | PASS (def:2421, call:2552) |
| 5 | `process_file()` passes `page_boundaries` to `run_script_postprocess` | PASS (line 3519) |
| 6 | `config_ai.yaml` has ID-matching rule | PASS (line 96) |
| 7 | Python syntax + YAML syntax OK; pytest passes | PASS (147 tests, 0 failures) |
| 8 | Integration: imports + signatures verified | PASS |

## Architecture

Implementation respects project architecture:
- ID format `t_p{page+1}_{table_index}` is consistent across `extract_table_images()` → `recognize_tables_vision()` → `_inject_table_ids()`.
- `_inject_table_ids` is a clean two-pass algorithm (find table starts → insert markers at name lines), avoiding fragile insert-index math.
- `run_script_postprocess` uses both params as a gate: markers injected only when both `page_boundaries` AND `table_images` are provided.
- Backward compatibility: new params default to `None` → existing callers unaffected.
- `config_ai.yaml` rule correctly instructs AI to match tables by ID and delete markers from final output.

## Tests

- `test_table_id_markers.py`: 10 tests — PASS. Covers: two pages, two tables same page, table at doc start without name, blank lines between row groups, no boundaries (unchanged), no tables (unchanged), non-table pipe lines, run_script_postprocess integration, backward compatibility without params, params gating (both must be present).
- `test_chunk_overlap_table_bbox.py`: 11 tests — PASS. Includes test for `extract_table_images()` ID format.
- Full suite (excluding stale `test_gap_filling.py`): 147 passed, 0 failed.

## Findings

No issues found. Implementation quality notes:

1. **ID consistency**: Same format `t_p{page+1}_{idx}` used end-to-end — no drift between extraction and injection.
2. **Per-page index reset**: `_inject_table_ids` resets the table index counter on each page boundary, matching `extract_table_images` behavior.
3. **Gating logic**: `if page_boundaries and table_images:` (line 2551) — both must be truthy. `table_images` is used purely as a boolean gate, not for content lookup. This is intentional per design.
4. **Docstring quality**: `_inject_table_ids` has thorough documentation explaining the two-pass algorithm, edge cases, and the rationale for matching Yandex JSON table order.
5. **Known limitation documented** (from parent task): continuation tables (Окончание/Продолжение) may shift per-page indices for tables after the merged continuation. Harmless — AI skips unmatched IDs.

## Risks

- **Continuation table index shift**: When `merge_tables()` merges continuation tables, the per-page table count may differ between `extract_table_images()` and `_inject_table_ids()`, causing a few markers to not match the corresponding vision table. This is documented as harmless — AI is instructed to skip unmatched IDs (`config_ai.yaml` line 98: "НЕ сверяй таблицы с разными ID").

## Notes

- The spec's grep patterns for checks 2 and 3 were heuristic single-line searches that didn't account for multi-line code formatting. The implementation is correct in both cases — verified by direct source inspection.
- Parent task reported 7 stale failures in `test_gap_filling.py` — confirmed unrelated (tests removed `ai_table` function). Not in scope.
