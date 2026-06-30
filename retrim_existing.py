#!/usr/bin/env python3
"""
One-off migration: apply the "hold trim" to TTMLs that were generated before the fix,
and rebuild their HTML viewers. No GPU / no re-alignment needed — it only caps each
word's lit duration (the tail a word absorbed from a following pause), which is the
exact thing that made the highlight crawl ("goes" lit for 1.3s). New songs generated
through align_lyrics.py already get this in the pipeline.

    python retrim_existing.py            # fix every out/*.ttml + rebuild its .html
"""
import re, os, sys, html, glob, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
PY = sys.executable
NORM = re.compile(r"[^a-z']")
SPAN = re.compile(r'<span begin="([^"]+)" end="([^"]+)">([^<]*)</span>')
P = re.compile(r'(<p\b[^>]*>)(.*?)(</p>)', re.S)

def t2s(x):
    m, s = x.split(":"); return int(m) * 60 + float(s)

def s2t(t):
    t = max(0.0, t); m = int(t // 60); return f"{m:02d}:{t - 60 * m:06.3f}"

def cap_for(word):
    norm = NORM.sub("", html.unescape(word).lower())
    return min(1.0, 0.22 + 0.08 * len(norm))          # matches align_lyrics.trim_holds

def fix_span(m):
    bs, es, w = t2s(m.group(1)), t2s(m.group(2)), m.group(3)
    ne = max(bs + 0.05, min(es, bs + cap_for(w)))
    return f'<span begin="{s2t(bs)}" end="{s2t(ne)}">{w}</span>'

def fix_p(m):
    head, inner, tail = m.groups()
    inner = SPAN.sub(fix_span, inner)
    begins = [t2s(b) for b, _, _ in SPAN.findall(inner)]
    ends = [t2s(e) for _, e, _ in SPAN.findall(inner)]
    if begins:                                         # keep <p> line timing consistent
        head = re.sub(r'begin="[^"]+"', f'begin="{s2t(min(begins))}"', head, count=1)
        head = re.sub(r'end="[^"]+"', f'end="{s2t(max(ends))}"', head, count=1)
    return head + inner + tail

def meta(ttml, key):
    m = re.search(rf'key="{key}" value="([^"]*)"', ttml)
    return html.unescape(m.group(1)) if m else ""

def main():
    ttmls = sorted(glob.glob(os.path.join(OUT, "*.ttml")))
    for tp in ttmls:
        base = os.path.splitext(os.path.basename(tp))[0]
        txt = open(tp, encoding="utf-8").read()
        new = P.sub(fix_p, txt)
        ends = [t2s(e) for _, e, _ in SPAN.findall(new)]
        if ends:                                       # keep <body dur> / <div end> in sync
            new = re.sub(r'(<body dur=")[^"]+(")', rf'\g<1>{s2t(max(ends))}\g<2>', new, count=1)
            new = re.sub(r'(<div begin="[^"]+" end=")[^"]+(")', rf'\g<1>{s2t(max(ends))}\g<2>', new, count=1)
        open(tp, "w", encoding="utf-8").write(new)

        # rebuild the viewer using the same metadata it already had
        hp = os.path.join(OUT, base + ".html")
        audio, gen = "", ""
        if os.path.exists(hp):
            h = open(hp, encoding="utf-8").read()
            ma = re.search(r'<audio[^>]*src="([^"]+)"', h); audio = ma.group(1) if ma else ""
            mg = re.search(r'generated in (\d+)s', h); gen = mg.group(1) if mg else ""
        if not audio:
            cand = [f for f in os.listdir(OUT) if f.startswith(base + ".") and
                    f.rsplit(".", 1)[-1] in ("mp3", "m4a", "wav", "opus", "ogg", "flac")]
            audio = cand[0] if cand else base + ".mp3"
        cmd = [PY, os.path.join(HERE, "make_view.py"), "--ttml", tp, "--audio", audio,
               "--out", hp, "--title", meta(txt, "musicName"), "--artist", meta(txt, "artists")]
        if gen:
            cmd += ["--gen-seconds", gen]
        subprocess.run(cmd, check=True)
        print(f"  retrimmed {base}")
    print(f"done: {len(ttmls)} song(s)")

if __name__ == "__main__":
    main()
