# Implementation report — t_af228810

## Summary

Added a new scripted postprocessing step to `run_script_postprocess()` in
`firmware/src/pipeline.py`: adding spaces before and after the exponent sign
`^` inside LaTeX formulas (`$...$` and `$$...$$`).

Previously this behavior existed only in the AI prompt (`config_ai.yaml`,
line 54) and required `--ai`. Now it runs in the scripted pipeline, i.e.
without `--ai`.

## Files changed

- `firmware/src/pipeline.py`
  - New function `fix_latex_caret_spaces(md_text)` (placed before the
    "Полная скриптовая постобработка" section).
  - Wired into `run_script_postprocess()` as a new numbered stage **9**,
    after the existing OCR-artifacts stage; added `log.info("  9. LaTeX:
    пробелы вокруг ^")` and updated the function docstring.
- `firmware/src/test_latex_caret_spaces.py` (new)
  - Parametrized unit tests for `fix_latex_caret_spaces` (task examples,
    edge cases, `\^` protection, no doubling, outside-formula `^` untouched).
  - Integration tests asserting the new stage is present in
    `run_script_postprocess` and applied end-to-end.

## Behavior

For every `$...$` and `$$...$$` formula in the text:

- if the char before `^` is not whitespace, a space is inserted before it;
- if the char after `^` is not whitespace, a space is inserted after it;
- existing spaces are never duplicated;
- `^` outside formulas is never touched;
- escaped `\^` (e.g. `90^{\circ}`) is protected with a lookbehind
  `(?<!\\)` and left untouched;
- `$$...$$` is processed before `$...$` (single regex alternation with a
  scoped `(?s:...)` flag) so inline matching cannot eat the display-formula
  boundaries; multiline display formulas work via DOTALL on that branch.

Verified examples:

| Input | Output |
|-------|--------|
| `$x^2$` | `$x ^ 2$` |
| `$a^{bc}$` | `$a ^ {bc}$` |
| `$x ^2$` | `$x ^ 2$` |
| `$x^ 2$` | `$x ^ 2$` |
| `$$\frac{a^2}{b}$$` | `$$\frac{a ^ 2}{b}$$` |
| `обычный текст^не формула` | unchanged |

## Deviation note (example table in task)

The task's example table shows `$a^{bc}$` → `$a ^{ bc}$` (with a space after
`{`). The stated rule in the task body is: "add space before `^` (if absent)
and after `^` (if absent)". Implementing exactly that rule yields
`$a ^ {bc}$` (space before and after `^`, no space inside the braces).
The extra space inside `{...}` in the example is not derivable from the rule
and would corrupt brace-group content, so it was not reproduced. All other
examples match exactly.

## Validation

- `python3 -m pytest test_latex_caret_spaces.py -v` → 18 passed
- Full suite: `pytest test_gap_filling.py test_ai_table.py
  test_latex_caret_spaces.py -q` → 32 passed
- `python3 test_no_hardcoded_prompts.py` → all checks passed
- `python3 -m py_compile pipeline.py test_latex_caret_spaces.py` → OK
- End-to-end `run_script_postprocess` with a temp `img_dir` → formulas spaced,
  temp dir left empty.

## Known limitations

- Like the existing `cleanup_latex`, `$` in ordinary prose (e.g. prices) can
  be misread as a formula boundary; this is pre-existing convention and not
  changed here.
- The pre-existing `\frac`→`raca` mangling in output comes from
  `cleanup_latex`/`_fix_latex_ocr_artifacts` (strips `\f`), not from this
  change.

## Constraints honored

- `config_ai.yaml` NOT modified.
- Existing postprocessing stages 1–8 NOT modified (only appended stage 9).
- No production code placed in `workflows/` (report only).
