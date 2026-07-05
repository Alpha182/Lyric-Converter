#!/usr/bin/env python3
"""
Local web app for the lyric aligner.

    python server.py
    -> open http://127.0.0.1:8770

Pages:
    /            songs library (covers, length, personal star rating)
    /generate    upload a song -> word-by-word karaoke

Data endpoints:
    /api/songs   library as JSON (metadata cached in library.json)
    /api/rate    POST {id, rating} -> saved to ratings.json
    /covers/<id>.jpg   album art, lazily fetched from Spotify's public
                       oEmbed endpoint and cached in covers/

Everything is served over http so the audio + clicking work (file:// blocks
local audio).
"""
import os, re, sys, html, json, time, threading, subprocess, urllib.parse, urllib.request
from flask import Flask, request, send_from_directory, redirect, render_template, jsonify, abort

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
UP = os.path.join(HERE, "uploads")
COVERS = os.path.join(HERE, "covers")
RATINGS_PATH = os.path.join(HERE, "ratings.json")
CACHE_PATH = os.path.join(HERE, "library.json")
for d in (OUT, UP, COVERS):
    os.makedirs(d, exist_ok=True)
PY = sys.executable

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB uploads

META = re.compile(r'key="([^"]+)" value="([^"]*)"')
BODY_DUR = re.compile(r'<body[^>]*\bdur="(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)"')
SPOTIFY_ID = re.compile(r"^[0-9A-Za-z]{22}$")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".ogg")

_io_lock = threading.Lock()
_cover_failed = {}  # base -> last failure time; don't re-hit Spotify for 10 min


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def safe_base(base):
    """Song ids double as filenames in out/ — reject anything path-like."""
    return base and not re.search(r'[/\\]|\.\.', base)


def ttml_meta(base):
    """Metadata from the song's .ttml header (title, artist, ids, fallback duration)."""
    out = {"title": "", "artist": "", "spotify_id": "", "aligner": "", "ttml_dur": None}
    tp = os.path.join(OUT, base + ".ttml")
    if not os.path.exists(tp):
        return out
    try:
        with open(tp, encoding="utf-8") as f:
            head = f.read(4000)
    except OSError:
        return out
    d = dict(META.findall(head))
    out["title"] = html.unescape(d.get("musicName", ""))
    out["artist"] = html.unescape(d.get("artists", ""))
    out["spotify_id"] = d.get("spotifyId", "")
    out["aligner"] = d.get("aligner", "")
    m = BODY_DUR.search(head)
    if m:
        h, mm, ss = (m.group(1) or 0), m.group(2), m.group(3)
        out["ttml_dur"] = int(h) * 3600 + int(mm) * 60 + float(ss)
    return out


def spotify_candidates(base, meta):
    """Possible Spotify track ids for a song, best guess first."""
    cands = [meta.get("spotify_id", ""), base, base.split(".")[0]]
    return [c for c in dict.fromkeys(cands) if c and SPOTIFY_ID.match(c)]


def spotify_title_artist(sid):
    """(title, artist) from the public Spotify embed page — no auth needed."""
    try:
        req = urllib.request.Request("https://open.spotify.com/embed/track/" + sid,
                                     headers={"User-Agent": "Mozilla/5.0"})
        h = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "replace")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S)
        ent = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
        title = ent.get("title") or ent.get("name") or ""
        artist = ", ".join(a.get("name", "") for a in ent.get("artists", [])) or ent.get("subtitle", "")
        return title, artist
    except Exception:
        return "", ""


def audio_path(base):
    """The song's audio in out/ — variants like <id>.A fall back to the plain id."""
    stems = [base] + ([base.split(".")[0]] if "." in base else [])
    for stem in stems:
        for ext in AUDIO_EXTS:
            p = os.path.join(OUT, stem + ext)
            if os.path.exists(p):
                return p
    return None


def probe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=15)
        return round(float(out.stdout.strip()), 1)
    except Exception:
        return None


