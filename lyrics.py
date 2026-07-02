#!/usr/bin/env python3
"""
One-shot karaoke maker: audio file -> word-by-word lyric viewer in your browser.

    python lyrics.py "C:\\path\\song.mp3" --search "Blueberry Faygo Lil Mosey"
    python lyrics.py "song.mp3" --search "Beauty Drake" --id 190jyVPHYjAqEaOGmMzdyk

Steps it runs for you:
  1. fetch lyrics from LRCLIB (or use --lyrics file.txt)
  2. separate vocals (GPU) + forced-align the words  -> <name>.ttml
  3. build a self-contained karaoke page             -> <name>.html
  4. open it in your default browser

Flags:
  --search "<text>"   what to look up on LRCLIB (song + artist works best)
  --id <spotifyId>    optional; names the .ttml so it can drop into the mod later
  --lyrics file.txt   skip the LRCLIB fetch and use your own lyrics
  --title / --artist  override the display name
  --outdir <dir>      where to write files (default: next to this script)
  --no-open           don't auto-open the browser
"""
import argparse, os, re, sys, json, time, subprocess, shutil, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

def run_retry(cmd, label, tries=3):
    """Run a pipeline stage, retrying on failure. The heavy stages (Demucs/MMS) can hit
    transient CUDA errors or momentary low commit-memory and die; a fresh process a few
    seconds later almost always succeeds, so retry before giving up with a clear message."""
    for k in range(tries):
        if subprocess.run(cmd).returncode == 0:
            return
        if k < tries - 1:
            print(f"      {label} failed (attempt {k + 1}/{tries}); retrying in 4s…", file=sys.stderr)
            time.sleep(4)
    sys.exit(f"{label} failed after {tries} tries — usually low GPU/commit memory. "
             f"Close some apps (Discord/browser) or enlarge the Windows pagefile, then retry.")

def slug(s):
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower() or "song"

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "lyrics-maker"})
    return json.loads(urllib.request.urlopen(req).read().decode("utf-8", "replace"))

def spotify_meta(track_id):
    """(title, artist) from the public Spotify embed page — no auth needed."""
    try:
        req = urllib.request.Request("https://open.spotify.com/embed/track/" + track_id,
                                     headers={"User-Agent": "Mozilla/5.0"})
        h = urllib.request.urlopen(req).read().decode("utf-8", "replace")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S)
        ent = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
        title = ent.get("title") or ent.get("name") or ""
        artist = ", ".join(a.get("name", "") for a in ent.get("artists", [])) or ent.get("subtitle", "")
        return title, artist
    except Exception:
        return "", ""

def audio_duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=noprint_wrappers=1:nokey=1", path],
                             capture_output=True, text=True)
        return float(out.stdout.strip())
    except Exception:
        return None

