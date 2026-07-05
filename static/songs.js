/* Songs library: fetch /api/songs, render as cover grid or list, with search,
   sort, and an interactive 1-5 star rating saved via /api/rate. */
(() => {
  const listEl = document.getElementById("list");
  const statsEl = document.getElementById("stats");
  const countEl = document.getElementById("count");
  const qEl = document.getElementById("q");
  const sortEl = document.getElementById("sort");
  const segGrid = document.getElementById("view-grid");
  const segList = document.getElementById("view-list");

  let songs = null; // null = still loading
  let view = localStorage.getItem("la-view") || "grid";

  const esc = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const fmtDur = s => {
    if (!s && s !== 0) return "–:––";
    const m = Math.floor(s / 60), sec = Math.round(s % 60);
    return `${m}:${String(sec).padStart(2, "0")}`;
  };

  const fmtTotal = s => {
    const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
    return h ? `${h} hr ${m} min` : `${m} min`;
  };

  const BADGES = { "v3-singing": ["sing", "singing"], "v2-blend": ["blend", "MMS blend"] };
  const badge = al => BADGES[al] || (al ? ["blend", esc(al)] : ["old", "older"]);

  const STAR = '<svg viewBox="0 0 24 24"><path d="M12 2.6l2.9 5.9 6.5.9-4.7 4.6 1.1 6.5L12 17.4l-5.8 3.1 1.1-6.5L2.6 9.4l6.5-.9z"/></svg>';
  const PLAY = '<svg viewBox="0 0 24 24"><path d="M7 4.5v15l13-7.5z"/></svg>';

  /* ---------------- shared bits ---------------- */

  const paint = (bar, rating) => {
    [...bar.querySelectorAll(".star")].forEach((b, i) => b.classList.toggle("lit", i < rating));
    bar.setAttribute("aria-label", `Your rating: ${rating || "none"} of 5`);
  };

  function starBar(song, small) {
    const div = document.createElement("div");
    div.className = "stars" + (small ? " sm" : "");
    for (let n = 1; n <= 5; n++) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "star" + (n <= song.rating ? " lit" : "");
      b.title = `Rate ${n}/5` + (song.rating === n ? " (click again to clear)" : "");
      b.innerHTML = STAR;
      b.addEventListener("click", async e => {
        e.stopPropagation();
        const next = song.rating === n ? 0 : n; // re-click clears
        const prev = song.rating;
        song.rating = next;
        paint(div, next);
        renderStats();
        if (next) { b.classList.remove("pop"); void b.offsetWidth; b.classList.add("pop"); }
        try {
          const r = await fetch("/api/rate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: song.id, rating: next }),
          });
          if (!r.ok) throw new Error(r.status);
        } catch {
          song.rating = prev; // server said no, put it back
          paint(div, prev);
          renderStats();
        }
      });
      div.appendChild(b);
    }
    paint(div, song.rating);
    return div;
  }

  function copyId(el, song) {
    el.title = "Copy ID";
    el.addEventListener("click", async e => {
      e.stopPropagation();
      try { await navigator.clipboard.writeText(song.id); } catch { return; }
      const old = el.textContent;
      el.textContent = "copied!";
      el.classList.add("copied");
      setTimeout(() => { el.textContent = old; el.classList.remove("copied"); }, 900);
    });
  }

  const coverImg = (song, cls) => {
    const img = new Image();
    img.alt = "";
    img.loading = "lazy";
    if (cls) img.className = cls;
    img.onload = () => img.classList.add("loaded");
    img.onerror = () => img.remove(); // keep the ♪ placeholder
    img.src = song.cover;
    return img;
  };

  const open = (el, song) => el.addEventListener("click", e => {
    if (e.target.closest("button, a")) return;
    location.href = song.url;
  });

  /* ---------------- grid card ---------------- */

  function card(song, i) {
    const li = document.createElement("li");
    li.className = "card in";
    li.style.animationDelay = `${Math.min(i * 18, 300)}ms`;

    const glow = coverImg(song, "glow");
    glow.setAttribute("aria-hidden", "true");

    const art = document.createElement("div");
    art.className = "art";
    art.appendChild(coverImg(song));
    const [bCls, bTxt] = badge(song.aligner);
    art.insertAdjacentHTML("beforeend", `<span class="badge ${bCls}">${bTxt}</span>`);
    const play = document.createElement("button");
    play.type = "button";
    play.className = "play";
    play.title = "Open karaoke page";
    play.innerHTML = PLAY;
    play.addEventListener("click", e => { e.stopPropagation(); location.href = song.url; });
    art.appendChild(play);

    const title = document.createElement("a");
    title.className = "card-title";
    title.href = song.url;
    title.textContent = song.title;
    title.title = song.title;

    const artist = document.createElement("div");
    artist.className = "card-artist";
    artist.textContent = song.artist || " ";

    const foot = document.createElement("div");
    foot.className = "card-foot";
    const dur = document.createElement("span");
    dur.className = "dur";
    dur.textContent = fmtDur(song.duration);
    foot.append(starBar(song, true), dur);

    const idBtn = document.createElement("button");
    idBtn.type = "button";
    idBtn.className = "card-id";
    idBtn.textContent = song.id;
    copyId(idBtn, song);

    li.append(glow, art, title, artist, foot, idBtn);
    open(li, song);
    return li;
  }

  /* ---------------- list row ---------------- */

  function row(song, i) {
    const li = document.createElement("li");
    li.className = "row in";
    li.style.animationDelay = `${Math.min(i * 16, 280)}ms`;

    const cover = document.createElement("span");
    cover.className = "cover";
    cover.appendChild(coverImg(song));

    const [bCls, bTxt] = badge(song.aligner);
    const main = document.createElement("a");
    main.className = "song-main";
    main.href = song.url;
    main.innerHTML =
      `<div class="song-title">${esc(song.title)}</div>` +
      `<div class="song-artist">${esc(song.artist) || "&nbsp;"}<span class="badge ${bCls}">${bTxt}</span></div>`;

    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.textContent = song.id;
    copyId(chip, song);

    const dur = document.createElement("span");
    dur.className = "dur";
    dur.textContent = fmtDur(song.duration);

    li.append(cover, main, chip, dur, starBar(song, false));
    open(li, song);
    return li;
  }

  /* ---------------- render ---------------- */

  function renderStats() {
    if (!songs) return;
    const total = songs.reduce((a, s) => a + (s.duration || 0), 0);
    const rated = songs.filter(s => s.rating > 0).length;
    statsEl.innerHTML =
      `<span><b>${songs.length}</b> songs</span><span class="sep">●</span>` +
      `<span><b>${fmtTotal(total)}</b> aligned</span><span class="sep">●</span>` +
      `<span><b class="gold">${rated}</b> rated</span>`;
  }

  function render() {
    if (!songs) return;
    const q = qEl.value.trim().toLowerCase();
    let list = songs.filter(s =>
      !q || `${s.title} ${s.artist} ${s.id}`.toLowerCase().includes(q));

    const key = sortEl.value;
    if (key === "title") list.sort((a, b) => a.title.localeCompare(b.title));
    else if (key === "rating") list.sort((a, b) => b.rating - a.rating || b.added - a.added);
    else if (key === "duration") list.sort((a, b) => (b.duration || 0) - (a.duration || 0));
    else list.sort((a, b) => b.added - a.added);

    countEl.textContent = q ? `${list.length} of ${songs.length}` : "";

    listEl.className = view === "grid" ? "grid" : "rows";
    segGrid.classList.toggle("on", view === "grid");
    segList.classList.toggle("on", view === "list");
    listEl.replaceChildren(...list.map(view === "grid" ? card : row));

    if (!list.length) {
      const li = document.createElement("li");
      li.className = "empty";
      li.innerHTML = songs.length
        ? `<b>No matches</b>Nothing fits “${esc(qEl.value.trim())}”. Try another search.`
        : `<b>No songs yet</b><a href="/generate">Generate your first karaoke page</a> to start the library.`;
      listEl.appendChild(li);
    }
  }

  async function load() {
    try {
      const r = await fetch("/api/songs");
      songs = (await r.json()).songs;
      renderStats();
      render();
    } catch {
      listEl.innerHTML =
        `<li class="empty"><b>Couldn't load the library</b>` +
        `Is the server still running? <a href="">Retry</a></li>`;
    }
  }

  const setView = v => { view = v; localStorage.setItem("la-view", v); render(); };
  segGrid.addEventListener("click", () => setView("grid"));
  segList.addEventListener("click", () => setView("list"));
  qEl.addEventListener("input", render);
  sortEl.addEventListener("change", render);
  load();
})();
