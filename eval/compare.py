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

STRUCT_CUT = 3.0  # |error| above this is a structural miss (matcher paired the wrong
                  # repeat of a word, or the aligner derailed) — counted, not averaged in

def score(cand, ref):
    """Match candidate words to reference words; return timing stats + structural count."""
    sm = difflib.SequenceMatcher(a=[w for w, _ in ref], b=[w for w, _ in cand], autojunk=False)
    errs = []
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            errs.append(cand[blk.b + k][1] - ref[blk.a + k][1])
    timing = [e for e in errs if abs(e) <= STRUCT_CUT]
    if not timing:
        return None
    ab = sorted(abs(e) for e in timing)
    return {
        "matched": len(errs), "ref_words": len(ref), "struct": len(errs) - len(timing),
        "mean": sum(ab) / len(ab), "median": ab[len(ab) // 2],
        "p90": ab[int(0.9 * (len(ab) - 1))], "max": ab[-1], "bias": st.mean(timing),
    }

def compare(name, cand, ref):
    s = score(cand, ref)
    if s is None:
        print(f"{name:30s}  no matches!")
        return
    print(f"{name:30s} matched {s['matched']:3d}/{s['ref_words']} "
          f"({100*s['matched']/s['ref_words']:3.0f}%)  "
          f"mean|e| {1000*s['mean']:4.0f}ms  median {1000*s['median']:4.0f}ms  "
          f"p90 {1000*s['p90']:4.0f}ms  max {1000*s['max']:5.0f}ms  "
          f"bias {1000*s['bias']:+5.0f}ms  struct-miss {s['struct']}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    ref = load_ttml(sys.argv[1])
    print(f"reference: {len(ref)} words")
    for arg in sys.argv[2:]:
        name, path = arg.split("=", 1)
        compare(name, load(path), ref)
