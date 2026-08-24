# JSON-Native Pipeline Architecture

**Status:** Designed (architect) — not implemented  
**Target:** `docs/architecture/json-native-pipeline.md`  
**Related:** ADR-010, `rag-v2-architecture.md`, `table-id-marker-contract.md`, `.hermes/STATE.md`

---

## 1. Problem Statement

The current pipeline (`firmware/src/create_markdown.py`) processes documents through an intermediate `raw.md` artifact:

```
Yandex JSON → parse_yandex_json_to_md() → raw.md → postprocessing → final.md
```

This design has critical defects (documented in `.hermes/STATE.md` and `table-id-marker-contract.md`):

1. **Multi-page tables not stitched**: `_stitch_continuation_tables` only compares adjacent tables; caption-less continuations spanning >2 pages (e.g., Table 4.1 in СП 52 — 4 pages) fail because the header on page N doesn't match the footer on page N-2.

2. **Marker binding is fragile**: table↔vision-image binding relies on `<!-- t_pN_M -->` markers threaded through parse→merge→postprocess; any step that reorders lines breaks the binding. JSON-native binding is structural (`page, table_index`) — no markers.

3. **ID markers cost**: 7 markers remain on first chunk (80K chars + 7 vision refs) because AI fails to strip them on large input. Markers are a crutch for lossy JSON→raw.md conversion.

4. **Spatial-pass floor logic**: `bottom >= 0.9*page_height` fails on СП 52 (tables end at 79–86% due to footer) → 2-page tables don't group into `component_images`.

5. **Heading extraction is lossy**: `layoutType=SECTION_HEADER` has no hierarchy; regex on flattened text loses structure.

**User Decision (2026-08-15, `.hermes/STATE.md`):** Build a **JSON-native pipeline** that:
- Extracts maximum structure from Yandex JSON (`textAnnotation`: `blocks[]`, `tables[]`, `pictures[]`)
- Operates directly on PDF via `fitz` for image/table extraction
- **Eliminates `raw.md` entirely** — Markdown is rendered once, at the end
- Removes ID markers and their mechanics (`_inject_table_ids`, marker emission, AI stripping)

---

## 2. Target Data Model (Structured In-Memory Format)

The core insight: **Yandex JSON already contains all structure**. We build an in-memory document model, then render Markdown once.

```python
@dataclass
class Document:
    pages: list[Page]
    headings: list[Heading]           # from JSON blocks (layoutType + number pattern + geometry)
    tables: list[Table]               # from JSON tables[] + spatial grouping
    pictures: list[Picture]           # from JSON pictures[] + PDF extraction
    metadata: DocumentMetadata

@dataclass
class Page:
    index: int                        # 0-based
    width: float                      # Yandex pixels
    height: float
    blocks: list[Block]               # filtered: not inside table bbox
    tables: list[Table]               # subset of document.tables on this page
    pictures: list[Picture]           # subset on this page

@dataclass
class Block:
    y: float                          # min Y of bbox (sort key)
    layout_type: str                  # SECTION_HEADER, LIST, TEXT, CAPTION, UNSPECIFIED
    text: str                         # full text (joined lines)
    bbox: BBox
    heading_level: int | None         # 1=##, 2=###, 3=####, 4=##### (if SECTION_HEADER + number pattern)
    heading_number: str | None        # "3", "3.2", "3.2.1", "3.2.1.1"

@dataclass
class Table:
    page: int                         # page index
    table_index: int                  # index in ta["tables"] on this page (stable ID source)
    bbox: BBox
    cells: list[Cell]                 # structured from JSON
    caption: str | None               # from blocks (caption block above table)
    table_num: str | None             # extracted from caption: "4.1", "Б.1"
    is_continuation: bool             # caption matches "Продолжение/Окончание таблицы N"
    component_images: list[str] | None  # populated by spatial grouping
    # Derived:
    md_lines: tuple[int, int] | None  # (start_line, end_line) in final MD (set during render)

@dataclass
class Picture:
    page: int
    bbox: BBox
    score: float
    image_path: str | None            # set after PDF extraction

@dataclass
class BBox:
    x0: float; y0: float; x1: float; y1: float  # Yandex pixels

@dataclass
class Cell:
    row: int; col: int; rowspan: int; colspan: int; text: str
```

**Key invariant:** `Table.table_index` = position in `ta["tables"]` array. Same index used by `extract_table_images()` → **ID identity guaranteed** (`t_p{page+1}_{table_index}`) without markers in MD.

