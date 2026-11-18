/* BlueAgent Next · 本地 Demo UI
 * 无框架、无构建：所有状态来自 /api/state，所有输入走 /api/send。
 * 这个页面上没有「新会话」按钮——线由后端按实体/场景自动维护。
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const TOKEN_KEY = "ba_owner_token";
  const bootQuery = new URLSearchParams(window.location.search);
  const readToken = () => {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
  };
  let authToken = String(bootQuery.get("token") || readToken() || "").trim();
  if (bootQuery.get("token")) {
    try { localStorage.setItem(TOKEN_KEY, authToken); } catch { /* private mode */ }
    bootQuery.delete("token");
    const rest = bootQuery.toString();
    window.history.replaceState({}, "", window.location.pathname + (rest ? `?${rest}` : ""));
  }
  let tokenPrompted = false;
  function promptToken() {
    tokenPrompted = true;
    const value = window.prompt(
      "需要访问 token。owner token 在服务端启动日志里；guest 用加入后拿到的 gt_… token。");
    if (!value || !value.trim()) return false;
    authToken = value.trim();
    try { localStorage.setItem(TOKEN_KEY, authToken); } catch { /* private mode */ }
    window.location.reload();
    return true;
  }
  const state = {
    entity: "mira",
    principal: "owner",
    mode: "direct",
    debug: true,
    busy: false,
    tab: "route",
    data: null,
    lastDecision: null,
    lastFrames: [],
    lastRecall: [],
    lastRecallDetail: [],
    lastSpeech: null,
    room: ["owner", "alice", "bob"],
    pending: null,
    streamText: "",
  };

  const AVATARS = {
    mira: "/static/assets/mira.png",
    aster: "/static/assets/aster.png",
    nori: "/static/assets/nori.png",
    nori: "/static/assets/nori.png",
  };
  const PORTRAITS = {
    mira: "/static/assets/mira_icon_normal.png",
    aster: "/static/assets/aster_icon_normal.png",
  };
  const FALLBACK_NAMES = { mira: "ミラ", aster: "アスター", nori: "ノリ",
                           owner: "主人", alice: "Alice", bob: "Bob", self: "她自己" };
  const LAYER_LABEL = { world: "世界", consensus: "共识", circle: "圈子",
                        self: "自己", pair: "两人", private: "私密" };
  const PRINCIPAL_COLORS = { owner: "#ffd8a8", alice: "#ff9ec4", bob: "#9fd4ff" };

  // ── 小工具 ──────────────────────────────────────────────────────────────────
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
  const entityList = () => (state.data?.entities) || [];
  const principalList = () => (state.data?.principals) || [];
  const entityById = (id) => entityList().find((e) => e.id === id) || { id, name: FALLBACK_NAMES[id] || id };
  const principalById = (id) => principalList().find((p) => p.id === id) || { id, name: FALLBACK_NAMES[id] || id };
  const entityName = (id) => entityById(id).name || id;
  const principalName = (id) => principalById(id).name || id;
  const avatarFor = (id) => AVATARS[id] || AVATARS.mira;
  const initials = (name) => String(name || "?").slice(0, 1).toUpperCase();

  async function api(path, options = {}) {
    const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
    if (authToken) headers.Authorization = `Bearer ${authToken}`;
    const response = await fetch(path, { ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) {
      authToken = "";
      try { localStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ }
      if (!tokenPrompted) promptToken();
      throw new Error("unauthorized");
    }
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  async function streamSend(payload, onFrame) {
    const headers = { "Content-Type": "application/json" };
    if (authToken) headers.Authorization = `Bearer ${authToken}`;
    const response = await fetch("/api/stream", {
      method: "POST", headers, body: JSON.stringify(payload),
    });
    if (response.status === 401) {
      authToken = "";
      try { localStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ }
      if (!tokenPrompted) promptToken();
      throw new Error("unauthorized");
    }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut = buffer.indexOf("\n\n");
      while (cut >= 0) {
        const block = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        let event = "message";
        const dataLines = [];
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
        }
        if (dataLines.length) {
          const data = JSON.parse(dataLines.join("\n"));
          if (event === "frame") onFrame(data);
          else if (event === "result") result = data;
          else if (event === "error") throw new Error(data.error || "stream error");
        }
        cut = buffer.indexOf("\n\n");
      }
    }
    if (!result) throw new Error("stream ended without result");
    return result;
  }

  function onStreamFrame(frame) {
    state.lastFrames.push(frame);
    if (frame.kind === "delta") {
      state.streamText += String(frame.payload?.text || "");
      renderTimeline();
    } else if (frame.kind === "memory") {
      state.lastRecall = frame.payload?.facts || state.lastRecall;
      state.lastRecallDetail = frame.payload?.detail || state.lastRecallDetail;
    } else if (frame.kind === "speech") {
      state.lastSpeech = frame.payload || null;
      // 真語音的 `audio` 是 base64 wav（內嵌在同一條流上）；照到達順序排隊播。
      if (frame.payload?.audio) playAudio(frame.payload);
    } else if (frame.kind === "speech_error") {
      state.lastSpeech = { ...(frame.payload || {}), error: true };
      flash(`这次没出声：${frame.payload?.reason || "tts failed"}`);
    }
  }

  // ── 語音播放（Owner 調試台只保證「能聽見」，不做口型/對齊）──────────────────
  const audioQueue = [];
  let audioBusy = false;

  function playAudio(payload) {
    audioQueue.push(payload);
    if (!audioBusy) playNextAudio();
  }

  function playNextAudio() {
    const next = audioQueue.shift();
    if (!next) {
      audioBusy = false;
      return;
    }
    audioBusy = true;
    try {
      const el = new Audio(`data:audio/wav;base64,${next.audio}`);
      el.onended = playNextAudio;
      el.onerror = () => {
        flash("语音播放失败（浏览器解不了这段音频）");
        playNextAudio();
      };
      const started = el.play();
      if (started && typeof started.catch === "function") {
        started.catch(() => {
          flash("浏览器拦了自动播放：点一下页面再发一句");
          playNextAudio();
        });
      }
    } catch (error) {
      flash(`语音播放失败：${error.message}`);
      playNextAudio();
    }
  }

  let toastTimer = 0;
  function flash(message) {
    const el = $("toast");
    el.textContent = message;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
  }

  function scopeFor() {
    if (state.mode === "self") return "self-trpg-table-1";
    if (state.mode === "group") return "group-discord-lobby";
    return state.principal;
  }

  function currentThread() {
    const threads = state.data?.threads || [];
    return threads.find((t) => t.scope === scopeFor() && t.entity === state.entity) || null;
  }

  function recallFromFrames(frames) {
    const memory = (frames || []).find((f) => f.kind === "memory");
    const done = (frames || []).find((f) => f.kind === "done");
    return {
      facts: memory?.payload?.facts || done?.payload?.recall || [],
      detail: memory?.payload?.detail || [],
    };
  }

  // ── 选择器与舞台 ────────────────────────────────────────────────────────────
  function renderEntitySelect() {
    const box = $("entity-select");
    box.innerHTML = "";
    const items = entityList().length ? entityList() : Object.keys(FALLBACK_NAMES).slice(0, 3).map((id) => ({ id, name: FALLBACK_NAMES[id] }));
    for (const entity of items) {
      const button = document.createElement("button");
      button.className = "entity-chip" + (entity.id === state.entity ? " active" : "");
      button.innerHTML = `<img src="${avatarFor(entity.id)}" alt=""><span>${esc(entity.name || entity.id)}</span>`;
      button.onclick = () => {
        state.entity = entity.id;
        state.lastDecision = null;
        state.lastRecall = [];
        state.lastRecallDetail = [];
        refreshAndRender();
      };
      box.appendChild(button);
    }
  }

  function renderPrincipals() {
    const box = $("principal-pills");
    box.innerHTML = "";
    const items = principalList().length ? principalList() : Object.keys(FALLBACK_NAMES).slice(3).map((id) => ({ id, name: FALLBACK_NAMES[id] }));
    for (const principal of items) {
      const button = document.createElement("button");
      button.className = "principal-chip" + (principal.id === state.principal ? " active" : "");
      const color = PRINCIPAL_COLORS[principal.id] || "#9fc3e8";
      button.innerHTML = `<span class="dot" style="color:${color}"></span>${esc(principal.name || principal.id)}`;
      button.onclick = () => {
        state.principal = principal.id;
        if (state.mode !== "direct" && !state.room.includes(principal.id)) state.room.push(principal.id);
        state.lastDecision = null;
        state.lastRecall = [];
        state.lastRecallDetail = [];
        refreshAndRender();
      };
      box.appendChild(button);
    }
  }

  function renderMode() {
    for (const button of document.querySelectorAll("#mode-select button")) {
      button.classList.toggle("active", button.dataset.mode === state.mode);
    }
    $("mention-wrap").classList.toggle("show", state.mode === "group");
    const phone = document.querySelector(".phone");
    if (phone) phone.classList.toggle("self-mode", state.mode === "self");
    $("room-bar").classList.toggle("show", state.mode !== "direct");
    const roomLabel = $("room-bar").querySelector(".label");
    if (roomLabel) roomLabel.textContent = state.mode === "self" ? "桌上的人" : "在场";
    $("hint").textContent = state.mode === "group"
      ? "群聊：不 @ 她、也没叫她名字时，她只旁听（记时间线、不出声）；记忆按在场的人隔离。"
      : state.mode === "self"
        ? "无主源：这是她自己的行动（TRPG 桌），不属于任何主人；记忆落在「她自己」那一层。"
        : "自动切片：闲置 90s / 6 轮 / 1600 字；封片后她仍记得。";
  }

  function renderRoom() {
    const bar = $("room-bar");
    const box = $("room-pills");
    box.innerHTML = "";
    if (state.mode === "direct") return;
    const items = principalList().length
      ? principalList()
      : Object.keys(FALLBACK_NAMES).slice(3).map((id) => ({ id, name: FALLBACK_NAMES[id] }));
    for (const principal of items) {
      const label = document.createElement("label");
      label.className = "room-chip" + (state.room.includes(principal.id) ? " on" : "");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = state.room.includes(principal.id);
      input.onchange = () => {
        if (input.checked) {
          if (!state.room.includes(principal.id)) state.room.push(principal.id);
        } else {
          state.room = state.room.filter((who) => who !== principal.id);
          if (!state.room.length) state.room = [principal.id];
        }
        renderRoom();
        refreshAndRender();
      };
      label.append(input, document.createTextNode(principal.name || principal.id));
      box.appendChild(label);
    }
  }

  function renderStage() {
    const entity = entityById(state.entity);
    $("entity-name").textContent = entity.name || state.entity;
    $("entity-sub").textContent = `char_${state.entity} · 一直在线`;
    $("stage-portrait").src = PORTRAITS[state.entity] || PORTRAITS.mira;
    const act = state.data?.life?.activity;
    $("stage-note").textContent = act
      ? (act.status === "running" ? `正在做：${act.title} · ${act.cursor + 1}/${act.total}`
        : act.status === "paused" ? `暂停在：${act.title}`
        : act.status === "done" ? `做完了：${act.title}` : "没有「新会话」按钮")
      : "没有「新会话」按钮";
    const box = $("presence");
    box.innerHTML = "";
    const presence = (state.data?.presence || []).find((p) => p.entity === state.entity);
    if (!presence || !presence.presence || !Object.keys(presence.presence).length) {
      box.innerHTML = `<span class="chip">此刻没有别的入口在听</span>`;
    } else {
      for (const [source, info] of Object.entries(presence.presence)) {
        const [kind] = source.split(":");
        const who = principalName(info.principal || "");
        const chip = document.createElement("span");
        chip.className = "chip live";
        chip.textContent = `${kind === "phone" ? "手机" : kind === "discord" ? "Discord" : kind} · ${who}`;
        box.appendChild(chip);
      }
      if (presence.attention_principal) {
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.textContent = `注意力 · ${principalName(presence.attention_principal)}`;
        box.appendChild(chip);
      }
    }
  }

  function renderQuick() {
    const box = $("quick");
    box.innerHTML = "";
    const add = (label, text, principal, mention, addressedTo = "") => {
      const button = document.createElement("button");
      button.textContent = label;
      button.onclick = () => {
        state.principal = principal;
        renderPrincipals();
        send(text, { principal, mention, addressed_to: addressedTo });
      };
      box.appendChild(button);
    };
    if (state.mode === "self") {
      add("以她自己行动（TRPG）", "我推开酒馆的门，看看桌边坐着谁。", "owner", false);
      add("她想起自己的经历", "上次那个叫灰鸦的冒险者，后来去哪了？", "owner", false);
      return;
    }
    if (state.mode === "group") {
      add("开放提问（会礼让）", "有人知道周末回老家吗？", "alice", false);
      add("群备忘（贴 📝）", "大家注意下，明天下午三点跟客户对齐", "alice", false);
      add("Alice → Bob（旁听）", "Bob，你昨天说的那个企划，后来怎么样了？", "alice", false, "bob");
      add("Alice @她", "ミラ，你觉得呢？", "alice", true);
      add("Bob @她", "ミラ，晚上好。今天也辛苦了。", "bob", true);
    } else {
      add("打 Discord 电话", "给我打个 Discord 电话。", "owner", false);
      add("用语音回我", "请用语音回我。", "owner", false);
      add("以 Alice 私聊", "你好呀，第一次来家里。", "alice", false);
    }
  }

  // ── 时间线 ──────────────────────────────────────────────────────────────────
  function messageNode(message, isLast, threadId, ownerless) {
    const group = state.mode === "group";
    const role = message.role;
    if (ownerless && role === "user") {
      const node = document.createElement("div");
      node.className = "msg entity self-action";
      node.innerHTML = `<img class="avatar" src="${avatarFor(state.entity)}" alt="">` +
        `<div class="msg-col"><div class="speaker">她自己 · 行动</div>` +
        `<div class="bubble">${esc(message.text)}</div></div>`;
      return node;
    }
    if (role === "user" && group && message.engage !== "reply"
        && message.addressed_to !== "entity") {
      const who = principalName(message.speaker || "owner");
      const target = message.addressed_to && message.addressed_to !== "entity"
        ? principalName(message.addressed_to) : "房间";
      const engage = message.engage || "silent";
      const row = document.createElement("div");
      row.className = "observe";
      const tag = engage === "yield" ? "等待补位"
        : engage === "react" ? "📝 记下" : "旁听";
      const eye = engage === "yield" ? "⏳" : engage === "react" ? "◉" : "◉";
      row.innerHTML = `<span class="eye">${eye}</span><b>${esc(who)}</b><span>→</span><b>${esc(target)}</b>` +
        `<span class="obs-text">${esc(message.text)}</span><span class="obs-tag">${tag}</span>`;
      return row;
    }

    const node = document.createElement("div");
    node.className = "msg " + (role === "assistant" ? "entity" : "user");
    const who = role === "assistant" ? entityName(state.entity) : principalName(message.speaker || "owner");
    let avatar;
    if (role === "assistant") {
      avatar = document.createElement("img");
      avatar.className = "avatar";
      avatar.alt = "";
      avatar.src = avatarFor(state.entity);
    } else {
      avatar = document.createElement("div");
      avatar.className = "avatar initial";
      avatar.textContent = initials(who);
    }
    const column = document.createElement("div");
    column.className = "msg-col";
    const speaker = document.createElement("div");
    speaker.className = "speaker";
    speaker.textContent = who;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = message.text;
    column.append(speaker, bubble);

    const speech = state.lastSpeech || state.data?.voice?.last_speech || null;
    if (role === "assistant" && isLast && speech && speech.thread_id === threadId) {
      const note = document.createElement("div");
      note.className = "speech-note";
      const mode = speech.mock === false
        ? `${((speech.duration_ms || 0) / 1000).toFixed(1)}s`
        : `${((speech.duration_ms || 0) / 1000).toFixed(1)}s（mock）`;
      note.innerHTML = `<span class="wave"><i></i><i></i><i></i><i></i></span>` +
        `语音已生成 · ${esc(speech.target || "auto")} · ${mode}`;
      column.appendChild(note);
    }
    if (role === "assistant" && isLast && state.lastDecision && state.lastDecision.thread === threadId) {
      const detail = state.lastRecallDetail || [];
      const recall = state.lastRecall || [];
      if (detail.length || recall.length) {
        const note = document.createElement("div");
        note.className = "recall";
        note.textContent = "她想起：" + (detail.length
          ? detail.map((fact) => `[${fact.layer_label || fact.layer}] ${fact.text}`).join("；")
          : recall.join("；"));
        column.appendChild(note);
      }
      const decision = state.lastDecision;
      if (decision) {
        const tag = document.createElement("button");
        tag.className = "route-tag";
        tag.textContent = `${decision.pipeline} · ${decision.engage} · ${decision.mode}`;
        tag.onclick = () => {
          state.tab = "route";
          showTab("route");
          document.body.classList.remove("debug-hidden");
          state.debug = true;
          $("btn-debug").textContent = "隐藏面板";
        };
        column.appendChild(tag);
      }
    }
    node.append(avatar, column);
    return node;
  }

  function typingNode() {
    const node = document.createElement("div");
    node.className = "msg entity";
    node.innerHTML = `<img class="avatar" src="${avatarFor(state.entity)}" alt="">` +
      `<div class="msg-col"><div class="speaker">${esc(entityName(state.entity))}</div>` +
      `<div class="bubble"><span class="typing"><i></i><i></i><i></i></span></div></div>`;
    return node;
  }

  function renderTimeline() {
    const timeline = $("timeline");
    const thread = currentThread();
    const messages = thread ? thread.messages : [];
    const signature = [
      thread ? thread.id : "",
      messages.length,
      messages.length ? messages[messages.length - 1].turn : "",
      state.busy ? 1 : 0,
      state.pending ? state.pending.text : "",
      state.streamText,
    ].join("|");
    if (state._timelineSig === signature) return;
    const sameThread = (state._timelineSig || "").startsWith((thread ? thread.id : "") + "|");
    const previousCount = sameThread ? (state._timelineCount || 0) : 0;
    const stick = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 90;
    timeline.innerHTML = "";

    if (!messages.length && !state.pending && !state.busy) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.innerHTML = `<p>这里没有「新会话」。</p>` +
        `<p class="dim">同一个人、同一个角色，跨端只有一条连续的时间线。<br>换场景才分线，记忆不跟着断。</p>`;
      timeline.appendChild(empty);
      return;
    }

    const threadId = thread ? thread.id : "";
    const ownerless = thread ? !!thread.ownerless : state.mode === "self";
    messages.forEach((message, index) => {
      const node = messageNode(message, index === messages.length - 1, threadId, ownerless);
      if (index >= previousCount) node.classList.add("new");
      timeline.appendChild(node);
    });

    if (state.pending) {
      const node = document.createElement("div");
      if (state.pending.ownerless) {
        node.className = "msg entity self-action";
        node.innerHTML = `<img class="avatar" src="${avatarFor(state.entity)}" alt="">` +
          `<div class="msg-col"><div class="speaker">她自己 · 行动</div>` +
          `<div class="bubble">${esc(state.pending.text)}</div></div>`;
      } else {
        node.className = "msg user";
        const who = principalName(state.pending.principal);
        node.innerHTML = `<div class="avatar initial">${esc(initials(who))}</div>` +
          `<div class="msg-col"><div class="speaker">${esc(who)}</div>` +
          `<div class="bubble">${esc(state.pending.text)}</div></div>`;
      }
      timeline.appendChild(node);
    }
    if (state.streamText) {
      const node = document.createElement("div");
      node.className = "msg entity";
      node.innerHTML = `<img class="avatar" src="${avatarFor(state.entity)}" alt="">` +
        `<div class="msg-col"><div class="speaker">${esc(entityName(state.entity))}</div>` +
        `<div class="bubble">${esc(state.streamText)}<span class="stream-caret">▍</span></div></div>`;
      timeline.appendChild(node);
    } else if (state.busy) {
      timeline.appendChild(typingNode());
    }

    if (stick || state.pending) timeline.scrollTop = timeline.scrollHeight;
    state._timelineSig = signature;
    state._timelineCount = messages.length;
  }

  // ── 调试面板 ────────────────────────────────────────────────────────────────
  function decisionCard(decision) {
    if (!decision) return `<div class="card"><span class="dim">还没有路由记录。发一句话试试。</span></div>`;
    const engage = decision.engage || "reply";
    const badge = engage === "reply" ? "reply"
      : (engage === "observe" || engage === "silent") ? "observe" : engage;
    const scope = decision.scope || {};
    return `<div class="card">
      <div class="card-head">
        <span class="card-title">${esc(decision.thread || "")}</span>
        <span class="badge ${badge}">${esc(engage)}</span>
      </div>
      <div class="kv">
        <span class="k">entity</span><span class="v">${esc(decision.entity || "")}</span>
        <span class="k">pipeline</span><span class="v">${esc(decision.pipeline || "")}</span>
        <span class="k">lane / prio</span><span class="v">${esc(decision.lane || "")} · ${esc(decision.priority ?? "")}</span>
        <span class="k">mode</span><span class="v">${esc(decision.mode || "")}</span>
        <span class="k">engage why</span><span class="v">${esc(decision.engage_reason || "—")}</span>
        <span class="k">who → to</span><span class="v">${esc(decision.principal || "她自己")}${decision.addressed_to ? " → " + esc(decision.addressed_to) : ""}</span>
        <span class="k">banks</span><span class="v mono small">${esc(scope.shared_bank || "")}<br>${esc(scope.private_bank || "")}</span>
      </div>
      <div class="reason">${esc(decision.reason || "")}</div>
    </div>`;
  }

  function renderRouteTab() {
    const data = state.data || {};
    const decision = state.lastDecision || (data.route_log || [])[0] || null;
    const log = (data.route_log || []).slice(0, 8);
    const attention = data.attention || {};
    const metrics = attention.metrics || {};
    const counts = metrics.counts || {};
    const countText = Object.entries(counts).map(([action, count]) => `${action} ${count}`).join(" · ") || "—";
    const leases = (attention.leases || []).map((lease) => `<div class="list-item">` +
      `<span class="mark">›</span><span>${esc(lease.principal)} · ${esc(lease.thread)}` +
      `<br><span class="dim small">剩 ${lease.turns_left} 轮 / ${Math.max(0, Math.round(lease.expires_at - (data.now || 0)))}s</span></span></div>`).join("");
    const pending = (data.pending || []).map((item) => `<div class="list-item">` +
      `<span class="mark">…</span><span>等待补位 · ${esc(item.principal)}` +
      `<br><span class="dim small">${esc(item.text)} · ${Math.max(0, Math.round(item.due_at - (data.now || 0)))}s</span></span></div>`).join("");
    const history = attention.history || {};
    const historyDays = (history.days || []).slice(-4).map((item) => `<div class="list-item">` +
      `<span class="mark">${esc(item.day.slice(5))}</span><span>${esc(Object.entries(item.counts || {}).map(([k, v]) => `${k} ${v}`).join(" · "))}` +
      `<br><span class="dim small">${item.total} 次</span></span></div>`).join("");
    const historyGroups = (history.groups || []).slice(0, 4).map((item) => `<div class="list-item">` +
      `<span class="mark">·</span><span>${esc(item.group)}` +
      `<br><span class="dim small">${esc(Object.entries(item.counts || {}).map(([k, v]) => `${k} ${v}`).join(" · "))}</span></span></div>`).join("");
    const historyCard = `<div class="card">
      <div class="card-head"><span class="card-title">注意力历史</span><span class="card-sub">近 7 天 · JSONL 聚合</span></div>
      <div class="kv"><span class="k">total</span><span class="v">${esc(history.total || 0)}</span>
        <span class="k">shadow</span><span class="v">${esc(history.shadow || 0)}</span></div>
      <div class="section-label">按日</div><div class="list">${historyDays || `<span class="dim small">无</span>`}</div>
      <div class="divider"></div>
      <div class="section-label">按群/源</div><div class="list">${historyGroups || `<span class="dim small">无</span>`}</div>
    </div>`;
    $("tab-route").innerHTML = decisionCard(decision) +
      `<div class="card">
        <div class="card-head"><span class="card-title">注意力</span><span class="card-sub">lease / yield / metrics</span></div>
        <div class="kv">
          <span class="k">mode</span><span class="v">${esc(attention.mode || "active")}</span>
          <span class="k">total</span><span class="v">${esc(metrics.total || 0)}</span>
          <span class="k">actions</span><span class="v small">${esc(countText)}</span>
        </div>
        <div class="section-label">焦点租约</div><div class="list">${leases || `<span class="dim small">无</span>`}</div>
        <div class="divider"></div>
        <div class="section-label">YIELD 等待</div><div class="list">${pending || `<span class="dim small">无</span>`}</div>
      </div>` + historyCard +
      `<div class="section-label">最近分流</div><div class="list">` +
      (log.length ? log.map((item) => `<div class="list-item"><span class="mark">›</span><span>` +
        `<b>${esc(item.engage || "")}</b> · ${esc(item.pipeline || "")} · ${esc(item.principal || "")}` +
        `<br><span class="dim mono small">${esc((item.reason || "").slice(0, 110))}</span></span></div>`).join("")
        : `<span class="dim small">空</span>`) +
      `</div>`;
  }

  async function guestAction(path, body = {}) {
    try {
      const data = await api(path, { method: "POST", body: JSON.stringify(body) });
      state.data = data.state;
      flash("已处理访客申请。");
      renderAll();
    } catch (error) {
      flash(`操作失败：${error.message}`);
    }
  }

  function renderGuestTab() {
    const guests = state.data?.guests || { pending: [], accounts: [] };
    const kinds = ["family", "friend", "colleague", "classmate", "partner",
                   "acquaintance", "stranger", "other"];
    const kindOptions = (selected) => kinds.map((kind) =>
      `<option value="${kind}" ${kind === selected ? "selected" : ""}>${kind}</option>`).join("");
    const pending = (guests.pending || []).map((item) => {
      const declared = item.declared || {};
      const id = item.id;
      const pairRows = (item.declared_guests || []).map((rel, index) => {
        const other = rel.other_principal || rel.other_discord_id || "guest";
        return `<div class="kv">
          <span class="k">${esc(other)}</span>
          <span class="v">声明：${esc(rel.kind)} · ${esc(rel.label || "—")}
            <select id="gpk-${id}-${index}">${kindOptions(rel.kind)}</select>
            <input id="gpl-${id}-${index}" value="${esc(rel.label || "")}" style="width:90px">
          </span></div>`;
      }).join("");
      return `<div class="card">
        <div class="card-head">
          <span class="card-title">${esc(item.display_name || item.discord_id)}</span>
          <span class="badge">待审核</span>
        </div>
        <div class="kv">
          <span class="k">Discord</span><span class="v mono">${esc(item.discord_id)}</span>
          <span class="k">声明关系</span><span class="v"><b>${esc(declared.kind)}</b> · ${esc(declared.label || "—")}</span>
          <span class="k">声明备注</span><span class="v small dim">${esc(declared.note || "—")}</span>
        </div>
        <div class="section-label">与已有 guest 的关系（新 guest 声明 → 主人拍板）</div>
        <div class="list">${pairRows || `<span class="dim small">没有声明与其他 guest 的关系</span>`}</div>
        <div class="divider"></div>
        <div class="section-label">主人拍板</div>
        <div class="kv">
          <span class="k">最终关系</span><span class="v"><select id="gk-${id}">${kindOptions(declared.kind)}</select></span>
          <span class="k">称呼</span><span class="v"><input id="gl-${id}" value="${esc(declared.label || "朋友")}"></span>
          <span class="k">信任</span><span class="v"><input id="gt-${id}" type="number" min="0" max="1" step="0.1" value="0.5"></span>
        </div>
        <div class="act-actions">
          <button id="ga-${id}">批准</button>
          <button id="gr-${id}" class="ghost danger">拒绝</button>
        </div>
      </div>`;
    }).join("");
    const accounts = (guests.accounts || []).map((item) => {
      const final = item.final || {};
      return `<div class="card">
        <div class="card-head"><span class="card-title">${esc(item.display_name || item.principal_id)}</span><span class="badge reply">已批准</span></div>
        <div class="kv">
          <span class="k">principal</span><span class="v mono">${esc(item.principal_id)}</span>
          <span class="k">Discord</span><span class="v mono">${esc(item.discord_id)}</span>
          <span class="k">最终关系</span><span class="v">${esc(final.kind || "—")} · ${esc(final.label || "—")}</span>
          <span class="k">guest 关系</span><span class="v small">${esc((item.final_guests || []).map((rel) => `${rel.other_principal}:${rel.kind}`).join(" / ") || "—")}</span>
        </div>
      </div>`;
    }).join("");
    $("tab-guests").innerHTML = `
      <div class="card">
        <div class="card-head"><span class="card-title">访客申请</span><span class="card-sub">declared → final</span></div>
        <div class="reason">Guest 只能声明「自己想和 master 是什么关系」；最终关系由主人在这里拍板。批准后才会写入 principal 与 Discord 白名单。</div>
      </div>
      ${pending || `<div class="card"><span class="dim">没有待审核的申请。</span></div>`}
      <div class="section-label">已批准</div>
      ${accounts || `<div class="card"><span class="dim">还没有访客。</span></div>`}`;
    for (const item of guests.pending || []) {
      const id = item.id;
      $("ga-" + id)?.addEventListener("click", () => guestAction(
        `/api/owner/guests/${encodeURIComponent(id)}/approve`, {
          final_kind: $("gk-" + id).value,
          final_label: $("gl-" + id).value,
          trust: Number($("gt-" + id).value || 0.5),
          guest_relations: (item.declared_guests || []).map((rel, index) => ({
            other_principal: rel.other_principal,
            other_discord_id: rel.other_discord_id,
            kind: $("gpk-" + id + "-" + index).value,
            label: $("gpl-" + id + "-" + index).value,
          })),
        }));
      $("gr-" + id)?.addEventListener("click", () => guestAction(
        `/api/owner/guests/${encodeURIComponent(id)}/reject`, {}));
    }
  }

  function renderVoiceTab() {
    const voice = state.data?.voice || { session: null, available: {} };
    const session = voice.session;
    const available = Object.values(voice.available || {})
      .map((item) => `<div class="list-item"><span class="mark">•</span><span>${esc(item.name)} ` +
        `<span class="dim small">${esc(item.target)}</span></span></div>`).join("");
    const current = session ? `<div class="card">
      <div class="card-head"><span class="card-title">通话中 · ${esc(session.end)}</span><span class="badge reply">connected</span></div>
      <div class="kv">
        <span class="k">target</span><span class="v mono">${esc(session.target)}</span>
        <span class="k">call_id</span><span class="v mono small">${esc(session.id)}</span>
        <span class="k">duration</span><span class="v">${esc(session.duration_s)}s</span>
        <span class="k">reason</span><span class="v small">${esc(session.reason || "—")}</span>
      </div>
    </div>` : `<div class="card"><span class="dim">当前没有通话。试试对她说「给我打个 Discord 电话」。</span></div>`;
    const floor = voice.floor || {};
    const floorText = Object.entries(floor).map(([entity, info]) =>
      `${entity}: ${info.holder}（prio ${info.priority}）`).join(" · ") || "空闲";
    const last = voice.last_speech
      ? `<div class="card">
           <div class="card-head"><span class="card-title">最近一次语音</span><span class="badge">${esc(voice.last_speech.target)}</span></div>
           <div class="kv"><span class="k">文本</span><span class="v">${esc(voice.last_speech.text)}</span>
             <span class="k">时长</span><span class="v">${((voice.last_speech.duration_ms || 0) / 1000).toFixed(1)}s${voice.last_speech.mock === false ? "" : "（mock）"}</span></div>
         </div>` : "";
    const floorCard = `<div class="card">
      <div class="card-head"><span class="card-title">发言权</span><span class="card-sub">FloorArbiter</span></div>
      <div class="reason">${esc(floorText)}</div>
    </div>`;
    $("tab-voice").innerHTML = `${current}${last}${floorCard}
      <div class="card">
        <div class="card-head"><span class="card-title">动作面</span><span class="card-sub">tool_call</span></div>
        <div class="reason">voice.call / voice.speak / voice.hangup 都是 brain 产出的 tool_call；UI 没有朗读开关。通话期间输出粘在 session 上。</div>
      </div>
      <div class="section-label">可用目标</div><div class="list">${available || `<span class="dim small">无</span>`}</div>`;
  }

  const ACTIVITY_LABEL = { ready: "准备中", running: "进行中", paused: "已暂停",
                           done: "已完成", failed: "失败" };
  const STEP_LABEL = { pending: "待做", running: "在做", done: "完成", failed: "失败" };

  async function activityAction(path, payload = {}) {
    try {
      const data = await api(path, { method: "POST", body: JSON.stringify(payload) });
      state.data = data.state;
      if (data.life) state.data.life = data.life;
      if (path.endsWith("/reload") && data.life?.recovered) {
        flash(`检测到未完成：从第 ${(data.life.activity?.cursor || 0) + 1} 步恢复。`);
      } else if (path.endsWith("/start")) {
        flash("已开始：她会一步一步做；人一开口就存检查点、说完接着做。");
      } else if (path.endsWith("/pause")) {
        flash("已暂停：检查点写盘。");
      } else if (path.endsWith("/reset")) {
        flash("已清除这件事的检查点。");
      }
      renderAll();
    } catch (error) {
      flash(`操作失败：${error.message}`);
    }
  }

  function renderActivityTab() {
    const life = state.data?.life || {};
    const act = life.activity;
    const status = act ? (ACTIVITY_LABEL[act.status] || act.status) : "空闲";
    const checkpoint = act?.checkpoint_at
      ? new Date(act.checkpoint_at * 1000).toLocaleTimeString() : "—";
    const steps = (act?.steps || []).map((step, index) => `<div class="step ${step.status}">` +
      `<span class="dot"></span><span class="step-title">${index + 1}. ${esc(step.title)}</span>` +
      `<span class="step-note">${esc(STEP_LABEL[step.status] || step.status)}</span></div>`).join("");
    const recovered = life.recovered
      ? `<div class="reason">上次没有正常收尾：当前步退回「待做」，从第 ${(act?.cursor || 0) + 1} 步恢复。</div>` : "";
    const humanNote = life.paused_by_human
      ? `<div class="reason">人来了：停在安全边界，检查点已写盘；你说完她会接着做。</div>` : "";
    const actions = act
      ? `<button id="act-pause" ${act.status === "running" ? "" : "disabled"}>暂停</button>` +
        `<button id="act-resume" ${act.status === "paused" ? "" : "disabled"}>继续</button>` +
        `<button id="act-reload">模拟重启</button><button id="act-reset">清除</button>`
      : `<button id="act-start">开始「准备 TRPG 角色卡」</button>`;
    $("tab-activity").innerHTML = `
      <div class="card">
        <div class="card-head">
          <span class="card-title">${act ? esc(act.title) : "她现在没有在做的事"}</span>
          <span class="badge">${status}</span>
        </div>
        <div class="kv">
          <span class="k">进度</span><span class="v">${act ? `${act.cursor}/${act.total} 步` : "—"}</span>
          <span class="k">检查点</span><span class="v">${checkpoint}</span>
          <span class="k">归属</span><span class="v">${act ? (act.ownerless ? "她自己（无主源）" : "某个主人") : "—"}</span>
        </div>
        ${recovered}${humanNote}
        <div class="steps">${steps || `<span class="dim small">没有进行中的步骤</span>`}</div>
        <div class="act-actions">${actions}<button id="act-new">新开一件事</button></div>
      </div>
      <div class="card">
        <div class="card-head"><span class="card-title">串行 · 断点 · 恢复</span><span class="card-sub">one step at a time</span></div>
        <div class="reason">同一实体只有一条 life loop、一个步骤在跑。人开口 → 当前步做完就停、写检查点；说完 → 自动接着做。进程重启 → RUNNING 降级为 PAUSED，当前步重试。</div>
      </div>`;
    $("act-start")?.addEventListener("click", () => activityAction("/api/activity/start",
      { entity: state.entity, principal: state.principal }));
    $("act-new")?.addEventListener("click", () => activityAction("/api/activity/start",
      { entity: state.entity, principal: state.principal }));
    $("act-pause")?.addEventListener("click", () => activityAction("/api/activity/pause"));
    $("act-resume")?.addEventListener("click", () => activityAction("/api/activity/resume"));
    $("act-reload")?.addEventListener("click", () => activityAction("/api/activity/reload"));
    $("act-reset")?.addEventListener("click", () => activityAction("/api/activity/reset"));
  }

  function renderMemoryTab() {
    const memory = state.data?.memory || { facts: [], visible_counts: {}, hidden_counts: {}, room: [] };
    const decision = state.lastDecision || (state.data?.route_log || [])[0] || null;
    const scope = decision?.scope || {};
    const room = (memory.room || []).map(principalName).join("、") || "—";
    const order = ["world", "consensus", "circle", "self", "pair", "private"];
    const visible = (memory.facts || []).filter((fact) => fact.visible);
    const grouped = {};
    for (const fact of visible) (grouped[fact.layer] ||= []).push(fact);
    const sections = order.filter((layer) => grouped[layer]).map((layer) => {
      const rows = grouped[layer].map((fact) => `<div class="list-item"><span class="mark">•</span>` +
        `<span><span class="badge">${esc(fact.layer_label || LAYER_LABEL[layer] || layer)}</span> ` +
        `${esc(fact.text)}<br><span class="dim small">${esc(fact.label || "")} · 可见域：${esc(fact.audience)}</span></span></div>`).join("");
      return `<div class="section-label">${esc(LAYER_LABEL[layer] || layer)} · ${grouped[layer].length}</div>` +
        `<div class="list">${rows}</div>`;
    }).join("");
    const hidden = Object.entries(memory.hidden_counts || {})
      .map(([layer, count]) => `${LAYER_LABEL[layer] || layer} ${count}`).join(" / ") || "无";
    $("tab-memory").innerHTML = `
      <div class="card">
        <div class="card-head"><span class="card-title">记忆池</span><span class="card-sub">layer + audience</span></div>
        <div class="kv">
          <span class="k">共享</span><span class="v mono small">${esc(scope.shared_bank || `blueagent-${state.entity}`)}</span>
          <span class="k">${scope.self_owned ? "她自己的" : "私池"}</span><span class="v mono small">${esc(scope.private_bank || `blueagent-${state.entity}-self`)}</span>
          <span class="k">在場</span><span class="v">${esc(room)}</span>
        </div>
        <div class="reason">可见规则：在場的人 P ⊆ 記憶可見域 A。任何一個在場的人不在 A 裡，這條就不進 prompt。</div>
      </div>
      ${sections || `<div class="card"><span class="dim">当前房间没有可读记忆。</span></div>`}
      <div class="divider"></div>
      <div class="section-label">被隔離 · 不可讀</div>
      <div class="list"><div class="list-item"><span class="mark">×</span><span class="dim">${esc(hidden)} 条</span></div></div>`;
  }

  function renderRelationsTab() {
    const social = state.data?.social || { relations: [], circles: [] };
    const circles = (social.circles || []).map((circle) => `<div class="card">
      <div class="card-head">
        <span class="card-title">${esc(circle.name)}</span>
        <span class="badge">${esc(circle.kind)}</span>
      </div>
      <div class="kv"><span class="k">成员</span><span class="v">${esc((circle.members || []).map(principalName).join("、"))}</span></div>
    </div>`).join("");
    const relations = (social.relations || []).map((relation) => `<div class="card">
      <div class="card-head">
        <span class="card-title">${esc(principalName(relation.a))} ↔ ${esc(principalName(relation.b))}</span>
        <span class="badge">${esc(relation.kind)}</span>
      </div>
      <div class="kv">
        <span class="k">称呼</span><span class="v">${esc(relation.label)}</span>
        <span class="k">亲密度</span><span class="v">${esc(relation.strength)} · 信任 ${esc(relation.trust)}</span>
      </div>
    </div>`).join("");
    const stances = (state.data?.stances || []).map((stance) => {
      const axes = (stance.axes || []).filter((axis) => axis.weight > 0).slice(0, 4);
      const rows = axes.map((axis) => `<div class="list-item">
        <span class="mark">·</span>
        <span><b>${esc(axis.label)}</b> <span class="dim small">${esc(axis.tier)} · ${esc(axis.weight)}</span>
        <span class="dim small">${esc(axis.note || axis.tone || "")}</span></span>
      </div>`).join("");
      return `<div class="card">
        <div class="card-head">
          <span class="card-title">她对 ${esc(principalName(stance.principal))} 的立场</span>
          <span class="badge">trust ${esc(stance.trust)}</span>
        </div>
        <div class="list">${rows || `<span class="dim small">还没有明确立场</span>`}</div>
      </div>`;
    }).join("");
    $("tab-relations").innerHTML = `
      <div class="card">
        <div class="card-head"><span class="card-title">关系认知</span><span class="card-sub">记忆可见域的依据</span></div>
        <div class="reason">关系图不是装饰：它决定哪些记忆能进同一个房间的 prompt。家人圈、同事圈、共识层各是一组 audience。</div>
      </div>
      <div class="section-label">圈子</div>${circles || `<span class="dim small">空</span>`}
      <div class="section-label">关系边</div>${relations || `<span class="dim small">空</span>`}
      <div class="divider"></div>
      <div class="card">
        <div class="card-head"><span class="card-title">她对每个人的立场</span><span class="card-sub">只影响语气，不打开记忆</span></div>
        <div class="reason">这是角色 → 人的慢变量（stance），和上面的 人 ↔ 人关系图分开存。立场再高也不会让 pair/private 记忆变得可读。</div>
      </div>
      ${stances || `<span class="dim small">空</span>`}`;
  }

  function renderEpisodesTab() {
    const episodes = (state.data?.episodes || []).slice().reverse();
    const cards = episodes.map((ep) => `<div class="card">
      <div class="card-head">
        <span class="card-title">#${ep.index} · ${esc(ep.id)}</span>
        <span class="badge">${esc(ep.reason)}</span>
      </div>
      <div class="card-sub">${ep.turns} 轮 · ${ep.chars} 字 · ${esc(ep.entity)}</div>
      <div class="kv" style="margin-top:8px"><span class="k">摘要</span><span class="v">${esc(ep.summary || "（待生成）")}</span></div>
      <div class="kv"><span class="k">预览</span><span class="v small dim">${esc((ep.preview || []).join(" / "))}</span></div>
    </div>`).join("");
    $("tab-episodes").innerHTML = `
      <div class="card">
        <div class="card-head"><span class="card-title">自动切片</span><span class="card-sub">无需用户操作</span></div>
        <div class="kv"><span class="k">触发</span><span class="v">闲置 90s / 6 轮 / 1600 字</span></div>
        <div style="margin-top:9px"><button id="btn-seal">立即封片（模拟闲置到点）</button></div>
      </div>${cards || `<div class="card"><span class="dim">还没有片段——继续聊，或点上面的按钮。</span></div>`}`;
    const seal = $("btn-seal");
    if (seal) seal.onclick = sealNow;
  }

  function renderThreadsTab() {
    const thread = currentThread();
    const all = state.data?.threads || [];
    const current = thread ? `<div class="card">
      <div class="card-head"><span class="card-title">${esc(thread.id)}</span><span class="badge">${esc(thread.state)}</span></div>
      <div class="kv">
        <span class="k">scope</span><span class="v mono">${esc(thread.scope)}</span>
        <span class="k">key</span><span class="v mono small">${esc(thread.scope)} / ${esc(thread.entity)} / ${esc(thread.scene)} / ${esc(thread.purpose)}</span>
        <span class="k">participants</span><span class="v">${esc((thread.participants || []).join(", ") || "—")}</span>
        <span class="k">turns / epoch</span><span class="v">${thread.turns} · ${thread.epoch}</span>
        <span class="k">segment</span><span class="v">${thread.segment} 条（封片后归零）</span>
        <span class="k">inflight</span><span class="v">${esc(thread.inflight || "—")} · queued ${thread.queued}</span>
        <span class="k">carry</span><span class="v small dim">${esc(thread.carry || "（无）")}</span>
      </div>
      <div class="divider"></div>
      <div class="timeline-mini">${(thread.messages || []).slice(-8).map((m) => `<div class="row ${m.role}">` +
        `<span class="mono">${esc((m.speaker || m.role).slice(0, 8))}</span><span class="txt">${esc(m.text)}</span></div>`).join("")}</div>
    </div>` : `<div class="card"><span class="dim">当前视图还没有线程。</span></div>`;
    const rows = all.map((item) => `<div class="list-item"><span class="mark">${item.scope === scopeFor() && item.entity === state.entity ? "›" : "·"}</span>` +
      `<span><span class="mono small">${esc(item.id)}</span><br><span class="dim small">${esc(item.scope)} / ${esc(item.entity)} · ` +
      `${item.turns} 轮 · ${esc(item.state)}</span></span></div>`).join("");
    $("tab-threads").innerHTML = current + `<div class="section-label">全部线程（${all.length}）</div><div class="list">${rows || `<span class="dim small">空</span>`}</div>`;
  }

  function renderScheduleTab() {
    const scheduler = state.data?.scheduler || {};
    const pipelines = state.data?.pipelines || [];
    $("tab-schedule").innerHTML = `
      <div class="card">
        <div class="card-head"><span class="card-title">调度器</span><span class="card-sub">实时并行 · 非实时排队</span></div>
        <div class="kv">
          <span class="k">pending</span><span class="v">${esc(scheduler.pending ?? 0)}</span>
          <span class="k">active</span><span class="v">${esc(JSON.stringify(scheduler.active || {}))}</span>
          <span class="k">slots</span><span class="v">${esc(JSON.stringify(scheduler.slots || {}))}</span>
          <span class="k">lanes</span><span class="v mono small">${esc(JSON.stringify(scheduler.lanes || {}))}</span>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><span class="card-title">管道</span><span class="card-sub">stage 是插件</span></div>
        ${pipelines.map((p) => `<div class="kv"><span class="k">${esc(p.id)}</span><span class="v small">${esc((p.stages || []).join(" → "))}</span></div>`).join("")}
      </div>`;
  }

  function renderDebug() {
    renderRouteTab();
    renderActivityTab();
    renderVoiceTab();
    renderGuestTab();
    renderMemoryTab();
    renderRelationsTab();
    renderEpisodesTab();
    renderThreadsTab();
    renderScheduleTab();
    $("debug-sub").textContent = `${(state.data?.threads || []).length} threads · ${(state.data?.episodes || []).length} episodes`;
  }

  function showTab(name) {
    state.tab = name;
    for (const button of document.querySelectorAll("#debug-tabs button")) {
      button.classList.toggle("active", button.dataset.tab === name);
    }
    for (const panel of document.querySelectorAll(".tab-panel")) {
      panel.classList.toggle("active", panel.id === `tab-${name}`);
    }
  }

  function renderCall() {
    const session = state.data?.voice?.session || null;
    const banner = $("call-banner");
    if (!session) {
      banner.classList.remove("show");
      return;
    }
    banner.classList.add("show");
    const end = session.end === "discord" ? "Discord" : (session.end === "phone" ? "手机" : session.end);
    $("call-text").textContent = `通话中 · ${end} · ${session.duration_s ?? 0}s`;
    $("call-hangup").onclick = async () => {
      try {
        await api("/api/voice/hangup", { method: "POST", body: "{}" });
        // 掛斷回應只帶語音那一塊（整份狀態太大，2026-11-01）⇒ 自己再拿一次
        await refreshAndRender();
        flash("已挂断，后续回复回到文字。");
      } catch (error) {
        flash(`挂断失败：${error.message}`);
      }
    };
  }

  function renderAll() {
    renderEntitySelect();
    renderPrincipals();
    renderMode();
    renderRoom();
    renderCall();
    renderStage();
    renderQuick();
    renderTimeline();
    renderDebug();
  }

  // ── 动作 ────────────────────────────────────────────────────────────────────
  async function refreshAndRender() {
    try {
      const room = (state.mode === "group" || state.mode === "self") && state.room.length
        ? state.room.join(",") : state.principal;
      const data = await api(`/api/state?principal=${encodeURIComponent(state.principal)}` +
        `&entity=${encodeURIComponent(state.entity)}&room=${encodeURIComponent(room)}`);
      state.data = data;
      if (!state.lastDecision) {
        const thread = currentThread();
        state.lastDecision = (data.route_log || []).find((item) => thread && item.thread === thread.id) || null;
      }
      renderAll();
    } catch (error) {
      flash(`状态读取失败：${error.message}`);
    }
  }

  async function send(text, options = {}) {
    text = String(text || "").trim();
    if (!text || state.busy) return;
    const group = state.mode === "group";
    const ownerless = state.mode === "self";
    const principal = ownerless ? state.principal : (options.principal || state.principal);
    const mention = options.mention !== undefined ? options.mention : (group && $("mention").checked);
    const addressedTo = options.addressed_to || "";
    const participants = (group || ownerless) ? state.room : [principal];
    if (!ownerless) state.principal = principal;
    state.pending = { text, principal, ownerless };
    state.busy = true;
    state.lastDecision = null;
    state.lastRecall = [];
    state.lastRecallDetail = [];
    state.lastSpeech = null;
    $("send").disabled = true;
    renderPrincipals();
    renderTimeline();
    state.lastFrames = [];
    state.streamText = "";
    try {
      const data = await streamSend({
        text, entity: state.entity, principal, group, mention,
        ownerless, addressed_to: addressedTo, participants,
      }, onStreamFrame);
      state.data = data.state;
      state.lastDecision = data.decision;
      const recall = recallFromFrames(state.lastFrames);
      state.lastRecall = recall.facts;
      state.lastRecallDetail = recall.detail;
      const speech = (data.frames || []).filter((frame) => frame.kind === "speech");
      state.lastSpeech = speech.length ? speech[speech.length - 1].payload : null;
      if (data.yielded) {
        flash(`她在礼让：等 ${((data.yield_ms || 8000) / 1000).toFixed(0)}s，没人接再补位。`);
      } else if (data.reacted) {
        flash("她贴了一个 📝（mock reaction），没有在群里刷一句话。");
      } else if (data.observed) {
        flash("她在旁听：这句话不是对她说的，没有插话，但记进了时间线。");
      } else if (data.duplicate) {
        flash("重复事件：已被幂等窗口拦下。");
      } else if (data.dropped) {
        flash("这句话被当作噪音丢掉了。");
      } else if (data.sealed) {
        flash(`自动封片：${data.sealed.reason} · ${data.sealed.turns} 轮`);
      } else if (ownerless) {
        flash("这是她自己的行动：已记进 self 层，不归属任何主人。");
      } else if (data.error) {
        flash(`这轮出错：${data.error}`);
      }
    } catch (error) {
      flash(`发送失败：${error.message}`);
    } finally {
      state.busy = false;
      state.pending = null;
      state.streamText = "";
      $("send").disabled = false;
      renderAll();
      const timeline = $("timeline");
      timeline.scrollTop = timeline.scrollHeight;
    }
  }

  async function sealNow() {
    try {
      const data = await api("/api/seal", {
        method: "POST",
        body: JSON.stringify({ entity: state.entity, principal: state.principal,
                               group: state.mode === "group",
                               ownerless: state.mode === "self",
                               participants: state.room }),
      });
      state.data = data.state;
      if (data.sealed) flash(`已封片：${data.sealed.reason} · ${data.sealed.turns} 轮`);
      else flash(data.reason || "当前片段还是空的。");
      renderAll();
    } catch (error) {
      flash(`封片失败：${error.message}`);
    }
  }

  async function resetDemo() {
    if (!confirm("重置演示？会清空当前线程与记忆。")) return;
    try {
      const data = await api("/api/reset", { method: "POST", body: "{}" });
      state.data = data.state;
      state.lastDecision = null;
      state.lastRecall = [];
      state.lastRecallDetail = [];
      state.lastSpeech = null;
      state.lastFrames = [];
      state.room = ["owner", "alice", "bob"];
      flash("已重置。");
      renderAll();
    } catch (error) {
      flash(`重置失败：${error.message}`);
    }
  }

  function bind() {
    $("send").onclick = () => send($("input").value);
    $("input").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        send($("input").value);
        $("input").value = "";
        resizeInput();
      }
    });
    $("input").addEventListener("input", resizeInput);
    for (const button of document.querySelectorAll("#mode-select button")) {
      button.onclick = () => {
        state.mode = button.dataset.mode;
        state.lastDecision = null;
        state.lastRecall = [];
        state.lastRecallDetail = [];
        renderMode();
        refreshAndRender();
      };
    }
    $("mention").onchange = () => { /* 送出時讀取 */ };
    $("btn-reset").onclick = resetDemo;
    $("btn-debug").onclick = () => {
      state.debug = !state.debug;
      document.body.classList.toggle("debug-hidden", !state.debug);
      $("btn-debug").textContent = state.debug ? "隐藏面板" : "显示面板";
    };
    for (const button of document.querySelectorAll("#debug-tabs button")) {
      button.onclick = () => showTab(button.dataset.tab);
    }
    if (window.innerWidth <= 1080) {
      state.debug = false;
      document.body.classList.add("debug-hidden");
      $("btn-debug").textContent = "显示面板";
    }
  }

  function resizeInput() {
    const input = $("input");
    input.style.height = "auto";
    input.style.height = Math.min(120, input.scrollHeight) + "px";
  }

  async function boot() {
    const params = new URLSearchParams(window.location.search);
    if (params.get("entity")) state.entity = params.get("entity");
    if (params.get("principal")) state.principal = params.get("principal");
    if (params.get("mode") === "group") state.mode = "group";
    if (params.get("mode") === "self") state.mode = "self";
    if (params.get("mention") === "1") $("mention").checked = true;
    bind();
    if (params.get("room")) state.room = params.get("room").split(",").filter(Boolean);
    const tabParam = params.get("tab");
    if (["route", "activity", "voice", "guests", "memory", "relations", "episodes", "threads", "schedule"].includes(tabParam)) {
      showTab(tabParam);
    }
    await refreshAndRender();
    const autoText = params.get("text");
    if (autoText) {
      setTimeout(() => send(autoText, {
        principal: params.get("as") || state.principal,
        addressed_to: params.get("to") || "",
      }), 250);
    }
    setInterval(() => {
      if (!state.busy) refreshAndRender();
    }, 4500);
    setInterval(() => {
      if (!state.busy && state.tab === "activity") refreshAndRender();
    }, 1200);
  }

  boot();
})();