def library():
    """All songs in out/ with metadata; slow bits (ffprobe) cached in library.json."""
    cache = load_json(CACHE_PATH, {})
    ratings = load_json(RATINGS_PATH, {})
    songs, dirty = [], False
    for f in sorted(os.listdir(OUT)):
        if not f.endswith(".html"):
            continue
        base = f[:-5]
        p_html = os.path.join(OUT, f)
        p_ttml = os.path.join(OUT, base + ".ttml")
        ref = p_ttml if os.path.exists(p_ttml) else p_html
        try:
            mt = os.path.getmtime(ref)
        except OSError:
            continue
        ent = cache.get(base)
        if not ent or ent.get("mtime") != mt:
            meta = ttml_meta(base)
            ap = audio_path(base)
            dur = (probe_duration(ap) if ap else None) or meta["ttml_dur"]
            ent = {"mtime": mt, "title": meta["title"] or base, "artist": meta["artist"],
                   "spotify_id": meta["spotify_id"], "aligner": meta["aligner"],
                   "duration": dur}
            cache[base] = ent
            dirty = True
        # Older ttml files have no musicName — backfill the display name from the
        # Spotify embed page once, and remember we tried so offline runs stay fast.
        if ent["title"] == base and not ent.get("looked_up"):
            for sid in spotify_candidates(base, ent):
                title, artist = spotify_title_artist(sid)
                if title:
                    ent.update(title=title, artist=artist, spotify_id=sid)
                    break
            ent["looked_up"] = True
            dirty = True
        songs.append({
            "id": base,
            "title": ent["title"],
            "artist": ent["artist"],
            "spotifyId": ent["spotify_id"],
            "aligner": ent["aligner"],
            "duration": ent["duration"],
            "rating": int(ratings.get(base, 0)),
            "added": os.path.getmtime(p_html),
            "url": "/songs/" + f,
            "cover": "/covers/" + base + ".jpg",
        })
    if dirty:
        with _io_lock:
            save_json(CACHE_PATH, cache)
    songs.sort(key=lambda s: -s["added"])
    return songs


# ---------------------------------------------------------------- pages

@app.route("/")
def songs_page():
    return render_template("songs.html", page="songs")


@app.route("/generate")
def generate_page():
    return render_template("generate.html", page="generate")


@app.route("/songs/<path:fn>")
def song_files(fn):
    return send_from_directory(OUT, fn)


FAVICON = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><g fill='#4aa3e8'>"
           "<rect x='3' y='10' width='3' height='8' rx='1.5'/><rect x='8' y='5' width='3' height='13' rx='1.5'/>"
           "<rect x='13' y='8' width='3' height='10' rx='1.5'/><rect x='18' y='12' width='3' height='6' rx='1.5'/>"
           "</g></svg>")


@app.route("/favicon.ico")
def favicon():
    return FAVICON, 200, {"Content-Type": "image/svg+xml",
                          "Cache-Control": "public, max-age=86400"}


# ---------------------------------------------------------------- api

@app.route("/api/songs")
def api_songs():
    return jsonify({"songs": library()})


@app.route("/api/rate", methods=["POST"])
def api_rate():
    data = request.get_json(force=True, silent=True) or {}
    base = str(data.get("id", ""))
    try:
        rating = int(data.get("rating", -1))
    except (TypeError, ValueError):
        rating = -1
    if not safe_base(base) or not 0 <= rating <= 5:
        abort(400)
    if not os.path.exists(os.path.join(OUT, base + ".html")):
        abort(404)
    with _io_lock:
        ratings = load_json(RATINGS_PATH, {})
        if rating == 0:
            ratings.pop(base, None)
        else:
            ratings[base] = rating
        save_json(RATINGS_PATH, ratings)
    return jsonify({"ok": True, "id": base, "rating": rating})


# ---------------------------------------------------------------- covers

def fetch_cover(base):
    """Album art via Spotify's public oEmbed endpoint (no auth). Cached on disk."""
    path = os.path.join(COVERS, base + ".jpg")
    if os.path.exists(path):
        return path
    if time.time() - _cover_failed.get(base, 0) < 600:
        return None
    cands = spotify_candidates(base, ttml_meta(base))
    if not cands:
        _cover_failed[base] = time.time()
        return None
    sid = cands[0]
    try:
        track = urllib.parse.quote("https://open.spotify.com/track/" + sid, safe="")
        req = urllib.request.Request("https://open.spotify.com/oembed?url=" + track,
                                     headers={"User-Agent": "Mozilla/5.0"})
        info = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8"))
        thumb = info.get("thumbnail_url")
        if not thumb:
            raise ValueError("no thumbnail")
        req = urllib.request.Request(thumb, headers={"User-Agent": "Mozilla/5.0"})
        img = urllib.request.urlopen(req, timeout=8).read()
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(img)
        os.replace(tmp, path)
        return path
    except Exception:
        _cover_failed[base] = time.time()
        return None


