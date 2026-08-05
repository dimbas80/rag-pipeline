# Review — t_3208b1ca

## Verdict

**PASS**

## Scope

Reviewed implementation of ADR-007: section-aware `_chunk_text()` + `ai_postprocess` prompt update.

Files reviewed:
- `firmware/src/pipeline.py` — `_chunk_text()` (line 2549), `_split_oversized_section()` (line 2468), `SECTION_BOUNDARY_RE` (line 2463)
- `firmware/src/config_ai.yaml` — `ai_postprocess.prompt` (lines 55–87)
- `firmware/src/test_chunk_overlap_table_bbox.py` — 6 new tests (lines 186–299)

Architecture documents verified against:
- `docs/architecture/decision-records/adr-007-section-aware-chunking.md`
- `docs/architecture/decision-records/adr-007-prompt-spec.md`
- `docs/architecture/architecture.md`

## Requirements Compliance

| Requirement | Status | Evidence |
|---|---|---|
| Two-phase section-aware chunking | PASS | `_chunk_text()` lines 2549–2599: Phase 1 (boundary detection) + Phase 2 (chunk assembly) |
| Regex captures `**N. ...**`, `**N.N. ...**`, `### N.N. ...`, `#### N.N.N. ...` | PASS | `SECTION_BOUNDARY_RE` line 2463–2465; verified by `test_section_headers_regex` |
| Old logic preserved as `_split_oversized_section()` | PASS | Lines 2468–2546, identical to old `_chunk_text()` logic |
| Fallback to old logic when no headers found | PASS | Line 2564–2566: `if not boundary_idx → _split_oversized_section()` |
| Prompt points 13–15 removed | PASS | Lines 70–86: no merge/notes/caption instructions |
| Table immobility prohibition added | PASS | Line 87: exact text from ADR-007-prompt-spec |
| All 91 tests pass | PASS | `91 passed in 10.41s` (verified independently) |

## Architecture Compliance

The implementation follows ADR-007 with two deviations from the **spec text** (not from the spec intent):

### Deviation 1: Mandatory dot after section number

**ADR-007 spec regex** (literal):
```
^(?:#{1,4}\s+\d+(?:\.\d+)*\s|\*\*\d+(?:\.\d+)*\s+[^*]+?\*\*)
```

**Implementation regex:**
```python
r"^(?:#{1,5}\s+\d+(?:\.\d+)*\.\s|\*\*\d+(?:\.\d+)*\.\s+[^*]+?\*\*)"
```

**Difference:** `\.\s` (dot-then-space) instead of just `\s` after the number.

**Assessment — JUSTIFIED CORRECTION.** The ADR-007 spec regex as literally written does NOT match its own examples:
- `## 4. ЗАЩИТА` — `\d+` matches `4`, then `\s` requires whitespace but next char is `.` → FAIL
- `**1. ВВЕДЕНИЕ**` — same problem with the dot after `1`

The implementation regex with mandatory `\.`:
- Correctly matches all real document headers (number always followed by dot)
- Excludes false positives like `**200 кА**` (number without dot — a table cell value, not a section header)
- Verified by `test_section_headers_regex`: all 7 `should_match` cases pass, all 6 `should_not_match` cases pass

### Deviation 2: `#{1,5}` instead of `#{1,4}`

**Assessment — JUSTIFIED EXTENSION.** ADR-8 (`_apply_headings_to_md`, already implemented in the working tree) renders level-4 headings as `#####`. The `#{1,5}` range ensures section-aware chunking works correctly with documents already processed by ADR-8 heading extraction. This is forward-compatible; a line of 5 `#`s with a numbered section identifier is unambiguously a heading.

### Prompt compliance

The actual prompt in `config_ai.yaml` matches `adr-007-prompt-spec.md` exactly:
- Points 13, 14, 15 removed ✓
- Old point 16 (OCR numbers) renumbered to 13 ✓
- Table immobility text added to «ЧТО НЕЛЬЗЯ» ✓

## Code Review

### `_chunk_text()` (lines 2549–2599)

**Correctness:** The two-phase algorithm is correctly implemented:
- Phase 1: boundary detection with `SECTION_BOUNDARY_RE`, building sections list, preamble handling ✓
- Phase 2: greedy section assembly with `max_chars` limit, oversized section delegation ✓
- Fallback: empty `boundary_idx` → `_split_oversized_section()` ✓

