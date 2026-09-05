#!/usr/bin/env python3
"""Сверка yandex_result.json ↔ финальный .md на полноту данных.

Ландмарки из OCR-блоков, которые обязаны дожить до финального markdown:
  1. Заголовки разделов (4.2.5 Название / Приложение А) — номер ищется в md
     толерантно к пробелам/разделителям.
  2. Числовые значения из прозы (0,5 / 4.2.5 / 2013) — tolerant-поиск.
  3. Абзацы (>=80 симв.): 3 окна по 6 слов ищутся подстрокой в конденсированном
     md. 0 из 3 → абзац потерян (критично), 1 из 3 → подозрительно.
  4. Таблицы: номера подписей из OCR («Т а б л и ц а Б.1» с де-спейсингом)
     должны встречаться в md; количество кусков таблиц в yandex vs t_p-маркеров
     в md — информация (слияние продолжений легитимно).
Шум полосы (колонтитул «ГОСТ …», номер страницы) игнорируется — пайплайн
убирает его по дизайну. AI-переписывание учитывается: сверка по числам и
шинглам, а не по точным строкам.

Использование:
  check_completeness.py DOC_DIR [--md PATH]        # один документ
  check_completeness.py --scan BASE_DIR            # пары tmp/* ↔ Markdown/*
  Опции: --json PATH (отчёт в JSON), --strict (код 1 и при предупреждениях)

Коды выхода: 0 — чисто; 1 — найдены потери; 2 — ошибка использования/IO.
Stdlib-only: работает любым python3, в гит не входит.
"""
import argparse
import json
import re
import sys
from pathlib import Path

NOISE_RE = re.compile(r"^(?:ГОСТ\s+\d+[—\-–\s]*\d*|\d{1,4}|—\s*\d{1,4}\s*—)\s*$", re.I)
HEADING_RE = re.compile(r"^(\d+(?:\.\d+)+)\s+(\S.*)$", re.U)
APPENDIX_RE = re.compile(r"^Приложение\s+([А-Я])\b(.*)$", re.U)
TABLE_NUM_RE = re.compile(
    r"(?:окончани[ея]|продолжени[ея]|продолж\.?|таблиц[аы]|табл\.?)\s+"
    r"([А-ЯA-Z]?\.?\d+(?:\.\d+)*)", re.I)
DECIMAL_RE = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)+)(?![\w.,])")
INT_RE = re.compile(r"(?<![\w.,])(\d{2,})(?![\w.,])")
MIN_BLOCK_CHARS = 80
SHINGLE_WORDS = 6
WINDOWS = 3
MAX_LIST = 12


def norm_full(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().replace("ё", "е")).strip()


def cond(s: str) -> str:
    return " ".join(re.findall(r"[а-яa-z0-9]+", norm_full(s)))


def despace(s: str) -> str:
    """«Т а б л и ц а» → «таблица» (OCR-разрядка одиночными буквами)."""
    return re.sub(r"\b(?:[а-я]\s){3,}[а-я]\b",
                  lambda m: m.group(0).replace(" ", ""), norm_full(s))


def num_needle(num: str) -> re.Pattern:
    parts = re.split(r"[.,]", num)
    return re.compile(r"\s*[.,]\s*".join(re.escape(p) for p in parts))


def block_text(block: dict) -> str:
    return " ".join(str(l.get("text", "")).strip()
                    for l in block.get("lines", []) if str(l.get("text", "")).strip()).strip()


