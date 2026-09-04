# rag-pipeline

Монорепозиторий системы вопрос-ответ по нормативным документам (ГОСТ, СП, СНиП, СанПиН): преобразование PDF/DOCX в структурированный Markdown, построение гибридного поискового индекса в Qdrant и веб-интерфейс RAG с чатом.

## Структура

| Каталог | Назначение | Бывший репозиторий |
|---|---|---|
| `create_markdown/` | Распознавание PDF/DOCX → Markdown: Yandex Vision OCR (модель math-markdown), таблицы с двойной обработкой (текстовый слой + vision по ID-маркерам), AI-постобработка (DeepSeek / Gemini через Provod) | `Create_Markdown_YA` |
| `build_search_index/` | Индексация Markdown в Qdrant и QA-система: гибридный поиск (dense SiliconFlow + sparse BM25), реранк Qwen3-Reranker, LangGraph-граф с LLM-генерацией ответов, Telegram-бот | `Build_Search_index` |
| `interface_rag/` | Веб-интерфейс (FastAPI): чат с базой, загрузка документов, настройки провайдеров, регистрация, управление сервисами пайплайна (статус/перезапуск бота и прод-юнитов) | `interface_RAG` |

## Поток данных

```
PDF/DOCX ──create_markdown──▶ Markdown ──build_search_index──▶ Qdrant (dense + sparse)
                                                                    │
        Telegram-бот / веб-чат ◀──генерация ответа (LLM + реранк) ◀─┘
                    ▲
        interface_rag (веб-панель управления всем конвейером)
```

## Связи между частями

- `interface_rag/config/` — рабочие копии конфигураций (`create_markdown_config.yaml`, `providers.yaml`, `search_config.yaml`); веб-панель читает и редактирует их, управляя поведением остальных компонентов.
- Все компоненты работают с одной базой Qdrant и общим набором провайдеров LLM/embedding (ключи в `.env`, см. `.gitignore` — секреты в репозиторий не попадают).
- Telegram-бот присутствует в `build_search_index`, управление им (статус, перезапуск) — из веб-интерфейса `interface_rag`.

## История

Каждый каталог перенесён со своей полной git-историей через `git subtree` (см. merge-коммиты `subtree:` в логе). История конкретного компонента: `git log --follow <каталог>`.

Подробности установки и использования — в README соответствующего каталога.
