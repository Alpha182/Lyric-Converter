#!/usr/bin/env python3
"""Swap AutoLyrixAlign word timings into an existing TTML's structure.

    python ala_to_ttml.py <in.ttml> <ala_aligned.txt> <out.ttml> [offset_s] [titleSuffix]

AutoLyrixAlign (singing-trained) outputs a flat `start end WORD` list with a constant late
bias; we subtract `offset_s` (default 0.29) and map the words back onto the source TTML's
line/word/background structure by sequence (difflib). Words ALA didn't align (background
ad-libs, skips) keep an interpolated time so nothing is left at 0. Everything else in the
TTML — display text, background groups, metadata — is preserved."""
import re, html, sys, difflib

SPAN = re.compile(r'<span begin="([^"]+)" end="([^"]+)">(.*?)</span>', re.S)
PBLK = re.compile(r'(<p\b[^>]*>)(.*?)(</p>)', re.S)
NORM = re.compile(r"[^a-z0-9]")

def t2s(x):
    p = x.split(":"); return sum(float(v) * 60 ** i for i, v in enumerate(reversed(p)))
def fmt(t):
    t = max(0.0, t); m = int(t // 60); return f"{m:02d}:{t - 60 * m:06.3f}"
def norm(w):
    return NORM.sub("", html.unescape(re.sub("<[^>]+>", "", w)).lower())

ttml_path, ala_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
OFF = float(sys.argv[4]) if len(sys.argv) > 4 else 0.29
suffix = sys.argv[5] if len(sys.argv) > 5 else " (AutoLyrixAlign)"

txt = open(ttml_path, encoding="utf-8").read()

# --- read AutoLyrixAlign output (offset-corrected) ---
ala = []
for ln in open(ala_path, encoding="utf-8"):
    p = ln.split()
    if len(p) >= 3:
        n = NORM.sub("", p[2].lower())
        if n:
            ala.append((float(p[0]) - OFF, float(p[1]) - OFF, n))

# --- every span in document order (main + background words) ---
spans = [(m.start(), m.end(), norm(m.group(3))) for m in SPAN.finditer(txt)]
ttml_norm = [s[2] for s in spans]
ala_norm = [a[2] for a in ala]

# --- align the two word sequences; matched spans get ALA times ---
new = [None] * len(spans)                                  # (begin, end) per span
sm = difflib.SequenceMatcher(None, ttml_norm, ala_norm)
for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if tag == "equal":
        for k in range(i2 - i1):
            a = ala[j1 + k]
            new[i1 + k] = (a[0], a[1])

# --- interpolate any span ALA didn't cover (background ad-libs, skips) ---
anchors = [(i, new[i][0]) for i in range(len(new)) if new[i] is not None]
if not anchors:
    sys.exit("no words matched between TTML and ALA output")
def interp_begin(i):
    prev = [(ai, at) for ai, at in anchors if ai <= i]
    nxt = [(ai, at) for ai, at in anchors if ai >= i]
    if prev and nxt and prev[-1][0] != nxt[0][0]:
        (pi, pt), (ni, nt) = prev[-1], nxt[0]
        return pt + (nt - pt) * (i - pi) / (ni - pi)
    return (prev[-1][1] if prev else nxt[0][1])
for i in range(len(new)):
    if new[i] is None:
        b = interp_begin(i)
        new[i] = (b, b + 0.3)

# --- keep begins monotonic and ends non-overlapping ---
begins = [nb for nb, _ in new]
for i in range(1, len(begins)):
    if begins[i] < begins[i - 1]:
        begins[i] = begins[i - 1]
final = []
for i in range(len(new)):
    b = begins[i]
    nb = begins[i + 1] if i + 1 < len(begins) else b + 0.4
    e = max(b + 0.05, min(new[i][1], nb))
    final.append((b, e))

# --- write the new times back into each span, in document order ---
it = iter(final)
def repl(m):
    b, e = next(it)
    return f'<span begin="{fmt(b)}" end="{fmt(e)}">{m.group(3)}</span>'
txt = SPAN.sub(repl, txt)

# --- fix each <p> begin/end from its (updated) spans ---
def fix_p(m):
    head, inner, tail = m.groups()
    ts = [(t2s(b), t2s(e)) for b, e, _ in SPAN.findall(inner)]
    if ts:
        head = re.sub(r'begin="[^"]+"', f'begin="{fmt(min(b for b, _ in ts))}"', head, count=1)
        head = re.sub(r'end="[^"]+"', f'end="{fmt(max(e for _, e in ts))}"', head, count=1)
    return head + inner + tail
txt = PBLK.sub(fix_p, txt)

# --- fix body dur / div end, and label the metadata ---
ends = [t2s(e) for _, e, _ in SPAN.findall(txt)]
if ends:
    txt = re.sub(r'(<body dur=")[^"]+(")', rf'\g<1>{fmt(max(ends))}\g<2>', txt, count=1)
    txt = re.sub(r'(<div begin="[^"]+" end=")[^"]+(")', rf'\g<1>{fmt(max(ends))}\g<2>', txt, count=1)
txt = re.sub(r'(key="aligner" value=")[^"]*(")', r'\g<1>v3-singing\g<2>', txt)
if 'key="aligner"' not in txt:
    txt = txt.replace('</metadata>', '<amll:meta key="aligner" value="v3-singing"/></metadata>')
m = re.search(r'key="musicName" value="([^"]*)"', txt)
if m and suffix and not m.group(1).endswith(suffix):
    txt = txt.replace(f'value="{m.group(1)}"', f'value="{m.group(1)}{html.escape(suffix)}"', 1)

open(out_path, "w", encoding="utf-8").write(txt)
matched = sum(1 for x in new if x is not None)
print(f"wrote {out_path}: {len(spans)} words ({len(anchors)} from ALA, "
      f"{len(spans) - len(anchors)} interpolated), offset -{OFF*1000:.0f}ms")
