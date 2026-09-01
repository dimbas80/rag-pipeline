# Implementation Report — t_19e9e8ac

## Summary

Fixed a RAG v2 heading-inheritance defect: backward scan for an unnumbered
`####`/`#####` clause in `_build_ancestors()` and `_build_heading_texts()`
now stops at a top-level `##` boundary, analogous to the already-implemented
bounding of the `section` search.

## Defect

For the sequence

```text
## 4. Старое
### 4.7. Раздел
#### 4.7.1. Пункт
## 1. Новое
#### Без номера
Текст
```

the unnumbered `####` after the new `##` previously inherited clause `4.7.1`
and the old clause text from the previous chapter. Expected: `clause: null`,
`heading_texts.clause: null` (no previous level>=4 heading in the current
chapter), while an unnumbered level>=4 heading *within the same chapter* must
still inherit the previous clause.

## Changes

- `firmware/src/pipeline.py`
  - `_build_ancestors()`: in the backward scan for an unnumbered clause
    (`level >= 4`, `number is None`), `break` when a heading of `level == 2`
    is reached, mirroring the existing section bound.
  - `_build_heading_texts()`: same boundary for the unnumbered clause-text
    scan; unnumbered heading with `number is None` no longer carries its own
    heading text as clause (inherits previous clause text within the chapter,
    or `None`).
  - Docstrings updated.
- `firmware/tests/test_rag_jsonl.py`: 3 new regression tests
  - `test_build_ancestors_unnumbered_clause_bounded_by_chapter`
  - `test_build_heading_texts_unnumbered_clause_bounded_by_chapter`
  - `test_build_heading_texts_unnumbered_subclause_inherits_text`

## Validation

- `cd firmware && python3 -m pytest tests/test_rag_jsonl.py -q` → 65 passed
- `cd firmware && python3 -m pytest tests/test_rag_v2.py tests/test_rag_jsonl.py -q` → 127 passed
- `cd firmware && python3 -m pytest tests/ -q --ignore=tests/test_gap_filling.py` → 224 passed
- Short program verification of the reproducing sequence:
  - `ancestors(4)` = `{'chapter': '1', 'section': None, 'clause': None}` ✓
  - `heading_texts(4)` = `{'chapter': '1. Новое', 'section': None, 'clause': None}` ✓
  - unnumbered `#####`/`####` inside the same chapter still inherits previous
    clause `3.2.1` and its text ✓

## Constraints respected

- No changes to external Markdown, `image/`, `rag_config.yaml`, AI/OCR stages.
- Occurrence IDs and `embedding_text` untouched.
- Production code changes only under `firmware/src/`.

## Commits

- `9622c8e` fix(rag): ограничить поиск ненумерованного clause границей top-level главы