def load_yandex(path: Path):
    """→ (blocks, table_pieces, captions)

    blocks: [(page, text)] текстовых блоков без шума полосы;
    table_pieces: количество кусков таблиц (textAnnotation.tables);
    captions: [(page, номер_таблицы)] из текстов блоков.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    blocks, captions, pieces = [], [], 0
    for pg in data:
        ta = pg.get("result", {}).get("textAnnotation", {})
        page = str(pg.get("result", {}).get("page", "?"))
        pieces += len(ta.get("tables") or [])
        for b in ta.get("blocks", []):
            lines = [str(l.get("text", "")).strip()
                     for l in b.get("lines", []) if str(l.get("text", "")).strip()]
            t = " ".join(lines).strip()
            if not t or NOISE_RE.match(t):
                continue
            blocks.append((page, t, lines))
            dn = despace(t)
            m = TABLE_NUM_RE.search(dn)
            if m and _is_caption_like(dn):
                captions.append((page, m.group(1).upper()))
    return blocks, pieces, captions


def _is_caption_like(dn: str) -> bool:
    """Подпись = «таблица N» в начале либо «окончание/продолжение таблицы N»."""
    return bool(re.match(r"^(?:таблиц[аы]|табл\.?|окончани[ея]|продолжени[ея]|продолж\.?)\b", dn, re.I)) \
        or bool(re.search(r"\b(?:окончани[ея]|продолжени[ея]|продолж\.?)\s+(?:таблиц[аы]|табл\.?)", dn, re.I))


LATEX_RE = re.compile(r"\\[a-zA-Z]+")
TOC_RE = re.compile(r"\.{3,}\s*\d{1,4}\b")


def collect_landmarks(blocks):
    """→ (заголовки, числа, абзацы-юниты)

    Абзац представлен шинглами своих OCR-СТРОК (физических единиц), а не
    склеенного блока: md/AI перестраивает структуру, стыки строк лгут.
    Абзац «потерян», только если не найдена хотя бы одна строка каждого юнита.
    """
    headings, numbers, shingles = [], set(), []
    seen_sh = set()
    for page, t, lines in blocks:
        dn = despace(t)
        hm = HEADING_RE.match(dn) or APPENDIX_RE.match(dn)
        if hm:
            headings.append((page, hm.group(1), (hm.group(2) or "")[:60]))
        is_formula = len(LATEX_RE.findall(t)) >= 3  # AI переписывает LaTeX — шинглы врут
        is_toc = len(TOC_RE.findall(dn)) >= 3      # оглавление: номера страниц, AI его убирает
        if not is_formula and not is_toc:
            for m in DECIMAL_RE.findall(dn):
                numbers.add(m.replace(".", ","))
            for m in INT_RE.findall(dn):
                if len(m) >= 2:
                    numbers.add(m)
        if is_formula or is_toc:
            continue  # формулы/оглавление проверяются числами, не шинглами
        units = []
        for ln in lines:
            toks = cond(ln).split()
            solid = [w for w in toks if len(w) >= 2]
            if len(ln) >= 60 and len(solid) >= SHINGLE_WORDS + 1 and len(set(solid)) > 2:
                hits = [" ".join(toks[:SHINGLE_WORDS])]
                if len(toks) > SHINGLE_WORDS + 6:
                    hits.append(" ".join(toks[-SHINGLE_WORDS:]))
                units.append((ln, hits))
        if not units:
            continue
        key = units[0][1][0]
        if key not in seen_sh:
            seen_sh.add(key)
            shingles.append((page, t, units))
    return headings, numbers, shingles


def rare_tokens(line: str) -> list[str]:
    """Длинные «опорные» слова строки: есть в md → перефразировано, нет → потеряно."""
    return [w for w in cond(line).split() if len(w) >= 9][:3]


def check_document(yandex_path: Path, md_path: Path) -> dict:
    blocks, pieces, captions = load_yandex(yandex_path)
    headings, numbers, shingles = collect_landmarks(blocks)

    md_raw = md_path.read_text(encoding="utf-8")
    md_no_comments = re.sub(r"<!--.*?-->", " ", md_raw, flags=re.S)
    md_n = norm_full(md_no_comments)
    md_c = cond(md_no_comments)
    md_despaced = despace(md_no_comments)
    md_cap_nums = {m.group(1).upper() for m in TABLE_NUM_RE.finditer(md_despaced)}
    md_markers = len(re.findall(r"t_p\d+_\d+", md_raw))
    res = {"doc": md_path.parent.name, "yandex": str(yandex_path), "md": str(md_path),
           "critical": [], "warning": [], "info": []}

    for page, num, title in headings:
        if not num_needle(num).search(md_n):
            res["critical"].append(
                f"заголовок {num} «{title}» (стр. {page}) не найден в md")
    if headings:
        res["n_headings"] = len(headings)

    cap_nums = {n for _, n in captions}
    heading_nums = {num for _, num, _ in headings}
    covered = {n.replace(".", ",") for n in cap_nums | heading_nums}  # проверяются своими категориями
    missing_nums = sorted((n for n in numbers if n not in covered and not num_needle(n).search(md_n)),
                          key=lambda x: (len(x), x))
    if missing_nums:
        cat = res["critical"] if len(missing_nums) > 3 else res["warning"]
        cat.append(f"числовых значений не найдено: {len(missing_nums)} из {len(numbers)}: "
                   + ", ".join(missing_nums[:MAX_LIST])
                   + (" …" if len(missing_nums) > MAX_LIST else ""))

    lost, shaky = [], []
    for page, t, units in shingles:
        missing = [ln for ln, hits in units if not any(h in md_c for h in hits)]
        if len(missing) == len(units):
            if any(any(rt in md_c for rt in rare_tokens(ln)) for ln in missing):
                shaky.append((page, t))  # опорные слова на месте — AI перефразировал
            else:
                lost.append((page, t))
        elif missing:
            shaky.append((page, t))
    for page, t in lost[:MAX_LIST]:
        res["critical"].append(
            f"абзац без следов в md (стр. {page}): «{t[:90]}…»" if len(t) > 90
            else f"абзац без следов в md (стр. {page}): «{t}»")
    if len(lost) > MAX_LIST:
        res["critical"].append(f"… и ещё потерянных абзацев: {len(lost) - MAX_LIST}")
    if shaky:
        res["warning"].append(f"абзацев совпадает слабо (перефразировано или требует проверки): {len(shaky)}")
    for page, t in shaky[:5]:
        res["warning"].append(f"абзац совпадает слабо (стр. {page}): «{t[:70]}…»")

    cap_nums = {n for _, n in captions}
    missing_caps = sorted(n for n in cap_nums if n not in md_cap_nums)
    for n in missing_caps:
        res["critical"].append(f"подпись таблицы {n} отсутствует в md")
    if pieces or md_markers:
        res["info"].append(f"таблиц: кусков в yandex {pieces}, t_p-маркеров в md {md_markers}; "
                           f"подписей yandex {len(cap_nums)}, найдено в md {len(cap_nums) - len(missing_caps)}")

    ya_chars = sum(len(t) for _, t, _ in blocks)
    md_chars = len(re.sub(r"\s+", " ", md_no_comments))
    res["info"].append(f"объём: yandex {ya_chars} симв. (без шума полосы) → md {md_chars} симв. "
                       f"({round(100 * md_chars / max(ya_chars, 1))}%)")
    res["counts"] = {"headings": len(headings), "numbers": len(numbers),
                     "paragraphs": len(shingles), "lost_paragraphs": len(lost),
                     "shaky_paragraphs": len(shaky),
                     "missing_numbers": len(missing_nums), "missing_table_captions": len(missing_caps)}
    res["verdict"] = "ПРОБЛЕМЫ" if res["critical"] else ("ПРЕДУПРЕЖДЕНИЯ" if res["warning"] else "OK")
    return res


def find_md_for(stem_dir: Path, base: Path) -> Path | None:
    explicit = stem_dir / "final.md"
    if explicit.exists():
        return explicit
    md_dir = base / "Markdown" / stem_dir.name
    if md_dir.is_dir():
        cands = [p for p in md_dir.glob("*.md") if not p.name.startswith("_")]
        cands = [p for p in cands if p.name != "table_images.json"]
        if cands:
            same = [p for p in cands if p.stem == stem_dir.name]
            return sorted(same or cands, key=lambda p: p.stat().st_size, reverse=True)[0]
    return None


def print_report(res: dict) -> None:
    print(f"== {res['doc']} ==  {res['verdict']}")
    for cat, tag in (("critical", "КРИТИЧНО"), ("warning", "предупр."), ("info", "инфо")):
        for line in res.get(cat, []):
            print(f"  [{tag}] {line}")
    if not (res.get("critical") or res.get("warning") or res.get("info")):
        print("  расхождений не найдено")


def main() -> int:
    ap = argparse.ArgumentParser(description="Сверка yandex_result.json ↔ финальный md")
    ap.add_argument("doc_dir", nargs="?", help="каталог документа с yandex_result.json")
    ap.add_argument("--scan", metavar="BASE", help="база (ищет tmp/*/yandex_result.json + Markdown/)")
    ap.add_argument("--md", help="явный путь к финальному md")
    ap.add_argument("--json", dest="json_out", help="сохранить отчёт в JSON")
    ap.add_argument("--strict", action="store_true", help="код 1 и при предупреждениях")
    args = ap.parse_args()

    pairs = []
    if args.scan:
        base = Path(args.scan)
        for yj in sorted((base / "tmp").glob("*/yandex_result.json")):
            md = Path(args.md) if args.md and len(list((base / "tmp").glob("*/yandex_result.json"))) == 1 \
                else find_md_for(yj.parent, base)
            if md and md.exists():
                pairs.append((yj, md))
            else:
                print(f"== {yj.parent.name} ==  ПРОПУСК: md не найден", file=sys.stderr)
    elif args.doc_dir:
        d = Path(args.doc_dir)
        yj = d / "yandex_result.json" if d.is_dir() else d
        if not yj.is_file():
            ap.error(f"нет {yj}")
        md = Path(args.md) if args.md else find_md_for(yj.parent, yj.parent.parent.parent)
        if not md or not md.exists():
            ap.error("md не найден — укажите --md")
        pairs.append((yj, md))
    else:
        ap.error("укажите DOC_DIR или --scan BASE")

    results = []
    for yj, md in pairs:
        try:
            res = check_document(yj, md)
        except Exception as e:  # noqa: BLE001
            res = {"doc": md.parent.name if md else yj.parent.name, "verdict": "ОШИБКА",
                   "critical": [f"{type(e).__name__}: {e}"], "warning": [], "info": []}
        results.append(res)
        print_report(res)
        print()

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
        print(f"JSON-отчёт: {args.json_out}")

    bad = sum(1 for r in results if r["critical"] or (args.strict and r["warning"]))
    print(f"Итого документов: {len(results)}, с проблемами: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
