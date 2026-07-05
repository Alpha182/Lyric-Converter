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
    for b, e, w in SPAN.findall(txt):
        n = norm(re.sub(r"&#x27;|&apos;", "'", w))
        if n:
            out.append((n, t2s(b), t2s(e)))
    return out

def load_json(path):
    data = json.load(open(path, encoding="utf-8"))
    return [(norm(w["word"]), w["start"], w["end"]) for w in data["words"] if norm(w["word"])]

def load(path):
    return load_json(path) if path.lower().endswith(".json") else load_ttml(path)

STRUCT_CUT = 3.0  # |error| above this is a structural miss (matcher paired the wrong
                  # repeat of a word, or the aligner derailed) — counted, not averaged in

HOLD_MIN = 1.2  # reference words at least this long are "holds" ("deeeeep")

def score(cand, ref):
    """Match candidate words to reference words; return timing stats + structural count.

    Perceptual notes baked in: a structural miss where the SAME text exists nearby in
    the reference is invisible on screen (repeated 'thunder' chants), so same-text
    misses are counted separately from real ones. jitter (stdev of error) tracks the
    wobble the eye notices far more than a constant bias. hold = median fraction of a
    long reference word's duration that the candidate keeps it lit."""
    sm = difflib.SequenceMatcher(a=[r[0] for r in ref], b=[c[0] for c in cand], autojunk=False)
    pairs = []
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            pairs.append((ref[blk.a + k], cand[blk.b + k]))
    if not pairs:
        return None
    errs = [(c[1] - r[1], r, c) for r, c in pairs]
    timing = [(e, r, c) for e, r, c in errs if abs(e) <= STRUCT_CUT]
    struct = [(e, r, c) for e, r, c in errs if abs(e) > STRUCT_CUT]
    # a structural miss is invisible if the same text occurs in the reference near the
    # candidate's chosen time (highlighting the wrong repeat of an identical word)
    by_word = {}
    for r in ref:
        by_word.setdefault(r[0], []).append(r[1])
    invisible = sum(1 for _e, r, c in struct
                    if any(abs(c[1] - t) <= STRUCT_CUT for t in by_word.get(r[0], [])))
    if not timing:
        return None
    es = [e for e, _r, _c in timing]
    ab = sorted(abs(e) for e in es)
    holds = sorted(max(0.0, min(c[2], r[2]) - max(c[1], r[1])) / (r[2] - r[1])
                   for _e, r, c in timing if r[2] - r[1] >= HOLD_MIN)
    return {
        "matched": len(errs), "ref_words": len(ref),
        "struct": len(struct) - invisible, "invis": invisible,
        "mean": sum(ab) / len(ab), "median": ab[len(ab) // 2],
        "p90": ab[int(0.9 * (len(ab) - 1))], "max": ab[-1], "bias": st.mean(es),
        "jitter": st.pstdev(es), "holds": len(holds),
        "hold": holds[len(holds) // 2] if holds else None,
    }

def compare(name, cand, ref):
    s = score(cand, ref)
    if s is None:
        print(f"{name:30s}  no matches!")
        return
    hold = f"{100*s['hold']:3.0f}%/{s['holds']}" if s["hold"] is not None else "  --"
    print(f"{name:30s} matched {s['matched']:3d}/{s['ref_words']} "
          f"({100*s['matched']/s['ref_words']:3.0f}%)  "
          f"median {1000*s['median']:4.0f}ms  p90 {1000*s['p90']:4.0f}ms  "
          f"bias {1000*s['bias']:+5.0f}ms  jitter {1000*s['jitter']:4.0f}ms  "
          f"hold {hold}  miss {s['struct']}+{s['invis']}i")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    ref = load_ttml(sys.argv[1])
    print(f"reference: {len(ref)} words")
    for arg in sys.argv[2:]:
        name, path = arg.split("=", 1)
        compare(name, load(path), ref)
