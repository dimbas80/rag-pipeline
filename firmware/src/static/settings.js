/* ============================================================
   Настройки interface_RAG (решения 11–15, §5.3 требований).
   Рендерит структурированные секции в #settings-root:
   - провайдеры и роли (chat/vision/embedding/rerank): «Основная
     модель» + чекбокс «Fallback» + пояснение роли;
   - Yandex OCR и ключи API (маскированы);
   - параметры узлов графа (search_config.yaml) с комментариями;
   - единая кнопка «Сохранить» (все секции одним действием),
     «Обновить модели» (POST /api/settings/providers/refresh),
     «Добавить провайдера» (модальное окно).
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

  // Канонические ключи .env (секция «Yandex OCR» и «Прочие ключи»).
  var KNOWN_ENV = [
    { key: "YANDEX_API_KEY", label: "Yandex API ключ", section: "yandex" },
    { key: "YANDEX_FOLDER_ID", label: "Yandex Folder ID", section: "yandex" },
    { key: "TELEGRAM_BOT_TOKEN", label: "Telegram Bot Token", section: "other" }
  ];

  // Сайдбар настроек (решение №27): разделы и порядок в меню.
  var NAV_SECTIONS = [
    { id: "providers", label: "Провайдеры и роли" },
    { id: "yandex", label: "Yandex OCR" },
    { id: "provider-keys", label: "Ключи API провайдеров" },
    { id: "other-keys", label: "Прочие ключи" },
    { id: "graph", label: "Параметры графа" },
    { id: "qdrant", label: "Папка Qdrant" }
  ];

  var state = {
    providers: { providers: {}, roles: {} },
    env: {},
    search: { nodes: {} },
    status: {},
    qdrant: { path: "", default: "", overridden: false },
    activeSection: "providers",
    qdrantReset: false,
    loaded: false,
    busy: false
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
        '<button id="settings-refresh" class="btn">Обновить модели</button>' +
        '<button id="add-provider" class="btn">Добавить провайдера</button>' +
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
          sectionHtml("providers", renderProvidersCard()) +
          sectionHtml("yandex", renderYandexCard()) +
          sectionHtml("provider-keys", renderProviderKeysCard()) +
          sectionHtml("other-keys", renderOtherKeysCard()) +
          sectionHtml("graph", renderGraphCard()) +
          sectionHtml("qdrant", renderQdrantCard()) +
          '<div id="refresh-results" hidden></div>' +
        "</div>" +
      "</div>";

    root.innerHTML = html;
    bindToolbar();
    bindRoleControls();
    bindGraphInputs();
    bindEnvInputs();
    bindQdrantControls();
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
  }

  function modelOptions(tag) {
    var options = [];
    var providers = state.providers.providers || {};
    Object.keys(providers).forEach(function (providerName) {
      var models = providers[providerName].models || {};
      Object.keys(models).forEach(function (modelName) {
        if (tag && models[modelName] !== tag) return;
        options.push({ value: providerName + "::" + modelName, text: providerName + " / " + modelName });
      });
    });
    return options;
  }

  function currentRoleSpec(kind) {
    var def = ROLE_DEFS[kind];
    var roles = (state.providers.roles || {})[def.role[0]] || {};
    return roles[def.role[1]] || {};
  }

  function optionHtml(options, selectedValue) {
    var html = '<option value=""></option>';
    options.forEach(function (item) {
      var selected = item.value === selectedValue ? " selected" : "";
      html += '<option value="' + esc(item.value) + '"' + selected + ">" + esc(item.text) + "</option>";
    });
    return html;
  }

  function renderProvidersCard() {
    var html = '<section class="settings-card"><h3>Провайдеры и роли</h3>' +
      '<p class="muted">Выбор основной модели и fallback для каждой роли. Синхронные роли записываются вместе: ' +
      "chat → ai_postprocess и query_processing; vision → table_vision и registration_vision.</p>";
    html += '<div class="role-cards">';
    Object.keys(ROLE_DEFS).forEach(function (kind) {
      var def = ROLE_DEFS[kind];
      var spec = currentRoleSpec(kind);
      var fallback = spec.fallback && spec.fallback.provider && spec.fallback.model ? spec.fallback : null;
      var fallbackValue = fallback ? fallback.provider + "::" + fallback.model : "";
      var mainValue = spec.provider && spec.model ? spec.provider + "::" + spec.model : "";
      html += '<div class="role-card" data-kind="' + kind + '">' +
        '<div class="role-head">' + esc(def.title) +
          '<span class="hint" title="' + esc(def.comment) + '">?</span></div>' +
        '<p class="role-desc">' + esc(def.comment) + "</p>" +
        '<label>Основная модель <select data-role-main="' + kind + '">' +
          optionHtml(modelOptions(def.tag), mainValue) + "</select></label>" +
        '<label class="fallback-toggle"><input type="checkbox" data-fallback-check="' + kind + '"' +
          (fallback ? " checked" : "") + '> Fallback</label>' +
        '<div class="fallback-fields" data-fallback-fields="' + kind + '"' +
          (fallback ? "" : " hidden") + ">" +
          '<label>Fallback модель <select data-role-fallback="' + kind + '">' +
            optionHtml(modelOptions(def.tag), fallbackValue) + "</select></label>" +
        "</div>" +
      "</div>";
    });
    html += "</div></section>";
    return html;
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

  function renderProviderKeysCard() {
    var providers = state.providers.providers || {};
    var providerKeys = [];
    Object.keys(providers).forEach(function (name) {
      var envName = providers[name].api_key_env;
      if (envName) providerKeys.push({ key: envName, label: "Ключ провайдера «" + name + "»" });
    });
    var providerRows = providerKeys
      .map(function (item) { return envRowHtml(item.key, item.label, state.env[item.key], true); }).join("");
    return '<section class="settings-card"><h3>Ключи API провайдеров</h3>' +
      '<p class="muted">Ключи, на которые ссылаются провайдеры из providers.yaml (api_key_env). Значения не отображаются — только маска.</p>' +
      '<div class="env-grid">' + (providerRows || '<div class="muted small">Провайдеры не настроены — добавьте их кнопкой «Добавить провайдера».</div>') +
      "</div></section>";
  }

  function renderOtherKeysCard() {
    var otherRows = KNOWN_ENV.filter(function (item) { return item.section === "other"; })
      .map(function (item) { return envRowHtml(item.key, item.label, state.env[item.key], false); }).join("");
    return '<section class="settings-card"><h3>Прочие ключи</h3>' +
      '<div class="env-grid">' + (otherRows || '<div class="muted small">Прочие ключи не настроены.</div>') +
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
    var refresh = $("settings-refresh");
    var add = $("add-provider");
    if (save) save.addEventListener("click", saveSettings);
    if (refresh) refresh.addEventListener("click", refreshModels);
    if (add) add.addEventListener("click", openProviderDialog);
  }

  function bindRoleControls() {
    document.querySelectorAll("[data-role-main]").forEach(function (select) {
      select.addEventListener("change", function () {
        var kind = select.getAttribute("data-role-main");
        var check = document.querySelector('[data-fallback-check="' + kind + '"]');
        var fields = document.querySelector('[data-fallback-fields="' + kind + '"]');
        var fallback = document.querySelector('[data-role-fallback="' + kind + '"]');
        // Если выбранной модели нет в списке (например, после «Обновить» модели
        // исчезли) — добавить её отдельной опцией, чтобы не потерять роль.
        if (select.value && !Array.prototype.some.call(select.options, function (o) { return o.value === select.value; })) {
          var option = new Option(select.value, select.value);
          select.appendChild(option);
          select.value = select.value;
        }
        if (check && fields && fallback) {
          fields.hidden = !check.checked;
          if (!check.checked) fallback.value = "";
        }
      });
    });
    document.querySelectorAll("[data-fallback-check]").forEach(function (check) {
      check.addEventListener("change", function () {
        var kind = check.getAttribute("data-fallback-check");
        var fields = document.querySelector('[data-fallback-fields="' + kind + '"]');
        if (fields) fields.hidden = !check.checked;
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
    var main = document.querySelector('[data-role-main="' + kind + '"]');
    if (!main || !main.value) return null;
    var parts = main.value.split("::");
    var spec = { provider: parts[0], model: parts[1] };
    var check = document.querySelector('[data-fallback-check="' + kind + '"]');
    var fallbackSelect = document.querySelector('[data-role-fallback="' + kind + '"]');
    if (check && check.checked && fallbackSelect && fallbackSelect.value) {
      var fb = fallbackSelect.value.split("::");
      spec.fallback = { provider: fb[0], model: fb[1] };
    }
    return spec;
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
      // 1. Роли (синхронная запись chat/vision в обе роли).
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
      // 2. Параметры графа.
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
      // 3. Ключи .env.
      var env = collectEnv();
      await api("/api/settings/env", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(env)
      });
      // 4. Папка Qdrant (решение №29) — только если изменилась или запрошен сброс.
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
      if (roleErrors.length) {
        setStatus("Сохранено, но не все роли записаны: " + roleErrors.join("; "), "warn");
      } else {
        setStatus("Сохранено ✓", "ok");
      }
    } catch (error) {
      setStatus("Ошибка сохранения: " + error.message, "err");
    } finally {
      state.busy = false;
    }
  }

  /* ---------- «Обновить модели» ---------- */
  async function refreshModels() {
    if (state.busy) return;
    state.busy = true;
    var button = $("settings-refresh");
    var results = $("refresh-results");
    if (button) button.disabled = true;
    setStatus("Запрашиваю списки моделей у провайдеров…");
    if (results) { results.hidden = false; results.innerHTML = ""; }
    try {
      var data = await api("/api/settings/providers/refresh", { method: "POST" });
      if (results) {
        var html = '<section class="settings-card"><h3>Результат обновления моделей</h3><div class="refresh-results">';
        Object.keys(data).forEach(function (name) {
          var item = data[name];
          if (item.ok) {
            html += '<div class="ok">✓ ' + esc(name) + " — моделей: " + Object.keys(item.models || {}).length + "</div>";
          } else {
            html += '<div class="err">✗ ' + esc(name) + " — " + esc(item.error || "ошибка") + "</div>";
          }
        });
        html += "</div></section>";
        results.innerHTML = html;
      }
      setStatus("Модели обновлены ✓", "ok");
      await loadSettings(); // обновит списки в селектах
    } catch (error) {
      setStatus("Не удалось обновить модели: " + error.message, "err");
    } finally {
      state.busy = false;
      if (button) button.disabled = false;
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
