/* SMS Gate - admin console */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const content = $("content");
  const S = { csrf: "", me: null, route: "", pageState: {} };

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pad = (n) => String(n).padStart(2, "0");
  function fmt(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
      pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  function toast(msg, kind) {
    const el = $("toast");
    el.textContent = msg;
    el.className = "toast show " + (kind || "");
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.className = "toast " + (kind || ""); }, 3000);
  }
  function badge(text, cls) { return '<span class="badge ' + (cls || "") + '">' + esc(text) + "</span>"; }
  function copy(text) {
    const s = String(text == null ? "" : text);
    if (!s) { toast("没有可复制的内容", "bad"); return; }
    const lines = s.split("\n").length;
    const good = () => toast(lines > 1 ? "已复制 " + lines + " 行" : "已复制", "good");
    const bad = () => toast("浏览器拦截了剪贴板，请点「下载 txt」或手动全选复制", "bad");
    const legacy = () => {
      try {
        const ta = document.createElement("textarea");
        ta.value = s;
        ta.setAttribute("readonly", "");
        ta.style.cssText = "position:fixed;top:-2000px;left:0;opacity:0";
        document.body.appendChild(ta);
        ta.focus(); ta.select(); ta.setSelectionRange(0, s.length);
        const done = document.execCommand("copy");
        ta.remove();
        done ? good() : bad();
      } catch (e) { bad(); }
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(s).then(good, legacy);
    } else legacy();
  }

  async function api(path, opt) {
    opt = opt || {};
    const init = { method: opt.method || "GET", credentials: "same-origin", headers: { "Accept": "application/json" } };
    if (opt.body) {
      init.headers["Content-Type"] = "application/json";
      init.headers["X-CSRF"] = S.csrf;
      init.body = JSON.stringify(opt.body);
    }
    const r = await fetch(path, init);
    let d;
    try { d = await r.json(); } catch (e) { d = { ok: false, error: "响应异常" }; }
    d._status = r.status;
    if (r.status === 401 && path !== "/admin/api/login") {
      location.href = "/admin/login";
      throw new Error("unauthorized");
    }
    return d;
  }

  async function boot() {
    let me;
    try {
      me = await api("/admin/api/me");
    } catch (e) {
      return; // 401 already redirected to the login page
    }
    if (!me.ok) return;
    S.me = me; S.csrf = me.csrf;
    $("meName").textContent = me.username;
    $("meAvatar").textContent = (me.username || "A").slice(0, 1).toUpperCase();
    window.addEventListener("hashchange", route);
    content.addEventListener("click", onClick);
    content.addEventListener("submit", onSubmit);
    // The dialog is a sibling of #content, so its buttons (复制全部 / 复制链接) never
    // reached the delegated handler and silently did nothing. Bind it as well.
    $("modal").addEventListener("click", onClick);
    $("modal").addEventListener("submit", onSubmit);
    $("modalClose").addEventListener("click", closeModal);
    $("modal").addEventListener("click", (e) => {
      if (e.target !== $("modal")) return;
      // Generated links only live in this dialog: a stray backdrop click must not destroy them.
      if ($("modal").dataset.lock === "1") { toast("请点右上角 × 关闭，避免误关丢失列表"); return; }
      closeModal();
    });
    $("btnLogout").addEventListener("click", async () => {
      await api("/admin/api/logout", { method: "POST", body: {} });
      location.href = "/admin/login";
    });
    setInterval(() => {
      const d = new Date();
      $("clock").textContent = pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
    }, 1000);
    route();
  }

  const TITLES = {
    dashboard: ["总览", "实时运营与安全态势"],
    cards: ["卡密管理", "创建、核销与状态跟踪"],
    relay: ["直链卡", "卡号:手机号----接码地址 · 可持续取码"],
    numbers: ["号码池", "号源库存与分配情况"],
    redemptions: ["兑换情况", "卡密授权与访问记录"],
    codes: ["接码情况", "短信验证码到达明细"],
    usage: ["使用情况", "卡密会话与访问"],
    audit: ["安全审计", "全量操作与安全事件"],
    ips: ["IP 管理", "访问统计、封禁与限流"],
    settings: ["系统设置", "号源、策略与站点信息"],
  };

  function route() {
    const hash = (location.hash || "#/dashboard").replace(/^#\//, "");
    const r = TITLES[hash] ? hash : "dashboard";
    S.route = r;
    document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === r));
    $("pageTitle").textContent = TITLES[r][0];
    $("pageSub").textContent = TITLES[r][1];
    ({ dashboard: pageDashboard, cards: pageCards, relay: pageRelay, numbers: pageNumbers, redemptions: pageRedemptions,
       codes: pageCodes, usage: pageUsage, audit: pageAudit, ips: pageIps, settings: pageSettings })[r]();
  }

  function closeModal() {
    $("modal").classList.add("hidden");
    $("modal").dataset.lock = "";
    $("modalBody").innerHTML = "";
  }
  function openModal(title, html, lock) {
    $("modalTitle").textContent = title;
    $("modalBody").innerHTML = html;
    $("modal").dataset.lock = lock ? "1" : "";
    $("modal").classList.remove("hidden");
  }

  function onClick(e) {
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const act = el.dataset.act;
    if (act === "copy") { copy(el.dataset.text || ""); return; }
    if (act === "page") { goPage(el.dataset.page); return; }
    if (act === "card-action") { cardAction(el.dataset.id, el.dataset.op); return; }
    if (act === "num-action") { numAction(el.dataset.id, el.dataset.op); return; }
    if (act === "block-ip") { blockIp(el.dataset.ip); return; }
    if (act === "unblock-ip") { ipAction(el.dataset.ip, "unblock"); return; }
    if (act === "test-provider") { testProvider(); return; }
    if (act === "test-d1jiema") { testProvider("d1jiema"); return; }
    if (act === "refill") { refillPool(); return; }
    if (act === "cooldown-clear") { clearCooldown(); return; }
    if (act === "queue-stats") { refreshQueueStats(); return; }
    if (act === "relay-action") { relayAction(el.dataset.id, el.dataset.op); return; }
    if (act === "relay-export") { relayExport(); return; }
    if (act === "copy-all") {
      const ta = $("allLinks");
      copy((S.lastRelay && S.lastRelay.lines) || (ta ? ta.value : ""));
      return;
    }
    if (act === "relay-batch-copy") { relayBatchExport("copy"); return; }
    if (act === "relay-batch-download") { relayBatchExport("download"); return; }
  }

  function onSubmit(e) {
    if (e.target.id === "createForm") { e.preventDefault(); createCards(); }
    else if (e.target.id === "relayForm") { e.preventDefault(); createRelay(); }
    else if (e.target.id === "relayAllocForm") { e.preventDefault(); allocateRelay(); }
    else if (e.target.id === "relayPoolForm") { e.preventDefault(); relayFromPool(e.target); }
    else if (e.target.id === "importForm") { e.preventDefault(); importNumbers(); }
    else if (e.target.id === "settingsForm") { e.preventDefault(); saveSettings(); }
    else if (e.target.id === "blockForm") { e.preventDefault(); blockIpSubmit(); }
  }

  const KIND = {
    card_redeem: "卡密兑换", number_alloc: "分配号码", code_received: "收到验证码",
    admin_login: "后台登录", admin_login_fail: "登录失败", admin_totp_fail: "动态码错误",
    admin_logout: "退出后台", ip_ban: "IP 封禁", ip_unblock: "IP 解封", ip_block: "IP 封禁",
    cards_create: "创建卡密", card_disable: "禁用卡密", card_enable: "启用卡密", card_delete: "删除卡密",
    numbers_import: "导入号码", number_disable: "禁用号码", number_enable: "启用号码", number_delete: "删除号码",
    settings_save: "修改设置", provider_test: "测试号源", blocked_hit: "拦截封禁 IP",
    redeem_fail: "卡密验证失败", redeem_invalid: "卡密格式错误", poll_error: "取码上游异常",
    admin_setup: "后台初始化", totp_bound: "绑定验证器", setup_fail: "初始化失败",
    allocate_error: "取号异常", unhandled_error: "系统异常", admin_login_step1: "账号验证通过",
    setup_totp_fail: "绑定验证失败", ip_note: "IP 备注", number_retry: "更换号码",
    relay_create: "生成直链卡", relay_from_pool: "号池转直链", relay_reset: "重置直链",
    relay_disable: "禁用直链", relay_enable: "启用直链", relay_delete: "删除直链",
    relay_error: "直链取码异常",
  };
  async function pageDashboard() {
    content.innerHTML = '<div class="loading">加载中…</div>';
    const d = await api("/admin/api/overview");
    if (!d.ok) { content.innerHTML = '<div class="empty">' + esc(d.error) + "</div>"; return; }
    const s = d.stats;
    $("providerPill").textContent = "号源 " + ({ pool: "本地号池", sms_activate: "SMS-Activate", fivesim: "5SIM", nexsms_premium: "NexSMS 优选", d1jiema: "d1jiema 国内" }[d.provider] || d.provider);
    const max = Math.max(1, ...d.series.map((x) => x.codes));
    const chart = d.series.map((x) => {
      const hgt = Math.round(x.codes / max * 100);
      return '<div class="col" title="' + esc(x.day) + ' 接码 ' + x.codes + ' / 兑换 ' + x.redemptions + '">' +
        '<span class="cnt">' + (x.codes || "") + "</span>" +
        '<div class="bar" style="height:' + Math.max(2, hgt) + '%"></div>' +
        '<span class="lbl">' + esc(x.day) + "</span></div>";
    }).join("");

    const stock = Object.entries(s.stock || {}).map(([k, v]) =>
      (k === "us" ? "美国" : k === "gb" ? "英国" : "香港") + " " + v).join(" · ");
    const events = (d.recent || []).map((e) =>
      "<tr><td class='mono'>" + fmt(e.ts) + "</td><td>" + badge(e.level, e.level === "alert" ? "violet" : e.level === "warn" ? "warn" : e.level === "error" ? "bad" : "info") +
      "</td><td>" + esc(KIND[e.kind] || e.kind) + "</td><td class='mono'>" + esc(e.ip || "—") + "</td><td class='mono'>" +
      esc((e.detail || "").slice(0, 70)) + "</td></tr>").join("");

    content.innerHTML =
      '<div class="stats">' +
      stat("卡密总数", s.cards_total, "今日新增 " + s.cards_today, "b") +
      stat("可用卡密", s.cards_active, "已核销 " + s.cards_used, "g") +
      stat("今日接码", s.codes_today + " / " + s.codes_total, "累计成功验证码", "g") +
      stat("今日兑换", s.redemptions_today, "取号 " + s.alloc_today + " 次", "b") +
      stat("号池库存", stock || "—", "按地区统计", "w") +
      stat("今日请求", s.events_today, "事件与操作审计", "b") +
      stat("封禁 IP", s.blocked_ips, "累计记录 " + s.ips_total + " 个 IP", s.blocked_ips ? "r" : "") +
      stat("已禁用卡密", s.cards_disabled, "可在卡密管理中恢复", "w") +
      "</div>" +
      '<div class="card"><div class="card-title"><h3>近 14 天接码趋势</h3><span class="hint">按北京时间自然日</span></div>' +
      '<div class="chart">' + chart + "</div></div>" +
      '<div class="card"><div class="card-title"><h3>最近安全事件</h3><a class="btn mini ghost" href="#/audit">查看全部</a></div>' +
      '<div class="tbl-wrap"><table><thead><tr><th>时间</th><th>级别</th><th>事件</th><th>IP</th><th>详情</th></tr></thead><tbody>' +
      (events || "<tr><td colspan=5 class='empty'>暂无记录</td></tr>") + "</tbody></table></div></div>";
  }
  function stat(k, v, s, cls) {
    return '<div class="stat ' + (cls || "") + '"><div class="k">' + esc(k) + '</div><div class="v">' + esc(v) +
      '</div><div class="s">' + esc(s) + "</div></div>";
  }

  async function pageCards() {
    const st = S.pageState.cards = S.pageState.cards || { status: "all", q: "", page: 1 };
    content.innerHTML =
      '<div class="card"><div class="card-title"><h3>生成卡密</h3><span class="hint">默认单次接码 · 链接即授权</span></div>' +
      '<form id="createForm" class="form-grid">' +
      '<label class="fld">数量<input name="count" type="number" min="1" max="500" value="10"></label>' +
      '<label class="fld">每卡可接码次数<input name="max_codes" type="number" min="1" max="20" value="1"></label>' +
      '<label class="fld">有效期（天，0 为永久）<input name="expires_days" type="number" min="0" max="3650" value="0"></label>' +
      '<label class="fld">号段（该链接只能接这一国号码）<select name="choice">' +
      opt("", "不限制（前台自选）") + opt("us", "美国实卡") + opt("gb", "英国") +
      opt("hk", "香港") + opt("cn_real", "国内实卡") + opt("cn_virtual", "国内虚拟卡") +
      "</select></label>" +
      '<label class="fld">备注<input name="note" maxlength="120" placeholder="例如：TG 渠道 / 批次 A"></label>' +
      '<div class="full"><button class="btn primary" type="submit">生成卡密链接</button>' +
      '<span class="help">生成后链接形如 <code id="linkSample">/c/XXXX-XXXX-XXXX-XXXX</code>，打开即自动授权。' +
      ' 需要 <b>卡号:手机号----接码地址</b>（可重复取码、隐藏上游）？<a href="#/relay" style="color:#34e0b0">去生成直链卡 →</a></span></div>' +
      "</form></div>" +
      '<div class="card"><div class="toolbar">' +
      '<select id="cardStatus">' + opt("all", "全部状态", st.status) + opt("active", "可用") + opt("used", "已核销") +
      opt("disabled", "已禁用") + opt("expired", "已过期") + "</select>" +
      '<input id="cardQ" placeholder="搜索卡密前缀 / 备注 / 批次" style="min-width:240px" value="' + esc(st.q) + '">' +
      '<button class="btn" id="cardSearch">搜索</button><div class="spacer"></div>' +
      '<span class="hint" id="cardTotal"></span></div><div class="mt12" id="cardTable"></div></div>';

    $("cardStatus").addEventListener("change", () => { st.status = $("cardStatus").value; st.page = 1; loadCards(); });
    $("cardQ").addEventListener("keydown", (e) => { if (e.key === "Enter") { st.q = $("cardQ").value.trim(); st.page = 1; loadCards(); } });
    $("cardSearch").addEventListener("click", () => { st.q = $("cardQ").value.trim(); st.page = 1; loadCards(); });
    $("linkSample").textContent = location.origin + "/c/XXXX-XXXX-XXXX-XXXX";
    loadCards();
  }

  async function loadCards() {
    const st = S.pageState.cards;
    const box = $("cardTable");
    if (!box) return;
    box.innerHTML = '<div class="loading">加载中…</div>';
    const d = await api("/admin/api/cards?status=" + encodeURIComponent(st.status) + "&q=" + encodeURIComponent(st.q) + "&page=" + st.page + "&size=50");
    if (!d.ok) { box.innerHTML = '<div class="empty">' + esc(d.error) + "</div>"; return; }
    $("cardTotal").textContent = "共 " + d.total + " 条";
    const now = d.now || Date.now() / 1000;
    const rows = d.items.map((c) => {
      let stt = c.status, cls = "ok", txt = "可用";
      if (c.used_codes >= c.max_codes) { stt = "used"; }
      if (stt === "used") { cls = "warn"; txt = "已核销"; }
      else if (stt === "disabled") { cls = "bad"; txt = "已禁用"; }
      else if (c.expires_at && c.expires_at < now) { cls = "bad"; txt = "已过期"; }
      const chCode = c.bound_choice || c.choice;
      const chName = ({ us: "美国", gb: "英国", hk: "香港", cn_real: "国内实卡",
        cn_virtual: "国内虚卡" }[chCode] || "未选择") +
        (c.bound_choice ? (c.choice ? " · 已取号" : " · 待取号") : "");
      return "<tr><td class='mono'>#" + c.id + "</td><td class='mono'>" + esc(c.key_prefix) + "…</td><td>" +
        badge(txt, cls) + "</td><td class='mono'>" + c.used_codes + " / " + c.max_codes + "</td><td>" +
        esc(chName) + "</td><td class='mono'>" +
        esc(c.batch_no || "—") + "</td><td>" + esc(c.note || "—") + "</td><td class='mono'>" + fmt(c.created_at) +
        "</td><td class='mono'>" + (c.last_ip ? esc(c.last_ip) : "—") + "</td><td class='mono'>" +
        (c.expires_at ? fmt(c.expires_at) : "永久") + "</td><td>" +
        (stt === "disabled" ? '<button class="btn mini" data-act="card-action" data-op="enable" data-id="' + c.id + '">启用</button> '
          : '<button class="btn mini" data-act="card-action" data-op="disable" data-id="' + c.id + '">禁用</button> ') +
        '<button class="btn mini" data-act="card-action" data-op="reset_choice" data-id="' + c.id + '">重置选择</button> ' +
        '<button class="btn mini danger" data-act="card-action" data-op="delete" data-id="' + c.id + '">删除</button></td></tr>';
    }).join("");
    box.innerHTML = '<div class="tbl-wrap"><table><thead><tr><th>ID</th><th>卡密</th><th>状态</th><th>次数</th><th>号段</th><th>批次</th><th>备注</th><th>创建时间</th><th>最近 IP</th><th>有效期</th><th>操作</th></tr></thead><tbody>' +
      (rows || "<tr><td colspan=10 class='empty'>暂无卡密</td></tr>") + "</tbody></table></div>" + pager(d.total, st.page, 50);
  }

  async function createCards() {
    const f = $("createForm");
    const btn = f.querySelector("button[type=submit]");
    btn.disabled = true;
    const body = {
      count: Number(f.count.value), max_codes: Number(f.max_codes.value),
      expires_days: Number(f.expires_days.value), note: f.note.value.trim(),
      choice: f.choice.value,
    };
    const d = await api("/admin/api/cards/create", { method: "POST", body });
    btn.disabled = false;
    if (!d.ok) { toast(d.error || "生成失败", "bad"); return; }
    const rows = d.keys.map((k, i) => {
      const link = (d.links && d.links[i]) || (location.origin + "/c/" + k);
      return '<div class="keyrow"><span class="k">' + esc(k) + '</span><span class="lnk">' + esc(link) +
        '</span><button class="btn mini" data-act="copy" data-text="' + esc(link) + '">复制链接</button></div>';
    }).join("");
    const all = (d.links && d.links.length ? d.links : d.keys.map((k) => location.origin + "/c/" + k)).join("\n");
    openModal("已生成 " + d.count + " 张卡密 · " + d.batch,
      '<div class="ok-box">卡密仅在此处显示，请立即保存。每张卡默认只能接一次码。</div>' +
      '<div class="mt12"><textarea id="allLinks" style="width:100%;height:70px" readonly>' + esc(all) + "</textarea>" +
      '<button class="btn mini mt12" data-act="copy-all">复制全部链接</button></div>' +
      '<div class="keylist">' + rows + "</div>");
    loadCards();
  }

  async function cardAction(id, op) {
    if (op === "delete" && !confirm("删除后该卡密链接立即失效，确定删除？")) return;
    if (op === "reset_choice" && !confirm("重置后该卡密可重新选择号段并取号（原号码作废），确定？")) return;
    const d = await api("/admin/api/cards/" + id + "/action", { method: "POST", body: { action: op } });
    toast(d.ok ? "操作成功" : (d.error || "操作失败"), d.ok ? "good" : "bad");
    if (d.ok) loadCards();
  }
  async function pageRelay() {
    const st = S.pageState.relay = S.pageState.relay || { status: "all", q: "", page: 1 };
    let base = "https://api.your-domain.com";
    const cfg = await api("/admin/api/settings");
    if (cfg.ok && cfg.settings && cfg.settings.relay_base) base = cfg.settings.relay_base;
    content.innerHTML =
      '<div class="card"><div class="card-title"><h3>生成号码直链卡</h3><span class="hint">系统自动购买号码 · 可反复取码</span></div>' +
      '<form id="relayAllocForm" class="form-grid">' +
      '<label class="fld">号段<select name="choice">' + opt("us", "美国实卡") + opt("gb", "英国") +
      opt("hk", "香港") + opt("cn_real", "国内实卡") + opt("cn_virtual", "国内虚拟卡") + "</select></label>" +
      '<label class="fld">数量<input name="count" type="number" min="1" max="50" value="1"></label>' +
      '<label class="fld">有效期（天，0 为永久）<input name="expires_days" type="number" min="0" max="3650" value="0"></label>' +
      '<label class="fld">备注<input name="note" maxlength="120" placeholder="渠道 / 批次"></label>' +
      '<div class="full"><button class="btn primary" type="submit">购买并生成直链卡</button>' +
      '<span class="help">会真实向上游购买号码（产生费用），生成 <code>卡号:+手机号----' + esc(base) +
      '/smsrecord?token=…</code>，买家看不到上游域名。</span></div>' +
      "</form></div>" +
      '<div class="card"><div class="card-title"><h3>已有号码导入（可选）</h3><span class="hint">不买号 · 只把上游地址换成自己的域名</span></div>' +
      '<form id="relayForm" class="form-grid">' +
      '<label class="fld full">粘贴上游号码（每行一条，必填）<textarea name="lines" rows="6" placeholder="每行一条，例如：&#10;卡号:+8613800000000----https://你的上游地址/smsrecord?token=你的token"></textarea></label>' +
      '<label class="fld">有效期（天，0 为永久）<input name="expires_days" type="number" min="0" max="3650" value="0"></label>' +
      '<label class="fld">备注<input name="note" maxlength="120" placeholder="渠道 / 批次"></label>' +
      '<div class="full"><button class="btn primary" type="submit">生成直链卡</button>' +
      '<span class="help">生成后统一替换为 <code>' + esc(base) + '/smsrecord?token=…</code>，买家看不到上游域名；同一链接可反复取码。</span></div>' +
      "</form></div>" +
      '<div class="card"><div class="card-title"><h3>从号池转直链</h3><span class="hint">把已购号码变成可重复取码的直链</span></div>' +
      '<form id="relayPoolForm" class="form-grid">' +
      '<label class="fld">数量<input name="count" type="number" min="1" max="200" value="10"></label>' +
      '<label class="fld">地区<select name="country"><option value="">全部</option><option value="us">美国</option><option value="gb">英国</option><option value="hk">香港</option><option value="cn">中国</option></select></label>' +
      '<div class="full"><button class="btn" type="submit">从号池生成</button></div></form></div>' +
      '<div class="card"><div class="card-title"><h3>批次导出</h3><span class="hint">弹窗关掉也不丢 · 任何一批都能重新拿回链接</span></div>' +
      '<div class="toolbar"><select id="relayBatch" style="min-width:340px"></select>' +
      '<button class="btn" data-act="relay-batch-copy">复制这一批</button>' +
      '<button class="btn" data-act="relay-batch-download">下载 txt</button>' +
      '<span class="hint" id="relayBatchHint">加载中…</span></div></div>' +
      '<div class="card"><div class="toolbar">' +
      '<select id="relayStatus">' + opt("all", "全部状态", st.status) + opt("active", "可用") +
      opt("disabled", "已禁用") + opt("expired", "已过期") + "</select>" +
      '<input id="relayQ" placeholder="搜索手机号 / 备注 / token" style="min-width:240px" value="' + esc(st.q) + '">' +
      '<button class="btn" id="relaySearch">搜索</button><div class="spacer"></div>' +
      '<button class="btn mini" data-act="relay-export">导出全部直链</button>' +
      '<span class="hint" id="relayTotal"></span></div><div class="mt12" id="relayTable"></div></div>';
    $("relayStatus").addEventListener("change", () => { st.status = $("relayStatus").value; st.page = 1; loadRelay(); });
    $("relayQ").addEventListener("keydown", (e) => { if (e.key === "Enter") { st.q = $("relayQ").value.trim(); st.page = 1; loadRelay(); } });
    $("relaySearch").addEventListener("click", () => { st.q = $("relayQ").value.trim(); st.page = 1; loadRelay(); });
    $("relayBatch").addEventListener("change", () => loadRelayBatches($("relayBatch").value));
    loadRelay();
    loadRelayBatches();
  }

  async function loadRelayBatches(keep) {
    const sel = $("relayBatch");
    if (!sel) return;
    const d = await api("/admin/api/relay/batches");
    if (!d.ok) { const h = $("relayBatchHint"); if (h) h.textContent = "批次列表加载失败"; return; }
    const items = d.items || [];
    sel.innerHTML = items.length ? items.map((b) =>
      '<option value="' + b.id + '">' + esc(b.batch_no) + " · " + b.live + " 条 · " + fmt(b.created_at) +
      (b.note ? " · " + esc(b.note) : "") + "</option>").join("") : '<option value="">暂无批次</option>';
    const want = keep == null ? sel.value : String(keep);
    if (want && Array.from(sel.options).some((o) => o.value === want)) sel.value = want;
    const cur = items.find((b) => String(b.id) === sel.value);
    const hint = $("relayBatchHint");
    if (hint) hint.textContent = cur ? ("当前这批 " + cur.live + " 条，共 " + items.length + " 批") : "暂无批次";
  }

  async function relayBatchExport(mode) {
    const sel = $("relayBatch");
    const id = sel ? sel.value : "";
    if (!id) { toast("还没有可导出的批次", "bad"); return; }
    const d = await api("/admin/api/relay/export?batch=" + encodeURIComponent(id));
    if (!d.ok) { toast(d.error || "导出失败", "bad"); return; }
    if (!d.count) { toast("这一批的卡已经全部删除了", "bad"); return; }
    if (mode === "copy") copy(d.text || "");
    else { downloadText("relay-batch-" + id + ".txt", d.text || ""); toast("已下载 " + d.count + " 条", "good"); }
  }

  async function loadRelay() {
    const st = S.pageState.relay;
    const box = $("relayTable");
    if (!box) return;
    box.innerHTML = '<div class="loading">加载中…</div>';
    const d = await api("/admin/api/relay?status=" + encodeURIComponent(st.status) + "&q=" +
      encodeURIComponent(st.q) + "&page=" + st.page + "&size=50");
    if (!d.ok) { box.innerHTML = '<div class="empty">' + esc(d.error) + "</div>"; return; }
    $("relayTotal").textContent = "共 " + d.total + " 条";
    const now = d.now || Date.now() / 1000;
    const rows = d.items.map((c) => {
      let cls = "ok", txt = "可用";
      if (c.status === "disabled") { cls = "bad"; txt = "已禁用"; }
      else if (c.expires_at && c.expires_at < now) { cls = "bad"; txt = "已过期"; }
      return "<tr><td class='mono'>#" + c.id + "</td><td class='mono'>" + esc(c.phone || "—") +
        "</td><td class='mono'>" + esc(c.token_prefix || "—") + "</td><td>" + badge(txt, cls) +
        "</td><td class='mono'>" + (c.reads || 0) + "</td><td class='mono'>" +
        (c.last_read_at ? fmt(c.last_read_at) : "—") + "</td><td class='mono'>" +
        (c.last_ip ? esc(c.last_ip) : "—") + "</td><td class='mono'>" + esc(c.upstream_host || "—") +
        "</td><td>" + esc(c.note || "—") + "</td><td class='mono'>" +
        (c.expires_at ? fmt(c.expires_at) : "永久") + "</td><td>" +
        '<button class="btn mini" data-act="copy" data-text="' + esc(c.line) + '">复制</button> ' +
        (c.status === "disabled"
          ? '<button class="btn mini" data-act="relay-action" data-op="enable" data-id="' + c.id + '">启用</button> '
          : '<button class="btn mini" data-act="relay-action" data-op="disable" data-id="' + c.id + '">禁用</button> ') +
        '<button class="btn mini" data-act="relay-action" data-op="reset" data-id="' + c.id + '">重置</button> ' +
        '<button class="btn mini danger" data-act="relay-action" data-op="delete" data-id="' + c.id + '">删除</button></td></tr>';
    }).join("");
    box.innerHTML = '<div class="tbl-wrap"><table><thead><tr><th>ID</th><th>手机号</th><th>Token</th><th>状态</th>' +
      "<th>读取</th><th>最近读取</th><th>最近 IP</th><th>上游</th><th>备注</th><th>有效期</th><th>操作</th></tr></thead><tbody>" +
      (rows || "<tr><td colspan=11 class='empty'>暂无直链卡</td></tr>") + "</tbody></table></div>" + pager(d.total, st.page, 50);
  }

  function relayResult(d) {
    const all = (d.lines || []).join("\n");
    S.lastRelay = { batch: d.batch || "", lines: all, count: d.count || 0 };
    openModal("已生成 " + d.count + " 条直链卡" + (d.batch ? " · " + d.batch : ""),
      '<div class="ok-box">买家拿到的是你自己的域名，上游地址只保存在服务器；同一链接可一直取码，除非禁用或过期。' +
      '<br>关掉这个窗口也不会丢：直链卡页顶部「批次导出」可以随时把这一批重新导出来。</div>' +
      '<div class="mt12"><textarea id="allLinks" style="width:100%;height:150px" readonly>' + esc(all) + "</textarea>" +
      '<button class="btn mini mt12" data-act="copy-all">复制全部（' + (d.count || 0) + " 条）</button> " +
      '<button class="btn mini mt12" id="relayDownload">下载 txt</button> ' +
      '<button class="btn mini mt12" data-act="relay-batch-download">从批次再下载</button></div>' +
      (d.bad && d.bad.length ? '<div class="mt12 hint">跳过 ' + d.bad.length + " 行：" +
        esc(d.bad.map((x) => (x.line ? "第" + x.line + "行 " : (x.phone || "") + " ") + x.why).join("；")) + "</div>" : ""), true);
    const dl = $("relayDownload");
    if (dl) dl.addEventListener("click", () => downloadText("relay-cards.txt", all));
    loadRelayBatches();
  }

  async function allocateRelay() {
    const f = $("relayAllocForm");
    const n = Math.max(1, Math.min(50, Number(f.count.value) || 1));
    const name = ({ us: "美国", gb: "英国", hk: "香港", cn_real: "国内实卡", cn_virtual: "国内虚拟卡" }[f.choice.value]) || f.choice.value;
    if (!confirm("将真实购买 " + n + " 个" + name + "号码并生成直链卡，可能产生费用，确定？")) return;
    const btn = f.querySelector("button[type=submit]");
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "购买中…（最多 1 分钟）";
    const d = await api("/admin/api/relay/allocate", { method: "POST", body: {
      choice: f.choice.value, count: n,
      expires_days: Number(f.expires_days.value), note: f.note.value.trim(),
    } });
    btn.disabled = false;
    btn.textContent = old;
    if (!d.ok) { toast(d.error || "生成失败", "bad"); return; }
    relayResult(d);
    loadRelay();
  }

  async function createRelay() {
    const f = $("relayForm");
    const raw = (f.lines.value || "").trim();
    if (!raw) {
      toast("输入框是空的：请粘贴 卡号:手机号----接码地址（灰色文字只是示例）", "bad");
      f.lines.focus();
      return;
    }
    const btn = f.querySelector("button[type=submit]");
    btn.disabled = true;
    const d = await api("/admin/api/relay/create", { method: "POST", body: {
      lines: raw, note: f.note.value.trim(), expires_days: Number(f.expires_days.value),
    } });
    btn.disabled = false;
    if (!d.ok) { toast(d.error || "生成失败", "bad"); return; }
    relayResult(d);
    loadRelay();
  }

  async function relayFromPool(f) {
    const btn = f.querySelector("button[type=submit]");
    btn.disabled = true;
    const d = await api("/admin/api/relay/from_pool", { method: "POST", body: {
      count: Number(f.count.value), country: f.country.value,
    } });
    btn.disabled = false;
    if (!d.ok) { toast(d.error || "生成失败", "bad"); return; }
    relayResult(d);
    loadRelay();
  }

  async function relayAction(id, op) {
    if (op === "delete" && !confirm("删除后该直链立即失效，确定删除？")) return;
    if (op === "reset" && !confirm("重置后旧 token 立刻失效，买家需要换新链接，确定？")) return;
    const d = await api("/admin/api/relay/" + id + "/action", { method: "POST", body: { action: op } });
    toast(d.ok ? "操作成功" : (d.error || "操作失败"), d.ok ? "good" : "bad");
    if (d.ok) { if (op === "reset" && d.line) copy(d.line); loadRelay(); }
  }

  async function relayExport() {
    const d = await api("/admin/api/relay/export");
    if (!d.ok) { toast(d.error || "导出失败", "bad"); return; }
    downloadText("relay-cards-all.txt", d.text || "");
    toast("已导出 " + d.count + " 条", "good");
  }

  function downloadText(name, text) {
    const blob = new Blob([text || ""], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }

  async function pageNumbers() {
    const st = S.pageState.numbers = S.pageState.numbers || { status: "all", country: "all", q: "", page: 1 };
    content.innerHTML =
      '<div class="card"><div class="card-title"><h3>导入号码</h3><span class="hint">支持 217api / 通用接码 URL</span></div>' +
      '<form id="importForm" class="form-grid">' +
      '<label class="fld">地区<select name="country"><option value="us">美国 +1</option><option value="gb">英国 +44</option><option value="hk">香港 +852</option></select></label>' +
      '<label class="fld">备注<input name="note" maxlength="80" placeholder="批次 / 渠道"></label>' +
      '<label class="fld full">号码与接码地址（每行一条）<textarea name="lines" rows="6" placeholder="+1 2025550123----http://217api.com:9900/api/sms/getcode?token=xxxxxxxx&#10;+44 7700900123|http://example.com/sms?token=yyyy"></textarea></label>' +
      '<div class="full"><button class="btn primary" type="submit">导入号码</button>' +
      '<span class="help">分隔符支持 <code>----</code>、<code>|</code>、逗号或制表符。接码地址按原样轮询，返回 <code>0|0</code> 或 <code>1|123456</code> 均可识别。</span></div>' +
      "</form></div>" +
      '<div class="card"><div class="toolbar">' +
      '<select id="numStatus">' + opt("all", "全部状态", st.status) + opt("available", "可用") + opt("allocated", "已分配") +
      opt("used", "已接码") + opt("expired", "已超时") + opt("disabled", "已禁用") + "</select>" +
      '<select id="numCountry">' + opt("all", "全部地区", st.country) + opt("us", "美国") + opt("gb", "英国") + opt("hk", "香港") + "</select>" +
      '<input id="numQ" placeholder="搜索号码 / 备注" style="min-width:200px" value="' + esc(st.q) + '">' +
      '<button class="btn" id="numSearch">搜索</button><div class="spacer"></div><span class="hint" id="numTotal"></span></div>' +
      '<div class="mt12" id="numTable"></div></div>';

    $("importForm").addEventListener("submit", (e) => { e.preventDefault(); });
    ["numStatus", "numCountry"].forEach((id) => $(id).addEventListener("change", () => {
      st.status = $("numStatus").value; st.country = $("numCountry").value; st.page = 1; loadNumbers();
    }));
    $("numSearch").addEventListener("click", () => { st.q = $("numQ").value.trim(); st.page = 1; loadNumbers(); });
    loadNumbers();
  }

  async function importNumbers() {
    const f = $("importForm");
    const btn = f.querySelector("button[type=submit]");
    btn.disabled = true;
    const d = await api("/admin/api/numbers/import", { method: "POST", body: {
      country: f.country.value, lines: f.lines.value, note: f.note.value.trim() } });
    btn.disabled = false;
    if (!d.ok) { toast(d.error || "导入失败", "bad"); return; }
    toast("导入成功 " + d.added + " 条，跳过 " + d.skipped + " 条", "good");
    f.lines.value = "";
    loadNumbers();
  }

  async function loadNumbers() {
    const st = S.pageState.numbers;
    const box = $("numTable");
    if (!box) return;
    box.innerHTML = '<div class="loading">加载中…</div>';
    const d = await api("/admin/api/numbers?status=" + encodeURIComponent(st.status) + "&country=" + encodeURIComponent(st.country) +
      "&q=" + encodeURIComponent(st.q) + "&page=" + st.page + "&size=50");
    if (!d.ok) { box.innerHTML = '<div class="empty">' + esc(d.error) + "</div>"; return; }
    $("numTotal").textContent = "共 " + d.total + " 条";
    const rows = d.items.map((n) => {
      const map = { available: ["ok", "可用"], allocated: ["info", "已分配"], used: ["warn", "已接码"],
        expired: ["bad", "已超时"], disabled: ["bad", "已禁用"] };
      const m = map[n.status] || ["", n.status];
      return "<tr><td class='mono'>#" + n.id + "</td><td class='mono'>" + esc(n.phone) + "</td><td>" +
        (n.country === "us" ? "美国" : n.country === "gb" ? "英国" : "香港") + "</td><td>" +
        badge(m[1], m[0]) + "</td><td class='mono'>" + esc(n.provider) + "</td><td class='mono'>" +
        (n.card_id ? "#" + n.card_id : "—") + "</td><td class='mono'>" + (n.codes || 0) + "</td><td class='mono'>" +
        (n.allocated_at ? fmt(n.allocated_at) : "—") + "</td><td>" +
        (n.status === "disabled"
          ? '<button class="btn mini" data-act="num-action" data-op="enable" data-id="' + n.id + '">启用</button> '
          : '<button class="btn mini" data-act="num-action" data-op="disable" data-id="' + n.id + '">禁用</button> ') +
        '<button class="btn mini danger" data-act="num-action" data-op="delete" data-id="' + n.id + '">删除</button></td></tr>';
    }).join("");
    box.innerHTML = '<div class="tbl-wrap"><table><thead><tr><th>ID</th><th>号码</th><th>地区</th><th>状态</th><th>号源</th><th>绑定卡密</th><th>接码数</th><th>分配时间</th><th>操作</th></tr></thead><tbody>' +
      (rows || "<tr><td colspan=9 class='empty'>号码池为空，请先导入</td></tr>") + "</tbody></table></div>" + pager(d.total, st.page, 50);
  }

  async function numAction(id, op) {
    if (op === "delete" && !confirm("确定删除该号码？")) return;
    const d = await api("/admin/api/numbers/" + id + "/action", { method: "POST", body: { action: op } });
    toast(d.ok ? "操作成功" : (d.error || "操作失败"), d.ok ? "good" : "bad");
    if (d.ok) loadNumbers();
  }

  async function pageRedemptions() {
    const st = S.pageState.redemptions = S.pageState.redemptions || { result: "all", q: "", page: 1 };
    content.innerHTML = '<div class="card"><div class="toolbar">' +
      '<select id="rdResult">' + opt("all", "全部结果", st.result) + opt("ok", "成功") + opt("fail", "失败") + "</select>" +
      '<input id="rdQ" placeholder="搜索 IP / 卡密前缀" style="min-width:220px" value="' + esc(st.q) + '">' +
      '<button class="btn" id="rdSearch">搜索</button><div class="spacer"></div><span class="hint" id="rdTotal"></span></div>' +
      '<div class="mt12" id="rdTable"></div></div>';
    $("rdResult").addEventListener("change", () => { st.result = $("rdResult").value; st.page = 1; loadRedemptions(); });
    $("rdSearch").addEventListener("click", () => { st.q = $("rdQ").value.trim(); st.page = 1; loadRedemptions(); });
    loadRedemptions();
  }
  async function loadRedemptions() {
    const st = S.pageState.redemptions;
    const box = $("rdTable"); if (!box) return;
    box.innerHTML = '<div class="loading">加载中…</div>';
    const d = await api("/admin/api/redemptions?result=" + encodeURIComponent(st.result) + "&q=" + encodeURIComponent(st.q) + "&page=" + st.page + "&size=50");
    if (!d.ok) { box.innerHTML = '<div class="empty">' + esc(d.error) + "</div>"; return; }
    $("rdTotal").textContent = "共 " + d.total + " 条";
    const rows = d.items.map((r) =>
      "<tr><td class='mono'>#" + r.id + "</td><td class='mono'>" + esc(r.key_prefix || "—") + "…</td><td>" +
      badge(r.result === "ok" ? "成功" : "失败", r.result === "ok" ? "ok" : "bad") + "</td><td class='mono'>" +
      esc(r.ip || "—") + "</td><td class='mono'>" + fmt(r.created_at) + "</td><td class='mono'>" +
      esc((r.detail || "").slice(0, 40)) + "</td><td class='mono small'>" + esc((r.ua || "").slice(0, 48)) + "</td></tr>").join("");
    box.innerHTML = '<div class="tbl-wrap"><table><thead><tr><th>ID</th><th>卡密</th><th>结果</th><th>IP</th><th>时间</th><th>详情</th><th>UA</th></tr></thead><tbody>' +
      (rows || "<tr><td colspan=7 class='empty'>暂无记录</td></tr>") + "</tbody></table></div>" + pager(d.total, st.page, 50);
  }

  async function pageCodes() {
    const st = S.pageState.codes = S.pageState.codes || { status: "all", q: "", page: 1 };
    content.innerHTML = '<div class="card"><div class="toolbar">' +
      '<select id="cdStatus">' + opt("all", "全部状态", st.status) + opt("received", "已收到") + opt("waiting", "等待中") +
      opt("expired", "已超时") + "</select>" +
      '<input id="cdQ" placeholder="搜索号码 / 验证码 / 卡密" style="min-width:240px" value="' + esc(st.q) + '">' +
      '<button class="btn" id="cdSearch">搜索</button><div class="spacer"></div><span class="hint" id="cdTotal"></span></div>' +
      '<div class="mt12" id="cdTable"></div></div>';
    $("cdStatus").addEventListener("change", () => { st.status = $("cdStatus").value; st.page = 1; loadCodes(); });
    $("cdSearch").addEventListener("click", () => { st.q = $("cdQ").value.trim(); st.page = 1; loadCodes(); });
    loadCodes();
  }
  async function loadCodes() {
    const st = S.pageState.codes;
    const box = $("cdTable"); if (!box) return;
    box.innerHTML = '<div class="loading">加载中…</div>';
    const d = await api("/admin/api/codes?status=" + encodeURIComponent(st.status) + "&q=" + encodeURIComponent(st.q) + "&page=" + st.page + "&size=50");
    if (!d.ok) { box.innerHTML = '<div class="empty">' + esc(d.error) + "</div>"; return; }
    $("cdTotal").textContent = "共 " + d.total + " 条";
    const rows = d.items.map((r) => {
      const map = { received: ["ok", "已收到"], waiting: ["info", "等待中"], expired: ["bad", "已超时"] };
      const m = map[r.status] || ["", r.status];
      return "<tr><td class='mono'>#" + r.id + "</td><td class='mono'>" + esc(r.phone) + "</td><td>" +
        (r.country === "us" ? "🇺🇸" : r.country === "gb" ? "🇬🇧" : "🇭🇰") + "</td><td>" + badge(m[1], m[0]) +
        "</td><td class='mono'>" + (r.code ? '<b style="color:#8dffdd;font-size:13px">' + esc(r.code) + "</b> " +
          '<button class="btn mini" data-act="copy" data-text="' + esc(r.code) + '">复制</button>' : "—") +
        "</td><td class='mono'>" + esc(r.key_prefix || "—") + "…</td><td class='mono'>" + (r.polls || 0) +
        "</td><td class='mono'>" + esc(r.provider || "pool") + "</td><td class='mono'>" + fmt(r.created_at) +
        "</td><td class='mono'>" + (r.got_at ? fmt(r.got_at) : "—") + "</td></tr>";
    }).join("");
    box.innerHTML = '<div class="tbl-wrap"><table><thead><tr><th>ID</th><th>号码</th><th>地区</th><th>状态</th><th>验证码</th><th>卡密</th><th>轮询</th><th>号源</th><th>创建</th><th>收到</th></tr></thead><tbody>' +
      (rows || "<tr><td colspan=10 class='empty'>暂无接码记录</td></tr>") + "</tbody></table></div>" + pager(d.total, st.page, 50);
  }
  async function pageUsage() {
    const st = S.pageState.usage = S.pageState.usage || { q: "", page: 1 };
    content.innerHTML = '<div class="card"><div class="toolbar">' +
      '<input id="usQ" placeholder="搜索 IP / 卡密前缀" style="min-width:240px" value="' + esc(st.q) + '">' +
      '<button class="btn" id="usSearch">搜索</button><div class="spacer"></div><span class="hint" id="usTotal"></span></div>' +
      '<div class="mt12" id="usTable"></div></div>';
    $("usSearch").addEventListener("click", () => { st.q = $("usQ").value.trim(); st.page = 1; loadUsage(); });
    loadUsage();
  }
  async function loadUsage() {
    const st = S.pageState.usage;
    const box = $("usTable"); if (!box) return;
    box.innerHTML = '<div class="loading">加载中…</div>';
    const d = await api("/admin/api/usage?q=" + encodeURIComponent(st.q) + "&page=" + st.page + "&size=50");
    if (!d.ok) { box.innerHTML = '<div class="empty">' + esc(d.error) + "</div>"; return; }
    $("usTotal").textContent = "共 " + d.total + " 条";
    const now = d.now || Date.now() / 1000;
    const rows = d.items.map((r) =>
      "<tr><td class='mono'>" + esc(r.key_prefix) + "…</td><td class='mono'>" + esc(r.phone || "—") +
      "</td><td>" + (r.used_codes >= r.max_codes ? badge("已核销", "warn") : badge("进行中", "ok")) +
      "</td><td class='mono'>" + esc(r.ip || "—") + "</td><td class='mono'>" + fmt(r.created_at) +
      "</td><td class='mono'>" + fmt(r.last_seen) + "</td><td class='mono'>" +
      (r.expires_at > now ? Math.ceil((r.expires_at - now) / 60) + " 分钟" : "已过期") +
      "</td><td class='mono small'>" + esc((r.ua || "").slice(0, 46)) + "</td></tr>").join("");
    box.innerHTML = '<div class="tbl-wrap"><table><thead><tr><th>卡密</th><th>号码</th><th>状态</th><th>IP</th><th>会话建立</th><th>最后活动</th><th>剩余</th><th>UA</th></tr></thead><tbody>' +
      (rows || "<tr><td colspan=8 class='empty'>暂无会话</td></tr>") + "</tbody></table></div>" + pager(d.total, st.page, 50);
  }

  async function pageAudit() {
    const st = S.pageState.audit = S.pageState.audit || { level: "all", kind: "", q: "", page: 1 };
    content.innerHTML = '<div class="card"><div class="toolbar">' +
      '<select id="auLevel">' + opt("all", "全部级别", st.level) + opt("info", "信息") + opt("warn", "警告") +
      opt("alert", "重要") + opt("error", "异常") + "</select>" +
      '<input id="auKind" placeholder="事件类型，如 login" style="width:170px" value="' + esc(st.kind) + '">' +
      '<input id="auQ" placeholder="搜索 IP / 操作者 / 详情" style="min-width:220px" value="' + esc(st.q) + '">' +
      '<button class="btn" id="auSearch">筛选</button><div class="spacer"></div><span class="hint" id="auTotal"></span></div>' +
      '<div class="mt12" id="auTable"></div></div>';
    $("auLevel").addEventListener("change", () => { st.level = $("auLevel").value; st.page = 1; loadAudit(); });
    $("auSearch").addEventListener("click", () => {
      st.kind = $("auKind").value.trim(); st.q = $("auQ").value.trim(); st.page = 1; loadAudit();
    });
    loadAudit();
  }
  async function loadAudit() {
    const st = S.pageState.audit;
    const box = $("auTable"); if (!box) return;
    box.innerHTML = '<div class="loading">加载中…</div>';
    const d = await api("/admin/api/audit?level=" + encodeURIComponent(st.level) + "&kind=" + encodeURIComponent(st.kind) +
      "&q=" + encodeURIComponent(st.q) + "&page=" + st.page + "&size=60");
    if (!d.ok) { box.innerHTML = '<div class="empty">' + esc(d.error) + "</div>"; return; }
    $("auTotal").textContent = "共 " + d.total + " 条";
    const rows = d.items.map((e) =>
      "<tr><td class='mono'>" + fmt(e.ts) + "</td><td>" + badge(e.level, e.level === "alert" ? "violet" : e.level === "warn" ? "warn" : e.level === "error" ? "bad" : "info") +
      "</td><td>" + esc(KIND[e.kind] || e.kind) + "</td><td class='mono'>" + esc(e.ip || "—") + "</td><td class='mono'>" +
      esc(e.actor || "—") + "</td><td class='mono'>" + (e.card_id ? "#" + e.card_id : "—") + "</td><td class='mono small'>" +
      esc((e.detail || "").slice(0, 110)) + "</td></tr>").join("");
    box.innerHTML = '<div class="tbl-wrap"><table><thead><tr><th>时间</th><th>级别</th><th>事件</th><th>IP</th><th>操作者</th><th>卡密</th><th>详情</th></tr></thead><tbody>' +
      (rows || "<tr><td colspan=7 class='empty'>暂无事件</td></tr>") + "</tbody></table></div>" + pager(d.total, st.page, 60);
  }

  async function pageIps() {
    const st = S.pageState.ips = S.pageState.ips || { state: "all", q: "", page: 1 };
    content.innerHTML =
      '<div class="card"><div class="card-title"><h3>手动封禁</h3><span class="hint">支持单个 IP 或 CIDR 段</span></div>' +
      '<form id="blockForm" class="toolbar">' +
      '<input name="ip" placeholder="<SERVER_IP> 或 <SERVER_IP>/24" style="min-width:210px">' +
      '<input name="minutes" type="number" min="1" value="60" style="width:100px" title="封禁分钟数">' +
      '<input name="reason" placeholder="原因" style="min-width:180px">' +
      '<button class="btn danger" type="submit">封禁</button><div class="spacer"></div>' +
      '<span class="help">自动风控：登录失败 5 次 / 10 分钟 → 封禁 30 分钟。</span></form></div>' +
      '<div class="card"><div class="toolbar">' +
      '<select id="ipState">' + opt("all", "全部 IP", st.state) + opt("blocked", "已封禁") + opt("clean", "正常") + "</select>" +
      '<input id="ipQ" placeholder="搜索 IP" style="min-width:190px" value="' + esc(st.q) + '">' +
      '<button class="btn" id="ipSearch">搜索</button><div class="spacer"></div><span class="hint" id="ipTotal"></span></div>' +
      '<div class="mt12" id="ipTable"></div></div>';
    $("ipState").addEventListener("change", () => { st.state = $("ipState").value; st.page = 1; loadIps(); });
    $("ipSearch").addEventListener("click", () => { st.q = $("ipQ").value.trim(); st.page = 1; loadIps(); });
    loadIps();
  }
  async function loadIps() {
    const st = S.pageState.ips;
    const box = $("ipTable"); if (!box) return;
    box.innerHTML = '<div class="loading">加载中…</div>';
    const d = await api("/admin/api/ips?state=" + encodeURIComponent(st.state) + "&q=" + encodeURIComponent(st.q) + "&page=" + st.page + "&size=60");
    if (!d.ok) { box.innerHTML = '<div class="empty">' + esc(d.error) + "</div>"; return; }
    $("ipTotal").textContent = "共 " + d.total + " 个 IP";
    const rows = d.items.map((i) =>
      "<tr><td class='mono'>" + esc(i.ip) + "</td><td>" + (i.blocked ? badge("封禁中", "bad") : badge("正常", "ok")) +
      "</td><td class='mono'>" + i.req_count + "</td><td class='mono'>" + i.fail_count + "</td><td class='mono'>" + fmt(i.last_seen) +
      "</td><td class='mono'>" + (i.blocked ? Math.ceil(i.blocked_for / 60) + " 分钟" : "—") + "</td><td class='mono'>" +
      esc(i.block_reason || "—") + "</td><td>" +
      (i.blocked ? '<button class="btn mini" data-act="unblock-ip" data-ip="' + esc(i.ip) + '">解封</button>'
        : '<button class="btn mini danger" data-act="block-ip" data-ip="' + esc(i.ip) + '">封禁</button>') + "</td></tr>").join("");
    box.innerHTML = '<div class="tbl-wrap"><table><thead><tr><th>IP</th><th>状态</th><th>请求</th><th>失败</th><th>最近访问</th><th>剩余</th><th>原因</th><th>操作</th></tr></thead><tbody>' +
      (rows || "<tr><td colspan=8 class='empty'>暂无 IP 记录</td></tr>") + "</tbody></table></div>" + pager(d.total, st.page, 60);
  }
  function blockIp(ip) {
    openModal("封禁 IP", '<div class="kv"><span class="k">目标</span><span class="v">' + esc(ip) + "</span></div>" +
      '<div class="form-grid mt12"><label class="fld">时长（分钟）<input id="biMin" type="number" min="1" value="60"></label>' +
      '<label class="fld">原因<input id="biReason" value="manual"></label></div>' +
      '<button class="btn danger mt12" id="biGo">确认封禁</button>');
    $("biGo").addEventListener("click", async () => {
      const d = await api("/admin/api/ips/action", { method: "POST", body: {
        ip, action: "block", minutes: Number($("biMin").value), reason: $("biReason").value } });
      toast(d.ok ? "已封禁 " + ip : (d.error || "失败"), d.ok ? "good" : "bad");
      if (d.ok) { closeModal(); loadIps(); }
    });
  }
  async function blockIpSubmit() {
    const f = $("blockForm");
    const d = await api("/admin/api/ips/action", { method: "POST", body: {
      ip: f.ip.value.trim(), action: "block", minutes: Number(f.minutes.value), reason: f.reason.value.trim() } });
    toast(d.ok ? "已封禁" : (d.error || "失败"), d.ok ? "good" : "bad");
    if (d.ok) { f.reset(); loadIps(); }
  }
  async function ipAction(ip, action) {
    const d = await api("/admin/api/ips/action", { method: "POST", body: { ip, action } });
    toast(d.ok ? "操作成功" : (d.error || "失败"), d.ok ? "good" : "bad");
    if (d.ok) loadIps();
  }

  async function pageSettings() {
    content.innerHTML = '<div class="loading">加载中…</div>';
    const d = await api("/admin/api/settings");
    if (!d.ok) { content.innerHTML = '<div class="empty">' + esc(d.error) + "</div>"; return; }
    const s = d.settings;
    const sel = (v, cur) => (v === cur ? " selected" : "");
    content.innerHTML =
      '<form id="settingsForm" class="card">' +
      '<div class="card-title"><h3>站点信息</h3><span class="hint">展示在前台与卡密链接中</span></div>' +
      '<div class="form-grid">' +
      '<label class="fld">站点名称<input name="site_name" value="' + esc(s.site_name) + '"></label>' +
      '<label class="fld">站点地址（生成卡密链接用）<input name="site_url" value="' + esc(s.site_url || "") + '" placeholder="https://your-domain.com"></label>' +
      '<label class="fld">购买号码链接（官网按钮跳转）<input name="shop_url" value="' + esc(s.shop_url || "https://wzyp.cn/shop/63EZ1CJK") + '" placeholder="https://wzyp.cn/shop/..."></label>' +
      '<label class="fld">直链卡域名（生成 卡号:手机号----接码地址 用）<input name="relay_base" value="' + esc(s.relay_base || "https://api.your-domain.com") + '" placeholder="https://api.your-domain.com"></label>' +
      '<label class="fld">直链每 token 每分钟上限<input name="relay_rate" type="number" min="1" max="6000" value="' + esc(s.relay_rate || "60") + '"></label>' +
      '<label class="fld">直链每 IP 每分钟上限<input name="relay_ip_rate" type="number" min="1" max="20000" value="' + esc(s.relay_ip_rate || "600") + '"></label>' +
      '<label class="fld">无效 token 每 IP 每分钟上限<input name="relay_bad_rate" type="number" min="1" max="20000" value="' + esc(s.relay_bad_rate || "300") + '"></label>' +
      '<label class="fld full">前台提示文案<input name="site_note" value="' + esc(s.site_note) + '"></label>' +
      "</div>" +
      '<div class="card-title" style="margin-top:20px"><h3>号源配置</h3><span class="hint">决定用户取号来源</span></div>' +
      '<div class="form-grid">' +
      '<label class="fld">号源类型<select name="provider">' +
      '<option value="pool"' + sel("pool", s.provider) + ">本地号池（217api / 通用 URL）</option>" +
      '<option value="sms_activate"' + sel("sms_activate", s.provider) + ">SMS-Activate（按需购号）</option>" +
      '<option value="fivesim"' + sel("fivesim", s.provider) + ">5SIM（按需购号）</option>" +
      '<option value="nexsms_premium"' + sel("nexsms_premium", s.provider) + ">NexSMS 优选（豆包 / 按需购号）</option>" +
      "</select></label>" +
      '<label class="fld">开放地区（国际）<select name="countries" multiple size="3">' +
      ["us", "gb", "hk"].map((c) => '<option value="' + c + '"' +
        ((s.allow_countries || "").split(",").includes(c) ? " selected" : "") + ">" +
        (c === "us" ? "美国 +1" : c === "gb" ? "英国 +44" : "香港 +852") + "</option>").join("") +
      "</select></label>" +
      '<label class="fld">开放号段（前台可选，选定即锁）<select name="choices" multiple size="5">' +
      [["us", "美国 +1"], ["gb", "英国 +44"], ["hk", "香港 +852"],
       ["cn_real", "国内 · 实卡"], ["cn_virtual", "国内 · 虚卡"]].map((x) => '<option value="' + x[0] + '"' +
        ((s.allow_choices || "").split(",").includes(x[0]) ? " selected" : "") + ">" + x[1] + "</option>").join("") +
      "</select></label>" +
      '<label class="fld">短信轮询间隔（秒）<input name="poll_interval" type="number" min="5" max="120" value="' + esc(s.poll_interval) + '"></label>' +
      '<label class="fld">号码有效期（分钟）<input name="number_ttl_min" type="number" min="2" max="180" value="' + esc(s.number_ttl_min) + '"></label>' +
      '<label class="fld">每卡最多换号次数（含首次，建议 3）<input name="max_alloc_attempts" type="number" min="1" max="10" value="' + esc(s.max_alloc_attempts || "3") + '"></label>' +
      '<label class="fld">自动封禁<select name="auto_ban_enabled"><option value="1"' + (s.auto_ban_enabled === "1" ? " selected" : "") + ">开启</option><option value=\"0\"" + (s.auto_ban_enabled === "0" ? " selected" : "") + ">关闭</option></select></label>" +
      '<label class="fld">核销后退出倒计时（秒，0=立即）<input name="exit_grace_sec" type="number" min="0" max="60" value="' + esc(s.exit_grace_sec || "8") + '"></label>' +
      '<label class="fld full">后台登录 IP 白名单（留空=不限制，逗号分隔，支持 CIDR）<input name="admin_ip_allow" value="' + esc(s.admin_ip_allow || "") + '" placeholder="例如 <SERVER_IP>,10.0.0.0/8"></label>' +
      "</div>" +
      '<div class="card-title" style="margin-top:20px"><h3>SMS-Activate</h3><span class="hint">留空表示不修改</span></div>' +
      '<div class="form-grid">' +
      '<label class="fld">API Key<input name="sms_activate_key" type="password" placeholder="' + (s.sms_activate_key_set ? "已设置：" + esc(s.sms_activate_key_hint) : "未设置") + '"></label>' +
      '<label class="fld">服务代码（go / wa / tg…）<input name="sms_activate_service" value="' + esc(s.sms_activate_service) + '"></label>' +
      "</div>" +
      '<div class="card-title" style="margin-top:20px"><h3>5SIM</h3><span class="hint">留空表示不修改</span></div>' +
      '<div class="form-grid">' +
      '<label class="fld">API Key<input name="fivesim_key" type="password" placeholder="' + (s.fivesim_key_set ? "已设置：" + esc(s.fivesim_key_hint) : "未设置") + '"></label>' +
      '<label class="fld">产品（any / google…）<input name="fivesim_product" value="' + esc(s.fivesim_product) + '"></label>' +
      '<label class="fld">运营商<input name="fivesim_operator" value="' + esc(s.fivesim_operator) + '"></label>' +
      "</div>" +
      '<div class="card-title" style="margin-top:20px"><h3>NexSMS 优选（豆包）</h3><span class="hint">从「优选号码」找项目，只接收指定前缀的短信</span></div>' +
      '<div class="form-grid">' +
      '<label class="fld">API Key<input name="nexsms_key" type="password" placeholder="' + (s.nexsms_key_set ? "已设置：" + esc(s.nexsms_key_hint) : "未设置") + '"></label>' +
      '<label class="fld">API 地址<input name="nexsms_base" value="' + esc(s.nexsms_base || "https://api.nexsms.net") + '"></label>' +
      '<label class="fld">项目关键词<input name="nexsms_project" value="' + esc(s.nexsms_project || "豆包") + '" placeholder="豆包"></label>' +
      '<label class="fld">短信前缀（只收以此开头的）<input name="nexsms_sms_prefix" value="' + esc(s.nexsms_sms_prefix || "[豆包]") + '" placeholder="[豆包]"></label>' +
      '<label class="fld">卡类型<select name="nexsms_type">' + opt("1", "1 · 首次卡", s.nexsms_type) + opt("2", "2 · 重启卡", s.nexsms_type) + opt("3", "3 · 续费卡", s.nexsms_type) + "</select></label>" +
      '<label class="fld">有效期档位<select name="nexsms_expiry">' + [["0","0 · 随机 / 默认"],["1","1 · 5-30 天"],["2","2 · 30-45 天"],["3","3 · 45-60 天"],["4","4 · 60-80 天"],["5","5 · 80-90 天"],["6","6 · 90 天以上"]].map(function (x) { return opt(x[0], x[1], s.nexsms_expiry); }).join("") + "</select></label>" +
      '<label class="fld">号码前缀（可空）<input name="nexsms_prefix" value="' + esc(s.nexsms_prefix || "") + '"></label>' +
      '<label class="fld">手动 appId（可空）<input name="nexsms_app_id" value="' + esc(s.nexsms_app_id || "") + '" placeholder="留空自动搜索项目"></label>' +
      '<label class="fld">批量购买数量（每个号池批次）<input name="nexsms_batch_qty" type="number" min="1" max="20" value="' + esc(s.nexsms_batch_qty || "5") + '"></label>' +
      '<label class="fld">目标库存（每地区预购）<input name="nexsms_stock_target" type="number" min="0" max="200" value="' + esc(s.nexsms_stock_target || "5") + '"></label>' +
      '<div class="full help">已改为直接取号：库存不足时自动批量补货，用户不再进入排队；补货失败会立即返回提示。</div>' +
      '<label class="fld">限流冷却基数（秒）<input name="nexsms_cooldown_sec" type="number" min="5" max="300" value="' + esc(s.nexsms_cooldown_sec || "30") + '"></label>' +
      '<label class="fld">上游代理（可选）<input name="nexsms_proxy" type="password" placeholder="' + (s.nexsms_proxy_set ? "已设置：" + esc(s.nexsms_proxy_hint) : "留空直连，如 http://user:pass@host:3000") + '"></label>' +
      "</div>" +
      '<div class="card-title" style="margin-top:20px"><h3>d1jiema 国内接码</h3><span class="hint">实卡 / 虚卡，按需购号，只收关键词短信</span></div>' +
      '<div class="form-grid">' +
      '<label class="fld">API Token<input name="d1jiema_token" type="password" placeholder="' + (s.d1jiema_token_set ? "已设置：" + esc(s.d1jiema_token_hint) : "未设置") + '"></label>' +
      '<label class="fld">API 地址<input name="d1jiema_base" value="' + esc(s.d1jiema_base || "https://api.d1jiema.com/zc/data.php") + '"></label>' +
      '<label class="fld">短信关键词<input name="d1jiema_keyword" value="' + esc(s.d1jiema_keyword || "豆包") + '" placeholder="豆包"></label>' +
      '<label class="fld">短信前缀（可空）<input name="d1jiema_sms_prefix" value="' + esc(s.d1jiema_sms_prefix || "[豆包]") + '" placeholder="[豆包]"></label>' +
      '<label class="fld">通道开关<select name="d1jiema_enabled">' + opt("1", "开启", s.d1jiema_enabled) + opt("0", "关闭", s.d1jiema_enabled) + "</select></label>" +
      '<label class="fld">请求间隔（毫秒）<input name="d1jiema_gap_ms" type="number" min="120" max="5000" value="' + esc(s.d1jiema_gap_ms || "300") + '"></label>' +
      "</div>" +
      '<div class="help" id="queueStats" style="margin-top:10px">号源状态读取中…</div>' +
      '<div class="mt18" style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">' +
      '<button class="btn primary" type="submit">保存设置</button>' +
      '<button class="btn" type="button" data-act="test-provider">测试号源连通性</button>' +
      '<button class="btn" type="button" data-act="test-d1jiema">测试 d1jiema</button>' +
      '<button class="btn" type="button" data-act="refill">立即补货</button>' +
      '<button class="btn ghost" type="button" data-act="cooldown-clear">解除限流冷却</button>' +
      '<button class="btn ghost" type="button" data-act="queue-stats">刷新号源状态</button>' +
      '<span class="help">NexSMS 优选只在短信内容以设定前缀（默认 [豆包]）开头时才返回验证码。</span></div>' +
      "</form>";
    $("settingsForm").addEventListener("submit", (e) => { e.preventDefault(); });
    refreshQueueStats();
  }

  async function saveSettings() {
    const f = $("settingsForm");
    const countries = [...f.countries.selectedOptions].map((o) => o.value).join(",");
    const choiceList = [...f.choices.selectedOptions].map((o) => o.value).join(",");
    const body = {
      site_name: f.site_name.value, site_url: f.site_url.value, site_note: f.site_note.value,
      shop_url: f.shop_url.value, relay_base: f.relay_base.value,
      relay_rate: f.relay_rate.value, relay_ip_rate: f.relay_ip_rate.value,
      relay_bad_rate: f.relay_bad_rate.value,
      provider: f.provider.value, allow_countries: countries || "us,gb",
      allow_choices: choiceList || "us,gb,hk",
      poll_interval: f.poll_interval.value, number_ttl_min: f.number_ttl_min.value,
      max_alloc_attempts: f.max_alloc_attempts.value, auto_ban_enabled: f.auto_ban_enabled.value,
      exit_grace_sec: f.exit_grace_sec.value, admin_ip_allow: f.admin_ip_allow.value,
      sms_activate_key: f.sms_activate_key.value, sms_activate_service: f.sms_activate_service.value,
      fivesim_key: f.fivesim_key.value, fivesim_product: f.fivesim_product.value,
      fivesim_operator: f.fivesim_operator.value,
      nexsms_key: f.nexsms_key.value, nexsms_base: f.nexsms_base.value,
      nexsms_project: f.nexsms_project.value, nexsms_sms_prefix: f.nexsms_sms_prefix.value,
      nexsms_type: f.nexsms_type.value, nexsms_expiry: f.nexsms_expiry.value,
      nexsms_prefix: f.nexsms_prefix.value, nexsms_app_id: f.nexsms_app_id.value,
      nexsms_batch_qty: f.nexsms_batch_qty.value, nexsms_stock_target: f.nexsms_stock_target.value,
      nexsms_cooldown_sec: f.nexsms_cooldown_sec.value, nexsms_proxy: f.nexsms_proxy.value,
      d1jiema_token: f.d1jiema_token.value, d1jiema_base: f.d1jiema_base.value,
      d1jiema_keyword: f.d1jiema_keyword.value, d1jiema_sms_prefix: f.d1jiema_sms_prefix.value,
      d1jiema_enabled: f.d1jiema_enabled.value, d1jiema_gap_ms: f.d1jiema_gap_ms.value,
    };
    const btn = f.querySelector("button[type=submit]");
    btn.disabled = true;
    const d = await api("/admin/api/settings", { method: "POST", body });
    btn.disabled = false;
    toast(d.ok ? "设置已保存" : (d.error || "保存失败"), d.ok ? "good" : "bad");
  }

  async function refreshQueueStats() {
    const el = $("queueStats");
    if (!el) return;
    const d = await api("/admin/api/queue/stats");
    if (!d.ok) { el.textContent = "号源状态读取失败"; return; }
    const streak = Number(d.streak || 0);
    const block = Number(d.supply_block || 0);
    el.textContent = "直接取号 · 无排队 · " +
      (block > 0 ? "补货暂停 " + block + " 秒（" + (d.supply_msg || "上游限制") + "）" : "号源通道正常") +
      " · 冷却 " + (d.cooldown || 0) + " 秒" +
      (streak > 1 ? "（连续限流 x" + streak + "）" : "");
  }

  async function refillPool() {
    toast("正在补货，请稍候…", "");
    const d = await api("/admin/api/nexsms/refill", { method: "POST", body: {} });
    toast(d.ok ? "补货完成" : (d.error || "补货失败"), d.ok ? "good" : "bad");
    refreshQueueStats();
  }

  async function clearCooldown() {
    const d = await api("/admin/api/queue/cooldown", { method: "POST", body: { action: "clear" } });
    toast(d.ok ? "已解除限流冷却" : (d.error || "操作失败"), d.ok ? "good" : "bad");
    refreshQueueStats();
  }

  async function testProvider(prov) {
    const d = await api("/admin/api/provider/test", {
      method: "POST", body: prov ? { provider: prov } : {}
    });
    toast((d.ok ? "连通正常：" : "测试失败：") + (d.message || d.error || ""), d.ok ? "good" : "bad");
  }

  function opt(v, label, cur) {
    return '<option value="' + v + '"' + (String(cur) === v ? " selected" : "") + ">" + label + "</option>";
  }
  function pager(total, page, size) {
    const pages = Math.max(1, Math.ceil(total / size));
    if (pages <= 1) return "";
    return '<div class="pager"><span>第 <b>' + page + "</b> / " + pages + " 页</span>" +
      '<button class="btn mini" data-act="page" data-page="' + Math.max(1, page - 1) + '"' + (page <= 1 ? " disabled" : "") + ">上一页</button>" +
      '<button class="btn mini" data-act="page" data-page="' + Math.min(pages, page + 1) + '"' + (page >= pages ? " disabled" : "") + ">下一页</button></div>";
  }
  function goPage(p) {
    const st = S.pageState[S.route];
    if (!st) return;
    st.page = Number(p) || 1;
    if (S.route === "cards") loadCards();
    else if (S.route === "relay") loadRelay();
    else if (S.route === "numbers") loadNumbers();
    else if (S.route === "redemptions") loadRedemptions();
    else if (S.route === "codes") loadCodes();
    else if (S.route === "usage") loadUsage();
    else if (S.route === "audit") loadAudit();
    else if (S.route === "ips") loadIps();
  }

  boot();
})();