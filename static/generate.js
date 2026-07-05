/* Generate page: source tabs (Spotify link / upload), drag-and-drop file
   picker, and a progress overlay with a step list. */
(() => {
  const form = document.getElementById("gen");
  const tabs = [...document.querySelectorAll(".tab")];
  const srcs = [...document.querySelectorAll(".src")];
  const spotify = document.getElementById("spotify");
  const drop = document.getElementById("drop");
  const input = document.getElementById("audio");
  const text = document.getElementById("drop-text");
  const ov = document.getElementById("ov");
  const steps = [...document.querySelectorAll("#steps .step")];
  const elapsed = document.getElementById("elapsed");
  const modal = document.getElementById("logmodal");
  const logBody = document.getElementById("log-body");

  let timer = null;

  const closeModal = () => (modal.hidden = true);
  document.getElementById("log-close").addEventListener("click", closeModal);
  document.getElementById("log-dismiss").addEventListener("click", closeModal);
  modal.addEventListener("click", e => { if (e.target === modal) closeModal(); });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !modal.hidden) closeModal();
  });

  const showLog = msg => {
    if (timer) { clearInterval(timer); timer = null; }
    ov.hidden = true;
    logBody.textContent = (msg || "The server didn't return a reason.").trim();
    modal.hidden = false;
  };

  let src = "spotify";
  const selectTab = s => {
    src = s;
    tabs.forEach(t => t.classList.toggle("on", t.dataset.src === s));
    srcs.forEach(el => (el.hidden = el.dataset.src !== s));
  };
  tabs.forEach(t => t.addEventListener("click", () => selectTab(t.dataset.src)));

  /* ---- file picker ---- */
  const showFile = () => {
    const f = input.files && input.files[0];
    drop.classList.toggle("has-file", !!f);
    text.innerHTML = f
      ? `<b>${f.name.replace(/</g, "&lt;")}</b> · ${(f.size / 1048576).toFixed(1)} MB`
      : "<b>Choose a song</b> or drop it here";
  };
  input.addEventListener("change", showFile);
  ["dragenter", "dragover"].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", e => {
    if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; showFile(); }
  });

  /* ---- submit: fetch so the log can pop up instead of navigating away ---- */
  form.addEventListener("submit", e => {
    e.preventDefault();
    const hasLink = spotify.value.trim().length > 0;
    const hasFile = input.files && input.files.length > 0;
    if (src === "spotify" && !hasLink) { spotify.focus(); return; }
    if (src === "upload" && !hasFile) { drop.classList.add("over"); return; }

    // The "Get audio" stage takes longer when we download from Spotify first.
    steps[0].textContent = src === "spotify" ? "Download from Spotify" : "Read audio file";

    ov.hidden = false;
    const t0 = Date.now();
    timer = setInterval(() => {
      const s = Math.floor((Date.now() - t0) / 1000);
      elapsed.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
      const active = steps.reduce((a, st, i) => s >= +st.dataset.at ? i : a, 0);
      steps.forEach((st, i) => {
        st.classList.toggle("done", i < active);
        st.classList.toggle("active", i === active);
      });
    }, 1000);

    fetch("/generate", { method: "POST", body: new FormData(form), headers: { Accept: "application/json" } })
      .then(r => r.ok ? r.json() : r.text().then(t => ({ ok: false, log: t })))
      .then(data => {
        if (data.ok && data.url) { location.href = data.url; return; }
        showLog(data.log);
      })
      .catch(() => showLog(
        "The server didn't respond. It may have run out of memory during " +
        "generation, or the process was interrupted. Check the terminal window " +
        "running server.py, then try again."));
  });
})();
