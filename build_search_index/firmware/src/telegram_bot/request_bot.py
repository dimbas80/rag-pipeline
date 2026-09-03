#!/usr/bin/env python3
"""
Telegram-бот для QA-системы нормативных документов.

Использует LangGraph qa_graph.py для ответа на вопросы пользователей.
Принимает сообщения, показывает «печатает...», возвращает ответ с цитатами.

Запуск:
    python request_bot.py

Зависимости: python-telegram-bot, qa_graph (локальный импорт)
"""
import asyncio
import logging
import logging.handlers
import os
import re
import sys

# Добавляем родительскую директорию для импорта qa_graph
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from asset_helpers import (
    extract_asset_references,
    format_document_list,
    is_document_list_request,
    resolve_images_to_send,
    strip_markdown_tables,
)

# Ключи подаются systemd EnvironmentFile=/root/RAG/config/.env (решение 36);
# локальный load_dotenv удалён — это был второй источник истины на удалённый
# Build_Search_index/.env.

# ─── Конфигурация ──────────────────────────────────────────────────────
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
QDRANT_PATH = os.environ.get("QDRANT_PATH", "/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data")
SEARCH_CONFIG = os.path.join(os.path.dirname(__file__), "..", "search_config.yaml")
PROVIDERS_CONFIG = os.path.join(os.path.dirname(__file__), "..", "providers.yaml")
ALLOWED_USERS = {
    int(value.strip())
    for value in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
    if value.strip().isdigit()
}
MAX_HISTORY = 3

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.handlers.RotatingFileHandler(
            os.path.join(os.path.dirname(__file__), "request_bot.log"),
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
                search_config_path=SEARCH_CONFIG,
                providers_path=PROVIDERS_CONFIG,
            )
        )
        logger.info("QAGraph инициализирован")
    return _qa


# ─── Обработчики ────────────────────────────────────────────────────────