@app.route("/covers/<base>.jpg")
def cover(base):
    if not safe_base(base):
        abort(400)
    if not fetch_cover(base):
        abort(404)
    resp = send_from_directory(COVERS, base + ".jpg")
    resp.headers["Cache-Control"] = "public, max-age=2592000"
    return resp


# ---------------------------------------------------------------- generate

ERR_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Generation failed</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body class="bare"><div class="err-wrap">
<h1>Generation failed</h1>
<p class="page-sub">The pipeline exited with an error. The log tail is below.
<a href="/generate">Try again</a></p>
<pre class="err-log">__LOG__</pre>
</div></body></html>"""


def err_page(log):
    return ERR_PAGE.replace("__LOG__", html.escape(log)), 500


@app.route("/generate", methods=["POST"])
def generate():
    # The page submits with fetch (Accept: application/json) so failures can be
    # shown in a popup; fall back to a full page for a plain form post.
    wants_json = "application/json" in request.headers.get("Accept", "")

    def fail(msg):
        if wants_json:
            return jsonify({"ok": False, "log": msg})
        return err_page(msg)

    spotify = request.form.get("spotify", "").strip()
    sid = request.form.get("id", "").strip()
    search = request.form.get("search", "").strip()
    lyrics = request.form.get("lyrics", "").strip()
    f = request.files.get("audio")

    # Source of audio: a pasted Spotify link (download it) or an uploaded file.
    if spotify:
        try:
            import fetch_track
            sid = fetch_track.track_id(spotify)      # 22-char id names the outputs
            apath = os.path.join(UP, "download.mp3")
            fetch_track.fetch(spotify, apath)
        except Exception as e:
            return fail("Couldn't download that track from Spotify:\n" + str(e))
    elif f and f.filename:
        ext = os.path.splitext(f.filename)[1].lower() or ".mp3"
        apath = os.path.join(UP, "upload" + ext)
        f.save(apath)
    else:
        return fail("Paste a Spotify link or choose an audio file to upload.")

    cmd = [PY, os.path.join(HERE, "lyrics.py"), apath, "--outdir", OUT, "--no-open"]
    if sid:
        cmd += ["--id", sid]
    if lyrics:
        lp = os.path.join(UP, "lyrics.txt")
        with open(lp, "w", encoding="utf-8") as fh:
            fh.write(lyrics)
        cmd += ["--lyrics", lp]
    elif search:
        cmd += ["--search", search]
    elif not sid:
        # With a Spotify id, lyrics.py resolves the name and finds lyrics itself.
        return fail("Add a Spotify link or ID so I can find the lyrics, "
                    "or type a search term / paste the lyrics.")

    # Force UTF-8 both ways: children emit it (PYTHONUTF8), and we decode with replace
    # so a stray Unicode char can't crash the capture thread and kill the server.
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    if proc.returncode != 0:
        return fail((proc.stdout or "")[-3500:] + "\n" + (proc.stderr or "")[-1500:])
    # lyrics.py's last line is "[4/4] done in Ns (...) -> <path>.html" — grab the
    # arrow target (path has spaces, so match to the final .html on the line).
    hits = re.findall(r"->\s*(.+\.html)", proc.stdout or "")
    if not hits:
        return fail((proc.stdout or "")[-3500:])
    url = "/songs/" + os.path.basename(hits[-1].strip())
    if wants_json:
        return jsonify({"ok": True, "url": url})
    return redirect(url)


if __name__ == "__main__":
    print("Lyric Aligner running -> http://127.0.0.1:8770")
    app.run(host="127.0.0.1", port=8770, debug=False, use_reloader=False, threaded=True)
