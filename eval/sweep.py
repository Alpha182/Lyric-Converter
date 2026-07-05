#!/usr/bin/env python3
"""Sweep the blend post-processing constants against all ground-truth songs.

    python eval/sweep.py [--quick]

Re-runs ONLY the align stage (--stage align, ~4s/song on GPU) per configuration,
using the cached vocal stems in out/<id>.vocals.wav — Demucs never re-runs.
Scores each (blend, lead) config on every song in eval/gt/ that has a stem +
lyrics, using the structural-miss-robust scorer in compare.py. Prints the grid
sorted by objective (mean median + 0.5 * mean p90 across songs) plus a
per-song breakdown of the winner vs the current defaults.
"""
import argparse, itertools, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import compare  # noqa: E402

BLENDS = [0.0, 0.15, 0.3, 0.45, 0.55, 0.7, 0.85, 1.0]
LEADS = [0.0, 0.06, 0.12, 0.18, 0.25]
DEFAULT = (0.55, 0.12)


def songs():
    out = []
    for f in sorted(os.listdir(os.path.join(HERE, "gt"))):
        if not f.endswith(".ttml"):
            continue
        sid = f[:-5]
        stem = os.path.join(ROOT, "out", sid + ".vocals.wav")
        lyr = os.path.join(ROOT, "out", sid + ".txt")
        if os.path.exists(stem) and os.path.exists(lyr):
            out.append((sid, stem, lyr, os.path.join(HERE, "gt", f)))
    return out


def run_config(sid, stem, lyr, blend, lead, tmp):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "align_lyrics.py"),
                        "--stage", "align", "--vocals", stem, "--lyrics", lyr,
                        "--out", tmp, "--blend", str(blend), "--lead", str(lead)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ! {sid} blend={blend} lead={lead} failed: {r.stderr[-200:]}")
        return None
    return compare.score(compare.load_ttml(tmp), compare.load_ttml(
        os.path.join(HERE, "gt", sid + ".ttml")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="coarse grid (for smoke tests)")
    a = ap.parse_args()
    blends = [0.3, 0.55, 0.85] if a.quick else BLENDS
    leads = [0.0, 0.12] if a.quick else LEADS

    ss = songs()
    print(f"{len(ss)} songs, {len(blends) * len(leads)} configs "
          f"-> {len(ss) * len(blends) * len(leads)} align runs\n")
    tmp = os.path.join(tempfile.gettempdir(), "sweep_tmp.ttml")

    results = {}      # (blend, lead) -> list of per-song score dicts
    per_song = {}     # (blend, lead, sid) -> score
    for blend, lead in itertools.product(blends, leads):
        scores = []
        for sid, stem, lyr, _gt in ss:
            s = run_config(sid, stem, lyr, blend, lead, tmp)
            if s:
                scores.append(s)
                per_song[(blend, lead, sid)] = s
        if scores:
            results[(blend, lead)] = scores
            m = sum(x["median"] for x in scores) / len(scores)
            p = sum(x["p90"] for x in scores) / len(scores)
            print(f"blend={blend:.2f} lead={lead:.2f}  avg-median {1000*m:4.0f}ms  "
                  f"avg-p90 {1000*p:4.0f}ms  obj {1000*(m + 0.5*p):4.0f}  "
                  f"struct {sum(x['struct'] for x in scores)}", flush=True)

    def obj(cfg):
        sc = results[cfg]
        return (sum(x["median"] for x in sc) + 0.5 * sum(x["p90"] for x in sc)) / len(sc)

    ranked = sorted(results, key=obj)
    print("\n=== top 5 configs ===")
    for cfg in ranked[:5]:
        sc = results[cfg]
        print(f"blend={cfg[0]:.2f} lead={cfg[1]:.2f}  obj {1000*obj(cfg):.0f}  "
              f"avg-median {1000*sum(x['median'] for x in sc)/len(sc):.0f}ms  "
              f"avg-p90 {1000*sum(x['p90'] for x in sc)/len(sc):.0f}ms")

    best = ranked[0]
    print(f"\n=== winner blend={best[0]:.2f} lead={best[1]:.2f} vs "
          f"default blend={DEFAULT[0]} lead={DEFAULT[1]}, per song ===")
    for sid, *_ in ss:
        b, d = per_song.get((*best, sid)), per_song.get((*DEFAULT, sid))
        if b and d:
            print(f"{sid}  median {1000*d['median']:4.0f} -> {1000*b['median']:4.0f}ms   "
                  f"p90 {1000*d['p90']:4.0f} -> {1000*b['p90']:4.0f}ms   "
                  f"bias {1000*d['bias']:+5.0f} -> {1000*b['bias']:+5.0f}ms")

    # leave-one-out: does the winner stay top-3 with each song dropped?
    print("\n=== leave-one-out stability of winner ===")
    for drop, *_ in ss:
        def obj_wo(cfg):
            sc = [per_song[(*cfg, sid)] for sid, *_ in ss
                  if sid != drop and (*cfg, sid) in per_song]
            return (sum(x["median"] for x in sc) + 0.5 * sum(x["p90"] for x in sc)) / len(sc)
        rank = sorted(results, key=obj_wo).index(best) + 1
        print(f"without {drop}: winner ranks #{rank}")


if __name__ == "__main__":
    main()
