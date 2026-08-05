#!/usr/bin/env python3
"""
Тест Gemini 2.5 Flash Lite через Provod: полный PDF → Markdown.

Отправляет каждую страницу PDF как изображение в vision-модель
и собирает результат в единый Markdown-файл.
"""

import argparse
import base64
import io
import os
import sys
import time
from pathlib import Path

import fitz  # PyMuPDF
import httpx
from dotenv import load_dotenv

# ── Промпт (утверждён пользователем) ────────────────────────────────────────

PROMPT = """Ты — OCR-парсер технической документации. Переведи данный документ в Markdown.

## Заголовки

Используй # для названия документа, ## для разделов, ### для подразделов,
#### для пунктов — строго по иерархии документа.

## Формулы

Все математические формулы — в LaTeX: inline $...$, блочные $$...$$.
Перед и после знака степени ^ ставь пробелы: $a^{2} + b^{2}$.
Если OCR исказил формулу — восстанови корректный LaTeX.

## Таблицы

- Представляй ТОЛЬКО в Markdown-формате (| ... | ... |).
- Объединённые ячейки заголовков РАЗБЕЙ в соответствии с количеством
  строк или столбцов таблицы. Текст объединённой ячейки ПРОДУБЛИРУЙ
  в каждой получившейся строке или столбце.
- Если таблица разорвана на нескольких страницах (с пометками
  «Продолжение таблицы», «Окончание таблицы») — ОБЪЕДИНИ её в одну.
- Имя таблицы (Таблица N — ...) вынеси над таблицей отдельной строкой.
- Разделительная строка (|---|---|) должна иметь ровно столько же
  столбцов, сколько строка заголовка.
- Если в первом столбце есть сквозная нумерация строк — выведи её
  в отдельную колонку.
- В ячейках таблиц LaTeX-формулы оборачивай в $...$.
- Примечания из ячеек таблиц ВЫНЕСИ из таблицы и оформи как цитаты
  сразу после таблицы: > Примечание — ...

## Рисунки

- Каждый рисунок вставляй ссылкой: ![Рисунок N](image/fig_N.png).
- Нумерация рисунков — сквозная по всему документу (fig_1, fig_2...).
- Подпись под рисунком — курсивом: *Рисунок N — ...*.

## Общие правила

- Примечания оформляй как цитаты: > Примечание — ...
- Не выдумывай данные — только то, что есть в документе.
- Номера страниц в вывод НЕ включай.
- Разрывы слов (пере- | нос) — склеивай.
- Выдай ТОЛЬКО Markdown без комментариев и пояснений."""


def pdf_to_pages(pdf_path: str) -> list[tuple[int, str]]:
    """Конвертировать PDF в список (номер_страницы, base64 PNG)."""
    doc = fitz.open(pdf_path)
    total = len(doc)
    pages = []
    for i in range(total):
        page = doc[i]
        pix = page.get_pixmap(dpi=200)
        img_bytes = pix.tobytes("png")
        b64 = base64.b64encode(img_bytes).decode("ascii")
        pages.append((i + 1, b64))
        print(f"  Стр. {i + 1}/{total}: {len(img_bytes) // 1024} KB PNG")
    doc.close()
    return pages


def call_gemini(
    image_b64: str,
    page_num: int,
    total: int,
    api_key: str,
    model: str = "google/gemini-2.5-flash-lite",
    base_url: str = "https://api.provod.ai/v1",
) -> str | None:
    """Отправить страницу в Gemini через Provod API."""
    page_hint = (
        f"\n\n[Это страница {page_num} из {total}. "
        f"Продолжай с того места, где остановился на предыдущей странице.]"
    )
    full_prompt = PROMPT + page_hint

    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": full_prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{image_b64}"
                }},
            ],
        }],
        "max_tokens": 8000,
        "temperature": 0.0,
    }

    for attempt in range(3):
        try:
            with httpx.Client(timeout=180) as client:
                resp = client.post(
                    f"{base_url}/chat/completions",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
            if resp.status_code == 503:
                print(f"    503, попытка {attempt + 1}/3")
                time.sleep(5)
                continue
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            # Убрать markdown-обёртки, если модель их добавила
            import re
            content = re.sub(
                r"^```(?:markdown)?\s*\n?", "", content, flags=re.MULTILINE
            )
            content = re.sub(r"\n```\s*$", "", content, flags=re.MULTILINE)
            usage = data.get("usage", {})
            print(
                f"    OK: {len(content)} символов, "
                f"токенов in={usage.get('prompt_tokens', '?')} "
                f"out={usage.get('completion_tokens', '?')}"
            )
            return content
        except Exception as e:
            print(f"    Ошибка: {e}, попытка {attempt + 1}/3")
            time.sleep(5)
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Тест Gemini 2.5 Flash Lite: PDF → Markdown"
    )
    parser.add_argument(
        "-i", "--input",
        default="/mnt/sdb/!База_ГОСТ/test_pdf_to_markdown.pdf",
        help="Путь к PDF",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Выходной .md (по умолчанию: рядом с PDF, суффикс _gemini_test)",
    )
    parser.add_argument(
        "--dpi", type=int, default=200,
        help="DPI для рендеринга страниц (по умолчанию 200)",
    )
    args = parser.parse_args()

    pdf_path = Path(args.input).resolve()
    if not pdf_path.exists():
        print(f"Файл не найден: {pdf_path}")
        sys.exit(1)

    # Выходной файл
    if args.output:
        out_path = Path(args.output)
    else:
        out_path = pdf_path.with_suffix("").parent / (
            pdf_path.stem + "_gemini_test.md"
        )

    # Загрузка .env
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        print(f".env загружен: {env_path}")
    else:
        print(f".env не найден: {env_path}, пробую переменные окружения")

    api_key = os.environ.get("PROVOD_API_KEY", "")
    if not api_key:
        print("ОШИБКА: PROVOD_API_KEY не задан ни в .env, ни в окружении")
        sys.exit(1)

    print(f"PDF: {pdf_path} ({pdf_path.stat().st_size / 1024:.0f} KB)")
    print(f"Выход: {out_path}")
    print(f"Модель: google/gemini-2.5-flash-lite (Provod)")
    print()

    # Конвертация PDF → страницы PNG
    print("Рендеринг PDF → PNG...")
    pages = pdf_to_pages(str(pdf_path))
    total = len(pages)
    print(f"  Всего страниц: {total}\n")

    # Отправка каждой страницы
    results: list[str] = []
    for page_num, b64 in pages:
        print(f"Стр. {page_num}/{total} → Gemini...")
        text = call_gemini(b64, page_num, total, api_key)
        if text is None:
            print(f"  НЕ УДАЛОСЬ распознать страницу {page_num}")
            results.append(f"\n<!-- [ОШИБКА: страница {page_num} не распознана] -->\n")
        else:
            results.append(text)
        print()

    # Сборка результата
    full_md = "\n\n".join(results)
    out_path.write_text(full_md, encoding="utf-8")
    print(f"Готово: {out_path}")
    print(f"Размер: {out_path.stat().st_size:,} байт, строк: {len(full_md.splitlines())}")


if __name__ == "__main__":
    main()
