#!/usr/bin/env python3
"""Score word-onset timing of candidate files against a hand-made reference TTML.

    python compare.py <reference.ttml> <name>=<candidate> [<name>=<candidate> ...]

Candidates may be TTML files (word <span begin=..>) or whisper-style JSON
({"words": [{"word","start","end"}]}). Words are normalized (lowercase,
punctuation stripped) and sequence-matched with difflib, so transcript
differences reduce the match count instead of corrupting the pairing.
Reported per candidate: matched-word coverage, mean/median/p90/max absolute
onset error, and signed bias (candidate minus reference).
"""
import json, re, sys, difflib, statistics as st

SPAN = re.compile(r'<span[^>]*?begin="([^"]+)"[^>]*?end="([^"]+)"[^>]*>([^<]+)</span>')

def t2s(t):
    sec = 0.0
    for p in t.rstrip("s").split(":"):
        sec = sec * 60 + float(p)
    return sec

def norm(w):
    return re.sub(r"[^a-z0-9]", "", w.lower())

def load_ttml(path):
    txt = open(path, encoding="utf-8").read()
    out = []
    for b, _e, w in SPAN.findall(txt):
        n = norm(re.sub(r"&#x27;|&apos;", "'", w))
        if n:
            out.append((n, t2s(b)))
    return out

def load_json(path):
    data = json.load(open(path, encoding="utf-8"))
    return [(norm(w["word"]), w["start"]) for w in data["words"] if norm(w["word"])]

def load(path):
    return load_json(path) if path.lower().endswith(".json") else load_ttml(path)

def compare(name, cand, ref):
    sm = difflib.SequenceMatcher(a=[w for w, _ in ref], b=[w for w, _ in cand], autojunk=False)
    errs = []
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            errs.append(cand[blk.b + k][1] - ref[blk.a + k][1])
    if not errs:
        print(f"{name:30s}  no matches!")
        return
    ab = sorted(abs(e) for e in errs)
    print(f"{name:30s} matched {len(errs):3d}/{len(ref)} ({100*len(errs)/len(ref):3.0f}%)  "
          f"mean|e| {1000*sum(ab)/len(ab):4.0f}ms  median {1000*ab[len(ab)//2]:4.0f}ms  "
          f"p90 {1000*ab[int(0.9*(len(ab)-1))]:4.0f}ms  max {1000*ab[-1]:5.0f}ms  "
          f"bias {1000*st.mean(errs):+5.0f}ms")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    ref = load_ttml(sys.argv[1])
    print(f"reference: {len(ref)} words")
    for arg in sys.argv[2:]:
        name, path = arg.split("=", 1)
        compare(name, load(path), ref)
