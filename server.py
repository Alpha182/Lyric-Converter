#!/usr/bin/env python3
"""
Local web app for the lyric aligner.

    python server.py
    -> open http://127.0.0.1:8770

Upload a song file + (Spotify id and/or a search term), it runs the pipeline and
shows the karaoke viewer. Everything is served over http so the audio + clicking
work (file:// blocks local audio).
"""
import os, re, sys, html, subprocess
from flask import Flask, request, send_from_directory, redirect

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
UP = os.path.join(HERE, "uploads")
os.makedirs(OUT, exist_ok=True)
os.makedirs(UP, exist_ok=True)
PY = sys.executable
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB uploads

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Lyric Aligner</title>
<style>
 :root{--accent:#1db954;}
 *{box-sizing:border-box;} body{margin:0;font-family:"Segoe UI",system-ui,sans-serif;color:#fff;
   background:radial-gradient(1000px 700px at 50% -10%,#1f2a24,#0b0f0d 60%,#070907);min-height:100vh;}
 .wrap{max-width:640px;margin:0 auto;padding:48px 22px;}
 h1{font-size:26px;margin:0 0 4px;} .sub{color:#9aa3a0;font-size:13px;margin-bottom:28px;}
 form{background:#0f1512;border:1px solid #ffffff14;border-radius:14px;padding:22px;}
 label{display:block;font-size:13px;color:#cfd6d3;margin:14px 0 6px;}
 input[type=text],input[type=file],textarea{width:100%;padding:10px 12px;border-radius:9px;
   border:1px solid #ffffff20;background:#070a09;color:#fff;font-size:14px;font-family:inherit;}
 textarea{min-height:90px;resize:vertical;} .hint{color:#6b736f;font-size:11px;margin-top:4px;}
 button{margin-top:20px;width:100%;padding:13px;border:none;border-radius:999px;background:var(--accent);
   color:#04130a;font-size:15px;font-weight:700;cursor:pointer;}
 .or{color:#6b736f;font-size:12px;text-align:center;margin:14px 0 0;}
 .songs{margin-top:30px;} .songs h2{font-size:14px;color:#9aa3a0;font-weight:600;}
 .songs a{display:flex;align-items:center;justify-content:space-between;gap:10px;color:#fff;
   text-decoration:none;padding:9px 12px;border-radius:8px;background:#0f1512;
   border:1px solid #ffffff10;margin-top:7px;font-size:14px;}
 .songs a:hover{border-color:var(--accent);}
 .songs .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
 .tag{flex:none;font-size:11px;padding:2px 9px;border-radius:999px;white-space:nowrap;}
 .tag.new{background:#1db95422;color:#1db954;border:1px solid #1db95455;}
 .tag.sing{background:#7c5cff22;color:#a48bff;border:1px solid #7c5cff66;}
 .tag.old{background:#ffffff10;color:#7a827e;border:1px solid #ffffff18;}
 #ov{position:fixed;inset:0;background:#070907ee;display:none;align-items:center;justify-content:center;
   flex-direction:column;text-align:center;z-index:9;}
 #ov .s{width:44px;height:44px;border:4px solid #ffffff22;border-top-color:var(--accent);border-radius:50%;
   animation:spin 1s linear infinite;margin-bottom:18px;} @keyframes spin{to{transform:rotate(360deg);}}
</style></head><body>
 <div id="ov"><div class="s"></div><div><b>Generating…</b><br><span style="color:#9aa3a0;font-size:13px">
   separating vocals + aligning on the GPU — about a minute. Keep this tab open.</span></div></div>
 <div class="wrap">
   <h1>🎤 Lyric Aligner</h1>
   <div class="sub">Upload a song → word-by-word karaoke (auto-aligned, unverified).</div>
   <form method="post" action="/generate" enctype="multipart/form-data" onsubmit="document.getElementById('ov').style.display='flex';">
     <label>Song audio file</label>
     <input type="file" name="audio" accept="audio/*" required>
     <label>Spotify track ID <span class="hint">(Share → Copy Song Link → the code in the URL)</span></label>
     <input type="text" name="id" placeholder="6wJYhPfqk3KGhHRG76WzOh">
     <label>Find lyrics — song + artist</label>
     <input type="text" name="search" placeholder="Blueberry Faygo Lil Mosey">
     <p class="or">— or paste the lyrics yourself —</p>
     <label>Lyrics <span class="hint">(optional; overrides the search)</span></label>
     <textarea name="lyrics" placeholder="One bad bitch, and she do what I say so&#10;Two big .40s and a big ass Draco (two, boom, boom)&#10;..."></textarea>
     <button type="submit">Generate karaoke</button>
   </form>
   <div class="songs"><h2>Already made</h2>__LIST__</div>
 </div>
</body></html>"""

META = re.compile(r'key="([^"]+)" value="([^"]*)"')

def song_info(base):
    """(display name, aligner version) read from the song's .ttml, falling back to the id."""
    tp = os.path.join(OUT, base + ".ttml")
    name, artist, aligner = "", "", ""
    if os.path.exists(tp):
        d = dict(META.findall(open(tp, encoding="utf-8").read()[:2000]))
        name = html.unescape(d.get("musicName", ""))
        artist = html.unescape(d.get("artists", ""))
        aligner = d.get("aligner", "")
    label = f"{name} — {artist}" if name and artist else (name or base)
    return label, aligner

@app.route("/")
def index():
    songs = [f[:-5] for f in os.listdir(OUT) if f.endswith(".html")]
    rows = [(base,) + song_info(base) for base in songs]
    # singing-aligned first, then MMS-blend, then older; alphabetical within each
    order = {"v3-singing": 0, "v2-blend": 1}
    rows.sort(key=lambda r: (order.get(r[2], 2), r[1].lower()))
    def badge(al):
        if al == "v3-singing":
            return "sing", "singing-aligned"
        return ("new", "MMS blend") if al else ("old", "older")
    parts = []
    for base, label, aligner in rows:
        cls, name = badge(aligner)
        parts.append(f'<a href="/songs/{base}.html"><span class="nm">{html.escape(label)}</span>'
                     f'<span class="tag {cls}">{name}</span></a>')
    items = "".join(parts) or '<a style="color:#6b736f">none yet</a>'
    return PAGE.replace("__LIST__", items)

@app.route("/songs/<path:fn>")
def songs(fn):
    return send_from_directory(OUT, fn)

@app.route("/generate", methods=["POST"])
def generate():
    f = request.files.get("audio")
    if not f or not f.filename:
        return "no audio file", 400
    sid = request.form.get("id", "").strip()
    search = request.form.get("search", "").strip()
    lyrics = request.form.get("lyrics", "").strip()

    ext = os.path.splitext(f.filename)[1].lower() or ".mp3"
    apath = os.path.join(UP, "upload" + ext)
    f.save(apath)

    cmd = [PY, os.path.join(HERE, "lyrics.py"), apath, "--outdir", OUT, "--no-open"]
    if sid:
        cmd += ["--id", sid]
    if lyrics:
        lp = os.path.join(UP, "lyrics.txt")
        open(lp, "w", encoding="utf-8").write(lyrics)
        cmd += ["--lyrics", lp]
    elif search:
        cmd += ["--search", search]
    else:
        return "Give me a search term or paste lyrics.", 400

    # Force UTF-8 both ways: children emit it (PYTHONUTF8), and we decode with replace
    # so a stray Unicode char (⚠, —, …) can't crash the capture thread and kill the server.
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    if proc.returncode != 0:
        tail = (proc.stdout or "")[-2500:] + "\n" + (proc.stderr or "")[-2500:]
        return f"<pre style='color:#fff;background:#111;padding:20px'>generation failed:\n{tail}</pre>", 500
    m = re.search(r"done -> (.+\.html)", proc.stdout or "")
    if not m:
        return f"<pre style='color:#fff;background:#111;padding:20px'>{(proc.stdout or '')[-2500:]}</pre>", 500
    return redirect("/songs/" + os.path.basename(m.group(1).strip()))

if __name__ == "__main__":
    print("Lyric Aligner running -> http://127.0.0.1:8770")
    app.run(host="127.0.0.1", port=8770, debug=False, use_reloader=False)