---

## 3. Pipeline Stages

```
┌─────────────────────────────────────────────────────────────────────────┐
│ STAGE 0: Load Yandex JSON                                               │
│   Input:  pages = send_to_yandex_ocr(pdf) | load from file             │
│   Output: pages: list[dict] (raw JSON)                                 │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ STAGE 1: Build Structured Document Model (parse_yandex_json_to_model)  │
│   1.1 Extract headings from blocks[]:                                  │
│       - Filter: layoutType=SECTION_HEADER                              │
│       - Number pattern: ^\d+(\.\d+)*\.?\s  +  length<100 + rel_x<0.50  │
│       - Y-isolation: no other block at same Y (±15px)                  │
│       - Level = depth of number (1→##, 2→###, 3→####, 4→#####)         │
│       - Handle "split headings": number on one line, title on next     │
│   1.2 Build tables[] from ta["tables"]:                                │
│       - Convert cells to structured matrix                             │
│       - Find caption: nearest block above with "Таблица" or CAPTION    │
│       - Extract table_num from caption                                 │
│       - Detect continuation: caption matches "Продолжение/Окончание"   │
│   1.3 Build pictures[] from ta["pictures"]                             │
│   1.4 Filter blocks: exclude those inside table bbox                   │
│   Output: Document (structured in-memory model)                        │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ STAGE 2: Multi-page Table Stitching (deterministic, no markers)        │
│   Input:  Document.tables (ordered by page, then table_index)          │
│   Algorithm:                                                            │
│     Single pass, maintaining the CURRENT GROUP HEAD:                   │
│                                                                         │
│     current_head = None     # head table of the current multi-page group│
│     current_num  = None     # table_num of current_head (authoritative) │
│                                                                         │
│     for table in ordered_tables:                                       │
│         has_caption = bool(table.caption)                              │
│         is_cont = table.is_continuation                                │
│                                                                         │
│         if has_caption and not is_cont:                                │
│             # NEW TABLE with its own caption → starts a new group      │
│             current_head = table                                       │
│             current_num  = table.table_num                             │
│                                                                         │
│         elif is_cont and current_num is not None                       │
│              and table.table_num == current_num:                       │
│             # EXPLICIT CONTINUATION: caption contains                  │
│             # "Продолжение/Окончание таблицы N" with MATCHING number.  │
│             # This is the AUTHORITATIVE signal — geometry is NOT used. │
│             # The continuation belongs to current_head's group.        │
│             table.caption = current_head.caption  # propagate caption  │
│             current_head.add_member(table)  # add to group             │
│                                                                         │
│         elif (not has_caption) and current_head is not None            │
│              and geometry_says_same_table(current_head.last_member,    │
│                                           table):                      │
│             # CAPTION-LESS table: only structural/geometric evidence.  │
│             # Conservative: column count match + horizontal overlap    │
│             # + vertical adjacency. On uncertainty → new group + warn. │
│             current_head.add_member(table)                             │
│                                                                         │
│         else:                                                          │
│             # Unrelated table → new group                              │
│             current_head = table                                       │
│             current_num  = table.table_num  # may be None              │
│                                                                         │
│     # After pass: for each group with >1 member, set component_images │
│     # (image_path filled in Stage 3)                                   │
│                                                                         │
│     geometry_says_same_table(prev, curr) → bool:                       │
│         # Conservative check for caption-less tables:                  │
│         1. prev.column_count == curr.column_count                      │
│         2. horizontal overlap: bbox.x ranges intersect ≥ 50%           │
│         3. curr.page == prev.page + 1 (adjacent pages only)            │
│         4. prev.bbox.bottom / prev.page_height ≥ 0.75 (looser)         │
│         All 4 must pass.                                               │
│                                                                         │
│   Output: Document with tables[].component_images populated per group  │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ STAGE 3: PDF Extraction (tables + pictures)                            │
│   Input:  pdf_path, Document.tables, Document.pictures                 │
│   Using: fitz (PyMuPDF)                                                │
│   3.1 Extract table images:                                            │
│        For each table:                                                  │
│          - Scale bbox: Yandex px → PDF points via ta[width]/height     │
│          - Include caption area (dynamic top margin via caption block) │
│          - Render PNG at 200 DPI → image/table_N.png                   │
│          - Set table.image_path                                        │
│   3.2 Extract pictures:                                                │
│        Match pictures[] bbox to PDF embedded images (IoU)              │
│        Extract → image/fig_N.png, set picture.image_path               │
│   Output: Document with all image_paths populated                      │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ STAGE 4: Single-Pass Markdown Render (render_document_to_md)           │
│   Input:  Document (fully populated)                                   │
│   Algorithm:                                                            │
│     For each page in order:                                            │
│       Merge page.blocks + page.tables + page.pictures by Y-coordinate  │
│       For each element in Y-order:                                     │
│         If Block:                                                       │
│           If heading_level: render as "#" * (level+1) + " " + text    │
│           Elif LIST: render as "- " + text                             │
│           Else: render as plain text                                   │
│         If Table:                                                       │
│           Convert cells to Markdown table                              │
│           If table.caption: render "*caption*" before table            │
│           Record md_lines = (current_line, end_line) in table          │
│         If Picture:                                                     │
│           Render "![caption](image/fig_N.png)"                         │
│     Join pages with "\n\n"                                             │
│   Output: final_markdown (string) — NO INTERMEDIATE raw.md             │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ STAGE 5: Script Postprocessing (run_script_postprocess)                │
│   Input:  final_markdown, Document.tables (for component_images)       │
│   Operations (same order, no ID marker logic):                         │
│     1. convert_html_tables (if any HTML tables remain)                 │
│     2. merge_tables (text-based, now with correct structure)           │
│     3. _merge_by_component_images (uses tables[].component_images)     │
│     4. cleanup_latex                                                   │
│     5. rename_images                                                   │
│     6. fix_image_captions                                              │
│     7. fix_notes                                                       │
│     8. fix_table_fig_labels                                            │
│     9. fix_ocr_artifacts                                               │
│    10. fix_latex_caret_spaces                                          │
│   Output: final_markdown (post-processed)                              │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ STAGE 6: AI Postprocessing (optional, --ai)                            │
│   Input:  final_markdown                                               │
│   Changes from current:                                                 │
│     - NO ID markers in input (removed)                                 │
│     - NO marker stripping logic                                        │
│     - NO heading detection (already correct)                           │
│     - ONLY: table correction (vision refs passed by structure) +       │
│       LaTeX/OCR cleanup + missing captions                             │
│   How AI binds to vision tables without markers:                       │
│     - Pass vision tables as named references:                          │
│       "=== Vision Table: Table 4.1 (pages 14-17) ==="                  │
│       + vision markdown                                                │
│     - AI matches by caption/table_num in prompt context                │
│   Output: final_markdown                                               │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ STAGE 7: RAG Indexing (optional, --rag)                                │
│   Input:  final_markdown, Document (headings, tables, pictures)        │
│   Uses: Existing RAG v2 pipeline (build_rag_jsonl_v2, asset registry)  │
│   No changes needed — works on final MD + image/                       │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Key Design Decisions

### 4.1 No ID Markers in Markdown

**Current:** `<!-- t_pN_M -->` emitted in parse, recalculated in postprocess, stripped by AI.

**New:** Markers never exist. Table identity = `(page, table_index)` from JSON array position.  
AI binding: vision tables passed as structured context with caption/table_num, not marker IDs.

**Rationale:** Markers are a workaround for lossy JSON→MD conversion. With structured model, identity is preserved without text pollution.

### 4.2 Multi-page Table Stitching by Geometry + Number

**Current:** `_stitch_continuation_tables` on MD text (fragile, only adjacent).

**New:** Deterministic grouping in Stage 2 using:
- Spatial: previous table bottom ≥ 90% page height (configurable)
- Semantic: continuation caption must match head's `table_num`
- Caption-less tables: auto-grouped spatially
- Cross-page: works for N pages (not just 2)

**Rationale:** Fixes "Table 4.1" 4-page case. Geometry is ground truth; captions are OCR-noisy.

### 4.3 Heading Extraction in Stage 1 (from JSON, not MD)

**Current:** `_extract_headings_from_json` runs after parse, then `_apply_headings_to_md` does string replace on `**text**`.

**New:** Headings extracted once from JSON blocks with geometry → stored in `Block.heading_level`. Rendered directly as `#` headings in Stage 4. No string replacement, no fallback.