def _is_authorized(update: Update) -> bool:
    """Allow everyone when unset; otherwise allow configured user IDs."""
    if not ALLOWED_USERS:
        return True
    user_id = getattr(update.effective_user, "id", None)
    if user_id in ALLOWED_USERS:
        return True
    logger.warning("Отклонено сообщение неавторизованного пользователя user_id=%s", user_id)
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветствие при /start."""
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "🔍 *QA-бот нормативных документов*\n\n"
        "Я отвечаю на вопросы по ГОСТ, СП, СНиП, СанПиН, ПУЭ, ПТЭ "
        "с цитатами из документов.\n\n"
        "Просто напишите вопрос — например:\n"
        "• «Допустимый ток ВВГ 4×120 в земле»\n"
        "• «Как организовать электроснабжение котельной»\n"
        "• «Можно ли использовать крышу как молниеприемник»\n\n"        
        "Команды:\n"
        "/list — перечень документов в базе",
        parse_mode="Markdown",
    )


async def list_documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /list — перечень документов в базе.

    В отличие от текстовой ветки handle_message: без _send_images и без
    записи в _history. Форматирование — через общий format_document_list.
    """
    if not _is_authorized(update):
        return
    try:
        documents = await asyncio.to_thread(get_qa().list_documents)
    except Exception as exc:
        logger.error("Не удалось получить список документов: %s", exc, exc_info=True)
        documents = None
    reply_text = format_document_list(documents)
    # Telegram-сообщения ограничены 4096 символами (страховка от длинного
    # списка документов — как для обычного answer ниже).
    if len(reply_text) > 4000:
        reply_text = reply_text[:4000] + "\n\n…(ответ обрезан)"
    await update.message.reply_text(reply_text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстового сообщения — поиск в QA-системе с контекстом диалога."""
    if not _is_authorized(update):
        return
    query = update.message.text.strip()
    if not query:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    logger.info(f"Запрос от {user.full_name} (@{user.username}) [chat={chat_id}]: {query[:80]}")

    if is_document_list_request(query):
        try:
            documents = await asyncio.to_thread(get_qa().list_documents)
            reply_text = format_document_list(documents)
            # Telegram-сообщения ограничены 4096 символами (страховка от
            # длинного списка документов — как для обычного answer ниже).
            if len(reply_text) > 4000:
                reply_text = reply_text[:4000] + "\n\n…(ответ обрезан)"
            await update.message.reply_text(reply_text)
        except Exception as exc:
            logger.error("Не удалось получить список документов: %s", exc, exc_info=True)
            await update.message.reply_text("База пуста / не удалось получить список")
        return

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

        # Граф может остановиться на уточняющем вопросе (ask_clarification):
        # в результате есть ключ __interrupt__ и нет final_answer. Отвечаем
        # текстом уточняющего вопроса, а НЕ «Не удалось сформировать ответ»
        # (фикс 3.3).
        interrupt_text = _interrupt_reply_text(result)
        if interrupt_text:
            await status_msg.delete()
            await update.message.reply_text(f"❓ {interrupt_text}")
            logger.info(f"Запрошено уточнение: {interrupt_text[:200]}")
            return

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
        answer_for_text = strip_markdown_tables(answer)
        if len(answer_for_text) > 4000:
            answer_for_text = answer_for_text[:4000] + "\n\n…(ответ обрезан)"

        await _send_answer(update, answer_for_text)
        # Отправляем изображения с приоритетом: явная ссылка в query →
        # явная ссылка в answer → старое cited-поведение (фикс 3.2).
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


def _interrupt_reply_text(result: dict) -> str | None:
    """Текст уточняющего вопроса из результата qa.run() (фикс 3.3).

    Если граф остановился на interrupt (ask_clarification), в result по
    ключу __interrupt__ лежит список Interrupt-объектов LangGraph; текст
    вопроса — в .value (как в qa_graph._print_interrupt_value). Возвращает
    текст вопроса или None, если прерывания не было.
    """
    interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
    if not interrupts:
        return None
    intr = interrupts[0]
    value = getattr(intr, "value", None)
    if value is None:
        value = str(intr)
    text = str(value).strip()
    return text or None


async def _send_images(update: Update, search_results: list, query: str,
                      cited_chunk_ids: list[str] | None = None,
                      answer: str = ""):
    """Отправляет изображения таблиц/рисунков к ответу.

    Приоритет выбора (фикс 3.2):
    1. Явная ссылка «Таблица N»/«Рисунок N» в QUERY → точечный выбор
       (однозначный asset по asset_type+caption), даже при непустых
       cited_chunk_ids.
    2. Ссылок в query нет, но есть в answer → точечный выбор по answer.
    3. Явных ссылок нет вовсе → старое cited-поведение: все asset'ы
       процитированных чанков по интенту запроса.
    4. Неоднозначность / нет совпадения → картинки НЕ отправляются
       (fail closed), warning в лог.
    """
    query_refs = extract_asset_references(query)
    answer_refs = extract_asset_references(answer)

    # ── 1-2. Явные ссылки — точечный выбор (query приоритетнее answer) ──
    if query_refs or answer_refs:
        prefer = "query" if query_refs else "answer"
        paths, warnings = resolve_images_to_send(
            search_results, answer, query, prefer=prefer
        )
        for w in warnings:
            logger.warning(w)
        if not paths:
            logger.info(
                f"_send_images: явная ссылка не найдена/неоднозначна "
                f"(query_refs={query_refs}, answer_refs={answer_refs}) — "
                f"картинки не отправляю (fail closed)"
            )
            return
        await _send_photo_paths(update, paths, "явная ссылка")
        return

    # ── 3. Явных ссылок нет → старое cited-поведение ──
    if cited_chunk_ids:
        cited_set = set(cited_chunk_ids)
        relevant = [r for r in search_results if r.get("chunk_id") in cited_set]
        logger.info(
            f"_send_images: cited_set={cited_set}, "
            f"relevant_chunks={[r.get('chunk_id') for r in relevant]}"
        )

        # ── Определение интента ──
        q = query.lower()
        want_figures = bool(re.search(r'рисун|изображ|картинк|рис\.\s*\d|fig', q))
        want_tables  = bool(re.search(r'таблиц|табл\.\s*\d|table', q))
        if not want_figures and not want_tables:
            want_figures = True
            want_tables = True

        paths: list[str] = []
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
                if full not in paths:
                    paths.append(full)

        await _send_photo_paths(update, paths, "cited")
        return

    # ── 4. Ни явных ссылок, ни cited_chunk_ids — картинки не отправляем ──
    logger.info("_send_images: явных ссылок и cited_chunk_ids нет — картинки не отправляю")


async def _send_photo_paths(update: Update, paths: list[str], label: str) -> None:
    """Отправляет изображения по путям; отсутствующие файлы пропускает."""
    for full in paths:
        if not os.path.exists(full):
            logger.warning(f"Изображение не найдено: {full}")
            continue
        try:
            with open(full, "rb") as f:
                await update.message.reply_photo(f, caption=os.path.basename(full))
            logger.info(f"Отправлено изображение ({label}): {full}")
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
            app.add_handler(CommandHandler("list", list_documents_command))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            app.add_error_handler(error_handler)
            logger.info("Бот запущен, ожидаю сообщения...")
            app.run_polling(allowed_updates=Update.ALL_TYPES)
            # Нормальный возврат из run_polling = получен stop-сигнал
            # (SIGTERM от systemctl stop/restart). Выходим, иначе while-True
            # сразу поднимал новый polling, сервис не мог остановиться и
            # systemd убивал процесс SIGKILL после TimeoutStopSec.
            logger.info("Polling остановлен (stop-сигнал) — завершаю процесс")
            return
        except Exception as e:
            logger.error(f"Бот упал: {e}. Перезапуск через 5 сек...")
            _time.sleep(5)


if __name__ == "__main__":
    main()
