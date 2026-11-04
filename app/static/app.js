/* AI Twitch Clipper — UI-Logik, ohne externe Abhängigkeiten. */
"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

let toastTimer = null;
function toast(message, kind = "info") {
  const element = $("#toast");
  if (!element) return;
  element.textContent = message;
  element.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    element.className = "toast";
  }, kind === "error" ? 6000 : 3500);
}

async function api(url, options = {}) {
  const config = { ...options, headers: { ...(options.headers || {}) } };
  if (config.body && typeof config.body !== "string") {
    config.headers["Content-Type"] = "application/json";
    config.body = JSON.stringify(config.body);
  }
  const response = await fetch(url, config);
  const contentType = response.headers.get("content-type") || "";
  if (!response.ok) {
    let detail = "";
    try {
      const payload = contentType.includes("application/json") ? await response.json() : null;
      detail = payload?.detail || "";
    } catch (_error) {
      /* ignore */
    }
    throw new Error(detail || `HTTP ${response.status}`);
  }
  if (response.status === 204) return null;
  return contentType.includes("application/json") ? response.json() : response.text();
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "–";
  const total = Math.max(0, Math.round(Number(seconds)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function formatBytes(bytes) {
  if (!bytes) return "–";
  let value = Number(bytes);
  const units = ["B", "KB", "MB", "GB", "TB"];
  for (const unit of units) {
    if (value < 1024 || unit === "TB") {
      return unit === "B" ? `${Math.round(value)} B` : `${value.toFixed(1)} ${unit}`;
    }
    value /= 1024;
  }
  return "–";
}

function formatTime(iso) {
  if (!iso) return "–";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "–";
  return date.toLocaleString("de-DE", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function scorePct(score) {
  return `${Math.round(Math.max(0, Math.min(1, Number(score) || 0)) * 100)} %`;
}

/* ---------------------------------------------------------------- Dashboard */
function sparklineSvg(points) {
  const width = 240;
  const height = 44;
  const safe = Array.isArray(points) ? points.filter((point) => typeof point.score === "number") : [];
  if (safe.length === 0) {
    return `<line class="base" x1="0" y1="${height - 1}" x2="${width}" y2="${height - 1}"></line>`;
  }
  const step = safe.length > 1 ? width / (safe.length - 1) : width;
  const coords = safe.map((point, index) => {
    const x = safe.length > 1 ? index * step : width;
    const y = height - 3 - Math.max(0, Math.min(1, point.score)) * (height - 8);
    return [x, y];
  });
  const line = coords.map(([x, y], index) => `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${line} L${width},${height} L0,${height} Z`;
  const threshold = height - 3 - 0.6 * (height - 8);
  return `
    <line class="base" x1="0" y1="${threshold.toFixed(1)}" x2="${width}" y2="${threshold.toFixed(1)}"></line>
    <path class="area" d="${area}"></path>
    <path class="line" d="${line}"></path>`;
}

function streamerCard(item) {
  const state = item.enabled === false ? "offline" : item.state || "offline";
  let note;
  if (item.last_error) {
    note = `<span class="error-text">${escapeHtml(item.last_error)}</span>`;
  } else if (state === "live") {
    note = `Chat: ${item.chat_connected ? "verbunden" : "nicht verbunden"} · Cooldown ${Math.round(item.cooldown_remaining_s || 0)} s`;
  } else {
    note = "wartet auf Live-Signal";
  }
  const badge = item.enabled === false ? "PAUSIERT" : String(state).toUpperCase();
  const labels = {
    chat_rate: "Chat-Rate",
    emote_burst: "Emote-Burst",
    clip_created: "Neuer Clip",
    viewers: "Zuschauer",
    clip_burst: "Clip-Häufung",
  };
  const components = Object.entries(item.last_components || {});
  const componentList = components.length
    ? `<ul class="component-list" title="Signal-Zusammensetzung des letzten Ticks">${components
        .map(([key, value]) => {
          const pct = Math.round(Math.max(0, Math.min(1, Number(value) || 0)) * 100);
          return `<li><span class="component-label">${escapeHtml(labels[key] || key)}</span><span class="component-bar"><span style="width:${pct}%"></span></span><span class="component-value">${pct} %</span></li>`;
        })
        .join("")}</ul>`
    : "";
  return `
    <article class="streamer-card" data-streamer-id="${item.id}" data-sparkline='${escapeHtml(JSON.stringify(item.sparkline || []))}'>
      <header>
        <span class="dot ${escapeHtml(state)}"></span>
        <div class="who">
          <h3>${escapeHtml(item.display_name)}</h3>
          <span class="muted small">@${escapeHtml(item.login)}</span>
        </div>
        <span class="badge ${escapeHtml(state)}">${escapeHtml(badge)}</span>
      </header>
      <dl class="metrics">
        <div><dt>Zuschauer</dt><dd>${Number(item.viewers || 0).toLocaleString("de-DE")}</dd></div>
        <div><dt>Uptime</dt><dd>${item.uptime_s ? formatDuration(item.uptime_s) : "–"}</dd></div>
        <div><dt>Puffer</dt><dd>${Math.round(item.buffer_s || 0)} s</dd></div>
        <div><dt>Score</dt><dd>${scorePct(item.last_score)}</dd></div>
      </dl>
      <div class="scorebar"><span style="width:${Math.round(Math.max(0, Math.min(1, item.last_score || 0)) * 100)}%"></span></div>
      ${componentList}
      <svg class="sparkline" viewBox="0 0 240 44" preserveAspectRatio="none" aria-label="Score der letzten 15 Minuten">${sparklineSvg(item.sparkline)}</svg>
      <p class="card-note">${note}</p>
    </article>`;
}

function recordingRow(item) {
  return `
    <tr>
      <td>${escapeHtml(formatTime(item.created_at))}</td>
      <td>@${escapeHtml(item.streamer_login)}</td>
      <td>${scorePct(item.score)}</td>
      <td>${item.duration_s ? formatDuration(item.duration_s) : "–"}</td>
      <td><span class="status ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span></td>
      <td>${formatBytes(item.size_bytes)}</td>
      <td><a class="btn btn-small btn-ghost" href="/recordings#rec-${item.id}">öffnen</a></td>
    </tr>`;
}

async function refreshDashboard() {
  const data = await api("/api/dashboard");
  const liveCount = data.streamers.filter((item) => item.state === "live").length;
  const ready = (data.counts.ready || 0) + (data.counts.truncated || 0);
  const buffer = data.streamers.reduce((sum, item) => sum + (item.buffer_s || 0), 0);

  const live = $("#stat-live");
  if (live) live.textContent = String(liveCount);
  const readyElement = $("#stat-ready");
  if (readyElement) readyElement.textContent = String(ready);
  const failed = $("#stat-failed-label");
  if (failed) failed.textContent = `${data.counts.failed || 0} fehlgeschlagen`;
  const disk = $("#stat-disk");
  if (disk) disk.textContent = data.disk.free_gb !== undefined ? `${data.disk.free_gb} GB` : "–";
  const diskPath = $("#stat-disk-path");
  if (diskPath) diskPath.textContent = data.disk.path || "";
  const bufferElement = $("#stat-buffer");
  if (bufferElement) bufferElement.textContent = `${Math.round(buffer)} s`;

  const capacity = data.capacity || {};
  const capacityElement = $("#stat-capacity");
  if (capacityElement) {
    capacityElement.textContent = `${capacity.active ?? 0} / ${capacity.max_concurrent ?? 0}`;
  }
  const queuedElement = $("#stat-queued");
  if (queuedElement) queuedElement.textContent = String(capacity.queued ?? 0);
  const processElement = $("#stat-processes");
  if (processElement) processElement.textContent = String(capacity.processes ?? 0);
  const memoryElement = $("#stat-memory");
  if (memoryElement) {
    memoryElement.textContent = capacity.mem_limit_mb
      ? `${capacity.mem_used_mb ?? 0} / ${capacity.mem_limit_mb} MB`
      : `${capacity.mem_used_mb ?? 0} MB`;
  }
  const gqlElement = $("#stat-gql");
  if (gqlElement) gqlElement.textContent = String(capacity.gql_requests_last_cycle ?? 0);

  const mode = $("#twitch-mode");
  if (mode) mode.textContent = `Twitch: ${data.twitch_mode}`;

  const grid = $("#streamer-grid");
  if (grid) {
    if (!data.streamers.length) {
      grid.innerHTML = `<div class="empty"><p>Noch keine Streamer angelegt.</p><a class="btn" href="/streamers">Ersten Streamer hinzufügen</a></div>`;
    } else {
      grid.innerHTML = data.streamers.map(streamerCard).join("");
    }
  }

  const tbody = $("#recent-recordings tbody");
  if (tbody) {
    tbody.innerHTML = data.recordings.length
      ? data.recordings.map(recordingRow).join("")
      : `<tr><td colspan="7" class="muted">Noch keine Aufnahmen.</td></tr>`;
  }

  const hint = $("#refresh-hint");
  if (hint) hint.textContent = `aktualisiert ${new Date().toLocaleTimeString("de-DE")}`;
}

function initDashboard() {
  const grid = $("#streamer-grid");
  if (grid) {
    $$(".streamer-card", grid).forEach((card) => {
      const svg = $("svg.sparkline", card);
      if (!svg) return;
      let points = [];
      try {
        points = JSON.parse(card.dataset.sparkline || "[]");
      } catch (_error) {
        points = [];
      }
      svg.innerHTML = sparklineSvg(points);
    });
  }
  refreshDashboard().catch((error) => console.warn("Dashboard:", error));
  setInterval(() => refreshDashboard().catch((error) => console.warn("Dashboard:", error)), 5000);
}

/* ---------------------------------------------------------------- Streamer */
function renderBulkReport(container, items) {
  if (!container) return;
  container.replaceChildren();
  const problems = (items || []).filter((item) => item.status !== "added");
  if (problems.length === 0) {
    container.hidden = true;
    return;
  }
  const heading = document.createElement("p");
  heading.className = "muted small";
  heading.textContent = "Nicht hinzugefügt:";
  const list = document.createElement("ul");
  list.className = "bulk-issues";
  for (const item of problems) {
    const entry = document.createElement("li");
    const code = document.createElement("code");
    code.textContent = item.input || item.login || "–";
    entry.append(code, document.createTextNode(` — ${item.detail || item.status}`));
    list.append(entry);
  }
  container.append(heading, list);
  container.hidden = false;
}

function initStreamerFilter(table) {
  const input = $("#streamer-filter");
  const counter = $("#streamer-count");
  const rows = $$("tbody tr[data-streamer-id]", table);
  if (!rows.length) {
    if (counter) counter.textContent = "0 von 0";
    return;
  }
  const apply = () => {
    const query = (input?.value || "").trim().toLowerCase();
    let visible = 0;
    for (const row of rows) {
      const login = (row.dataset.login || "").toLowerCase();
      const name = (row.dataset.displayName || "").toLowerCase();
      const matches = !query || login.includes(query) || name.includes(query);
      row.hidden = !matches;
      if (matches) visible += 1;
    }
    if (counter) counter.textContent = `${visible} von ${rows.length}`;
  };
  if (input) input.addEventListener("input", apply);
  apply();
}

function initStreamers() {
  const form = $("#add-streamer-form");
  if (form) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("button[type=submit]", form);
      const login = form.elements.login.value.trim();
      if (!login) return;
      button.disabled = true;
      try {
        await api("/api/streamers", { method: "POST", body: { login } });
        toast(`@${login} hinzugefügt.`, "success");
        setTimeout(() => window.location.reload(), 500);
      } catch (error) {
        toast(error.message, "error");
      } finally {
        button.disabled = false;
      }
    });
  }

  const bulkForm = $("#bulk-import-form");
  if (bulkForm) {
    bulkForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("button[type=submit]", bulkForm);
      const summary = $("#bulk-result");
      const report = $("#bulk-report");
      const input = bulkForm.elements.input.value;
      if (!input.trim()) {
        if (summary) {
          summary.className = "error-text";
          summary.textContent = "Bitte mindestens einen Kanal eingeben.";
        }
        return;
      }
      button.disabled = true;
      if (summary) {
        summary.className = "muted small";
        summary.textContent = "importiert …";
      }
      try {
        const result = await api("/api/streamers/bulk", { method: "POST", body: { input } });
        const counts = result.counts || {};
        if (summary) {
          summary.className = "small";
          summary.textContent = `${counts.added || 0} hinzugefügt, ${counts.duplicate || 0} bereits vorhanden, ${counts.not_found || 0} nicht gefunden, ${counts.invalid || 0} ungültig`;
        }
        renderBulkReport(report, result.items);
        bulkForm.elements.input.value = "";
        toast("Import abgeschlossen.", "success");
        if ((counts.added || 0) > 0) {
          setTimeout(() => window.location.reload(), 2500);
        }
      } catch (error) {
        if (summary) {
          summary.className = "error-text";
          summary.textContent = error.message;
        }
        toast(error.message, "error");
      } finally {
        button.disabled = false;
      }
    });
  }

  const table = $("#streamer-table");
  if (!table) return;

  initStreamerFilter(table);

  table.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    const row = button.closest("tr[data-streamer-id]");
    if (!row) return;
    const id = row.dataset.streamerId;
    const login = $("strong", row)?.textContent || id;

    if (button.classList.contains("js-toggle")) {
      const enabled = row.dataset.enabled !== "true";
      button.disabled = true;
      try {
        await api(`/api/streamers/${id}`, { method: "PATCH", body: { enabled } });
        toast(`@${login.replace(/^@/, "")} ${enabled ? "aktiviert" : "pausiert"}.`, "success");
        setTimeout(() => window.location.reload(), 400);
      } catch (error) {
        toast(error.message, "error");
        button.disabled = false;
      }
    }

    if (button.classList.contains("js-delete")) {
      if (!window.confirm(`Streamer ${login} wirklich löschen? Bereits erzeugte Aufnahmen bleiben erhalten.`)) return;
      button.disabled = true;
      try {
        await api(`/api/streamers/${id}`, { method: "DELETE" });
        toast("Streamer entfernt.", "success");
        setTimeout(() => window.location.reload(), 400);
      } catch (error) {
        toast(error.message, "error");
        button.disabled = false;
      }
    }
  });
}

