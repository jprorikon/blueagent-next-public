// 腦端管理台（owner 2026-11-09）：思考日誌／任務看板／帳號發行與外部帳號綁定。
// 資料全部走主人 token 的 /api/*；她寫的東西一律用 textContent 放進畫面（不當 HTML）。
(() => {
  "use strict";

  const TOKEN_KEY = "ba_owner_token";            // 與調試台（client/app.js）共用
  const ENTITY_KEY = "ba_admin_entity";
  const $ = (id) => document.getElementById(id);
  const state = { tab: "thoughts", view: "board", hours: 24, timer: 0, moodTimer: 0, data: null };

  const KIND = { monologue: "独白", prethink: "预想", mood: "心情", private: "私记" };
  const STATE_NAMES = {
    pending: "排着", processing: "在做", hanging: "做了一半",
    locked: "等条件", outdated: "放下了", completed: "做完",
  };
  const ACTIVITY = { running: "进行中", paused: "暂停", done: "做完", failed: "没做成", pending: "排着" };
  const SKIP = {
    owner_talking: "主人正在说话", below_threshold: "不太想说", min_gap: "离上次太近",
    daily_max: "今天说够了", quiet_hours: "安静时段", circadian_sleep: "在睡觉",
    warmup: "刚启动", premise: "出口闸：主人看不懂", repeat: "跟最近说过的重复",
    repeat_unanswered: "跟还没回的那条重复", owner_in_call: "主人在通话",
    owner_busy_elsewhere: "主人在别处聊", no_text: "没组出句子",
  };

  // ── 小工具 ────────────────────────────────────────────────────────────────
  const token = () => { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; } };
  const setToken = (value) => {
    try { value ? localStorage.setItem(TOKEN_KEY, value) : localStorage.removeItem(TOKEN_KEY); } catch { /* private */ }
  };
  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "class") node.className = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  };
  const when = (at) => {
    if (!at) return "";
    const date = new Date(Number(at) * 1000);
    const now = new Date();
    const hm = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return date.toDateString() === now.toDateString() ? hm
      : `${date.getMonth() + 1}/${date.getDate()} ${hm}`;
  };
  const toast = (text) => {
    const box = $("toast");
    box.textContent = text;
    box.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { box.hidden = true; }, 2600);
  };

  async function api(path, body) {
    const headers = { Authorization: `Bearer ${token()}` };
    const init = { headers };
    if (body !== undefined) {
      init.method = "POST";
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const response = await fetch(path, init);
    let payload = {};
    try { payload = await response.json(); } catch { /* empty */ }
    if (response.status === 401 || response.status === 403) {
      showLogin("token 无效，或不是主人");
      throw new Error("unauthorized");
    }
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  // ── 登入 ──────────────────────────────────────────────────────────────────
  function showLogin(message) {
    $("app").hidden = true;
    $("logout").hidden = true;
    $("login").hidden = false;
    $("login-error").hidden = !message;
    $("login-error").textContent = message || "";
    stopAuto();
    stopMoodAuto();
  }
  async function enter() {
    try {
      const me = await api("/api/me");
      if (!(me.role === "master" || (me.can || {}).admin)) {      // `/api/me`：主人是 role=master、can.admin
        showLogin("这个 token 不是主人");
        return;
      }
    } catch (error) {
      if (error.message !== "unauthorized") showLogin(`连不上：${error.message}`);
      return;
    }
    $("login").hidden = true;
    $("app").hidden = false;
    $("logout").hidden = false;
    show(state.tab);
  }

  // ── 分頁 ──────────────────────────────────────────────────────────────────
  function show(tab) {
    state.tab = tab;
    document.querySelectorAll(".tabs button").forEach((button) => {
      button.setAttribute("aria-selected", String(button.dataset.tab === tab));
    });
    for (const name of ["thoughts", "mood", "tasks", "accounts"]) $(`panel-${name}`).hidden = name !== tab;
    $("entity").disabled = tab === "accounts";
    stopAuto();
    stopMoodAuto();
    if (tab === "thoughts") { loadThoughts(); startAuto(); }
    if (tab === "mood") { loadMood(); startMoodAuto(); }
    if (tab === "tasks") loadTasks();
    if (tab === "accounts") loadAccounts();
  }
  const entity = () => $("entity").value;
  function startAuto() {
    if ($("auto").checked) state.timer = setInterval(loadThoughts, 15000);
  }
  function stopAuto() { clearInterval(state.timer); state.timer = 0; }
  function stopMoodAuto() { clearInterval(state.moodTimer); state.moodTimer = 0; }
  function startMoodAuto() {
    if ($("mood-auto").checked) state.moodTimer = setInterval(loadMood, 15000);
  }

  // ── ① 思考日誌 ────────────────────────────────────────────────────────────
  async function loadThoughts() {
    let data;
    try { data = await api(`/api/admin/thoughts?entity=${encodeURIComponent(entity())}&limit=80`); }
    catch (error) { if (error.message !== "unauthorized") toast(`读不到：${error.message}`); return; }
    state.data = data;
    renderNow(data.now || {});
    renderTimeline(data.thoughts || []);
  }
  function renderNow(now) {
    const mood = now.mood || {};
    const activity = now.activity || {};
    const premise = now.last_premise || {};
    const cells = [
      ["在做", activity.title ? `${activity.title}（${ACTIVITY[activity.status] || activity.status || ""} ${activity.cursor ?? ""}/${activity.total ?? ""}）` : "没有"],
      ["心情", mood.label ? `${mood.label}　想说话 ${Math.round((mood.propensity || 0) * 100)}%` : "—"],
      ["心跳", now.ticks !== undefined ? `第 ${now.ticks} 拍，开口 ${now.spoken} 次` : "—"],
      ["上一拍没开口", SKIP[now.last_skip] || now.last_skip || "—"],
      ["出口闸", premise.verdict ? `${premise.verdict}${premise.missing ? `：${premise.missing}` : ""}` : "—"],
      ["主动消息", now.unanswered ? `${now.unanswered} 条还没回${now.delivery_state ? `（${now.delivery_state === "read" ? "已读" : "未读"}）` : ""}` : "都回了"],
    ];
    $("now").replaceChildren(...cells.map(([label, value]) => el("div", { class: "cell" }, el("b", {}, label), el("span", {}, value))));
  }
  function renderTimeline(rows) {
    const wanted = new Set([...document.querySelectorAll("#kind-filters input:checked")].map((box) => box.value));
    const shown = rows.filter((row) => wanted.has(row.kind));
    if (!shown.length) {
      $("timeline").replaceChildren(el("li", { class: "empty" }, "还没有记录"));
      return;
    }
    $("timeline").replaceChildren(...shown.map((row) => {
      const meta = [];
      if (row.kind === "monologue" && row.action) meta.push(`想做：${{ talk: "找主人说话", work: "做自己的事", none: "只是想想" }[row.action] || row.action}`);
      if (row.why) meta.push(`为什么：${row.why}`);
      if (row.label) meta.push(row.label);
      if (row.kind === "prethink") meta.push(row.used ? "已用在回话里" : "还没用");
      return el("li", {},
        el("time", {}, when(row.at)),
        el("span", { class: `kind ${row.kind}` }, KIND[row.kind] || row.kind),
        el("div", { class: "body" }, row.text || "", meta.length ? el("div", { class: "meta" }, meta.join(" · ")) : null));
    }));
  }

  // ── ② 心情 AEM（owner 2026-11-09「AEM 相關數值也做成可視化」）───────────────
  // 曲線是關閉式投影（任意歷史時刻由 episode 重算），不是取樣記錄；見 agent/brain/runtime/inner/affect.py `series()`。
  const pct = (value) => `${Math.round(Number(value || 0) * 100)}%`;
  const signed = (value) => {
    const number = Number(value || 0);
    return `${number > 0 ? "+" : ""}${number.toFixed(3)}`;
  };

  async function loadMood() {
    const step = state.hours > 24 ? 900 : 300;
    let data;
    try {
      data = await api(`/api/admin/aem?entity=${encodeURIComponent(entity())}`
        + `&hours=${state.hours}&step=${step}&events=40`);
    } catch (error) {
      if (error.message !== "unauthorized") toast(`读不到：${error.message}`);
      return;
    }
    $("mood-stamp").textContent = data.available === false ? "情绪层没接上" : `更新于 ${when(data.generated_at)}`;
    renderAemNow(data.now || {});
    drawAemChart($("aem-chart"), data.series || [], data.events || []);
    renderAemCause(data.cause || {});
    renderAemEvents(data.events || []);
    renderAemExit(data);
  }

  function bar(label, value, min, max, center) {
    const ratio = (Number(value || 0) - min) / (max - min);
    const clamped = Math.max(0, Math.min(1, ratio));
    const fill = el("span", { class: "fill" });
    fill.style.width = `${clamped * 100}%`;
    const track = el("div", { class: "aem-track" }, fill);
    if (center !== undefined) {
      const zero = el("i", { class: "zero" });
      zero.style.left = `${(((center - min) / (max - min)) * 100)}%`;
      track.append(zero);
    }
    return el("div", { class: "aem-bar" },
      el("b", {}, label),
      track,
      el("span", { class: "num" }, `${Number(value || 0).toFixed(3)}`));
  }

  function renderAemNow(now) {
    const idle = now.idle_s === null || now.idle_s === undefined ? null : Number(now.idle_s);
    const idleText = idle === null ? "—"
      : idle < 90 ? "刚刚还在说话"
        : idle < 3600 ? `${Math.round(idle / 60)} 分钟没说话了`
          : `${(idle / 3600).toFixed(1)} 小时没说话了`;
    $("aem-now").replaceChildren(
      el("div", { class: "aem-label" }, now.label || "—",
        el("span", { class: "muted" }, "此刻的心情标签")),
      bar("valence（愉快 ↔ 低落）", now.valence, -1, 1, 0),
      bar("arousal（被唤起）", now.arousal, 0, 1),
      el("div", { class: "cells" },
        el("div", { class: "cell" }, el("b", {}, "想说话"), el("span", {}, pct(now.propensity))),
        el("div", { class: "cell" }, el("b", {}, "寂寞"), el("span", {}, pct(now.social_need))),
        el("div", { class: "cell" }, el("b", {}, "独处"), el("span", {}, idleText))));
  }

  function drawAemChart(canvas, series, events) {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const width = Math.max(280, canvas.clientWidth || 720);
    const height = 240;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.height = `${height}px`;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.font = "11px system-ui, sans-serif";
    if (!series.length) {
      ctx.fillStyle = "#8a8a8a";
      ctx.fillText("这个窗口里还没有数据", 12, 24);
      return;
    }
    const pad = { left: 42, right: 12, top: 10, bottom: 20 };
    const innerWidth = width - pad.left - pad.right;
    const paneHeight = (height - pad.top - pad.bottom - 14) / 2;
    const first = Number(series[0].at || 0);
    const last = Number(series[series.length - 1].at || first + 1);
    const xOf = (at) => pad.left + (last === first ? 0 : ((Number(at) - first) / (last - first)) * innerWidth);
    const panes = [
      { key: "valence", min: -1, max: 1, top: pad.top, color: "#5aa9e6" },
      { key: "arousal", min: 0, max: 1, top: pad.top + paneHeight + 14, color: "#e0a458" },
    ];
    for (const pane of panes) {
      const yOf = (value) => pane.top + (1 - ((Number(value || 0) - pane.min) / (pane.max - pane.min))) * paneHeight;
      ctx.strokeStyle = "rgba(128,128,128,.35)";
      ctx.lineWidth = 1;
      ctx.strokeRect(pad.left, pane.top, innerWidth, paneHeight);
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(pad.left, yOf(0));
      ctx.lineTo(width - pad.right, yOf(0));
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#8a8a8a";
      ctx.fillText(pane.key, 4, pane.top + 11);
      ctx.strokeStyle = pane.color;
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      series.forEach((point, index) => {
        const px = xOf(point.at);
        const py = yOf(point[pane.key]);
        if (index) ctx.lineTo(px, py); else ctx.moveTo(px, py);
      });
      ctx.stroke();
    }
    // 事件：時間軸底部的小刻度（綠＝推上去、紅＝壓下來），hover 由下面的表格看細節
    for (const row of events) {
      const px = xOf(row.at);
      if (px < pad.left || px > width - pad.right) continue;
      ctx.strokeStyle = row.down ? "#d9534f" : "#3fa45b";
      ctx.beginPath();
      ctx.moveTo(px, height - pad.bottom + 2);
      ctx.lineTo(px, height - pad.bottom + 8);
      ctx.stroke();
    }
    ctx.fillStyle = "#8a8a8a";
    ctx.fillText(when(first), pad.left, height - 4);
    const endLabel = when(last);
    ctx.fillText(endLabel, width - pad.right - ctx.measureText(endLabel).width, height - 4);
  }

  function renderAemCause(cause) {
    if (!cause || !cause.key) {
      $("aem-cause").textContent = "没有还有效的情绪事件（或者情绪层没接上）";
      return;
    }
    const mood = cause.down ? "往下压" : "往上推";
    const visible = cause.hidden ? "主人看不见这件事，所以她要提就得先交代一句"
      : "主人看得见这件事（是他那一句引起的）";
    $("aem-cause").replaceChildren(
      el("strong", {}, cause.words || cause.key),
      el("span", { class: "muted" }, `　${mood}　·　${visible}`));
  }

  function renderAemEvents(rows) {
    $("aem-event-count").textContent = rows.length ? `${rows.length} 条` : "这个窗口里没有事件";
    if (!rows.length) {
      $("aem-events").replaceChildren(el("tr", {}, el("td", { colspan: "6", class: "empty" }, "没有")));
      return;
    }
    $("aem-events").replaceChildren(...rows.map((row) => el("tr", {},
      el("td", {}, when(row.at)),
      el("td", {}, row.kind || ""),
      el("td", { class: row.down ? "neg" : "pos" }, signed(row.delta_valence)),
      el("td", {}, signed(row.delta_arousal)),
      el("td", {}, row.target || "—"),
      el("td", {}, `${row.words || row.cause || ""}${row.hidden ? "（你看不见）" : ""}`))));
  }

  function renderAemExit(data) {
    const beat = data.heartbeat || {};
    const premise = beat.last_premise || {};
    const proactive = data.proactive || {};
    const cells = [
      ["上一拍没开口", SKIP[beat.last_skip] || beat.last_skip || "—"],
      ["独白走哪条路", beat.last_talk_route === "monologue_raw" ? "原文直发（旧行为）"
        : beat.last_talk_route === "monologue" ? "独白 → 组句器" : beat.last_talk_route || "—"],
      ["出口闸", premise.verdict ? `${premise.verdict}${premise.missing ? `：${premise.missing}` : ""}` : "—"],
      ["心跳", beat.ticks !== undefined ? `第 ${beat.ticks} 拍　开口 ${beat.spoken ?? 0} 次　跳过 ${beat.skipped ?? 0} 次` : "—"],
      ["主动消息", proactive.unanswered ? `${proactive.unanswered} 条还没回`
        + (proactive.delivery_state ? `（${proactive.delivery_state === "read" ? "已读" : "未读"}）` : "") : "都回了"],
      ["预算", (data.budget || {}).points !== undefined ? `${data.budget.points} 点`
        : (data.budget || {}).level || "—"],
    ];
    $("aem-exit").replaceChildren(...cells.map(([label, value]) =>
      el("div", { class: "cell" }, el("b", {}, label), el("span", {}, value))));
  }

  // ── ③ 任務 ────────────────────────────────────────────────────────────────
  async function loadTasks() {
    const view = state.view;
    let data;
    try { data = await api(`/api/tasks?entity=${encodeURIComponent(entity())}&view=${view}&days=14`); }
    catch (error) { if (error.message !== "unauthorized") toast(`读不到：${error.message}`); return; }
    $("board").hidden = view !== "board";
    $("calendar").hidden = view !== "calendar";
    if (view === "board") renderBoard(data); else renderCalendar(data);
  }
  function taskCard(task) {
    const tags = [];
    if (task.origin === "self") tags.push(el("span", { class: "tag self" }, "她自己加的"));
    if (task.due) tags.push(el("span", { class: "tag" }, `预定 ${task.due}`));
    if (task.steps) tags.push(el("span", { class: "tag" }, `${task.cursor}/${task.steps} 步`));
    if (task.failures) tags.push(el("span", { class: "tag fail" }, `失败 ${task.failures}`));
    const comments = (task.comments || []).map((row) =>
      el("div", { class: "comment" }, `💬 ${row.text}${row.seen ? "（她看过了）" : ""}`));
    return el("div", { class: "task" },
      el("div", { class: "title" }, task.title || "（无题）"),
      task.because ? el("div", { class: "because" }, task.because) : null,
      task.note ? el("div", { class: "note" }, task.note) : null,
      tags.length ? el("div", { class: "tags" }, tags) : null,
      comments,
      task.id ? el("button", { class: "ghost", onclick: () => comment(task) }, "批注") : null);
  }
  function renderBoard(data) {
    const counts = data.counts || {};
    $("task-counts").textContent = Object.entries(counts).map(([key, value]) => `${STATE_NAMES[key] || key} ${value}`).join("　");
    const columns = data.columns || {};
    $("board").replaceChildren(...Object.keys(STATE_NAMES).map((key) => el("div", { class: "column" },
      el("h3", {}, el("span", {}, STATE_NAMES[key]), el("span", { class: "muted" }, String(counts[key] ?? (columns[key] || []).length))),
      ...((columns[key] || []).map(taskCard)))));
  }
  function renderCalendar(data) {
    $("task-counts").textContent = `今天 ${data.today || ""}`;
    const days = Object.entries(data.by_day || {});
    const blocks = days.map(([day, rows]) => el("div", { class: "day" }, el("h3", {}, day), rows.map(taskCard)));
    blocks.push(el("div", { class: "day" }, el("h3", {}, "没定日子"),
      (data.unscheduled || []).length ? data.unscheduled.map(taskCard) : el("p", { class: "muted" }, "没有")));
    $("calendar").replaceChildren(...blocks);
  }
  async function comment(task) {
    const text = window.prompt(`给「${task.title}」留一句批注（她下次检查时会看到，不改状态）`);
    if (!text || !text.trim()) return;
    try {
      await api("/api/tasks/comment", { entity: entity(), id: task.id, text: text.trim() });
      toast("批注已留下");
      loadTasks();
    } catch (error) { if (error.message !== "unauthorized") toast(`没留成：${error.message}`); }
  }

  // ── ③ 帳號 ────────────────────────────────────────────────────────────────
  async function loadAccounts() {
    let data;
    try { data = await api("/api/admin/identities"); }
    catch (error) { if (error.message !== "unauthorized") toast(`读不到：${error.message}`); return; }
    renderAccounts(data);
  }
  function renderAccounts(data) {
    const sources = data.sources || ["app", "discord"];
    $("account-rows").replaceChildren(...(data.accounts || []).map((row) => {
      const revoked = Boolean(row.revoked_at);
      const chips = [];
      for (const [source, ids] of Object.entries(row.links || {})) {
        for (const id of ids) {
          const fixed = source === "app" && id === row.account_id;   // 預設的 App 綁定＝帳號本身，解不掉
          chips.push(el("span", { class: `chip${fixed ? " fixed" : ""}`, title: fixed ? "App 账号就是这个脑端账号（默认）" : "" },
            `${source}: ${id}`,
            fixed || revoked ? null : el("button", { title: "解除绑定", "aria-label": `解除 ${source} ${id}`,
              onclick: () => link(row.account_id, source, id, true) }, "×")));
        }
      }
      const input = el("input", { placeholder: "外部账号 id", "aria-label": "外部账号 id" });
      const select = el("select", { "aria-label": "来源" }, sources.map((source) => el("option", { value: source }, source)));
      const form = el("form", { class: "link-form", onsubmit: (event) => {
        event.preventDefault();
        if (input.value.trim()) link(row.account_id, select.value, input.value.trim(), false);
      } }, select, input, el("button", { type: "submit" }, "绑定"));
      const actions = revoked ? el("span", { class: "muted" }, "已撤销") : el("div", { class: "chips" },
        el("button", { class: "ghost", onclick: () => rotate(row) }, "换 token"),
        row.role === "master" ? null : el("button", { class: "danger", onclick: () => revoke(row) }, "撤销"));
      return el("tr", { class: revoked ? "revoked" : "" },
        el("td", {}, row.display || row.account_id, el("small", {}, row.account_id)),
        el("td", {}, el("span", { class: `role ${row.role}` }, row.role === "master" ? "主人" : "客人"), el("small", {}, row.principal)),
        el("td", {}, el("div", { class: "chips" }, chips)),
        el("td", {}, revoked ? null : form),
        el("td", {}, actions));
    }));
    $("binding-rows").replaceChildren(...(data.bindings || []).map((row) =>
      el("tr", {}, el("td", {}, row.source), el("td", {}, row.external_id), el("td", {}, `→ ${row.principal}`))));
  }
  async function link(accountId, source, externalId, unlink) {
    if (unlink && !window.confirm(`解除 ${source}: ${externalId} 的绑定？之后这个外部账号就不再被认成这个人。`)) return;
    try {
      const data = await api(`/api/admin/identities/${unlink ? "unlink" : "link"}`,
        { account_id: accountId, source, external_id: externalId });
      renderAccounts(data);
      toast(unlink ? "已解除绑定" : "已绑定");
    } catch (error) {
      if (error.message === "already_linked") toast("这个外部账号已经绑在别人身上了");
      else if (error.message !== "unauthorized") toast(`没成功：${error.message}`);
    }
  }
  function showToken(value) {
    $("token-value").textContent = value;
    $("token-once").hidden = false;
  }
  async function rotate(row) {
    if (!window.confirm(`给 ${row.display || row.account_id} 换一个新 token？旧的会立刻失效。`)) return;
    try {
      const data = await api(`/api/accounts/${encodeURIComponent(row.account_id)}/rotate`, {});
      showToken(data.token || "");
      if (row.role === "master") setToken(data.token || "");      // 换的是自己 ⇒ 这台浏览器跟着换
      loadAccounts();
    } catch (error) { if (error.message !== "unauthorized") toast(`没换成：${error.message}`); }
  }
  async function revoke(row) {
    if (!window.confirm(`撤销 ${row.display || row.account_id}？他的 token 会立刻失效。`)) return;
    try {
      await api(`/api/accounts/${encodeURIComponent(row.account_id)}/revoke`, {});
      toast("已撤销");
      loadAccounts();
    } catch (error) { if (error.message !== "unauthorized") toast(`没撤成：${error.message}`); }
  }

  // ── 事件 ──────────────────────────────────────────────────────────────────
  document.querySelectorAll(".tabs button").forEach((button) => button.addEventListener("click", () => show(button.dataset.tab)));
  document.querySelectorAll(".seg button").forEach((button) => button.addEventListener("click", () => {
    if (!button.dataset.view) return;                 // 心情頁的 24h／3d／7d 是另一組
    state.view = button.dataset.view;
    document.querySelectorAll(".seg button").forEach((other) => {
      if (other.dataset.view) other.setAttribute("aria-pressed", String(other === button));
    });
    loadTasks();
  }));
  document.querySelectorAll('.seg button[data-hours]').forEach((button) => button.addEventListener("click", () => {
    state.hours = Number(button.dataset.hours) || 24;
    document.querySelectorAll('.seg button[data-hours]').forEach((other) =>
      other.setAttribute("aria-pressed", String(other === button)));
    loadMood();
  }));
  $("refresh-mood").addEventListener("click", loadMood);
  $("mood-auto").addEventListener("change", () => { stopMoodAuto(); startMoodAuto(); });
  $("entity").addEventListener("change", () => {
    try { localStorage.setItem(ENTITY_KEY, entity()); } catch { /* private */ }
    show(state.tab);
  });
  $("auto").addEventListener("change", () => { stopAuto(); startAuto(); });
  $("refresh-thoughts").addEventListener("click", loadThoughts);
  $("kind-filters").addEventListener("change", () => renderTimeline((state.data || {}).thoughts || []));
  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    setToken($("token").value.trim());
    enter();
  });
  $("logout").addEventListener("click", () => { setToken(""); showLogin(""); });
  $("issue-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const principal = $("issue-principal").value.trim();
    try {
      const data = await api("/api/accounts", { role: "guest", principal, display: $("issue-display").value.trim() || principal });
      showToken(data.token || "");
      $("issue-form").reset();
      loadAccounts();
    } catch (error) { if (error.message !== "unauthorized") toast(`没发成：${error.message}`); }
  });
  $("copy-token").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("token-value").textContent); toast("已复制"); }
    catch { toast("复制不了，请手动选取"); }
  });

  try { const saved = localStorage.getItem(ENTITY_KEY); if (saved) $("entity").value = saved; } catch { /* private */ }
  if (token()) enter(); else showLogin("");
})();
