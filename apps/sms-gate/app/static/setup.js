(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let csrf = "";
  let mode = "new";

  const params = new URLSearchParams(location.search);
  const t = params.get("t") || params.get("token") || "";
  if (t) $("token").value = t;

  function showErr(id, msg) {
    const el = $(id);
    el.textContent = msg;
    el.classList.toggle("hidden", !msg);
  }

  const boxes = [...$("totpBoxes").querySelectorAll("input")];
  boxes.forEach((box, i) => {
    box.addEventListener("input", () => {
      box.value = box.value.replace(/\D/g, "").slice(0, 1);
      if (box.value && i < boxes.length - 1) boxes[i + 1].focus();
    });
    box.addEventListener("keydown", (e) => {
      if (e.key === "Backspace" && !box.value && i > 0) boxes[i - 1].focus();
    });
    box.addEventListener("paste", (e) => {
      e.preventDefault();
      const txt = (e.clipboardData.getData("text") || "").replace(/\D/g, "").slice(0, 6);
      txt.split("").forEach((ch, k) => { if (boxes[k]) boxes[k].value = ch; });
      boxes[Math.min(5, txt.length)].focus();
    });
  });

  function showQr(r) {
    csrf = r.csrf || csrf;
    $("qrWrap").innerHTML = r.qr_svg || "";
    const m = /secret=([A-Z2-7]+)/.exec(r.otpauth || "");
    $("secretText").textContent = m ? m[1] : "—";
    $("setup1").classList.add("hidden");
    $("setup2").classList.remove("hidden");
    boxes[0].focus();
  }

  function setResumeMode() {
    mode = "resume";
    $("setupTitle").textContent = "继续完成初始化";
    $("setupSub").innerHTML =
      "管理员已创建，但谷歌验证器还没有绑定。输入初始化令牌和刚才设置的账号密码，继续扫码绑定。";
    $("btnSetup").textContent = "继续绑定验证器";
  }

  async function boot() {
    try {
      const b = await fetch("/admin/api/bootstrap", { credentials: "same-origin" })
        .then((x) => x.json());
      if (!b || !b.ok) return;
      if (b.logged_in) { location.href = "/admin"; return; }
      if (!b.needs_setup && !b.setup_pending) { location.href = "/admin/login"; return; }
      if (b.setup_pending && b.stage === "setup") {
        const p = await fetch("/admin/api/setup/pending", { credentials: "same-origin" })
          .then((x) => x.json());
        if (p && p.ok) { mode = "resume"; showQr(p); return; }
      }
      if (b.setup_pending) setResumeMode();
    } catch (e) {
      /* 网络异常时保留默认的新建表单 */
    }
  }

  $("setupForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    showErr("err1", "");
    const f = e.target;
    if (f.password.value !== f.password2.value) { showErr("err1", "两次输入的密码不一致"); return; }
    const btn = $("btnSetup");
    const oldText = btn.textContent;
    btn.disabled = true; btn.textContent = mode === "resume" ? "验证中…" : "创建中…";
    const url = mode === "resume" ? "/admin/api/setup/resume" : "/admin/api/setup";
    const r = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token: f.token.value.trim(),
        username: f.username.value.trim(),
        password: f.password.value,
      }),
    }).then((x) => x.json()).catch(() => ({ ok: false, error: "网络异常" }));
    btn.disabled = false; btn.textContent = oldText;
    if (!r.ok) { showErr("err1", r.error || "操作失败"); return; }
    showQr(r);
  });

  $("verifyForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    showErr("err2", "");
    const code = boxes.map((b) => b.value).join("");
    if (code.length !== 6) { showErr("err2", "请输入 6 位验证码"); return; }
    const btn = $("btnVerify");
    btn.disabled = true; btn.textContent = "校验中…";
    const r = await fetch("/admin/api/setup/verify", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF": csrf },
      body: JSON.stringify({ code }),
    }).then((x) => x.json()).catch(() => ({ ok: false, error: "网络异常" }));
    btn.disabled = false; btn.textContent = "完成绑定并登录";
    if (!r.ok) {
      showErr("err2", r.error || "验证失败");
      boxes.forEach((b) => { b.value = ""; });
      boxes[0].focus();
      return;
    }
    location.href = "/admin";
  });

  boot();
})();