**Rationale:** JSON has geometry (rel_x, Y-isolation) that MD loses. One authoritative extraction.

### 4.4 Single Markdown Render

**Current:** `raw.md` → postprocess → `final.md` → (maybe AI) → `final.md`.

**New:** One render pass (Stage 4) after all structure is resolved. Postprocess (Stage 5) only cleans up formatting.

**Rationale:** Eliminates intermediate artifact, ensures tables/headings/pictures are in correct positions from the start.

### 4.5 AI Stage Simplified

**Current:** Complex marker resolution, chunking with marker awareness, post-check for unstripped markers.

**New:** 
- Input MD has no markers
- Vision tables passed as `{"caption": "...", "table_num": "4.1", "markdown": "..."}`
- Prompt: "Correct the following tables using the vision references by caption/table number"
- No post-check needed

---

## 5. Migration Plan (Incremental)

Per user requirement: **NOT a big-bang rewrite**. Incremental migration with working pipeline at each step.

### Phase 1: Add Structured Model + Stage 1 (parse_yandex_json_to_model)
- New function: `parse_yandex_json_to_model(pages) -> Document`
- Keeps existing `parse_yandex_json_to_md` for backward compatibility
- Extracts headings, tables, pictures into dataclasses
- **Validation:** Compare headings/tables output with current parse on test documents