**Edge cases handled:**
- Preamble (text before first header) → separate section, first chunk ✓
- Single section > max_chars at start → split via `_split_oversized_section()` ✓
- Section > max_chars mid-stream → flush current, split oversized ✓
- No boundaries found → full fallback ✓
- Empty text → returns `[]` (both paths converge) ✓

**No logic errors, race conditions, or resource leaks detected.**

### `_split_oversized_section()` (lines 2468–2546)

Preserves original `_chunk_text()` logic intact: table tracking (`in_table`), code block tracking (`in_code`), paragraph-level splitting, table integrity preservation. No changes needed.

### `SECTION_BOUNDARY_RE` (lines 2463–2465)

Comments document the rationale for the dot requirement and `#{1,5}` range. Clear and informative.

### `config_ai.yaml` prompt (lines 55–87)

- Clean YAML, parses correctly
- All removed instructions are absent
- Table immobility prohibition is present and verbatim from spec
- New point 13 (OCR numbers) correctly renumbered
- No unrelated prompt changes

## Tests

### New tests (6 added, all pass)

| Test | Coverage |
|---|---|
| `test_section_aware_chunking` | Every chunk starts with section boundary |
| `test_section_aware_bold_subsection_headers` | Bold `**N.N. Title**` are boundaries |
| `test_no_boundaries_fallback` | No headers → fallback, 10 paragraphs → 10 chunks |
| `test_oversized_section_split` | Giant section split; tables intact; header preserved |
| `test_preamble_first_chunk` | Preamble in first chunk with first section |
| `test_section_headers_regex` | 7 should-match, 6 should-not-match — all correct |

Tests are well-designed, cover the critical paths, and include both positive and negative cases for the regex.

### Existing tests (85 tests, all pass)

No regressions. Full suite: `91 passed in 10.41s`.

## Findings

### Finding 1 — MEDIUM: ADR-007 spec regex is buggy; document should be updated

**Severity:** MEDIUM  
**Location:** `docs/architecture/decision-records/adr-007-section-aware-chunking.md`, line 42

**Problem:** The regex in the ADR-007 document:
```
^(?:#{1,4}\s+\d+(?:\.\d+)*\s|\*\*\d+(?:\.\d+)*\s+[^*]+?\*\*)
```
does not match the document's own examples. The `\s` after the number fails because real document headers have a dot between the number and the title text (e.g., `## 4. ЗАЩИТА`).

**Expected:** ADR-007 should record the working regex:
```
^(?:#{1,5}\s+\d+(?:\.\d+)*\.\s|\*\*\d+(?:\.\d+)*\.\s+[^*]+?\*\*)
```
with the mandatory dot and `#{1,5}` range, along with rationale for both decisions.

**Reason:** Architecture documents should reflect the as-built state. The current ADR-007 regex is a specification bug — future implementers following it literally would produce broken code.

### Finding 2 — LOW: ADR-007 document references stale line numbers

**Severity:** LOW  
**Location:** `docs/architecture/decision-records/adr-007-section-aware-chunking.md`, line 176

**Problem:** The implementation scope table references `firmware/src/test_chunk_overlap_table_bbox.py` as a file to be modified, but the line numbers for `_chunk_text()` (2334–2412) are stale after the implementation changed the file.

**Expected:** Either remove stale line numbers or update them to the current state (2549–2599 for `_chunk_text`, 2468–2546 for `_split_oversized_section`).

**Reason:** Stale line numbers in architecture documents cause confusion during future maintenance.

## Required Changes

None. Both findings are documentation issues, not implementation defects. Implementation is correct and ready for merge.

## Risks

- **Risk-1 (ADR-007):** Exotic section headers without numbered dots (rare GOST documents, non-standard formatting) will fall through to the old logic — acceptable, no regression.
- **Risk-2 (ADR-007):** Single paragraph > `AI_MAX_CHARS` with no blank lines cannot be split — same limitation as the old logic, not a regression.

## Notes

1. The coder's regex analysis in the implementation report is accurate: the ADR-007 spec regex as literally recorded is broken. The implementation's regex is the correct one and should be backported to the ADR document.
2. The `_split_oversized_section()` function is a clean preservation of the old `_chunk_text()` logic. If the old function had any pre-existing issues, they are preserved — but no new issues are introduced.
3. The prompt update is minimal and surgical: only the `ai_postprocess.prompt` block was changed, nothing else in `config_ai.yaml`.
