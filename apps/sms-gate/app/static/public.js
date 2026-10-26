/* SMS Gate - public card page */
(function () {
  "use strict";
  const app = document.getElementById("app");
  const $ = (id) => document.getElementById(id);
  const viewAuth = $("view-auth"), viewWork = $("view-work");
  const keyInput = $("keyInput"), btnAuth = $("btnAuth"), authError = $("authError");
  const countryGrid = $("countryGrid"), btnNumber = $("btnNumber"), numberBox = $("numberBox");
  const btnPoll = $("btnPoll"), codeBox = $("codeBox"), pollState = $("pollState");
  const btnRetryNum = $("btnRetryNum");
  const lastPollEl = $("lastPoll"), ttlEl = $("ttlText"), pollBar = $("pollBar");
  const logEl = $("log"), cardPrefix = $("cardPrefix"), cardState = $("cardState");
  const netStatus = $("netStatus");
  const COUNTRY_NAMES = {
    us: "美国", gb: "英国", hk: "香港",
    cn_real: "国内实卡", cn_virtual: "国内虚卡", cn: "中国"
  };
  const cname = (code) => COUNTRY_NAMES[code] || String(code || "").toUpperCase();

  const st = {
    csrf: "", session: null, country: "", choice: "", locked: false,
    number: null, code: "",
    polling: false, timer: null, ttlTimer: null, done: false, interval: 15,
    ttlEnd: 0, busy: false, logs: [], queue: null, qTimer: null,
    exitTimer: null, exiting: false, revoked: false,
    retryTimer: null, attempts: 0, maxAttempts: 3
  };

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function toast(msg, kind) {
    const el = $("toast");
    el.textContent = msg;
    el.className = "toast show " + (kind || "");
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.className = "toast " + (kind || ""); }, 3200);
  }

  function log(msg, kind) {
    const now = new Date();
    const t = now.toTimeString().slice(0, 8);
    st.logs.unshift({ t, msg, kind: kind || "" });
    if (st.logs.length > 40) st.logs.pop();
    renderLog();
  }
  function renderLog() {
    logEl.innerHTML = st.logs.map((l) =>
      '<li><time>' + esc(l.t) + '</time><span class="' + l.kind + '">' + esc(l.msg) + "</span></li>").join("");
    $("logMeta").textContent = st.logs.length ? st.logs.length + " 条" : "";
  }

  async function api(path, opts) {
    opts = opts || {};
    opts.credentials = "same-origin";
    opts.headers = Object.assign({ "Accept": "application/json" }, opts.headers || {});
    if (opts.body) {
      opts.headers["Content-Type"] = "application/json";
      if (st.csrf) opts.headers["X-CSRF"] = st.csrf;
    }
    let r = await fetch(path, opts);
    let data = null;
    try { data = await r.json(); } catch (e) { data = { ok: false, error: "响应异常" }; }
    // Upstream queueing: wait for the server-provided retry window, then try once more.
    if (r.status === 429 && opts.retry !== false) {
      const ra = Number((data && data.retry_after) || 5);
      if (ra > 30) {
        // Long upstream ban: retrying in 2s is pointless, say when it comes back.
        toast("号源通道限流，约 " + Math.ceil(ra / 60) + " 分钟后恢复", "bad");
        data._status = r.status;
        return data;
      }
      const wait = Math.min(25, Math.max(2, ra));
      toast("号源补货中，" + wait + " 秒后自动重试", "");
      await new Promise((res) => setTimeout(res, wait * 1000));
      r = await fetch(path, opts);
      try { data = await r.json(); } catch (e) { data = { ok: false, error: "响应异常" }; }
    }
    data._status = r.status;
    return data;
  }

  function setNet(ok) {
    netStatus.className = "chip " + (ok ? "chip-ok" : "chip-bad");
    netStatus.innerHTML = ok ? '<i class="pulse"></i>通道在线' : '<i class="pulse"></i>连接中断';
  }

  function showAuth(msg) {
    stopQueuePoll();
    viewWork.classList.add("hidden");
    viewAuth.classList.remove("hidden");
    if (msg) { authError.textContent = msg; authError.classList.remove("hidden"); }
    else authError.classList.add("hidden");
  }
  function showWork() {
    viewAuth.classList.add("hidden");
    viewWork.classList.remove("hidden");
  }

  function maskKey(s) {
    s = String(s || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
    const out = [];
    for (let i = 0; i < s.length && out.length < 4; i += 4) out.push(s.slice(i, i + 4));
    return out.join("-");
  }
  keyInput.addEventListener("input", () => {
    const raw = keyInput.value.replace(/[^A-Za-z0-9]/g, "").toUpperCase().slice(0, 16);
    keyInput.value = maskKey(raw);
  });

  // ------------------------------------------------------------ auth
  async function redeem(key) {
    btnAuth.disabled = true;
    btnAuth.textContent = "验证中…";
    const r = await api("/api/redeem", { method: "POST", body: JSON.stringify({ key }) });
    btnAuth.disabled = false;
    btnAuth.textContent = "验证并进入";
    if (!r.ok) {
      showAuth(r.error || "验证失败");
      setNet(r._status !== 0);
      return;
    }
    setNet(true);
    authError.classList.add("hidden");
    log("卡密验证通过", "ok");
    // 手动输入的卡密也切到专属链接，刷新时可恢复本次会话
    if (!(app.dataset.key || "").trim()) {
      try { history.replaceState(null, "", "/c/" + encodeURIComponent(key)); } catch (e) { /* ignore */ }
    }
    await loadSession(true);
  }

  $("authForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const key = keyInput.value.trim();
    if (key.replace(/-/g, "").length < 8) { showAuth("请输入完整卡密"); return; }
    redeem(key);
  });

  function paintChoice(code) {
    [...countryGrid.querySelectorAll(".country")].forEach((b) => {
      const on = b.dataset.code === code;
      b.classList.toggle("active", on);
      if (st.locked) {
        b.disabled = true;
        b.classList.toggle("locked", on);
      }
    });
  }

  function lockChoice(code, silent) {
    st.choice = code;
    st.country = code;
    st.locked = true;
    paintChoice(code);
    $("numHint").textContent = cname(code) + "号段 · 已锁定";
    btnNumber.disabled = false;
    if (!silent) log("号段已锁定：" + cname(code) + "（不可更改）", "ok");
  }

  async function chooseCountry(code) {
    if (st.locked || st.code || st.queue) { toast("号段已锁定，无法更改", "bad"); return; }
    if (code === st.choice) return;
    const name = cname(code);
    if (!window.confirm("号段一经选择不可更改。\n确认使用「" + name + "」号段接码吗？")) return;
    const r = await api("/api/choice", {
      method: "POST", retry: false,
      body: JSON.stringify({ choice: code, csrf: st.csrf })
    });
    if (!r.ok) {
      if (r.locked) lockChoice(r.locked, true);
      toast(r.error || "号段锁定失败", "bad");
      return;
    }
    lockChoice(code);
    toast("已锁定「" + name + "」号段", "good");
  }
  countryGrid.addEventListener("click", (e) => {
    const b = e.target.closest(".country");
    if (b) chooseCountry(b.dataset.code);
  });

  // ------------------------------------------------------------ session
  async function loadSession(silent) {
    const r = await api("/api/session");
    if (!r.ok) {
      if (r._status === 401) { showAuth(""); return false; }
      toast(r.error || "加载失败", "bad");
      return false;
    }
    st.csrf = r.csrf;
    st.session = r;
    st.interval = Number(r.poll_interval || 15);
    st.cooldown = Number(r.cooldown || 0);
    if (st.cooldown > 60) {
      toast("号源通道限流，约 " + Math.ceil(st.cooldown / 60) + " 分钟后恢复", "bad");
    }
    $("pollInt").textContent = st.interval;
    cardPrefix.textContent = r.card.prefix;
    renderCardState(r.card.status);
    renderCountries(r.choices || r.countries || []);
    if (r.choice && r.choice.code) lockChoice(r.choice.code, true);
    if (r.number) {
      if (r.number.choice) lockChoice(r.number.choice, true);
      st.number = r.number;
      st.ttlEnd = Date.now() / 1000 + Number(r.number.ttl_left || 0);
      renderNumber(r.number);
      btnPoll.disabled = false;
      if (!st.ttlTimer) startTtl();
    }
    if (r.sms && r.sms.code) {
      foundCode(r.sms.code, r.sms.polls || 0,
                { force: Number(r.card.remaining) === 0, grace: 8 });
    } else if (r.number && !st.done) {
      startPolling();
    }
    if (!silent) log("会话已恢复");
    showWork();
    return true;
  }

  function renderCardState(s) {
    const map = {
      active: ["chip-ok", "可接码"], used: ["chip-warn", "已核销"],
      disabled: ["chip-bad", "已禁用"], expired: ["chip-bad", "已过期"]
    };
    const m = map[s] || ["chip-idle", s];
    cardState.className = "chip " + m[0];
    cardState.textContent = m[1];
    if (s !== "active") st.code = st.code || "";
  }

  function renderCountries(list) {
    const map = {};
    list.forEach((c) => { map[c.code] = c; });
    [...countryGrid.querySelectorAll(".country")].forEach((b) => {
      const c = map[b.dataset.code];
      const stockEl = b.querySelector(".c-stock");
      if (!c) { b.style.display = "none"; return; }
      b.style.display = "";
      if (c.stock >= 999 && st.cooldown > 60) { stockEl.textContent = "通道限流"; stockEl.classList.remove("has"); }
      else if (c.stock >= 999) { stockEl.textContent = "按需取号"; stockEl.classList.add("has"); }
      else if (c.stock > 0) { stockEl.textContent = "库存 " + c.stock; stockEl.classList.add("has"); }
      else { stockEl.textContent = "缺货"; stockEl.classList.remove("has"); }
    });
    const visible = list.filter((c) => map[c.code]);
    const fixed = list.length === 1 ? list[0] : null;
    const badge = $("choiceFixed");
    const step1 = $("step1");
    if (step1) {
      const h2 = step1.querySelector("h2"), hint = step1.querySelector(".step-hint");
      if (h2) h2.textContent = fixed ? "卡密号段" : "选择号段";
      if (hint) hint.textContent = fixed ? "已指定 · 不可更改" : "选定后不可更改";
    }
    if (badge) {
      if (fixed) {
        badge.classList.remove("hidden");
        badge.innerHTML = '<span class="cf-flag">' + esc(fixed.flag || "") + "</span>" +
          '<span class="cf-name">' + esc(fixed.name || cname(fixed.code)) + "</span>" +
          '<span class="cf-tag">卡密指定 · 不可更改</span>';
        countryGrid.classList.add("hidden");
      } else {
        badge.classList.add("hidden");
        countryGrid.classList.remove("hidden");
      }
    }
    if (fixed && !st.number) {
      const es = numberBox.querySelector(".empty-state span:last-child");
      if (es) es.textContent = "点击下方按钮获取号码";
    }
    if (st.locked) { paintChoice(st.choice); return; }
    if (!map[st.choice]) {
      const first = fixed || visible[0];
      if (first) {
        st.choice = first.code;
        $("numHint").textContent = cname(first.code) + "号段";
      }
    }
    paintChoice(st.choice);
  }

  // ------------------------------------------------------------ number
  function renderNumber(n) {
    numberBox.classList.add("filled");
    numberBox.innerHTML =
      '<div class="num-display">' +
      '<div class="num-flag">' + esc((n.name || cname(n.choice || n.country)) + " · " + String(n.country || "").toUpperCase()) + "</div>" +
      '<div class="num-value" id="numValue">' + esc(n.phone) + "</div>" +
      '<div class="num-sub">号码已分配 · 收到短信后请尽快使用</div>' +
      '<button class="copy-btn" id="copyNum" type="button">复制号码</button>' +
      "</div>";
    const btn = $("copyNum");
    if (btn) btn.addEventListener("click", () => copy(n.phone, "号码已复制"));
  }

  function hideRetry() {
    if (!btnRetryNum) return;
    if (st.retryTimer) { clearInterval(st.retryTimer); st.retryTimer = null; }
    btnRetryNum.classList.add("hidden");
    btnRetryNum.disabled = true;
  }

  function armRetry(waitSec) {
    if (!btnRetryNum) return;
    if (st.retryTimer) { clearInterval(st.retryTimer); st.retryTimer = null; }
    const remain = Math.max(0, Number(st.maxAttempts || 3) - Number(st.attempts || 1));
    if (remain <= 0) {
      btnRetryNum.textContent = "换号次数已用完";
      btnRetryNum.disabled = true;
      btnRetryNum.classList.remove("hidden");
      return;
    }
    let left = Math.max(0, Number(waitSec) || 0);
    const paint = () => {
      btnRetryNum.classList.remove("hidden");
      btnRetryNum.disabled = left > 0;
      btnRetryNum.textContent = left > 0
        ? "收不到？换一个号码（" + left + "s，剩 " + remain + " 次）"
        : "收不到？换一个号码（剩 " + remain + " 次）";
    };
    paint();
    if (left <= 0) return;
    st.retryTimer = setInterval(() => {
      left -= 1;
      paint();
      if (left <= 0) { clearInterval(st.retryTimer); st.retryTimer = null; }
    }, 1000);
  }

  function adoptNumber(n, meta) {
    if (n.choice) lockChoice(n.choice, true);
    st.queue = null;
    if (meta) {
      st.attempts = Number(meta.attempts || st.attempts || 1);
      st.maxAttempts = Number(meta.max_attempts || st.maxAttempts || 3);
    }
    st.number = Object.assign({}, n, { name: n.name || cname(n.choice || n.country) });
    st.ttlEnd = Date.now() / 1000 + Number(n.ttl_left || 0);
    renderNumber(st.number);
    $("numHint").textContent = cname(n.choice || n.country) + "号段 · 已锁定";
    pollState.className = "chip chip-warn";
    pollState.textContent = "等待短信";
    btnNumber.disabled = false;
    btnNumber.textContent = "已取号";
    btnPoll.disabled = false;
    log("号码已分配：" + n.phone, "ok");
    toast("号码已就绪", "good");
    armRetry(45);
    startTtl();
    startPolling();
  }

  async function takeNumber(retry) {
    if (st.busy || st.done || st.queue) return;
    if (!st.choice) { toast("请先选择号段", "bad"); return; }
    if (!st.locked) {
      const name = cname(st.choice);
      if (!window.confirm("号段一经选择不可更改。\n确认使用「" + name + "」号段接码吗？")) return;
      const cr = await api("/api/choice", {
        method: "POST", retry: false,
        body: JSON.stringify({ choice: st.choice, csrf: st.csrf })
      });
      if (!cr.ok) {
        if (cr.locked) lockChoice(cr.locked, true);
        toast(cr.error || "号段锁定失败", "bad");
        return;
      }
      lockChoice(st.choice);
    }
    st.busy = true;
    btnNumber.disabled = true;
    btnNumber.textContent = retry ? "换号中…" : "取号中…";
    const payload = { choice: st.choice, csrf: st.csrf };
    if (retry) payload.retry = true;
    const r = await api("/api/number", { method: "POST", body: JSON.stringify(payload) });
    st.busy = false;
    if (!r.ok) {
      if (r.locked) lockChoice(r.locked, true);
      btnNumber.disabled = false;
      btnNumber.textContent = "获取号码";
      toast(r.error || "取号失败", "bad");
      log(r.error || "取号失败", "bad");
      return;
    }
    if (r.queued) {
      // Legacy response from an older server build: never park the browser in a queue.
      btnNumber.disabled = false;
      btnNumber.textContent = "重新获取";
      toast("号源正在补货，请重试", "");
      return;
    }
    btnNumber.disabled = false;
    if (r.retry_early) {
      toast("号码刚分配，" + Number(r.retry_in || 0) + " 秒后才能换号", "");
      adoptNumber(r.number, r);
      return;
    }
    adoptNumber(r.number, r);
  }
  btnNumber.addEventListener("click", () => takeNumber(false));
  if (btnRetryNum) btnRetryNum.addEventListener("click", () => {
    if (st.busy || st.done || !st.number) return;
    takeNumber(true);
  });

  // ------------------------------------------------------------ queue
  function stopQueuePoll() {
    if (st.qTimer) { clearInterval(st.qTimer); st.qTimer = null; }
  }

  function startQueuePoll() {
    stopQueuePoll();
    pollQueue();
    st.qTimer = setInterval(pollQueue, 2000);
  }

  async function pollQueue() {
    if (!st.queue) { stopQueuePoll(); return; }
    const t = st.queue;
    const r = await api("/api/queue/status", {
      method: "POST", retry: false,
      body: JSON.stringify({ ticket: t.id, token: t.token, csrf: st.csrf })
    });
    if (!st.queue) return;
    if (r.ok && r.number) {
      stopQueuePoll();
      st.queue = null;
      adoptNumber(r.number);
      return;
    }
    if (r.ok && r.state === "waiting") {
      st.queue = Object.assign({}, t, r);
      renderQueue(st.queue, r.queue);
      return;
    }
    stopQueuePoll();
    st.queue = null;
    btnNumber.disabled = false;
    btnNumber.textContent = "重新获取";
    const msg = r.error || "排队失败，请重试";
    pollState.className = "chip chip-bad";
    pollState.textContent = "排队失败";
    toast(msg, "bad");
    log(msg, "bad");
  }

  function renderQueue(q, qs) {
    const ahead = Math.max(0, Number(q.ahead || 0));
    const total = Math.max(1, Number(q.total || 1));
    const pos = Number(q.position || ahead + 1);
    const pct = Math.max(4, Math.min(100, ((total - ahead) / total) * 100));
    const cd = Number((q && q.cooldown) || (qs && qs.cooldown) || 0);
    $("numHint").textContent = "排队中";
    pollState.className = "chip chip-warn";
    pollState.textContent = "排队中";
    numberBox.innerHTML =
      '<div class="queue-box">' +
      '<div class="q-top"><span class="q-badge">排队中</span><span class="q-total">第 ' + pos + " 位 / 共 " + total + " 人</span></div>" +
      '<div class="q-num"><b>' + ahead + "</b><span>人排在你前面</span></div>" +
      '<div class="q-sub">' + (cd > 0
        ? "上游限流冷却，约 " + cd + " 秒后继续购号"
        : "预计 " + (q.eta || 0) + " 秒，系统正在批量购号") + "</div>" +
      '<div class="q-bar"><i style="width:' + pct.toFixed(1) + '%"></i></div>' +
      "</div>";
  }

  // ------------------------------------------------------------ polling
  function startPolling() {
    if (st.timer || st.done || !st.number) return;
    pollOnce();
    st.timer = setInterval(() => { if (!st.done) pollOnce(); }, st.interval * 1000);
  }

  function stopPolling() {
    if (st.timer) { clearInterval(st.timer); st.timer = null; }
  }

  async function pollOnce() {
    if (st.polling || st.done) return;
    st.polling = true;
    const t0 = Date.now();
    const r = await api("/api/poll", { method: "POST", body: JSON.stringify({ csrf: st.csrf }), retry: false });
    st.polling = false;
    lastPollEl.textContent = new Date().toTimeString().slice(0, 8);
    if (r.ok) {
      setNet(true);
      if (r.state === "code" && r.code) {
        foundCode(r.code, r.polls || 0, { force: !!r.force_exit, grace: r.grace });
      } else {
        pollState.className = "chip chip-warn";
        pollState.textContent = "等待短信";
        log("第 " + (r.polls || 0) + " 次刷新 · 暂无新短信");
      }
      animateBar(t0);
      return;
    }
    if (r._status === 401) { stopPolling(); forceLogout("会话已结束，请重新验证卡密"); return; }
    if (r._status === 410) {
      stopPolling();
      pollState.className = "chip chip-bad";
      pollState.textContent = "号码超时";
      log(r.error || "号码已超时", "bad");
      toast("号码已超时，请重新取号", "bad");
      btnNumber.textContent = "重新获取";
      btnNumber.disabled = false;
      armRetry(0);
      return;
    }
    if (r._status === 429) { log("刷新过快，已跳过本次"); animateBar(t0); return; }
    setNet(false);
    log(r.error || "刷新失败", "bad");
    animateBar(t0);
  }

  function animateBar(t0) {
    pollBar.style.width = "0%";
    requestAnimationFrame(() => {
      const pct = Math.min(100, ((Date.now() - t0) / 1000) / st.interval * 100);
      pollBar.style.width = Math.max(4, pct) + "%";
      setTimeout(() => { pollBar.style.width = "0%"; }, 300);
    });
  }

  btnPoll.addEventListener("click", () => {
    if (!st.number) { toast("请先获取号码", "bad"); return; }
    pollOnce();
  });

  function foundCode(code, polls, opts) {
    st.code = code;
    st.done = true;
    hideRetry();
    stopPolling();
    if (st.ttlTimer) { clearInterval(st.ttlTimer); st.ttlTimer = null; }
    const digits = String(code).padEnd(6, " ").slice(0, 6).split("");
    codeBox.innerHTML = digits.map((d) => '<span class="d">' + esc(d.trim() || " ") + "</span>").join("");
    codeBox.classList.add("found");
    pollState.className = "chip chip-ok";
    pollState.textContent = "已收到";
    btnPoll.disabled = true;
    btnPoll.textContent = "接码完成";
    ttlEl.textContent = "已完成";
    renderCardState("used");
    btnNumber.disabled = true;
    btnNumber.textContent = "\u5df2\u6838\u9500";
    log("收到验证码 " + code + "（第 " + (polls || 0) + " 次）", "ok");
    toast("验证码已到达", "good");
    setTimeout(() => copy(code, "验证码已复制"), 250);
    if (opts && opts.force) {
      st.revoked = true;
      const g = Number(opts.grace);
      startExitCountdown(Number.isFinite(g) ? g : 8);
    }
  }

  function startExitCountdown(sec) {
    if (st.exitTimer) clearInterval(st.exitTimer);
    let left = Math.max(0, Math.round(sec));
    if (left === 0) { forceLogout("卡密已核销，页面已自动退出"); return; }
    const wrap = codeBox.parentElement;
    let btn = $("btnExitNow");
    if (!btn) {
      btn = document.createElement("button");
      btn.id = "btnExitNow";
      btn.type = "button";
      btn.className = "btn primary wide";
      btn.addEventListener("click", () => forceLogout("已退出"));
      wrap.appendChild(btn);
    }
    const paint = () => {
      pollState.className = "chip chip-ok";
      pollState.textContent = left > 0 ? "已核销 · " + left + " 秒后退出" : "已核销";
      btn.textContent = "立即退出（" + Math.max(0, left) + "）";
    };
    paint();
    st.exitTimer = setInterval(() => {
      left -= 1;
      paint();
      if (left <= 0) {
        clearInterval(st.exitTimer);
        st.exitTimer = null;
        forceLogout("卡密已核销，页面已自动退出");
      }
    }, 1000);
  }

  async function forceLogout(msg) {
    if (st.exiting) return;
    st.exiting = true;
    if (st.exitTimer) { clearInterval(st.exitTimer); st.exitTimer = null; }
    stopPolling();
    stopQueuePoll();
    if (st.ttlTimer) { clearInterval(st.ttlTimer); st.ttlTimer = null; }
    if (!st.revoked) {
      try {
        await api("/api/logout", { method: "POST", body: JSON.stringify({ csrf: st.csrf }), retry: false });
      } catch (e) { /* ignore */ }
    }
    // Drop every trace from the page before leaving.
    st.csrf = ""; st.session = null; st.number = null; st.code = ""; st.done = true; st.queue = null;
    codeBox.innerHTML = ["–", "–", "–", "–", "–", "–"].map((d) => '<span class="d">' + d + "</span>").join("");
    codeBox.classList.remove("found");
    numberBox.classList.remove("filled");
    try { history.replaceState(null, "", "/"); } catch (e) { /* ignore */ }
    toast(msg || "已退出", "good");
    setTimeout(() => location.replace("/"), 650);
  }

  function startTtl() {
    if (st.ttlTimer) clearInterval(st.ttlTimer);
    st.ttlTimer = setInterval(() => {
      const left = Math.max(0, Math.floor(st.ttlEnd - Date.now() / 1000));
      ttlEl.textContent = left > 0 ? "号码剩余 " + fmt(left) : (st.done ? "已完成" : "号码已超时");
      if (left === 0 && !st.done) {
        pollState.className = "chip chip-bad";
        pollState.textContent = "号码超时";
        stopPolling();
      }
    }, 1000);
  }

  function fmt(s) {
    const m = Math.floor(s / 60), ss = s % 60;
    return String(m).padStart(2, "0") + ":" + String(ss).padStart(2, "0");
  }

  async function copy(text, msg) {
    try {
      await navigator.clipboard.writeText(String(text));
      toast(msg || "已复制", "good");
    } catch (e) {
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); toast(msg || "已复制", "good"); }
      catch (e2) { toast("复制失败，请手动选择", "bad"); }
      ta.remove();
    }
  }

  $("btnExit").addEventListener("click", async () => {
    stopPolling();
    await api("/api/logout", { method: "POST", body: JSON.stringify({ csrf: st.csrf }) });
    location.href = "/";
  });

  // ------------------------------------------------------------ boot
  (async function boot() {
    setNet(true);
    const preset = (app.dataset.key || "").trim();
    if (!preset) {
      // 裸访问：绝不自动恢复旧会话进入接码页，必须凭卡密链接或手动输入卡密
      st.csrf = "";
      st.session = null;
      showAuth("");
      setNet(true);
      return;
    }
    const r = await api("/api/session");
    const pfx = maskKey(preset).slice(0, 9).toUpperCase();
    if (r.ok && r.card && String(r.card.prefix || "").toUpperCase() === pfx) {
      log("已检测到该卡密的有效会话");
      await loadSession(true);
      return;
    }
    showAuth("");
    keyInput.value = maskKey(preset);
    log("检测到卡密链接，自动验证");
    setTimeout(() => redeem(preset), 260);
  })();
})();