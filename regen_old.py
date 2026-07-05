#!/usr/bin/env python3
"""Regenerate every song made before the current pipeline (its TTML has no
aligner tag) through the same flow the Generate tab uses: lyrics.py with the
song's stored audio (out/<id>.mp3) and stored lyrics (out/<id>.txt).

    python regen_old.py --limit 5      # do the next 5 pending songs, then stop
    python regen_old.py                # do everything pending

Already-regenerated songs (TTML has an aligner tag) are skipped, so re-running
continues where the last chunk stopped. A cooldown between songs lets VRAM
settle so the align stage doesn't fight the previous song's memory.
"""
import argparse, glob, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
PY = sys.executable
AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".opus", ".ogg", ".flac")


def aligner_tag(tp):
    head = open(tp, encoding="utf-8").read(4000)
    m = re.search(r'key="aligner" value="([^"]*)"', head)
    return m.group(1) if m else ""


def audio_for(base):
    for ext in AUDIO_EXTS:
        p = os.path.join(OUT, base + ext)
        if os.path.exists(p):
            return p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="regenerate at most this many songs, then stop (0 = all)")
    ap.add_argument("--cooldown", type=float, default=10.0,
                    help="seconds to wait between songs so VRAM can settle")
    a = ap.parse_args()

    todo = []
    for tp in sorted(glob.glob(os.path.join(OUT, "*.ttml"))):
        base = os.path.splitext(os.path.basename(tp))[0]
        if "." in base or aligner_tag(tp):
            continue                     # variant copy, or already current gen
        audio = audio_for(base)
        lyr = os.path.join(OUT, base + ".txt")
        if audio and os.path.exists(lyr):
            todo.append((base, audio, lyr))
        else:
            print(f"skip {base}: missing {'audio' if not audio else 'lyrics'}", flush=True)

    remaining = len(todo)
    if a.limit:
        todo = todo[:a.limit]
    print(f"regenerating {len(todo)} of {remaining} pending songs", flush=True)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    fails = []
    t_all = time.time()
    for i, (base, audio, lyr) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {base}", flush=True)
        t0 = time.time()
        r = subprocess.run([PY, os.path.join(HERE, "lyrics.py"), audio,
                            "--lyrics", lyr, "--id", base,
                            "--outdir", OUT, "--no-open"], env=env)
        ok = r.returncode == 0
        if not ok:
            fails.append(base)
        print(f"    {'ok' if ok else 'FAILED'} in {time.time() - t0:.0f}s", flush=True)
        if i < len(todo):
            time.sleep(a.cooldown)

    print(f"\nbatch done in {(time.time() - t_all) / 60:.0f} min: "
          f"{len(todo) - len(fails)} ok, {len(fails)} failed", flush=True)
    if fails:
        print("failed: " + ", ".join(fails), flush=True)


if __name__ == "__main__":
    main()
