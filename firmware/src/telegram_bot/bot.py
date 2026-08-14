#!/usr/bin/env python3
"""
Telegram-бот для QA-системы нормативных документов.

Использует LangGraph qa_graph.py для ответа на вопросы пользователей.
Принимает сообщения, показывает «печатает...», возвращает ответ с цитатами.

Запуск:
    python bot.py

Зависимости: python-telegram-bot, qa_graph (локальный импорт)
"""
import asyncio
import logging
import logging.handlers
import os
import sys

# Добавляем родительскую директорию для импорта qa_graph
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

from asset_helpers import resolve_images_to_send

# Загружаем .env из корня проекта
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env"))

# ─── Конфигурация ──────────────────────────────────────────────────────
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
QDRANT_PATH = os.environ.get("QDRANT_PATH", "/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data")
LLM_CONFIG = os.path.join(os.path.dirname(__file__), "..", "llm_config.yaml")
MAX_HISTORY = 3  # сколько последних пар вопрос-ответ хранить

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.handlers.RotatingFileHandler(
            os.path.join(os.path.dirname(__file__), "bot.log"),
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("telegram_bot")

if not TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN не задан в .env!")
    sys.exit(1)

# Ленивая инициализация QA-графа (тяжёлый импорт)
_qa = None
# История диалогов: chat_id -> list[(question, answer)]
_history: dict[int, list[tuple[str, str]]] = {}


def get_qa():
    """Ленивая инициализация QAGraph."""
    global _qa
    if _qa is None:
        from qa_graph import QAGraph, QAGraphConfig

        _qa = QAGraph(
            QAGraphConfig(
                qdrant_path=QDRANT_PATH,
                llm_config_path=LLM_CONFIG,
            )
        )
        logger.info("QAGraph инициализирован")
    return _qa


# ─── Обработчики ────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветствие при /start."""
    await update.message.reply_text(
        "🔍 *QA-бот нормативных документов*\n\n"
        "Я отвечаю на вопросы по ГОСТ, СП, СНиП, СанПиН "
        "с цитатами из документов.\n\n"
        "Просто напишите вопрос — например:\n"
        "• «Допустимый ток ВВГ 4×120 в земле»\n"
        "• «Как организовать электроснабжение котельной»\n"
        "• «Можно ли использовать крышу как молниеприемник»\n\n"
        "База: ГОСТ 31996, СП 89.13330, СО 153-34.",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстового сообщения — поиск в QA-системе с контекстом диалога."""
    query = update.message.text.strip()
    if not query:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    logger.info(f"Запрос от {user.full_name} (@{user.username}) [chat={chat_id}]: {query[:80]}")

    # Собираем контекст из истории диалога
    history = _history.get(chat_id, [])
    if history:
        context_parts = []
        for q, a in history[-MAX_HISTORY:]:
            # Берём первые 500 символов ответа — достаточно для контекста
            context_parts.append(f"Предыдущий вопрос: {q}\nПредыдущий ответ: {a[:500]}")
        context_block = "\n\n".join(context_parts)
        full_query = f"{context_block}\n\nНовый вопрос (отвечай только на него, учитывая контекст выше): {query}"
    else:
        full_query = query

    # Показываем «печатает...» и отправляем сообщение-статус
    await context.bot.send_chat_action(
        chat_id=chat_id, action="typing"
    )
    status_msg = await update.message.reply_text("🔍 Ищу ответ на ваш запрос. Подождите...")

    try:
        qa = get_qa()
        result = await asyncio.to_thread(qa.run, full_query)
        answer = result.get("final_answer", "")
        cited = result.get("cited_chunk_ids", [])
        logger.info(f"cited_chunk_ids from QA: {cited}")
        if answer:
            logger.info(f"LLM answer (first 300 chars): {answer[:300]}")

        # Удаляем статус-сообщение
        await status_msg.delete()

        if not answer:
            await update.message.reply_text(
                "❌ Не удалось сформировать ответ. Попробуйте переформулировать запрос."
            )
            return

        # Сохраняем в историю (полный текст до обрезки)
        _history.setdefault(chat_id, []).append((query, answer))
        if len(_history[chat_id]) > MAX_HISTORY * 2:
            _history[chat_id] = _history[chat_id][-MAX_HISTORY:]

        # Telegram-сообщения ограничены 4096 символами
        if len(answer) > 4000:
            answer = answer[:4000] + "\n\n…(ответ обрезан)"

        await _send_answer(update, answer)
        # Отправляем изображения: по cited_chunk_ids (старое поведение),
        # при пустом списке — точечный fallback по явным ссылкам в answer.
        await _send_images(
            update,
            result.get("search_results", []),
            query,
            cited_chunk_ids=result.get("cited_chunk_ids", []),
            answer=answer,
        )

    except Exception as e:
        logger.error(f"Ошибка обработки запроса: {e}", exc_info=True)
        try:
            await status_msg.delete()
        except Exception:
            pass
        await update.message.reply_text(
            "⚠️ Произошла ошибка при обработке запроса. Попробуйте позже."
        )


async def _send_images(update: Update, search_results: list, query: str,
                      cited_chunk_ids: list[str] | None = None,
                      answer: str = ""):
    """Отправляет изображения таблиц/рисунков к ответу.

    Алгоритм:
    1. cited_chunk_ids НЕ пуст → старое поведение: берём только
       процитированные чанки, фильтруем по интенту запроса.
    2. cited_chunk_ids пуст → точечный fallback: из answer (при отсутствии
       ссылок — из query) извлекаем явные ссылки «Таблица N»/«Рисунок N»
       и ищем ОДНОЗНАЧНЫЙ asset по asset_type и caption. Если номер не
       найден — картинки не отправляются; если несколько разных путей —
       warning и пачка не отправляется.
    """
    import re

    # ── cited_chunk_ids не пуст: старое поведение ──
    logger.info(f"_send_images: cited_chunk_ids={cited_chunk_ids}, search_results_count={len(search_results)}")
    if cited_chunk_ids:
        cited_set = set(cited_chunk_ids)
        relevant = [r for r in search_results if r.get("chunk_id") in cited_set]
        logger.info(f"_send_images: cited_set={cited_set}, relevant_chunks={[r.get('chunk_id') for r in relevant]}")

        # ── Определение интента ──
        q = query.lower()
        want_figures = bool(re.search(r'рисун|изображ|картинк|рис\.\s*\d|fig', q))
        want_tables  = bool(re.search(r'таблиц|табл\.\s*\d|table', q))
        if not want_figures and not want_tables:
            want_figures = True
            want_tables = True

        sent: set[str] = set()

        for r in relevant:
            doc_dir = r.get("doc_dir", "")
            for asset in r.get("assets") or []:
                asset_type = (asset.get("asset_type") or "").lower()
                image_path = asset.get("image_path") or ""

                # Фильтр по интенту
                if asset_type == "image" and not want_figures:
                    continue
                if asset_type == "table" and not want_tables:
                    continue
                if not image_path:
                    continue

                full = os.path.join(doc_dir, image_path) if doc_dir else image_path
                if full in sent or not os.path.exists(full):
                    if not os.path.exists(full):
                        logger.warning(f"Изображение не найдено: {full}")
                    continue

                try:
                    with open(full, "rb") as f:
                        await update.message.reply_photo(f, caption=os.path.basename(full))
                    sent.add(full)
                    logger.info(f"Отправлено изображение ({asset_type}): {full}")
                except Exception as e:
                    logger.warning(f"Не удалось отправить {full}: {e}")
        return

    # ── cited_chunk_ids пуст: точечный fallback по caption ──
    paths, warnings = resolve_images_to_send(search_results, answer, query)
    for w in warnings:
        logger.warning(w)
    if not paths:
        logger.info("_send_images: fallback — явные ссылки не найдены/неоднозначны, картинки не отправляю")
        return
    for full in paths:
        if not os.path.exists(full):
            logger.warning(f"Изображение не найдено: {full}")
            continue
        try:
            with open(full, "rb") as f:
                await update.message.reply_photo(f, caption=os.path.basename(full))
            logger.info(f"Отправлено изображение (fallback по caption): {full}")
        except Exception as e:
            logger.warning(f"Не удалось отправить {full}: {e}")


async def _send_answer(update: Update, answer: str):
    """Отправляет ответ: Markdown, при ошибке парсинга — plain text."""
    try:
        await update.message.reply_text(answer, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(answer)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик ошибок."""
    logger.error(f"Ошибка: {context.error}", exc_info=context.error)


# ─── Запуск ─────────────────────────────────────────────────────────────

def main():
    import time as _time
    import asyncio as _asyncio
    while True:
        try:
            # При падении сети asyncio event loop может быть закрыт —
            # создаём новый на каждой итерации рестарта.
            _asyncio.set_event_loop(_asyncio.new_event_loop())
            app = Application.builder().token(TOKEN).build()
            app.add_handler(CommandHandler("start", start))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            app.add_error_handler(error_handler)
            logger.info("Бот запущен, ожидаю сообщения...")
            app.run_polling(allowed_updates=Update.ALL_TYPES)
        except Exception as e:
            logger.error(f"Бот упал: {e}. Перезапуск через 5 сек...")
            _time.sleep(5)


if __name__ == "__main__":
    main()
