/* ============================================================
   interface_RAG — логика главной страницы (vanilla JS).
   Вкладки, чат, «Добавить документ» (3 области: загрузка /
   регистрация / лог), MD-вьювер, gating шагов.
   Настройки рендерит settings.js (вкладка «Настройки»).
   ============================================================ */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  var state = {
    sid: null,
    stem: null,
    currentJob: null,
    eventSource: null,
    rolesConfigured: false,
    ws: null
  };

  /* ---------- утилиты ---------- */
  async function api(url, options) {
    var response = await fetch(url, options);
    var data = null;
    try { data = await response.json(); } catch (_) { /* пустое тело */ }
    if (!response.ok) {
      throw new Error((data && (data.detail || data.error)) || ("HTTP " + response.status));
    }
    return data;
  }

  function setStatus(id, message, cls) {
    var el = $(id);
    if (!el) return;
    el.textContent = message || "";
    el.className = "status-line" + (cls ? " " + cls : "");
  }

  function logLine(text, cls) {
    var log = $("job-log");
    if (!log) return;
    var line = document.createElement("div");
    line.className = "log-" + (cls || "info");
    line.textContent = text;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
  }

  function classify(line) {
    if (/error|traceback|failed|exception|ошибк|не удалось|прерван/i.test(line)) return "error";
    if (/warn|предупрежд|не найден|пропущен/i.test(line)) return "warn";
    if (/done|завершено|успешно|\[phase \d+\/\d+\] done|ок\b/i.test(line)) return "ok";
    return "info";
  }

  /* ---------- вкладки ---------- */
  function switchTab(name) {
    document.querySelectorAll(".tab").forEach(function (button) {
      button.classList.toggle("active", button.getAttribute("data-tab") === name);
    });
    document.querySelectorAll(".tab-panel").forEach(function (panel) {
      panel.hidden = panel.id !== name;
    });
    if (name === "settings" && window.initSettings) {
      window.initSettings();
    }
    if (name === "documents") {
      updateGates();
    }
  }

  document.querySelectorAll(".tab").forEach(function (button) {
    button.addEventListener("click", function () {
      switchTab(button.getAttribute("data-tab"));
    });
  });

  /* ---------- чат ---------- */
  var pendingQuery = null;

  function chatSend(type, text) {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
      if (type === "query") pendingQuery = text;
      return;
    }
    state.ws.send(JSON.stringify({ type: type, text: text }));
  }

  function openChat() {
    var scheme = location.protocol === "https:" ? "wss" : "ws";
    state.ws = new WebSocket(scheme + "://" + location.host + "/ws/chat");
    state.ws.onopen = function () {
      if (pendingQuery) {
        state.ws.send(JSON.stringify({ type: "query", text: pendingQuery }));
        pendingQuery = null;
      }
    };
    state.ws.onmessage = function (event) {
      var message = JSON.parse(event.data);
      var answer = $("answer");
      if (message.type === "answer") {
        answer.textContent = message.answer || "";
        (message.images || []).forEach(function (image) {
          var img = document.createElement("img");
          img.src = image.url;
          img.alt = image.caption || "Изображение";
          answer.appendChild(img);
        });
        if (message.sources && message.sources.length) {
          var wrap = document.createElement("div");
          wrap.className = "answer-source";
          wrap.textContent = "Источники: " + message.sources
            .map(function (s) { return s.title || s.document_id || s.chunk_id; })
            .filter(Boolean).slice(0, 5).join("; ");
          answer.appendChild(wrap);
        }
        $("reply-row").hidden = true;
        setStatus("chat-status", "", "");
      } else if (message.type === "clarification") {
        answer.textContent = "Уточняющий вопрос: " + (message.text || "");
        $("reply-row").hidden = false;
        $("reply-input").focus();
      } else if (message.type === "error") {
        answer.textContent = "Ошибка: " + (message.text || message);
        $("reply-row").hidden = true;
        setStatus("chat-status", "", "");
      } else if (message.type === "node") {
        setStatus("chat-status", "Обрабатывается узел: " + message.node, "");
      }
    };
  }

  $("ask").addEventListener("click", function () {
    var query = $("query").value.trim();
    if (!query) return;
    $("answer").textContent = "Думаю…";
    if (!state.ws || state.ws.readyState === WebSocket.CLOSED) {
      pendingQuery = query;
      openChat();
    } else {
      chatSend("query", query);
    }
  });

  $("reply-send").addEventListener("click", function () {
    var text = $("reply-input").value.trim();
    if (!text) return;
    chatSend("reply", text);
    $("reply-input").value = "";
  });
  $("reply-input").addEventListener("keydown", function (event) {
    if (event.key === "Enter") $("reply-send").click();
  });

  $("show-docs").addEventListener("click", async function () {
    var box = $("base-docs");
    box.textContent = "Загружаю…";
    try {
      var docs = await api("/api/documents-in-base");
      if (!docs.length) {
        box.textContent = "В базе пока нет документов.";
        return;
      }
      box.innerHTML = "";
      docs.forEach(function (doc) {
        var item = document.createElement("div");
        item.className = "doc-item";
        item.innerHTML =
          '<div class="doc-id">' + escapeHtml(doc.document_id) + "</div>" +
          '<div class="doc-meta">' + escapeHtml(doc.title || "") +
          (doc.domain ? " · " + escapeHtml(doc.domain) : "") +
          (doc.document_type ? " · " + escapeHtml(doc.document_type) : "") +
          " · фрагментов: " + doc.count + "</div>";
        box.appendChild(item);
      });
    } catch (error) {
      box.textContent = "Ошибка: " + error.message;
    }
  });

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  /* ---------- «Добавить документ»: gating ---------- */
  async function rolesReady() {
    try {
      var status = await api("/api/settings/status");
      var roles = status.roles || {};
      return Boolean(roles["create_markdown.table_vision"] && roles["create_markdown.ai_postprocess"]);
    } catch (_) {
      return false;
    }
  }

  async function updateGates() {
    var register = $("register");
    var convert = $("convert");
    var index = $("index");
    var stop = $("stop");
    var areaRegister = $("area-register");
    var areaProgress = $("area-progress");

    register.disabled = !state.sid;
    convert.disabled = !(state.sid && state.rolesConfigured);
    index.disabled = true;
    stop.disabled = !state.currentJob;
    areaRegister.classList.toggle("is-disabled", !state.sid);
    areaProgress.classList.toggle("is-disabled", !state.sid);

    if (!state.sid) return;
    try {
      var session = await api("/api/documents/" + state.sid);
      index.disabled = !session.can_index;
      $("md-view").hidden = !session.md_exists;
      $("md-download").hidden = !session.md_exists;
      if (session.md_exists) {
        var href = "/api/files/markdown/" + encodeURIComponent(state.stem);
        $("md-view").href = href;
        $("md-download").href = href;
        loadMdPreview();
      }
    } catch (error) {
      setStatus("doc-status", "Не удалось получить состояние сессии: " + error.message, "err");
    }
  }

  /* ---------- загрузка ---------- */
  $("upload").addEventListener("click", async function () {
    var fileInput = $("file");
    var file = fileInput.files[0];
    if (!file) {
      setStatus("doc-status", "Выберите файл (PDF / DOCX / DOC / MD).", "warn");
      return;
    }
    var form = new FormData();
    form.append("file", file);
    $("upload").disabled = true;
    setStatus("doc-status", "Загружаю «" + file.name + "»…");
    try {
      var result = await api("/api/documents", { method: "POST", body: form });
      state.sid = result.session_id;
      state.stem = result.stem;
      fillField("reg-source_file", file.name);
      setStatus("doc-status", "Файл загружен. Распознаю первую страницу для автозаполнения формы…");
      await prefillRegistration();
      state.rolesConfigured = await rolesReady();
      await updateGates();
      setStatus("doc-status", "Файл загружен: " + file.name + ". Проверьте поля регистрации (распознанные — заполнены) и нажмите «Зарегистрировать документ».", "ok");
    } catch (error) {
      setStatus("doc-status", "Ошибка загрузки: " + error.message, "err");
    } finally {
      $("upload").disabled = false;
    }
  });

  /* ---------- регистрация: prefill + форма ---------- */
  async function prefillRegistration() {
    if (!state.sid) return;
    try {
      var result = await api("/api/documents/" + state.sid + "/register/prefill", { method: "POST" });
      var fields = result.fields || {};
      fillField("reg-document_id", fields.document_id);
      fillField("reg-title", fields.title);
      fillField("reg-domain", fields.domain || fields.domain_hint);
      fillSelect("reg-document_type", fields.document_type);
      // Автоопределение года издания из обозначения (последняя группа цифр).
      var edition = extractEdition(fields.document_id);
      if (edition) fillField("reg-edition", edition);
      if (!$("reg-document_type").value) deriveTypeFromId();
      if (!result.image_available) {
        setStatus("reg-status", "Распознать первую страницу не удалось — заполните форму вручную.", "warn");
      } else {
        setStatus("reg-status", "Распознанные поля заполнены — проверьте и отредактируйте.", "ok");
      }
    } catch (error) {
      setStatus("reg-status", "Распознавание недоступно (" + error.message + ") — заполните вручную.", "warn");
    }
  }

  function fillField(id, value) {
    if (value == null || value === "") return;
    var input = $(id);
    if (input) input.value = value;
  }

  function fillSelect(id, value) {
    if (value == null || value === "") return;
    var select = $(id);
    if (!select) return;
    Array.prototype.forEach.call(select.options, function (option) {
      if (option.value.toLowerCase() === String(value).toLowerCase()) {
        select.value = option.value;
      }
    });
  }

  function extractEdition(documentId) {
    if (!documentId) return "";
    var match = String(documentId).match(/(\d{2,4})(?!\d)/g);
    if (!match) return "";
    var year = parseInt(match[match.length - 1], 10);
    if (year >= 1900 && year <= 2100) return String(year);
    if (year >= 50 && year <= 99) return String(1900 + year);
    return "";
  }

  // Автоопределение типа документа из обозначения (ГОСТ/СП/СО/СНиП/ПУЭ).
  function deriveTypeFromId() {
    var text = $("reg-document_id").value.trim().toUpperCase();
    var prefixes = ["ГОСТ", "СНиП", "ПУЭ", "СП", "СО"];
    for (var i = 0; i < prefixes.length; i++) {
      if (text.indexOf(prefixes[i]) === 0) {
        fillSelect("reg-document_type", prefixes[i]);
        return;
      }
    }
  }

  $("reg-document_id").addEventListener("input", function () {
    if (!$("reg-document_type").value) deriveTypeFromId();
  });

  function collectRegistrationFields() {
    var form = $("reg-form");
    var fields = {};
    ["document_id", "document_id_alt", "document_type", "domain", "title", "edition",
     "date_enacted", "date_amended", "amended_by", "source_file",
     "status_reason", "replaced_by_document_id", "replaced_by_doc_key"
    ].forEach(function (name) {
      var control = form.elements[name];
      if (!control) return;
      var value = control.value.trim();
      fields[name] = value === "" ? null : value;
    });
    fields.status = form.elements["status"].value || "active";
    var ignore = form.elements["ignore_sections"].value.trim();
    if (ignore) {
      fields.ignore_sections = ignore.split(",").map(function (item) { return item.trim(); }).filter(Boolean);
    }
    return fields;
  }

  function validateRegistration(fields) {
    var required = ["document_id", "title", "document_type", "domain", "edition", "date_enacted"];
    var missing = required.filter(function (name) { return !fields[name]; });
    return missing;
  }

  $("register").addEventListener("click", async function () {
    if (!state.sid) return;
    var fields = collectRegistrationFields();
    var missing = validateRegistration(fields);
    if (missing.length) {
      setStatus("reg-status", "Заполните обязательные поля: " + missing.join(", ") + ".", "err");
      return;
    }
    $("register").disabled = true;
    setStatus("reg-status", "Сохраняю регистрацию…");
    try {
      var result = await api("/api/documents/" + state.sid + "/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fields: fields })
      });
      setStatus("reg-status", "Документ зарегистрирован: " + result.slug + " ✓", "ok");
      await updateGates();
      if (!$("convert").disabled) {
        setStatus("doc-status", "Регистрация завершена. Можно конвертировать в Markdown.", "ok");
      }
    } catch (error) {
      setStatus("reg-status", "Ошибка регистрации: " + error.message, "err");
      $("register").disabled = false;
    }
  });

  /* ---------- конвертация / индексация / стоп ---------- */
  function watchJob(jobId) {
    state.currentJob = jobId;
    $("stop").disabled = false;
    if (state.eventSource) state.eventSource.close();
    var source = new EventSource("/api/jobs/" + jobId + "/events");
    state.eventSource = source;
    source.onmessage = function (event) {
      var line = "";
      try { line = JSON.parse(event.data); } catch (_) { line = String(event.data); }
      logLine(line, classify(line));
    };
    source.onerror = async function () {
      source.close();
      state.eventSource = null;
      state.currentJob = null;
      $("stop").disabled = true;
      logLine("— задача завершена —", "info");
      await updateGates();
    };
  }

  async function startJob(endpoint) {
    setStatus("doc-status", "Запускаю…");
    try {
      var result = await api(endpoint, { method: "POST" });
      if (result.id) {
        logLine("[job " + result.kind + "] запущен (" + result.id + ")", "info");
        watchJob(result.id);
      } else {
        setStatus("doc-status", JSON.stringify(result), "warn");
      }
      await updateGates();
    } catch (error) {
      setStatus("doc-status", "Не удалось запустить: " + error.message, "err");
    }
  }

  $("convert").addEventListener("click", function () {
    if (!state.sid) return;
    startJob("/api/documents/" + state.sid + "/convert");
  });

  $("index").addEventListener("click", function () {
    if (!state.sid) return;
    startJob("/api/documents/" + state.sid + "/index");
  });

  $("stop").addEventListener("click", async function () {
    if (!state.currentJob) return;
    try {
      var result = await api("/api/jobs/" + state.currentJob + "/stop", { method: "POST" });
      logLine("[stop] статус: " + result.status, "warn");
    } catch (error) {
      setStatus("doc-status", "Ошибка остановки: " + error.message, "err");
    }
  });

  /* ---------- MD-вьювер ---------- */
  function mdUrl(stem) {
    return "/api/files/markdown/" + encodeURIComponent(stem);
  }

  async function loadMdPreview() {
    var preview = $("md-preview");
    if (!preview || !state.stem || !preview.hidden) return;
    try {
      var response = await fetch(mdUrl(state.stem));
      if (!response.ok) return;
      var text = await response.text();
      preview.innerHTML = renderMarkdown(text, state.stem);
      preview.hidden = false;
    } catch (_) {
      // ссылки на просмотр/скачивание остаются доступны
    }
  }

  function renderMarkdown(md, stem) {
    var lines = md.replace(/\r\n/g, "\n").split("\n");
    var html = "";
    var inCode = false;
    var codeBuffer = [];
    var inTable = false;
    var tableBuffer = [];

    function flushTable() {
      if (!tableBuffer.length) return;
      var rows = tableBuffer.map(function (row) {
        return row.map(function (cell) { return "<td>" + inlineMd(cell.trim()) + "</td>"; }).join("");
      });
      var header = rows.shift() || "";
      html += "<table><thead><tr>" + header + "</tr></thead><tbody>" +
        rows.map(function (row) { return "<tr>" + row + "</tr>"; }).join("") + "</tbody></table>";
      tableBuffer = [];
    }

    function flushCode() {
      if (!codeBuffer.length) return;
      html += "<pre><code>" + escapeHtml(codeBuffer.join("\n")) + "</code></pre>";
      codeBuffer = [];
    }

    lines.forEach(function (line) {
      var trimmed = line.trim();
      if (trimmed.indexOf("```") === 0) {
        if (inCode) { flushCode(); inCode = false; }
        else { flushTable(); inCode = true; }
        return;
      }
      if (inCode) { codeBuffer.push(line); return; }

      if (trimmed.indexOf("|") === 0 && trimmed.indexOf("|---") !== -1) {
        return; // строка-разделитель таблицы
      }
      if (trimmed.indexOf("|") === 0 && trimmed.lastIndexOf("|") > 0) {
        if (!inTable) { flushTable(); inTable = true; }
        tableBuffer.push(trimmed.slice(1, -1).split("|"));
        return;
      }
      if (inTable) { flushTable(); inTable = false; }

      if (!trimmed) { html += "<p></p>"; return; }

      var heading = trimmed.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        var level = heading[1].length;
        html += "<h" + level + ">" + inlineMd(heading[2]) + "</h" + level + ">";
        return;
      }
      if (/^([-*_])\s*$/.test(trimmed)) { html += "<hr>"; return; }
      if (trimmed.indexOf("> ") === 0) {
        html += "<blockquote>" + inlineMd(trimmed.slice(2)) + "</blockquote>";
        return;
      }
      var list = trimmed.match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
      if (list) {
        var tag = /^\d+\.$/.test(list[2]) ? "ol" : "ul";
        html += "<" + tag + "><li>" + inlineMd(list[3]) + "</li></" + tag + ">";
        return;
      }
      html += "<p>" + inlineMd(trimmed) + "</p>";
    });
    flushTable();
    flushCode();
    return html;
  }

  function inlineMd(text) {
    var result = escapeHtml(text);
    // изображения ![alt](path)
    result = result.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, function (match, alt, src) {
      var url = /^https?:\/\//.test(src) || src.indexOf("/") === 0 ? src : imageUrl(src);
      return '<img src="' + url + '" alt="' + alt + '">';
    });
    // ссылки [text](url)
    result = result.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function (match, text, url) {
      return '<a href="' + url + '" target="_blank" rel="noopener">' + text + "</a>";
    });
    // inline-код
    result = result.replace(/`([^`]+)`/g, "<code>$1</code>");
    // жирный и курсив
    result = result.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    result = result.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    return result;
  }

  function imageUrl(relativePath) {
    // Пути в .md относительны каталога документа: image/fig_1.jpg →
    // /api/images/<stem>/image/fig_1.jpg
    if (!state.stem) return relativePath;
    return "/api/images/" + encodeURIComponent(state.stem) + "/" + relativePath.replace(/^\.?\//, "");
  }

  /* ---------- init ---------- */
  $("md-view").addEventListener("click", function (event) {
    // «Просмотр .md» показывает/скрывает встроенный рендер; скачивание — ссылкой ниже.
    event.preventDefault();
    var preview = $("md-preview");
    if (!preview.hidden) {
      preview.hidden = true;
    } else if (state.stem) {
      loadMdPreview();
    }
  });
  switchTab("chat");
})();
