#!/usr/bin/env python3
"""Chunked AutoLyrixAlign: align a song per line-window instead of whole-song.

    python ala_chunked.py <in.ttml> <audio> <out_aligned.txt> [--nus DIR] [--image IMG]

Reads line timings from an existing TTML (the MMS-blend output — its line-level
times are plenty accurate to place windows), groups lines into ~10-45s windows
cut at inter-line gaps, pads each window with acoustic context, and feeds them
to the NUS Kaldi recipe as one multi-utterance data dir (segments file). Kaldi
then aligns each small window independently: peak RAM stays low and the
whole-song lattice blowup (and its runaway-drift failure mode) can't happen.

The transcript is partitioned, never duplicated, so "stitching" is just adding
each window's start offset (done in lyric order by ConvertCTMSegments.py inside
the container). Output format is identical to RunAlignment.sh: "start end WORD"
lines in global song time, ready for ala_to_ttml.py.
"""
import argparse, html, os, re, shutil, subprocess, sys, threading, time

PBLK = re.compile(r'<p\b[^>]*?begin="([^"]+)"[^>]*?end="([^"]+)"[^>]*>(.*?)</p>', re.S)
SPAN = re.compile(r'<span begin="[^"]+" end="[^"]+">(.*?)</span>', re.S)

GAP_CUT = 2.0    # start a new window at an inter-line gap this big (s)
MIN_WIN = 10.0   # don't cut before a window is at least this long (s)
MAX_WIN = 45.0   # force a cut at the next >=0.6s gap beyond this length (s)
PAD = 1.5        # acoustic context added on each side of a window (s)


def t2s(x):
    p = x.split(":")
    return sum(float(v) * 60 ** i for i, v in enumerate(reversed(p)))


def clean_word(raw):
    """Port of NUS PreProcessing.GetLyrics word rules (must match its lexicon
    expectations): drop bracketed ad-libs, trailing ' -> g, 'cause -> cause,
    leading ' stripped, hyphen splits, uppercase."""
    w = html.unescape(re.sub("<[^>]+>", "", raw)).strip().lower()
    w = w.replace("’", "'")
    w = re.sub(r'[,\.!?"“”]', "", w)
    if not w or "(" in w or ")" in w or "[" in w or "]" in w:
        return []
    if w == "'cause":
        w = "cause"
    if w == "'head":
        w = "head"
    if w.endswith("'"):
        w = w[:-1] + "g"
    if w.startswith("'"):
        w = w[1:]
    out = []
    for part in w.replace("-", " ").split():
        part = re.sub(r"[^a-z0-9']", "", part)
        if part:
            out.append(part.upper())
    return out


def parse_lines(ttml_path):
    txt = open(ttml_path, encoding="utf-8").read()
    lines = []
    for b, e, inner in PBLK.findall(txt):
        words = [w for span in SPAN.findall(inner) for w in clean_word(span)]
        if words:
            lines.append((t2s(b), t2s(e), words))
    if not lines:
        sys.exit("no timed lines found in " + ttml_path)
    return lines


def build_windows(lines, song_dur):
    groups, cur = [], [lines[0]]
    for ln in lines[1:]:
        gap = ln[0] - cur[-1][1]
        dur = cur[-1][1] - cur[0][0]
        if (gap >= GAP_CUT and dur >= MIN_WIN) or (dur >= MAX_WIN and gap >= 0.6):
            groups.append(cur)
            cur = [ln]
        else:
            cur.append(ln)
    groups.append(cur)
    wins = []
    for g in groups:
        start = max(0.0, g[0][0] - PAD)
        end = min(song_dur, g[-1][1] + PAD)
        words = [w for _b, _e, ws in g for w in ws]
        wins.append((start, end, words))
    return wins


def ffprobe_dur(audio):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", audio], capture_output=True, text=True)
    return float(out.stdout.strip())


def lf_write(path, lines):
    with open(path, "w", encoding="ascii", newline="\n") as f:
        f.writelines(l + "\n" for l in lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ttml"); ap.add_argument("audio"); ap.add_argument("out")
    ap.add_argument("--nus", default=r"D:\autolyrixalign\NUSAutoLyrixAlign")
    ap.add_argument("--image", default="kaczmarj/apptainer:latest")
    a = ap.parse_args()

    reco = re.sub(r"[^A-Za-z0-9]", "", os.path.splitext(os.path.basename(a.ttml))[0]) or "song"
    song_dur = ffprobe_dur(a.audio)
    lines = parse_lines(a.ttml)
    wins = build_windows(lines, song_dur)
    nwords = sum(len(w) for _s, _e, w in wins)
    print(f"[chunk] {len(lines)} lines -> {len(wins)} windows, {nwords} words, "
          f"{sum(e - s for s, e, _ in wins):.0f}s of {song_dur:.0f}s audio")
    for i, (s, e, w) in enumerate(wins):
        print(f"  u{i:04d}  {s:7.2f} - {e:7.2f}  ({e - s:5.1f}s, {len(w):3d} words)")

    wavdir = os.path.join(a.nus, "wavfiles")
    os.makedirs(wavdir, exist_ok=True)
    wav = os.path.join(wavdir, reco + ".wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", a.audio,
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav], check=True)

    dt = os.path.join(a.nus, "data", "test")
    shutil.rmtree(dt, ignore_errors=True)
    os.makedirs(dt)
    utts = [f"{reco}-u{i:04d}" for i in range(len(wins))]
    lf_write(os.path.join(dt, "wav.scp"), [f"{reco} wavfiles/{reco}.wav"])
    lf_write(os.path.join(dt, "segments"),
             [f"{u} {reco} {s:.2f} {e:.2f}" for u, (s, e, _w) in zip(utts, wins)])
    lf_write(os.path.join(dt, "text"),
             [f"{u} {' '.join(w)}" for u, (_s, _e, w) in zip(utts, wins)])
    lf_write(os.path.join(dt, "utt2spk"), [f"{u} {reco}" for u in utts])
    lf_write(os.path.join(dt, "spk2utt"), [f"{reco} {' '.join(utts)}"])

    # peak container RAM, sampled while the alignment runs
    peak = [0.0]
    def poll():
        while not done.is_set():
            r = subprocess.run(["docker", "stats", "--no-stream", "--format",
                                "{{.MemUsage}}", "ala_chunked"], capture_output=True, text=True)
            m = re.match(r"([\d.]+)(KiB|MiB|GiB)", r.stdout.strip())
            if m:
                mem = float(m.group(1)) * {"KiB": 2**-20, "MiB": 2**-10, "GiB": 1}[m.group(2)]
                peak[0] = max(peak[0], mem)
            done.wait(2)
    done = threading.Event()
    threading.Thread(target=poll, daemon=True).start()

    t0 = time.time()
    r = subprocess.run(["docker", "run", "--rm", "--privileged", "--name", "ala_chunked",
                        "-v", f"{a.nus}:/work", "-w", "/work", a.image,
                        "exec", "kaldi.simg", "bash", "./RunAlignmentSeg.sh"])
    done.set()
    elapsed = time.time() - t0
    if r.returncode != 0:
        sys.exit(f"container failed (exit {r.returncode})")

    src = os.path.join(a.nus, "AlignedLyricsOutput", "alignedoutput.txt")
    shutil.copyfile(src, a.out)
    got = sum(1 for _ in open(a.out))
    print(f"[done] {got}/{nwords} words aligned in {elapsed:.0f}s, "
          f"peak container RAM {peak[0]:.1f} GiB -> {a.out}")


if __name__ == "__main__":
    main()
