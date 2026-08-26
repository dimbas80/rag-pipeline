# Реестр провайдеров

`firmware/src/providers.yaml` хранит UI-редактируемый реестр LLM-провайдеров и роли пайплайна. Секреты в файл не помещаются: `api_key_env` — имя переменной окружения из `.env`.

## Схема

```yaml
providers:
  provider-name:
    base_url: https://service.example/v1
    api_key_env: SERVICE_API_KEY
    models:
      model-name: chat  # chat, vision, embedding или rerank
roles:
  create_markdown:
    ai_postprocess:
      provider: provider-name
      model: model-name
      fallback: {provider: other, model: other-model}
```

`base_url` всегда является базовым URL с `/v1`; endpoint (`/chat/completions`, `/embeddings` или `/rerank`) добавляется вызывающим pipeline. Capability `chat` используется для текстовой обработки, `vision` — для таблиц, `embedding` и `rerank` зарезервированы для RAG-пайплайна.

## Разрешение ролей

`create_markdown.py` принимает `--providers-config` (по умолчанию `./providers.yaml`). `load_providers_config` проверяет наличие YAML, секции `providers` и уникальность `api_key_env` среди провайдеров. `resolve_role(role_cfg, providers)` проверяет провайдера и модель, затем возвращает плоскую структуру с `provider`, `model`, `api_key_env`, `base_url` и одноуровневым `fallback` с теми же полями. Неизвестные провайдеры, модели, роли и дубли ключевых переменных являются ошибкой.

Промпты и RAG-секции (`defaults`, `references`) остаются в `create_markdown_config.yaml`. Для `--reg` промпт `reg_extract` сохраняется отдельно, а модель берётся из разрешённой роли `ai_postprocess`.
