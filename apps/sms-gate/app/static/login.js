(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);

  function showErr(id, msg) {
    const el = $(id);
    el.textContent = msg;
    el.classList.toggle("hidden", !msg);
  }

  function readBoxes() {
    return [...$("totpBoxes").querySelectorAll("input")].map((i) => i.value).join("");
  }

  const boxes = [...$("totpBoxes").querySelectorAll("input")];
  boxes.forEach((box, i) => {
    box.addEventListener("input", () => {
      box.value = box.value.replace(/\D/g, "").slice(0, 1);
      if (box.value && i < boxes.length - 1) boxes[i + 1].focus();
      if (readBoxes().length === 6) $("totpForm").requestSubmit();
    });
    box.addEventListener("keydown", (e) => {
      if (e.key === "Backspace" && !box.value && i > 0) boxes[i - 1].focus();
      if (e.key === "ArrowLeft" && i > 0) boxes[i - 1].focus();
      if (e.key === "ArrowRight" && i < boxes.length - 1) boxes[i + 1].focus();
    });
    box.addEventListener("paste", (e) => {
      e.preventDefault();
      const txt = (e.clipboardData.getData("text") || "").replace(/\D/g, "").slice(0, 6);
      txt.split("").forEach((ch, k) => { if (boxes[k]) boxes[k].value = ch; });
      boxes[Math.min(5, txt.length)].focus();
    });
  });

  $("totpForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    showErr("err2", "");
    const code = readBoxes();
    if (code.length !== 6) { showErr("err2", "请输入 6 位动态验证码"); return; }
    const btn = $("btnTotp");
    btn.disabled = true; btn.textContent = "校验中…";
    const r = await fetch("/admin/api/login/code", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    }).then((x) => x.json()).catch(() => ({ ok: false, error: "网络异常" }));
    btn.disabled = false; btn.textContent = "进入后台";
    if (!r.ok) {
      showErr("err2", r.error || "验证失败");
      boxes.forEach((b) => { b.value = ""; });
      boxes[0].focus();
      return;
    }
    location.href = "/admin";
  });

  fetch("/admin/api/bootstrap", { credentials: "same-origin" })
    .then((x) => x.json())
    .then((b) => {
      if (b && b.ok && b.setup_pending) $("setupHint").classList.remove("hidden");
    })
    .catch(() => {});

  // Already signed in? Skip the login screen.
  fetch("/admin/api/me", { credentials: "same-origin" })
    .then((x) => (x.ok ? x.json() : null))
    .then((me) => { if (me && me.ok) location.href = "/admin"; })
    .catch(() => {});

  setTimeout(() => { if (boxes[0]) boxes[0].focus(); }, 50);
})();