### Phase 2: Add Stage 2 (Table Stitching) + Stage 3 (PDF Extraction)
- New: `stitch_tables(document)`, `extract_table_images_from_model(pdf, document)`
- Populate `table.image_path` and `table.component_images`
- **Validation:** Check `component_images` for Table Б.1 (3 parts), Table 4.1 (4 parts)

### Phase 3: Add Stage 4 (Single-Pass Render)
- New: `render_document_to_md(document) -> str`
- Produces final MD directly (no raw.md)
- **Validation:** Diff against current `final.md` on СП 52, СП 89, ГОСТ 18410

### Phase 4: Remove ID Marker Mechanics
- Delete `_inject_table_ids()` entirely
- Remove marker emission from parse (but keep for backward compat flag)
- Remove marker logic from `run_script_postprocess`, AI stage
- Update `table-id-marker-contract.md` → mark superseded

### Phase 5: Simplify AI Stage
- Remove marker parsing/stripping
- Pass vision tables by caption/table_num
- Update prompt

### Phase 6: Cleanup & Tests
- Remove `raw.md` writes
- Remove `page_boundaries` from signatures where unused
- Update regression tests
- Document new architecture in `json-native-pipeline.md` (this file)

---

## 6. Interface Definitions

### 6.1 New Public Functions (added to create_markdown.py)

```python
# Phase 1
def parse_yandex_json_to_model(pages: list[dict]) -> Document:
    """Build structured document model from Yandex JSON pages."""
    ...

# Phase 2
def stitch_tables(document: Document) -> None:
    """Populate table.component_images via spatial + semantic grouping. Mutates document."""
    ...

def extract_table_images_from_model(
    pdf_path: str | Path,
    document: Document,
    img_dir: str | Path,
) -> None:
    """Extract all table images, set table.image_path. Mutates document."""
    ...

def extract_pictures_from_model(
    pdf_path: str | Path,
    document: Document,
    img_dir: str | Path,
) -> None:
    """Extract pictures, set picture.image_path. Mutates document."""
    ...

# Phase 3
def render_document_to_md(document: Document) -> str:
    """Single-pass render of structured document to final Markdown."""
    ...

# Phase 5 (AI)
def ai_postprocess_json_native(
    md_text: str,
    config: dict,
    file_label: str,
    vision_tables: list[dict],  # [{"caption", "table_num", "markdown"}, ...]
) -> str:
    """AI postprocess without ID markers. Vision tables passed by caption."""
    ...
```

### 6.2 Modified Functions

| Function | Change |
|----------|--------|
| `process_file()` | Add `use_json_native` flag; new code path |
| `run_script_postprocess()` | Remove `page_boundaries` param; remove marker logic |
| `extract_table_images()` | Keep for backward compat; new model-based version above |
| `parse_yandex_json_to_md()` | Keep for backward compat; mark deprecated |

### 6.3 Data Classes (in create_markdown.py or new `models.py`)

