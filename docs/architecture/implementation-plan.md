# Implementation Plan: JSON-Native Pipeline Migration

**Task:** `t_f82ca6a2` — Design architecture for JSON-native pipeline (no `raw.md`)  
**Output:** `docs/architecture/json-native-pipeline.md` (completed) + this plan  
**Branch:** `refactor/json-pipeline` (only `firmware/src/pipeline.py` changes)  
**Method:** Incremental phases, working pipeline at each step

---

## Phase Overview

| Phase | Focus | New Functions | Modified Functions | Validation |
|-------|-------|---------------|-------------------|------------|
| 1 | Structured Model + Stage 1 Parser | `parse_yandex_json_to_model`, dataclasses | — | Unit tests T1-T5 from table-id-marker-contract |
| 2 | Table Stitching + PDF Extraction | `stitch_tables`, `extract_table_images_from_model`, `extract_pictures_from_model` | — | Table 4.1 (4 parts), Б.1 (3 parts), no false grouping |
| 3 | Single-Pass Render | `render_document_to_md` | — | Diff vs current `final.md` on 3 corpus docs |
| 4 | Remove ID Marker Mechanics | — | Delete `_inject_table_ids`, remove marker emission, update `run_script_postprocess` | No `<!-- t_p -->` in output; all tests pass |
| 5 | Simplify AI Stage | `ai_postprocess_json_native` | `process_file` (new code path) | AI corrects tables without markers |
| 6 | Cleanup & Integration | — | `process_file` flag `--json-native`, remove `raw.md` writes | Full regression: 312 tests pass |

---

## Phase 1: Structured Document Model + Stage 1 Parser

### 1.1 Add Dataclasses (top of pipeline.py, after imports)

```python
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class BBox:
    x0: float; y0: float; x1: float; y1: float

@dataclass
class Cell:
    row: int; col: int; rowspan: int; colspan: int; text: str

@dataclass
class Block:
    y: float
    layout_type: str
    text: str
    bbox: BBox
    heading_level: Optional[int] = None
    heading_number: Optional[str] = None
    is_table_caption: bool = False
    is_continuation_caption: bool = False

@dataclass
class Table:
    page: int
    table_index: int
    bbox: BBox
    cells: list[Cell]
    caption: Optional[str] = None
    table_num: Optional[str] = None
    is_continuation: bool = False
    component_images: Optional[list[str]] = None
    image_path: Optional[str] = None
    md_lines: Optional[tuple[int, int]] = None

@dataclass
class Picture:
    page: int
    bbox: BBox
    score: float
    image_path: Optional[str] = None

@dataclass
class Heading:
    page: int
    level: int
    number: str
    text: str
    full_text: str
    y: float

@dataclass
class Page:
    index: int
    width: float
    height: float
    blocks: list[Block]
    table_indices: list[int] = field(default_factory=list)
    picture_indices: list[int] = field(default_factory=list)

@dataclass
class Document:
    pages: list[Page]
    headings: list[Heading]
    tables: list[Table]
    pictures: list[Picture]
    metadata: dict = field(default_factory=dict)
```

### 1.2 Implement `parse_yandex_json_to_model(pages) -> Document`

