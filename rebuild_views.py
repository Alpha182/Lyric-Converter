#!/usr/bin/env python3
"""Rebuild every out/*.html karaoke viewer from its TTML with the current
make_view.py template. No GPU, no re-alignment. Titles come from library.json
(which has Spotify-backfilled names) with the TTML metadata as fallback.

    python rebuild_views.py
"""
import re, os, sys, html, glob, json, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
PY = sys.executable

def meta(ttml, key):
    m = re.search(rf'key="{key}" value="([^"]*)"', ttml)
    return html.unescape(m.group(1)) if m else ""

def main():
    try:
        lib = json.load(open(os.path.join(HERE, "library.json"), encoding="utf-8"))
    except Exception:
        lib = {}
    ttmls = sorted(glob.glob(os.path.join(OUT, "*.ttml")))
    for tp in ttmls:
        base = os.path.splitext(os.path.basename(tp))[0]
        txt = open(tp, encoding="utf-8").read()
        ent = lib.get(base, {})
        title = ent.get("title") or meta(txt, "musicName") or base
        artist = ent.get("artist") or meta(txt, "artists")

        # keep whatever audio file and generation time the old page referenced
        hp = os.path.join(OUT, base + ".html")
        audio, gen = "", ""
        if os.path.exists(hp):
            h = open(hp, encoding="utf-8").read()
            ma = re.search(r'<audio[^>]*src="([^"]+)"', h)
            audio = ma.group(1) if ma else ""
            mg = re.search(r'generated in (\d+)s', h)
            gen = mg.group(1) if mg else ""
        if not audio:
            cand = [f for f in os.listdir(OUT) if f.startswith(base + ".") and
                    f.rsplit(".", 1)[-1] in ("mp3", "m4a", "wav", "opus", "ogg", "flac")]
            audio = cand[0] if cand else base + ".mp3"

        cmd = [PY, os.path.join(HERE, "make_view.py"), "--ttml", tp, "--audio", audio,
               "--out", hp, "--title", title, "--artist", artist,
               "--cover", f"/covers/{base}.jpg"]
        if gen:
            cmd += ["--gen-seconds", gen]
        subprocess.run(cmd, check=True)
        print(f"  rebuilt {base}")
    print(f"done: {len(ttmls)} page(s)")

if __name__ == "__main__":
    main()