```python
@dataclass
class Document:
    pages: list[Page]
    headings: list[Heading]
    tables: list[Table]
    pictures: list[Picture]
    metadata: DocumentMetadata

@dataclass
class Page:
    index: int
    width: float
    height: float
    blocks: list[Block]
    table_indices: list[int]  # indices into document.tables
    picture_indices: list[int]

@dataclass
class Block:
    y: float
    layout_type: str
    text: str
    bbox: BBox
    heading_level: int | None
    heading_number: str | None
    is_table_caption: bool
    is_continuation_caption: bool

@dataclass
class Table:
    page: int
    table_index: int          # index in ta["tables"] — stable ID
    bbox: BBox
    cells: list[Cell]
    caption: str | None
    table_num: str | None
    is_continuation: bool
    component_images: list[str] | None
    image_path: str | None
    md_lines: tuple[int, int] | None  # set during render

@dataclass
class Picture:
    page: int
    bbox: BBox
    score: float
    image_path: str | None

@dataclass
class BBox:
    x0: float; y0: float; x1: float; y1: float

@dataclass
class Cell:
    row: int; col: int; rowspan: int; colspan: int; text: str

@dataclass
class Heading:
    page: int
    level: int
    number: str
    text: str
    full_text: str
    y: float

@dataclass
class DocumentMetadata:
    source_file: str
    page_count: int
    ...
```

---

## 7. Configuration Changes

### 7.1 New Config Options (in `rag_config.yaml` or `config_ai.yaml`)

```yaml
# Table stitching thresholds
table_stitching:
  require_table_num_match: true      # explicit "Продолжение/Окончание таблицы N" must match head's table_num (authoritative — no geometry)
  bottom_threshold_ratio: 0.75       # geometry fallback ONLY for caption-less continuations (conservative; no join + warning when uncertain)
  max_gap_pages: 1                   # max blank pages between components (future)
```

### 7.2 CLI Flag

```bash
# New flag for incremental adoption
--json-native    # Use new JSON-native pipeline (experimental)
# Default: false (current pipeline)
```

---

## 8. Acceptance Criteria (Regression Suite)

| Document | Current Defects | JSON-Native Must Fix |
|----------|-----------------|---------------------|
| **СП 52.13330.2016** | Table 4.1 (4 pages) → 2 separate tables; 7 ID markers remain on chunk 0 | Single Table 4.1 with 4 `component_images`; no markers in output |
| **СП 89.13330.2016** | Table Б.1 (3 pages) works; Table Ж.1 (3 pages) works; Table И.1 (2 pages) works | Same correctness; no markers |
| **ГОСТ 18410-73** | Table 2 (3 unrelated) falsely grouped by `table_num` | No false grouping; each Table 2 separate |
| **All** | `raw.md` exists as intermediate | No `raw.md` written |

**Test Commands:**
```bash
# Full regression (no gap_filling)
cd firmware && python3 -m pytest tests/ -q --ignore=tests/test_gap_filling.py

# Specific document test
python3 create_markdown.py -i "СП 52.pdf" --json-native --ai --rag
# Verify: Table 4.1 has 4 component_images; no <!-- t_p --> in output
```

---

## 9. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| JSON structure varies across Yandex API versions | Medium | High | Stage 1: defensive parsing with fallbacks; log unknown fields |
| Heading detection misses non-standard headings | Medium | Medium | Keep `_apply_headings_to_md` as fallback for `--json-native=false` |
| Caption-less continuation without semantic signal (no «Продолжение/Окончание таблицы N») | Medium | Medium | Conservative geometry fallback (column count + adjacency); no join + warning when uncertain |
| AI stage without markers needs new prompt tuning | Medium | Medium | Phase 5: A/B test with marker-based on sample docs |
| Performance: building model + render vs current parse | Low | Low | Model is in-memory; render is single pass; comparable |

---

## 10. Deliverables

1. **Architecture Document:** This file → `docs/architecture/json-native-pipeline.md`
2. **Implementation Report:** `workflows/t_f82ca6a2/implementation-report.md` (after coder completes)
3. **Git Branch:** `refactor/json-pipeline` with only `firmware/src/create_markdown.py` changes
4. **Updated Contracts:** `table-id-marker-contract.md` marked superseded; new section in `rag-v2-architecture.md`

---

## 11. Out of Scope (Explicitly NOT in this design)

- Changing `rag_config.yaml` or `config_ai.yaml` schemas (user forbids)
- Changing `table_images.json` schema (user forbids)
- Rewriting `firmware/src/create_markdown.py` from scratch (incremental only)
- New dependencies beyond `transformers` (already in RAG v2)
- Big-bang migration — each phase must keep pipeline working

---

*End of Architecture Document*