**Algorithm:**
1. Iterate pages → extract `ta = page["result"]["textAnnotation"]`
2. **Headings from blocks[]:**
   - Filter `layout_type == "LAYOUT_TYPE_SECTION_HEADER"`
   - Number pattern: `^\d+(\.\d+)*\.?\s` (includes "1. ", "3.2.1. ")
   - Length < 100, words < 10
   - rel_x = x_left / page_width < 0.50
   - Y-isolation: no other block at same Y (±15px)
   - Level = number.count(".") + 1 (1→##, 2→###, 3→####, 4→#####)
   - Handle split headings: number on line N, title on line N+1 (same x, adjacent Y)
3. **Tables from ta["tables"][]:**
   - Convert cells via existing `_build_cell_matrix` + `_merge_table_headers`
   - Find caption: reuse `_find_table_caption_block` + `_caption_block_to_text`
   - Extract `table_num` via `_extract_table_num_from_text`
   - Detect continuation: `_is_continuation_caption(caption)`
   - Store `table_index` = position in `ta["tables"]` array
4. **Pictures from ta["pictures"][]:** bbox + score
5. **Filter blocks:** exclude blocks inside any table bbox (reuse `_block_in_table`)
6. Build `Page` objects with cross-references (indices into document.tables/pictures)
7. Return `Document`

**Unit Tests (from table-id-marker-contract.md §10.2):**
- T1: Page boundary regression — Table 2 on page 2 gets `table_index=1` not `0`
- T2: ID correspondence — same `pages` through parse + extract_table_images → matching IDs
- T3: No caption → marker before table rows
- T4: Array order ≠ Y-order → indices follow array order
- T5: End-to-end ID match with extract_table_images (mock fitz)

### 1.3 Validation Script

```bash
# Compare headings/tables between old parse and new model
python3 -c "
from pipeline import parse_yandex_json_to_md, parse_yandex_json_to_model
pages = load_test_pages()
md_old, _, _ = parse_yandex_json_to_md(pages=pages)
doc_new = parse_yandex_json_to_model(pages)
print(f'Old headings: {count_headings(md_old)}')
print(f'New headings: {len(doc_new.headings)}')
print(f'Old tables: {count_tables(md_old)}')
print(f'New tables: {len(doc_new.tables)}')
"
```

---

## Phase 2: Table Stitching + PDF Extraction

### 2.1 Implement `stitch_tables(document: Document) -> None`

**Algorithm (deterministic, no markers):**
```python
def stitch_tables(doc: Document) -> None:
    """
    Single-pass stitching using explicit continuation captions as
    the authoritative signal. Geometry used ONLY for caption-less tables.
    """
    # Order tables by page, then table_index
    ordered_tables = sorted(doc.tables, key=lambda t: (t.page, t.table_index))

    current_head: Optional[Table] = None
    current_num: Optional[str] = None  # table_num of head (authoritative)

    for table in ordered_tables:
        has_caption = bool(table.caption)
        is_cont = table.is_continuation

        if has_caption and not is_cont:
            # NEW TABLE with its own caption → starts a new group
            current_head = table
            current_num = table.table_num

        elif is_cont and current_num is not None and table.table_num == current_num:
            # EXPLICIT CONTINUATION: caption contains
            # "Продолжение/Окончание таблицы N" with MATCHING number.
            # This is the AUTHORITATIVE signal — geometry is NOT used.
            # The continuation belongs to current_head's group.
            table.caption = current_head.caption  # propagate caption
            if current_head.component_images is None:
                current_head.component_images = []
            # No explicit group storage needed; we propagate caption
            # and will regroup in populate_component_images()

        elif (not has_caption) and current_head is not None \
             and geometry_says_same_table(doc.pages[current_head.page],
                                          current_head, table):
            # CAPTION-LESS table: only structural/geometric evidence.
            # Conservative: column count match + horizontal overlap
            # + vertical adjacency. On uncertainty → new group + warn.
            table.caption = current_head.caption

        else:
            # Unrelated table → new group
            current_head = table
            current_num = table.table_num  # may be None


def geometry_says_same_table(page, prev, curr) -> bool:
    # Conservative check for caption-less tables:
    # 1. prev.column_count == curr.column_count
    # 2. horizontal overlap: bbox.x ranges intersect ≥ 50%
    # 3. curr.page == prev.page + 1 (adjacent pages only)
    # 4. prev.bbox.bottom / page.height ≥ 0.75 (looser threshold)
    # All 4 must pass.
    if curr.page != prev.page + 1:
        return False

    pc = count_columns(prev)
    cc = count_columns(curr)
    if pc != cc:
        return False

    # Horizontal overlap
    overlap = min(prev.bbox.x1, curr.bbox.x1) - max(prev.bbox.x0, curr.bbox.x0)
    if overlap <= 0:
        return False
    span = max(prev.bbox.x1 - prev.bbox.x0, curr.bbox.x1 - curr.bbox.x0)
    if overlap / span < 0.5:
        return False

    # Vertical: prev must be near bottom
    if prev.bbox.y1 < page.height * 0.75:
        return False

    return True
```

**Configurable threshold:** Read from `rag_config.yaml` → `table_stitching.bottom_threshold_ratio` (default 0.75 for caption-less; explicit continuations ignore geometry)

### 2.1b Implement `populate_component_images(doc: Document) -> None`

After stitching (caption propagation), group by head caption and populate `component_images`:

```python
def populate_component_images(doc: Document) -> None:
    # Group by caption (head's caption propagated to all members)
    groups: dict[str, list[Table]] = {}
    for t in doc.tables:
        if t.caption:
            groups.setdefault(t.caption, []).append(t)

    for caption, group in groups.items():
        if len(group) > 1:
            paths = [t.image_path for t in group if t.image_path]
            for t in group:
                t.component_images = paths
```

### 2.2 Implement `extract_table_images_from_model(pdf_path, document, img_dir)`

```python
def extract_table_images_from_model(pdf_path, doc, img_dir):
    import fitz
    with fitz.open(str(pdf_path)) as pdf:
        for table in doc.tables:
            page = pdf[table.page]
            ta = doc.pages[table.page]  # Page object has width/height
            sx = page.rect.width / ta.width if ta.width > 0 else 1.0
            sy = page.rect.height / ta.height if ta.height > 0 else 1.0

            # Include caption area
            caption_block = find_caption_block_for_table(doc, table)  # reuse logic
            y0 = calculate_crop_top(caption_block, table.bbox.y0 * sy, sy)

            rect = fitz.Rect(
                table.bbox.x0 * sx - 2,
                y0,
                table.bbox.x1 * sx + 2,
                table.bbox.y1 * sy + 2,
            )
            pix = page.get_pixmap(clip=rect, dpi=200)
            fname = f"table_{table.table_index + 1}.png"  # or global counter
            pix.save(str(img_dir / fname))
            table.image_path = fname
```

### 2.3 Implement `extract_pictures_from_model(pdf_path, document, img_dir)`

Reuse existing `extract_images_from_pdf` logic, but write to `picture.image_path`.

### 2.4 Populate `component_images` After Extraction

```python
def populate_component_images(doc: Document) -> None:
    # Group by head table (same caption)
    groups: dict[str, list[Table]] = {}
    for t in doc.tables:
        if t.caption and t.component_images is not None:
            groups.setdefault(t.caption, []).append(t)

    for caption, group in groups.items():
        if len(group) > 1:
            paths = [t.image_path for t in group if t.image_path]
            for t in group:
                t.component_images = paths
```

### 2.5 Validation

```bash
# Test on СП 52 (Table 4.1 = 4 pages)
python3 -c "
from pipeline import parse_yandex_json_to_model, stitch_tables, extract_table_images_from_model
doc = parse_yandex_json_to_model(pages)
stitch_tables(doc)
extract_table_images_from_model('SP52.pdf', doc, Path('image'))
t41 = [t for t in doc.tables if t.table_num == '4.1']
print(f'Table 4.1 parts: {len(t41)}')
print(f'component_images: {t41[0].component_images}')
"
# Expected: 4 parts, component_images = [table_1.png, table_2.png, table_3.png, table_4.png]
```

---

## Phase 3: Single-Pass Markdown Render

### 3.1 Implement `render_document_to_md(document: Document) -> str`

```python
def render_document_to_md(doc: Document) -> str:
    page_parts = []
    line_counter = 0

    for page in doc.pages:
        page_lines = []
        # Merge blocks + tables + pictures by Y
        elements = []

        for block in page.blocks:
            elements.append((block.y, 'block', block))
        for ti in page.table_indices:
            table = doc.tables[ti]
            elements.append((table.bbox.y0, 'table', table))
        for pi in page.picture_indices:
            pic = doc.pictures[pi]
            elements.append((pic.bbox.y0, 'picture', pic))

        elements.sort(key=lambda x: x[0])

        for y, etype, elem in elements:
            if etype == 'block':
                if elem.heading_level:
                    page_lines.append('#' * (elem.heading_level + 1) + ' ' + elem.full_text)
                elif elem.layout_type == 'LAYOUT_TYPE_LIST':
                    page_lines.append('- ' + elem.text)
                else:
                    page_lines.append(elem.text)

            elif etype == 'table':
                table = elem
                md_table = table_cells_to_md(table.cells)
                if table.caption:
                    page_lines.append(f'*{table.caption}*')
                start_line = len(page_lines)
                page_lines.append(md_table)
                end_line = len(page_lines)
                table.md_lines = (line_counter + start_line, line_counter + end_line)

            elif etype == 'picture':
                pic = elem
                caption = f"Рис. {pic.page + 1}"  # or extract from nearby blocks
                page_lines.append(f'![{caption}](image/{pic.image_path})')

        page_text = '\n\n'.join(page_lines).strip()
        if page_text:
            page_parts.append(page_text)
            line_counter += page_text.count('\n') + 1

    return '\n\n'.join(page_parts)
```

### 3.2 Helper: `table_cells_to_md(cells) -> str`

Reuse existing `_matrix_to_md_table` logic.

### 3.3 Validation

```bash
# Diff against current pipeline output
python3 pipeline.py -i "SP52.pdf" --json-native  # new
python3 pipeline.py -i "SP52.pdf"                # old
diff Markdown/SP52/SP52.md Markdown/SP52/SP52.md.old
# Should show only formatting differences, no structural changes
```

---

## Phase 4: Remove ID Marker Mechanics

### 4.1 Delete `_inject_table_ids()` Entirely

Remove function (≈lines 2711-2802 in current pipeline.py).

### 4.2 Remove Marker Emission from Parse

In `parse_yandex_json_to_model`: **do not** emit `<!-- t_pN_M -->` markers.  
Keep `table.table_index` as authoritative ID source.

### 4.3 Update `run_script_postprocess()`

**Signature change:**
```python
def run_script_postprocess(
    md_text: str,
    img_dir: str | Path,
    table_images: list[dict] | None = None,  # keep for component_images
    # REMOVE: page_boundaries parameter
) -> str:
```

**Body changes:**
- Delete step 2b (call to `_inject_table_ids`)
- Keep step 2c (`_merge_by_component_images`) — now uses `table.component_images` from model
- Update docstring

### 4.4 Update `_merge_by_component_images()`

Currently reads `table_images[].component_images`. Change to read from `Document.tables` or pass `table_images` built from model.

```python
# In process_file, after extraction:
table_images_for_stitch = [
    {"id": f"t_p{t.page+1}_{t.table_index}", "component_images": t.component_images, "path": t.image_path}
    for t in doc.tables if t.component_images
]
md_text = _merge_by_component_images(md_text, table_images_for_stitch)
```

### 4.5 Validation

```bash
# No markers in output
python3 pipeline.py -i "SP52.pdf" --json-native
grep -c '<!-- t_p' Markdown/SP52/SP52.md
# Expected: 0

# Full regression
cd firmware && python3 -m pytest tests/ -q --ignore=tests/test_gap_filling.py
# Expected: 312 passed
```

---

## Phase 5: Simplify AI Stage

### 5.1 New Function: `ai_postprocess_json_native()`

```python
def ai_postprocess_json_native(
    md_text: str,
    config: dict,
    file_label: str,
    vision_tables: list[dict],  # [{"caption", "table_num", "markdown"}, ...]
) -> str:
    """
    AI postprocess without ID markers.
    vision_tables: structured references from vision extraction.
    """
    # Build vision context by caption/table_num
    vision_by_caption = {v['caption']: v['markdown'] for v in vision_tables if v.get('caption')}
    vision_by_num = {v['table_num']: v['markdown'] for v in vision_tables if v.get('table_num')}

    # Chunk text (reuse _chunk_text)
    chunks = _chunk_text(md_text, AI_MAX_CHARS)

    # For each chunk, find relevant vision tables
    for i, chunk in enumerate(chunks):
        # Match by caption mention in chunk
        relevant = []
        for caption, vmd in vision_by_caption.items():
            if caption in chunk:
                relevant.append(vmd)
        for num, vmd in vision_by_num.items():
            if f'Таблица {num}' in chunk or f'Table {num}' in chunk:
                relevant.append(vmd)

        if relevant:
            prompt = build_prompt_with_vision(chunk, relevant)
        else:
            prompt = chunk

        result = _call_ai_api(prompt, config, f"{file_label} [ч.{i+1}]")
        chunks[i] = result or chunk

    return '\n\n'.join(chunks)
```

### 5.2 Update `process_file()` for JSON-Native Path

```python
def process_file(...):
    if use_json_native:
        # New path
        pages = send_to_yandex_ocr(...)
        doc = parse_yandex_json_to_model(pages)
        stitch_tables(doc)
        extract_table_images_from_model(pdf_path, doc, img_dir)
        extract_pictures_from_model(pdf_path, doc, img_dir)
        md_text = render_document_to_md(doc)
        md_text = run_script_postprocess(md_text, img_dir, table_images_from_model(doc))
        if use_ai:
            vision_tables = load_vision_tables(doc, tmp_dir)  # read table_N.md files
            md_text = ai_postprocess_json_native(md_text, config, file_stem, vision_tables)
        # --reg, --rag as before
        return save_and_finish(md_text, doc, ...)
    else:
        # Old path (unchanged)
        ...
```

### 5.3 Validation

```bash
# AI run on SP52 with --json-native --ai
python3 pipeline.py -i "SP52.pdf" --json-native --ai
# Verify: tables corrected, no marker warnings in log
```

---

## Phase 6: Cleanup & Integration

### 6.1 Add `--json-native` Flag

```python
# In parse_args()
parser.add_argument(
    '--json-native',
    action='store_true',
    help='Использовать новую JSON-native архитектуру (без raw.md, без ID-маркеров)'
)
```

### 6.2 Remove `raw.md` Writes

In `process_file`: delete `safe_write(file_tmp_dir / "raw.md", md_text)` for JSON-native path.

### 6.3 Remove Unused Parameters

- `page_boundaries` from `run_script_postprocess` signature and calls
- `page_boundaries` from `_stitch_continuation_tables` if no longer used (check)
- `table_raw_index` logic from old parse (replaced by model)

### 6.4 Update Tests

- Delete tests for `_inject_table_ids` (table-id-marker-contract §10.1)
- Update `test_table_id_markers.py` → test new model-based ID correspondence
- Add integration test for `--json-native` flag

### 6.5 Full Regression

```bash
cd firmware
python3 -m pytest tests/ -q --ignore=tests/test_gap_filling.py
# 312 passed

# Test all 3 corpus documents
for doc in "SP52.pdf" "SP89.pdf" "GOST18410.pdf"; do
    python3 pipeline.py -i "$doc" --json-native --ai --rag
    # Verify outputs
done
```

---

## File Changes Summary

**Only `firmware/src/pipeline.py` modified:**

| Region | Change |
|--------|--------|
| Lines 35-50 | Add dataclasses (Document, Page, Block, Table, Picture, Heading, BBox, Cell) |
| After parse section | Add `parse_yandex_json_to_model()` |
| After extract_table_images | Add `stitch_tables()`, `extract_table_images_from_model()`, `extract_pictures_from_model()`, `populate_component_images()` |
| After postprocess section | Add `render_document_to_md()`, `table_cells_to_md()` |
| AI section | Add `ai_postprocess_json_native()` |
| `run_script_postprocess` | Remove `page_boundaries` param, delete `_inject_table_ids` call |
| `_inject_table_ids` | **DELETE ENTIRELY** |
| `process_file` | Add `use_json_native` branch |
| `parse_args` | Add `--json-native` flag |
| `main` | Pass flag to `process_file` |

---

## Rollback Plan

If any phase breaks regression:

1. `git stash` → revert to working pipeline
2. Fix on branch → re-test
3. Each phase committed separately for bisectability

---

## Timeline Estimate

| Phase | Effort | Dependencies |
|-------|--------|--------------|
| 1 | 2-3 hours | None |
| 2 | 2-3 hours | Phase 1 |
| 3 | 1-2 hours | Phase 2 |
| 4 | 1 hour | Phase 3 |
| 5 | 1-2 hours | Phase 4 |
| 6 | 1 hour | Phase 5 |
| **Total** | **8-12 hours** | Sequential |

---

## Deliverables

1. ✅ `docs/architecture/json-native-pipeline.md` (architecture document)
2. This file → `workflows/t_f82ca6a2/implementation-plan.md`
3. Branch `refactor/json-pipeline` with `firmware/src/pipeline.py` changes
4. Updated `table-id-marker-contract.md` (mark superseded)
5. Implementation report after coder completes

---

*End of Implementation Plan*