def fetch_lyrics(title, artist, query, dur):
    """Prefer synced lyrics, disambiguate by duration. Returns
    (text, title, artist, synced, matched_duration) or None."""
    best = None
    if title and artist:                       # precise lookup first
        try:
            best = get_json("https://lrclib.net/api/get?" + urllib.parse.urlencode(
                {"artist_name": artist, "track_name": title, "duration": int(dur or 0)}))
        except Exception:
            best = None
        if best and not (best.get("syncedLyrics") or best.get("plainLyrics")):
            best = None
    if not best:                               # fall back to search + duration filter
        q = query or f"{title} {artist}".strip()
        try:
            res = get_json("https://lrclib.net/api/search?" + urllib.parse.urlencode({"q": q}))
        except Exception:
            res = []
        cand = [r for r in res if r.get("syncedLyrics") or r.get("plainLyrics")]
        if dur:
            cand = [r for r in cand if abs((r.get("duration") or 9e9) - dur) <= 8] or cand
        cand.sort(key=lambda r: (0 if r.get("syncedLyrics") else 1,
                                 abs((r.get("duration") or 9e9) - (dur or 0))))
        best = cand[0] if cand else None
    if not best:
        return None
    return (best.get("syncedLyrics") or best.get("plainLyrics"),
            best.get("trackName", title), best.get("artistName", artist),
            bool(best.get("syncedLyrics")), best.get("duration"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--search", default="")
    ap.add_argument("--lyrics", default="")
    ap.add_argument("--id", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--artist", default="")
    ap.add_argument("--outdir", default=HERE)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    if not os.path.isfile(a.audio):
        sys.exit(f"audio not found: {a.audio}")
    os.makedirs(a.outdir, exist_ok=True)

    # 1) lyrics
    dur = audio_duration(a.audio)
    title, artist = a.title, a.artist
    if a.id and not (title and artist):                # resolve real song from the Spotify id
        st, sa = spotify_meta(a.id)
        title = title or st
        artist = artist or sa
        if st:
            print(f"[1/4] track: {st} — {sa}  ({dur:.0f}s)" if dur else f"[1/4] track: {st} — {sa}")
    if a.lyrics:
        lyrics_text = open(a.lyrics, encoding="utf-8").read()
    else:
        print(f"      looking up lyrics: {title or a.search} {artist}".rstrip())
        got = fetch_lyrics(title, artist, a.search, dur)
        if not got:
            sys.exit("no lyrics found — try --search \"song artist\" or --lyrics file.txt")
        lyrics_text, title, artist, synced, mdur = got
        warn = ""
        if dur and mdur and abs(mdur - dur) > 8:
            warn = f"  ⚠ matched length {mdur:.0f}s vs your audio {dur:.0f}s — may be the wrong song"
        print(f"      matched: {title} — {artist}  (synced={'yes' if synced else 'no'}){warn}")

    name = a.id or slug(f"{title}-{artist}" if title else os.path.basename(a.audio))
    lyr_path = os.path.join(a.outdir, name + ".txt")
    ttml_path = os.path.join(a.outdir, name + ".ttml")
    audio_local = name + os.path.splitext(a.audio)[1].lower()      # relative name for html
    html_path = os.path.join(a.outdir, name + ".html")
    open(lyr_path, "w", encoding="utf-8").write(lyrics_text)
    dst_audio = os.path.join(a.outdir, audio_local)
    if os.path.abspath(a.audio) != os.path.abspath(dst_audio):
        shutil.copyfile(a.audio, dst_audio)

    # 2) align — run as TWO separate processes so each model gets a clean CUDA
    #    context (Demucs + MMS in one process fragments GPU memory and OOMs).
    voc_path = os.path.join(a.outdir, name + ".vocals.wav")
    print("[2/4] separating vocals (GPU)…")
    t = time.time()
    run_retry([PY, os.path.join(HERE, "align_lyrics.py"), "--stage", "separate",
               "--audio", a.audio, "--vocals", voc_path], "separation")
    sep_s = time.time() - t
    print(f"      separation: {sep_s:.0f}s — aligning words (GPU)…")
    t = time.time()
    run_retry([PY, os.path.join(HERE, "align_lyrics.py"), "--stage", "align",
               "--vocals", voc_path, "--lyrics", lyr_path, "--out", ttml_path,
               "--spotify-id", a.id, "--title", title, "--artist", artist], "alignment")
    ali_s = time.time() - t
    gen_s = sep_s + ali_s
    try: os.remove(voc_path)
    except OSError: pass

    # 3) build view (embeds the generation time)
    print(f"[3/4] alignment: {ali_s:.0f}s — building karaoke page…")
    subprocess.run([PY, os.path.join(HERE, "make_view.py"),
                    "--ttml", ttml_path, "--audio", audio_local, "--out", html_path,
                    "--title", title, "--artist", artist, "--gen-seconds", str(round(gen_s))], check=True)

    # 4) open
    print(f"[4/4] done in {gen_s:.0f}s (separate {sep_s:.0f}s + align {ali_s:.0f}s) -> {html_path}")
    if not a.no_open:
        os.startfile(html_path)   # opens in default browser (Windows)

if __name__ == "__main__":
    main()