/* --------------------------------------------------------------- Aufnahmen */
function initRecordings() {
  $$(".js-delete-recording").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = button.dataset.id;
      if (!window.confirm("Aufnahme inklusive Datei, Sidecar und Thumbnail löschen?")) return;
      button.disabled = true;
      try {
        await api(`/api/recordings/${id}?delete_files=true`, { method: "DELETE" });
        const card = button.closest(".recording");
        if (card) card.remove();
        toast("Aufnahme gelöscht.", "success");
      } catch (error) {
        toast(error.message, "error");
        button.disabled = false;
      }
    });
  });
}

/* ------------------------------------------------------------- Einstellungen */
function initSettings() {
  const form = $("#settings-form");
  if (!form) return;

  const checkButton = $("#check-path");
  const pathResult = $("#path-result");
  if (checkButton) {
    checkButton.addEventListener("click", async () => {
      const path = form.elements.storage_root.value.trim();
      if (!path) return;
      checkButton.disabled = true;
      try {
        const result = await api("/api/settings/check-path", { method: "POST", body: { path } });
        if (result.writable) {
          pathResult.className = "muted small";
          pathResult.textContent = `${result.message} · frei ${result.free_gb} GB von ${result.total_gb} GB`;
        } else {
          pathResult.className = "error-text";
          pathResult.textContent = result.message || "Pfad ist nicht beschreibbar.";
        }
      } catch (error) {
        pathResult.className = "error-text";
        pathResult.textContent = error.message;
      } finally {
        checkButton.disabled = false;
      }
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = $("button[type=submit]", form);
    const result = $("#settings-result");
    submit.disabled = true;
    result.className = "muted small";
    result.textContent = "speichert …";

    const payload = {
      storage_root: form.elements.storage_root.value.trim(),
      threshold: parseFloat(form.elements.threshold.value),
      cooldown_s: parseInt(form.elements.cooldown_s.value, 10),
      pre_roll_s: parseInt(form.elements.pre_roll_s.value, 10),
      post_roll_s: parseInt(form.elements.post_roll_s.value, 10),
      quality: form.elements.quality.value.trim(),
      retention_days: parseInt(form.elements.retention_days.value, 10),
      max_gb_per_streamer: parseInt(form.elements.max_gb_per_streamer.value, 10),
      max_concurrent_streams: parseInt(form.elements.max_concurrent_streams.value, 10),
      audio_enabled: form.elements.audio_enabled.checked,
      weights: {
        chat_rate: parseFloat(form.elements.weight_chat_rate.value),
        emote_burst: parseFloat(form.elements.weight_emote_burst.value),
        clip_created: parseFloat(form.elements.weight_clip_created.value),
        viewers: parseFloat(form.elements.weight_viewers.value),
        clip_burst: parseFloat(form.elements.weight_clip_burst.value),
      },
    };

    try {
      await api("/api/settings", { method: "PUT", body: payload });
      result.className = "small";
      result.textContent = "gespeichert";
      toast("Einstellungen gespeichert.", "success");
      setTimeout(() => window.location.reload(), 700);
    } catch (error) {
      result.className = "error-text";
      result.textContent = error.message;
      toast(error.message, "error");
    } finally {
      submit.disabled = false;
    }
  });
}

/* --------------------------------------------------------------------- Logs */
function levelClass(level) {
  return String(level || "").toLowerCase();
}

function renderLogs(items) {
  const view = $("#log-view");
  if (!view) return;
  const autoscroll = $("#log-autoscroll")?.checked;
  view.innerHTML = items.length
    ? items.map((item) => {
        const time = new Date(item.ts).toLocaleTimeString("de-DE", { hour12: false });
        return `<span class="log-line level-${escapeHtml(levelClass(item.level))}">${escapeHtml(time)} ${escapeHtml(String(item.level).toUpperCase())} [${escapeHtml(item.streamer_login || "app")}] ${escapeHtml(item.message)}</span>`;
      }).join("")
    : `<span class="muted">Noch keine Logeinträge.</span>`;
  if (autoscroll) view.scrollTop = view.scrollHeight;
  const updated = $("#log-updated");
  if (updated) updated.textContent = `aktualisiert ${new Date().toLocaleTimeString("de-DE")}`;
}

function initLogs() {
  const view = $("#log-view");
  if (!view) return;
  const refresh = () => api("/api/logs?limit=500").then((data) => renderLogs(data.items)).catch((error) => console.warn("Logs:", error));
  refresh();
  setInterval(refresh, 3000);
  $("#log-refresh")?.addEventListener("click", refresh);
  $("#log-wrap")?.addEventListener("change", (event) => {
    view.classList.toggle("no-wrap", !event.target.checked);
  });
  const updated = $("#log-updated");
  if (updated) updated.textContent = "bereit";
}

/* ------------------------------------------------------------------- Start */
document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  if (page === "dashboard") initDashboard();
  if (page === "streamers") initStreamers();
  if (page === "recordings") initRecordings();
  if (page === "settings") initSettings();
  if (page === "logs") initLogs();
});
