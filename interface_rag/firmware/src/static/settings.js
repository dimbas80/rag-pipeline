/* ============================================================
   Настройки interface_RAG (решения 11–15; реворк 41–46, §13 арх-ры).
   Рендерит структурированные секции в #settings-root:
   - Провайдеры: карточка на провайдер (имя/rename, base_url, ключ
     маской, список моделей с тегами, «+ модель», per-provider
     «Обновить модели», «Удалить» с проверкой использования на сервере);
   - Роли: каскадный выбор Провайдер → Модель + Fallback;
   - Yandex OCR / Телеграм (ключи .env, маскированы);
   - параметры узлов графа (search_config.yaml) с комментариями;
   - единая кнопка «Сохранить» (все секции одним действием).
   Без сырых дампов providers.yaml/.env.
   Используется и вкладкой «Настройки» в index.html, и отдельной
   страницей static/settings.html.
   ============================================================ */
(function () {
  "use strict";

  var MASK_PREFIX = "\u2022\u2022\u2022\u2022"; // •••• — значение из GET /api/settings/env

  var ROLE_DEFS = {
    chat: {
      title: "Основная чат-модель",
      comment: "Генерация ответов: постобработка Markdown (ai_postprocess) и обработка запросов в чате (query_processing). Выбор пишется синхронно в обе роли.",
      tag: null,
      role: ["create_markdown", "ai_postprocess"],
      sync: "chat"
    },
    vision: {
      title: "Основная vision-модель",
      comment: "Распознавание таблиц при конвертации (table_vision) и полей формы регистрации (registration_vision). Выбор пишется синхронно в обе роли.",
      tag: "vision",
      role: ["create_markdown", "table_vision"],
      sync: "vision"
    },
    embedding: {
      title: "Основная embedding-модель",
      comment: "Построение векторных представлений фрагментов документов для семантического поиска.",
      tag: "embedding",
      role: ["build_search_index", "embedding"],
      sync: "embedding"
    },
    rerank: {
      title: "Основная rerank-модель",
      comment: "Переранжирование найденных фрагментов перед формированием ответа.",
      tag: "rerank",
      role: ["build_search_index", "rerank"],
      sync: "rerank"
    }
  };

  var NODE_COMMENTS = {
    analyze_query: "Анализ запроса: разбор вопроса, определение намерения и ключевых сущностей перед поиском.",
    reformulate_query: "Переформулировка: уточнение или расширение запроса, если первый поиск дал мало результатов.",
    ask_clarification: "Уточнение: если запрос неоднозначен — задать пользователю уточняющий вопрос.",
    generate_answer: "Формирование ответа: генерация итогового ответа с цитатами по найденным фрагментам."
  };

  // Канонические ключи .env (секции «Yandex OCR» и «Телеграм»).
  var KNOWN_ENV = [
    { key: "YANDEX_API_KEY", label: "Yandex API ключ", section: "yandex" },
    { key: "YANDEX_FOLDER_ID", label: "Yandex Folder ID", section: "yandex" },
    { key: "TELEGRAM_BOT_TOKEN", label: "Telegram Bot Token", section: "telegram" },
    { key: "TELEGRAM_ALLOWED_USERS", label: "Допустимые чаты (ID через запятую)", section: "telegram" }
  ];

  // Сайдбар настроек (решение №27; реворк №41/42): разделы и порядок в меню.
  var NAV_SECTIONS = [
    { id: "providers", label: "Провайдеры" },
    { id: "roles", label: "Роли" },
    { id: "yandex", label: "Yandex OCR" },
    { id: "telegram", label: "Телеграм" },
    { id: "graph", label: "Параметры графа" },
    { id: "qdrant", label: "Папка Qdrant" }
  ];

  var state = {
    providers: { providers: {}, roles: {} },
    env: {},
    search: { nodes: {} },
    status: {},
    qdrant: { path: "", default: "", overridden: false },
    bot: null,        // статус interface-rag-bot.service (null = ещё не запрашивали)
    botResult: null,  // { text, cls } — сообщение после перезапуска
    botBusy: false,   // идёт запрос (статус/рестарт) — не перерисовывать кнопку
    activeSection: "providers",
    qdrantReset: false,
    loaded: false,
    busy: false,
    removedProviders: {} // имя провайдера -> true: помечен к удалению до «Сохранить» (№41)
  };

  function $(id) { return document.getElementById(id); }
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function api(url, options) {
    var response = await fetch(url, options);
    var data = null;
    try { data = await response.json(); } catch (_) { /* пустое тело */ }
    if (!response.ok) {
      throw new Error((data && (data.detail || data.error)) || ("HTTP " + response.status));
    }
    return data;
  }

  /* ---------- загрузка ---------- */
  async function loadSettings() {
    try {
      var results = await Promise.all([
        api("/api/settings/providers"),
        api("/api/settings/env"),
        api("/api/settings/search-config"),
        api("/api/settings/status"),
        api("/api/settings/qdrant")
      ]);
      state.providers = results[0] || { providers: {}, roles: {} };
      state.env = results[1] || {};
      state.search = results[2] || { nodes: {} };
      state.status = results[3] || {};
      state.qdrant = results[4] || { path: "", default: "", overridden: false };
      state.qdrantReset = false;
      state.loaded = true;
      render();
    } catch (error) {
      renderError("Не удалось загрузить настройки: " + error.message);
    }
  }

  function renderError(message) {
    var root = $("settings-root");
    if (root) root.innerHTML = '<div class="status-line err">' + esc(message) + "</div>";
  }

  /* ---------- рендер ---------- */
  function sectionHtml(id, inner) {
    return '<div class="settings-section" data-section="' + id + '"' +
      (id === state.activeSection ? "" : " hidden") + ">" + inner + "</div>";
  }

  function render() {
    var root = $("settings-root");
    if (!root) return;
    var html =
      '<div class="settings-toolbar">' +
        '<button id="settings-save" class="btn primary">Сохранить</button>' +
        '<span id="settings-status" class="status-line"></span>' +
        '<span class="spacer" style="flex:1 1 auto"></span>' +
        '<span class="badge">' + esc(state.status.environment || "—") + '</span>' +
        '<span class="badge ' + (state.status.write_enabled ? "ok" : "warn") + '">Qdrant запись: ' +
          (state.status.write_enabled ? "разрешена" : "запрещена (dev)") + '</span>' +
      "</div>" +
      '<div class="settings-layout">' +
        '<nav class="settings-nav">' +
          NAV_SECTIONS.map(function (section) {
            return '<button type="button" data-section="' + section.id + '"' +
              (section.id === state.activeSection ? ' class="active"' : "") + ">" +
              esc(section.label) + "</button>";
          }).join("") +
        "</nav>" +
        '<div class="settings-content">' +
          sectionHtml("providers", renderProvidersSection()) +
          sectionHtml("roles", renderRolesSection()) +
          sectionHtml("yandex", renderYandexCard()) +
          sectionHtml("telegram", renderOtherKeysCard()) +
          sectionHtml("graph", renderGraphCard()) +
          sectionHtml("qdrant", renderQdrantCard()) +
        "</div>" +
      "</div>";

    root.innerHTML = html;
    bindToolbar();
    bindProviderControls();
    bindRoleControls();
    bindGraphInputs();
    bindEnvInputs();
    bindQdrantControls();
    bindBotControls();
    bindSettingsNav();
  }

  function bindSettingsNav() {
    document.querySelectorAll(".settings-nav button").forEach(function (button) {
      button.addEventListener("click", function () {
        state.activeSection = button.getAttribute("data-section");
        applySectionVisibility();
      });
    });
  }

  function applySectionVisibility() {
    document.querySelectorAll(".settings-section").forEach(function (section) {
      section.hidden = section.getAttribute("data-section") !== state.activeSection;
    });
    document.querySelectorAll(".settings-nav button").forEach(function (button) {
      button.classList.toggle("active", button.getAttribute("data-section") === state.activeSection);
    });
    // Статус бота проверяем при каждом открытии раздела «Телеграм»:
    // systemd active не гарантирует, что бот отвечает (сеть/прокси).
    if (state.activeSection === "telegram") refreshBotStatus();
  }

  function optionHtml(options, selectedValue) {
    var html = '<option value=""></option>';
    options.forEach(function (item) {
      var selected = item.value === selectedValue ? " selected" : "";
      html += '<option value="' + esc(item.value) + '"' + selected + ">" + esc(item.text) + "</option>";
    });
    return html;
  }

  function providerNames() {
    return Object.keys(state.providers.providers || {});
  }

  function providerModels(name, tag) {
    var provider = (state.providers.providers || {})[name];
    var models = (provider && provider.models) || {};
    return Object.keys(models).filter(function (m) {
      return !tag || models[m] === tag;
    });
  }

  function currentRoleSpec(kind) {
    var def = ROLE_DEFS[kind];
    var roles = (state.providers.roles || {})[def.role[0]] || {};
    return roles[def.role[1]] || {};
  }

  /* ---------- секция «Провайдеры» (решения 41–45) ---------- */
  function providerCardHtml(name, provider) {
    var models = provider.models || {};
    var modelRows = Object.keys(models).map(function (modelName) {
      return modelRowHtml(modelName, models[modelName]);
    }).join("");
    var envName = provider.api_key_env || "";
    return '<div class="settings-card provider-card" data-provider="' + esc(name) + '">' +
      '<div class="provider-head">' +
        '<label class="provider-name-field">Имя <input type="text" data-provider-name value="' + esc(name) + '"></label>' +
        '<label class="provider-url-field">Base URL <input type="text" data-provider-url value="' + esc(provider.base_url || "") + '" placeholder="https://api.example.com/v1"></label>' +
      "</div>" +
      (envName ? envRowHtml(envName, "API ключ (" + envName + ")", state.env[envName], true) : '<div class="field-help err">У провайдера нет api_key_env</div>') +
      '<details class="models-details" open><summary>Модели (' + Object.keys(models).length + ")</summary>" +
        '<div class="model-list" data-model-list>' + modelRows + "</div>" +
        '<div class="model-add">' +
          '<input type="text" data-new-model-name placeholder="имя модели">' +
          '<select data-new-model-tag>' + tagOptionsHtml("chat") + "</select>" +
          '<button type="button" class="btn ghost" data-model-add>+ модель</button>' +
        "</div>" +
      "</details>" +
      '<div class="provider-actions">' +
        '<button type="button" class="btn" data-provider-refresh>Обновить модели</button>' +
        '<button type="button" class="btn danger" data-provider-delete>Удалить</button>' +
        '<span class="provider-refresh-result" data-provider-refresh-result></span>' +
      "</div>" +
    "</div>";
  }

  function tagOptionsHtml(selected) {
    return ["chat", "vision", "embedding", "rerank"].map(function (tag) {
      return '<option value="' + tag + '"' + (tag === selected ? " selected" : "") + ">" + tag + "</option>";
    }).join("");
  }

  function modelRowHtml(modelName, tag) {
    return '<div class="model-row" data-model="' + esc(modelName) + '">' +
      '<span class="model-name" title="' + esc(modelName) + '">' + esc(modelName) + "</span>" +
      '<select data-model-tag>' + tagOptionsHtml(tag) + "</select>" +
      '<button type="button" class="btn ghost model-del" data-model-del title="Удалить модель из списка">×</button>' +
    "</div>";
  }

  function renderProvidersSection() {
    var providers = state.providers.providers || {};
    var removed = Object.keys(state.removedProviders);
    var cards = providerNames().filter(function (name) { return !state.removedProviders[name]; })
      .map(function (name) { return providerCardHtml(name, providers[name] || {}); }).join("");
    var removedBar = removed.length
      ? '<div class="provider-removed-bar">Помечены к удалению: ' +
        removed.map(esc).join(", ") + " — применится при «Сохранить». " +
        '<button type="button" class="btn ghost" id="restore-providers">Вернуть все</button></div>'
      : "";
    return '<section class="settings-card"><h3>Провайдеры</h3>' +
      '<p class="muted">Каждая карточка — провайдер из providers.yaml: имя (переименование обновит ссылки в ролях), ' +
      'base_url, API-ключ (хранится в .env, показан маской) и список моделей с тегами. ' +
      '«Обновить модели» запрашивает /models только у этого провайдера.</p>' +
      '<div class="provider-toolbar"><button id="add-provider" class="btn">Добавить провайдера</button></div>' +
      removedBar +
      '<div class="provider-cards">' + (cards || '<div class="muted small">Провайдеры не настроены — добавьте кнопкой выше.</div>') +
      "</div></section>";
  }

  /* ---------- секция «Роли» (решение №42: каскад Провайдер → Модель) ---------- */
  function cascadeSelectsHtml(kind, spec) {
    var def = ROLE_DEFS[kind];
    var providerValue = spec.provider || "";
    var modelValue = spec.model || "";
    return '<label>Провайдер <select data-role-provider="' + kind + '">' +
        optionHtml(providerNames().map(function (n) { return { value: n, text: n }; }), providerValue) +
      "</select></label>" +
      '<label>Модель <select data-role-model="' + kind + '"></select></label>' +
      '<label class="fallback-toggle"><input type="checkbox" data-fallback-check="' + kind + '"' +
        (spec.fallback && spec.fallback.provider ? " checked" : "") + "> Fallback</label>" +
      '<div class="fallback-fields" data-fallback-fields="' + kind + '"><label>Провайдер <select data-role-provider-fb="' + kind + '">' +
        optionHtml(providerNames().map(function (n) { return { value: n, text: n }; }), (spec.fallback || {}).provider || "") +
      "</select></label><label>Модель <select data-role-model-fb=\"" + kind + '"></select></label></div>';
  }

  function renderRolesSection() {
    var html = '<section class="settings-card"><h3>Роли</h3>' +
      '<p class="muted">Каскадный выбор: сначала провайдер, затем его модель (список фильтруется по назначению роли). ' +
      "Синхронные роли записываются вместе: chat → ai_postprocess + query_processing; " +
      "vision → table_vision + registration_vision.</p>" +
      '<div class="role-cards">';
    Object.keys(ROLE_DEFS).forEach(function (kind) {
      var def = ROLE_DEFS[kind];
      var spec = currentRoleSpec(kind);
      html += '<div class="role-card" data-kind="' + kind + '">' +
        '<div class="role-head">' + esc(def.title) +
          '<span class="hint" title="' + esc(def.comment) + '">?</span></div>' +
        '<p class="role-desc">' + esc(def.comment) + "</p>" +
        cascadeSelectsHtml(kind, spec) +
      "</div>";
    });
    html += "</div></section>";
    return html;
  }

  // Перестроить список «Модель» из выбранного провайдера с фильтром по тегу роли.
  // Если текущая роль ссылается на модель вне списка — сохранить её отдельной
  // опцией (не теряем роль; как в прежнем bindRoleControls).
  function repopulateRoleModels(kind, suffix, valueOverride) {
    var sel = document.querySelector('[data-role-model' + suffix + '="' + kind + '"]');
    var provSel = document.querySelector('[data-role-provider' + suffix + '="' + kind + '"]');
    if (!sel || !provSel) return;
    var current = valueOverride !== undefined ? valueOverride : sel.value;
    var tag = ROLE_DEFS[kind].tag;
    var names = providerModels(provSel.value, tag);
    var html = '<option value=""></option>';
    names.forEach(function (m) {
      html += '<option value="' + esc(m) + '"' + (m === current ? " selected" : "") + ">" + esc(m) + "</option>";
    });
    if (current && names.indexOf(current) === -1) {
      html += '<option value="' + esc(current) + '" selected>' + esc(current) + " (вне списка)</option>";
    }
    sel.innerHTML = html;
    sel.value = current || "";
  }

  function envRowHtml(key, label, value, isProvider) {
    var masked = state.env[key];
    var display = masked || "";
    var placeholder = masked ? "" : "не задан";
    return '<div class="env-row" data-env-key="' + esc(key) + '">' +
      '<label>' + esc(label) + ' <code>' + esc(key) + "</code></label>" +
      '<div class="env-inputs">' +
        '<input type="text" data-env-value placeholder="' + esc(placeholder) + '" value="' + esc(display) + '">' +
        '<label class="del-label"><input type="checkbox" data-env-delete> удалить</label>' +
      "</div>" +
      '<div class="field-help">' + (isProvider ? "" : "значение маскировано — введите новое, чтобы заменить") + "</div>" +
    "</div>";
  }

  function renderYandexCard() {
    var yandexRows = KNOWN_ENV.filter(function (item) { return item.section === "yandex"; })
      .map(function (item) { return envRowHtml(item.key, item.label, state.env[item.key], false); }).join("");
    return '<section class="settings-card"><h3>Yandex OCR</h3>' +
      '<p class="muted">Ключи распознавания Yandex Vision OCR (используются при конвертации PDF/DOCX).</p>' +
      '<div class="env-grid">' + yandexRows + "</div></section>";
  }

  /* ---------- секция «Телеграм»: статус/перезапуск сервиса бота ---------- */
  function botBadgeHtml() {
    var bot = state.bot;
    if (!bot || bot.loading) return '<span class="badge">статус: проверяю…</span>';
    if (!bot.available) {
      return '<span class="badge">недоступно: ' + esc(bot.reason || "юнит не найден") + "</span>";
    }
    if (!bot.active) {
      return '<span class="badge err">остановлен (' + esc(bot.sub_state || "—") + ")</span>";
    }
    if (bot.telegram_ok === false) {
      return '<span class="badge warn">работает, нет связи с Telegram</span>';
    }
    return '<span class="badge ok">работает</span>';
  }

  function botStatusInnerHtml() {
    var bot = state.bot;
    var meta = "";
    if (bot && bot.available) {
      meta = "запущен: " + (bot.started_at || "—") +
        ", рестартов: " + (bot.restarts != null ? bot.restarts : 0);
      if (bot.token_configured === false) meta += ", токен не задан";
    }
    var result = state.botResult;
    var resultHtml = result
      ? '<span class="bot-result ' + esc(result.cls || "") + '">' + esc(result.text) + "</span>"
      : '<span class="bot-result"></span>';
    var restartDisabled = (!bot || !bot.available || state.botBusy) ? " disabled" : "";
    return botBadgeHtml() +
      '<span class="bot-meta">' + esc(meta) + "</span>" +
      resultHtml +
      '<span class="spacer" style="flex:1 1 auto"></span>' +
      '<button type="button" id="bot-restart" class="btn"' + restartDisabled + ">Перезапустить бота</button>";
  }

  function updateBotStatusBlock() {
    var node = $("bot-status-block");
    if (!node) return;
    node.innerHTML = botStatusInnerHtml();
    bindBotControls();
  }

  async function refreshBotStatus() {
    if (state.botBusy) return;
    state.botBusy = true;
    state.bot = { loading: true };
    updateBotStatusBlock();
    try {
      state.bot = await api("/api/settings/telegram/bot");
    } catch (error) {
      state.bot = { available: false, reason: "эндпоинт недоступен (" + error.message + ")" };
    }
    state.botBusy = false;
    updateBotStatusBlock();
  }

  function bindBotControls() {
    var button = $("bot-restart");
    if (!button || button.disabled) return;
    button.addEventListener("click", async function () {
      state.botBusy = true;
      button.disabled = true;
      button.textContent = "Перезапускаю…";
      state.botResult = null;
      try {
        state.bot = await api("/api/settings/telegram/bot/restart", { method: "POST" });
        state.botResult = { text: "Перезапущен", cls: "ok" };
      } catch (error) {
        state.botResult = { text: "Ошибка: " + error.message, cls: "err" };
      }
      state.botBusy = false;
      updateBotStatusBlock();
    });
  }

  function renderOtherKeysCard() {
    var otherRows = KNOWN_ENV.filter(function (item) { return item.section === "telegram"; })
      .map(function (item) { return envRowHtml(item.key, item.label, state.env[item.key], false); }).join("");
    return '<section class="settings-card"><h3>Телеграм</h3>' +
      '<p class="muted">Настройки Телеграм-бота (interface-rag-bot.service). Изменения ключей применяются не сразу: ' +
      "сохраните, затем нажмите «Перезапустить бота».</p>" +
      '<div class="bot-status-row" id="bot-status-block">' + botStatusInnerHtml() + "</div>" +
      '<div class="env-grid">' + (otherRows || '<div class="muted small">Телеграм-ключи не настроены.</div>') +
      "</div></section>";
  }

  function renderQdrantCard() {
    var q = state.qdrant || { path: "", default: "", overridden: false };
    var hint = q.overridden
      ? "Указан override (QDRANT_PATH в .env); дефолт из config.yaml: " + esc(q.default)
      : "Используется дефолт из config.yaml: " + esc(q.default);
    return '<section class="settings-card"><h3>Папка Qdrant</h3>' +
      '<p class="muted">База семантического поиска. Путь сохраняется в .env (QDRANT_PATH) и передаётся пайплайну через --qdrant-path. Пустая строка = вернуться к дефолту.</p>' +
      '<div class="env-row" data-qdrant-row>' +
        '<label>Путь к базе Qdrant <code>QDRANT_PATH</code></label>' +
        '<div class="env-inputs">' +
          '<input type="text" id="qdrant-path-input" value="' + esc(q.path) + '" placeholder="' + esc(q.default) + '">' +
          '<button type="button" id="qdrant-reset" class="btn ghost">Сбросить к дефолту</button>' +
        "</div>" +
        '<div class="field-help">' + hint + "</div>" +
      "</div></section>";
  }

  function renderGraphCard() {
    var nodes = state.search.nodes || {};
    var html = '<section class="settings-card"><h3>Параметры графа</h3>' +
      '<p class="muted">Параметры узлов поискового графа (search_config.yaml) — температура и максимум токенов генерации.</p>';
    Object.keys(NODE_COMMENTS).forEach(function (name) {
      var node = nodes[name] || { temperature: 0.0, max_tokens: 256 };
      var temp = Math.round((Number(node.temperature) || 0) * 1000) / 1000;
      html += '<div class="node-row" data-node="' + esc(name) + '">' +
        '<div><div class="node-name">' + esc(name) + "</div>" +
        '<div class="node-comment">' + esc(NODE_COMMENTS[name]) + "</div></div>" +
        '<label>temperature <input type="number" step="0.1" min="0" max="2" data-node-temp value="' +
          esc(temp) + '"></label>' +
        '<label>max_tokens <input type="number" step="1" min="1" data-node-tokens value="' +
          esc(node.max_tokens) + '"></label>' +
      "</div>";
    });
    html += "</section>";
    return html;
  }

  /* ---------- привязка событий ---------- */
  function bindToolbar() {
    var save = $("settings-save");
    if (save) save.addEventListener("click", saveSettings);
  }

  /* Карточки провайдеров: rename-url, модели, refresh, delete (решения 41–45). */
  function bindProviderControls() {
    var add = $("add-provider");
    if (add) add.addEventListener("click", openProviderDialog);
    var restore = $("restore-providers");
    if (restore) restore.addEventListener("click", function () {
      state.removedProviders = {};
      render();
    });
    document.querySelectorAll(".provider-card").forEach(function (card) {
      var origName = card.getAttribute("data-provider");
      var list = card.querySelector("[data-model-list]");
      var countEl = card.querySelector("summary");
      var updateCount = function () {
        if (countEl) countEl.textContent = "Модели (" + list.querySelectorAll(".model-row").length + ")";
      };
      // «+ модель» — добавить строку в список (без тега имени: вложенность не ломается)
      var addBtn = card.querySelector("[data-model-add]");
      var nameInput = card.querySelector("[data-new-model-name]");
      if (addBtn && list && nameInput) {
        addBtn.addEventListener("click", function () {
          var modelName = nameInput.value.trim();
          if (!modelName) return;
          var existing = list.querySelector('[data-model="' + CSS.escape(modelName) + '"]');
          if (existing) { nameInput.value = ""; return; }
          var tag = card.querySelector("[data-new-model-tag]").value;
          var wrap = document.createElement("div");
          wrap.innerHTML = modelRowHtml(modelName, tag);
          var row = wrap.firstChild;
          list.appendChild(row);
          bindModelRow(row, updateCount);
          nameInput.value = "";
          updateCount();
        });
      }
      // существующие строки моделей
      if (list) {
        list.querySelectorAll(".model-row").forEach(function (row) { bindModelRow(row, updateCount); });
      }
      // «Обновить модели» per-provider (№45) — сразу на сервер, изоляция ошибки
      var refreshBtn = card.querySelector("[data-provider-refresh]");
      var resultEl = card.querySelector("[data-provider-refresh-result]");
      if (refreshBtn) {
        refreshBtn.addEventListener("click", async function () {
          refreshBtn.disabled = true;
          if (resultEl) resultEl.textContent = "обновляю…";
          try {
            var data = await api("/api/settings/providers/" + encodeURIComponent(origName) + "/refresh", { method: "POST" });
            if (data.ok) {
              if (resultEl) { resultEl.className = "provider-refresh-result ok"; resultEl.textContent = "✓ моделей: " + Object.keys(data.models || {}).length; }
              await loadSettings(); // перерисует карточки и каскады
            } else {
              if (resultEl) { resultEl.className = "provider-refresh-result err"; resultEl.textContent = "✗ " + (data.error || "ошибка"); }
            }
          } catch (error) {
            if (resultEl) { resultEl.className = "provider-refresh-result err"; resultEl.textContent = "✗ " + error.message; }
          } finally {
            refreshBtn.disabled = false;
          }
        });
      }
      // «Удалить» (№41) — клиентская пометка; проверка использования — на сервере при Save
      var delBtn = card.querySelector("[data-provider-delete]");
      if (delBtn) {
        delBtn.addEventListener("click", function () {
          if (!confirm("Удалить провайдера «" + origName + "»?\n" +
              "Если он назначен в роль — удаление отклонится при сохранении.")) return;
          state.removedProviders[origName] = true;
          render();
        });
      }
    });
  }

  function bindModelRow(row, updateCount) {
    var del = row.querySelector("[data-model-del]");
    if (del) del.addEventListener("click", function () {
      row.remove();
      if (updateCount) updateCount();
    });
  }

  /* Каскад «Провайдер → Модель» + fallback (решение №42). */
  function bindRoleControls() {
    Object.keys(ROLE_DEFS).forEach(function (kind) {
      var spec = currentRoleSpec(kind);
      repopulateRoleModels(kind, "", spec.model || "");
      repopulateRoleModels(kind, "-fb", (spec.fallback || {}).model || "");
      var fbFields = document.querySelector('[data-fallback-fields="' + kind + '"]');
      var fbCheck = document.querySelector('[data-fallback-check="' + kind + '"]');
      if (fbFields) fbFields.hidden = !(fbCheck && fbCheck.checked);
    });
    document.querySelectorAll("[data-role-provider]").forEach(function (select) {
      var kind = select.getAttribute("data-role-provider");
      select.addEventListener("change", function () { repopulateRoleModels(kind, "", ""); });
    });
    document.querySelectorAll("[data-role-provider-fb]").forEach(function (select) {
      var kind = select.getAttribute("data-role-provider-fb");
      select.addEventListener("change", function () { repopulateRoleModels(kind, "-fb", ""); });
    });
    document.querySelectorAll("[data-fallback-check]").forEach(function (check) {
      check.addEventListener("change", function () {
        var kind = check.getAttribute("data-fallback-check");
        var fields = document.querySelector('[data-fallback-fields="' + kind + '"]');
        if (fields) fields.hidden = !check.checked;
        if (!check.checked) {
          var fbModel = document.querySelector('[data-role-model-fb="' + kind + '"]');
          if (fbModel) fbModel.value = "";
        }
      });
    });
  }

  function bindGraphInputs() {
    // значения читаются при сохранении; валидация на лету не требуется
  }

  function bindEnvInputs() {
    document.querySelectorAll("[data-env-value]").forEach(function (input) {
      input.addEventListener("input", function () {
        var row = input.closest(".env-row");
        var key = row.getAttribute("data-env-key");
        var deleteCheck = row.querySelector("[data-env-delete]");
        // Новое значение снимает «удалить»
        if (input.value.trim() && deleteCheck) deleteCheck.checked = false;
      });
    });
  }

  function bindQdrantControls() {
    var reset = $("qdrant-reset");
    if (reset) reset.addEventListener("click", function () {
      var input = $("qdrant-path-input");
      if (input && state.qdrant) input.value = state.qdrant.default || "";
      state.qdrantReset = true;
      setStatus("Сброс к дефолту — нажмите «Сохранить», чтобы применить.", "warn");
    });
    var input = $("qdrant-path-input");
    if (input) input.addEventListener("input", function () {
      state.qdrantReset = false;
    });
  }

  /* ---------- сохранение (единая кнопка) ---------- */
  function setStatus(message, cls) {
    var status = $("settings-status");
    if (status) {
      status.textContent = message || "";
      status.className = "status-line" + (cls ? " " + cls : "");
    }
  }

  function collectRoleSpec(kind) {
    var mainProv = document.querySelector('[data-role-provider="' + kind + '"]');
    var mainModel = document.querySelector('[data-role-model="' + kind + '"]');
    if (!mainProv || !mainProv.value || !mainModel || !mainModel.value) return null;
    var spec = { provider: mainProv.value, model: mainModel.value };
    var check = document.querySelector('[data-fallback-check="' + kind + '"]');
    var fbProv = document.querySelector('[data-role-provider-fb="' + kind + '"]');
    var fbModel = document.querySelector('[data-role-model-fb="' + kind + '"]');
    if (check && check.checked && fbProv && fbProv.value && fbModel && fbModel.value) {
      spec.fallback = { provider: fbProv.value, model: fbModel.value };
    }
    return spec;
  }

  /* Собрать правки по карточкам провайдеров: {origName: {name, base_url, models}}. */
  function collectProviderEdits() {
    var edits = [];
    document.querySelectorAll(".provider-card").forEach(function (card) {
      var origName = card.getAttribute("data-provider");
      var name = card.querySelector("[data-provider-name]").value.trim();
      var baseUrl = card.querySelector("[data-provider-url]").value.trim();
      var models = {};
      card.querySelectorAll("[data-model-list] .model-row").forEach(function (row) {
        var modelName = row.getAttribute("data-model");
        models[modelName] = row.querySelector("[data-model-tag]").value;
      });
      var original = (state.providers.providers || {})[origName] || {};
      var changed = name !== origName || baseUrl !== (original.base_url || "") ||
        JSON.stringify(models) !== JSON.stringify(original.models || {});
      if (changed && name) {
        edits.push({ origName: origName, patch: { name: name, base_url: baseUrl, models: models } });
      }
    });
    return edits;
  }

  function collectEnv() {
    var values = {};
    var deleteKeys = [];
    document.querySelectorAll(".env-row").forEach(function (row) {
      var key = row.getAttribute("data-env-key");
      if (!key) return;
      var input = row.querySelector("[data-env-value]");
      var deleteCheck = row.querySelector("[data-env-delete]");
      if (deleteCheck && deleteCheck.checked) {
        deleteKeys.push(key);
        return;
      }
      var value = input ? input.value.trim() : "";
      if (!value || value.indexOf(MASK_PREFIX) === 0) return; // маска/пусто — не трогаем
      values[key] = value;
    });
    return { values: values, delete: deleteKeys };
  }

  function collectGraphNodes() {
    var nodes = {};
    var errors = [];
    document.querySelectorAll("[data-node]").forEach(function (row) {
      var name = row.getAttribute("data-node");
      var temp = parseFloat(row.querySelector("[data-node-temp]").value);
      var tokens = parseInt(row.querySelector("[data-node-tokens]").value, 10);
      if (isNaN(temp) || temp < 0 || temp > 2) { errors.push(name + ": temperature от 0 до 2"); return; }
      if (isNaN(tokens) || tokens < 1) { errors.push(name + ": max_tokens >= 1"); return; }
      nodes[name] = { temperature: Math.round(temp * 1000) / 1000, max_tokens: tokens };
    });
    return { nodes: nodes, errors: errors };
  }

  async function saveSettings() {
    if (state.busy) return;
    state.busy = true;
    setStatus("Сохранение…");
    try {
      // 1. Провайдеры (решения 41–44): сначала переименования/правки, затем удаления.
      var providerErrors = [];
      var edits = collectProviderEdits();
      for (var i = 0; i < edits.length; i++) {
        try {
          await api("/api/settings/providers/" + encodeURIComponent(edits[i].origName), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(edits[i].patch)
          });
        } catch (error) {
          providerErrors.push(edits[i].origName + ": " + error.message);
        }
      }
      var removedNames = Object.keys(state.removedProviders);
      for (var j = 0; j < removedNames.length; j++) {
        try {
          await api("/api/settings/providers/" + encodeURIComponent(removedNames[j]), { method: "DELETE" });
          delete state.removedProviders[removedNames[j]];
        } catch (error) {
          providerErrors.push("удалить " + removedNames[j] + ": " + error.message);
        }
      }
      // 2. Роли (синхронная запись chat/vision в обе роли).
      var roleErrors = [];
      for (var kind in ROLE_DEFS) {
        var spec = collectRoleSpec(kind);
        if (!spec) continue;
        try {
          await api("/api/settings/providers/roles", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ kind: ROLE_DEFS[kind].sync, spec: spec })
          });
        } catch (error) {
          roleErrors.push(kind + ": " + error.message);
        }
      }
      // 3. Параметры графа.
      var graph = collectGraphNodes();
      if (graph.errors.length) {
        setStatus("Проверьте параметры графа: " + graph.errors.join("; "), "err");
        state.busy = false;
        return;
      }
      await api("/api/settings/search-config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nodes: graph.nodes })
      });
      // 4. Ключи .env.
      var env = collectEnv();
      await api("/api/settings/env", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(env)
      });
      // 5. Папка Qdrant (решение №29) — только если изменилась или запрошен сброс.
      var qdrantInput = $("qdrant-path-input");
      if (qdrantInput) {
        var wantReset = state.qdrantReset;
        state.qdrantReset = false;
        var currentPath = state.qdrant ? state.qdrant.path : "";
        var inputPath = qdrantInput.value.trim();
        if (wantReset || inputPath !== currentPath) {
          await api("/api/settings/qdrant", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: wantReset ? "" : inputPath })
          });
        }
      }
      await loadSettings();
      var problems = providerErrors.concat(roleErrors.map(function (m) { return "роль " + m; }));
      if (problems.length) {
        setStatus("Сохранено, но не всё записано: " + problems.join("; "), "warn");
      } else {
        setStatus("Сохранено ✓", "ok");
      }
    } catch (error) {
      setStatus("Ошибка сохранения: " + error.message, "err");
    } finally {
      state.busy = false;
    }
  }

  /* ---------- «Добавить провайдера» ---------- */
  function openProviderDialog() {
    var existing = $("provider-dialog");
    if (existing) { existing.remove(); }

    var dialog = document.createElement("dialog");
    dialog.id = "provider-dialog";
    dialog.innerHTML =
      "<h2>Добавить провайдера</h2>" +
      '<div class="dialog-field"><label>Имя провайдера</label><input id="provider-name" type="text" placeholder="например: myprovider"></div>' +
      '<div class="dialog-field"><label>Base URL</label><input id="provider-url" type="text" placeholder="https://api.example.com/v1"></div>' +
      '<div class="dialog-field"><label>API ключ</label><input id="provider-key" type="password" placeholder="sk-…"></div>' +
      '<div class="scan-result" id="provider-scan-result"></div>' +
      '<div class="dialog-actions">' +
        '<button id="provider-cancel" class="btn ghost">Отмена</button>' +
        '<button id="provider-test" class="btn">Тест</button>' +
        '<button id="provider-add" class="btn primary" disabled>Добавить</button>' +
      "</div>";
    document.body.appendChild(dialog);
    dialog.showModal();

    var nameInput = $("provider-name");
    var urlInput = $("provider-url");
    var keyInput = $("provider-key");
    var resultBox = $("provider-scan-result");
    var addButton = $("provider-add");
    var scan = null;

    $("provider-cancel").addEventListener("click", function () { dialog.close(); });
    $("provider-test").addEventListener("click", async function () {
      resultBox.textContent = "Проверяю…";
      addButton.disabled = true;
      try {
        scan = await api("/api/settings/providers/scan", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ base_url: urlInput.value.trim(), api_key: keyInput.value })
        });
        var byTag = {};
        (scan.models || []).forEach(function (item) {
          byTag[item.tag] = (byTag[item.tag] || 0) + 1;
        });
        resultBox.innerHTML = "Моделей найдено: " + (scan.models || []).length +
          (Object.keys(byTag).length
            ? " — " + Object.keys(byTag).map(function (t) { return esc(t) + ": " + byTag[t]; }).join(", ")
            : "") + ".";
        addButton.disabled = false;
      } catch (error) {
        resultBox.innerHTML = '<span class="err">' + esc(error.message) + "</span>";
      }
    });

    $("provider-add").addEventListener("click", async function () {
      addButton.disabled = true;
      var name = nameInput.value.trim();
      var key = keyInput.value;
      var envName = (name.toUpperCase().replace(/[^A-Z0-9]+/g, "_") || "PROVIDER") + "_API_KEY";
      if (!name || !scan) { resultBox.textContent = "Сначала нажмите «Тест»."; addButton.disabled = false; return; }
      try {
        await api("/api/settings/providers/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: name,
            base_url: urlInput.value.trim(),
            api_key_env: envName,
            models: (scan.models || []).map(function (item) { return item.name; })
          })
        });
        await api("/api/settings/env", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ values: { [envName]: key }, delete: [] })
        });
        dialog.close();
        await loadSettings();
        setStatus("Провайдер «" + name + "» добавлен ✓", "ok");
      } catch (error) {
        resultBox.innerHTML = '<span class="err">' + esc(error.message) + "</span>";
        addButton.disabled = false;
      }
    });
  }

  window.initSettings = loadSettings;
})();
