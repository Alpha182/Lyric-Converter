#!/usr/bin/env python3
"""
Word-by-word lyric aligner -> AMLL-style TTML.

Pipeline:
  1. decode any audio to 44.1k stereo wav (ffmpeg)
  2. isolate the vocal stem (torchaudio Hybrid-Demucs)
  3. forced-align the known lyrics to the vocals (torchaudio MMS_FA)
  4. emit <p>/<span begin/end> TTML the Spotify mod already parses

Usage:
  python align_lyrics.py --audio song.mp3 --lyrics lyrics.txt \
      --out 7mykoq6R3BArsSpNDjFQTm.ttml \
      --spotify-id 7mykoq6R3BArsSpNDjFQTm \
      --title "Song" --artist "Artist"
"""
import argparse, os, re, sys, subprocess, tempfile, html, wave

# NB: do NOT use expandable_segments on Windows — its low-level CUDA VMM path is rejected
# by WDDM and throws spurious OOMs (fails a 90 MB alloc with 14 GB free). Separate processes
# + CPU-side accumulation already keep fragmentation low.
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "garbage_collection_threshold:0.8")

import torch
import torchaudio
from torchaudio.transforms import Fade


def log(*a): print(*a, file=sys.stderr, flush=True)


def save_wav(path, mono, sr):
    """Write a (1, N) or (N,) float tensor to a 16-bit PCM wav via stdlib (no numpy)."""
    import array
    x = mono.reshape(-1).clamp(-1, 1)
    ints = (x * 32767.0).round().to(torch.int16).cpu().tolist()
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(array.array("h", ints).tobytes())


# ---------------------------------------------------------------- audio io
def decode_to_wav(src, sr=44100):
    """Use ffmpeg to get a clean 44.1k stereo 16-bit PCM wav regardless of input format."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-ar", str(sr), "-ac", "2",
         "-acodec", "pcm_s16le", tmp],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return tmp


def load_wav(path):
    """Read a 16-bit PCM wav via stdlib (avoids torchaudio's torchcodec dependency)."""
    with wave.open(path, "rb") as w:
        ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise RuntimeError(f"expected 16-bit PCM, got sampwidth={sw}")
    t = torch.frombuffer(bytearray(raw), dtype=torch.int16).float() / 32768.0
    return t.view(-1, ch).t().contiguous(), sr  # (channels, length), sr


# ------------------------------------------------------- source separation
def separate_vocals(model, mix, sample_rate, device, segment=10.0, overlap=0.1):
    """Chunked Hybrid-Demucs separation (official torchaudio recipe). `mix` stays on the
    CPU; only one chunk at a time is moved to the GPU and the result is accumulated back
    on the CPU. Peak VRAM is just model + one chunk, so it survives a busy desktop GPU
    (browsers/games can hold 8+ GB and the full-length output won't fit alongside)."""
    batch, channels, length = mix.shape
    chunk_len = int(sample_rate * segment * (1 + overlap))
    overlap_frames = int(overlap * sample_rate)
    fade = Fade(fade_in_len=0, fade_out_len=overlap_frames, fade_shape="linear")
    final = torch.zeros(batch, len(model.sources), channels, length)  # CPU accumulator
    start, end = 0, chunk_len
    while start < length - overlap_frames:
        chunk = mix[:, :, start:end].to(device)
        with torch.no_grad():
            out = model.forward(chunk).cpu()
        del chunk
        out = fade(out)
        final[:, :, :, start:end] += out
        if start == 0:
            fade.fade_in_len = overlap_frames
            start += chunk_len - overlap_frames
        else:
            start += chunk_len
        end += chunk_len
        if end >= length:
            fade.fade_out_len = 0
    if device.type == "cuda":
        torch.cuda.empty_cache()
    vocals_idx = model.sources.index("vocals")
    return final[:, vocals_idx]  # (batch, channels, length) on CPU


# ------------------------------------------------------------ lyric parsing
WORD_RE = re.compile(r"[^a-z']")
BRK = re.compile(r"[(\[][^)\]]*[)\]]")  # a parenthetical chunk: (...) or [...]

# Numbers are sung as words, so spell them for the (Latin-only) aligner while the
# original digits stay in the display. Otherwise "40" / "1999" get stripped to nothing
# and silently dropped, desyncing the rest of the line.
_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
         "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
         "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

def _below_1000(n):
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    return _ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + _below_1000(n % 100))

def num_to_words(s):
    try:
        n = int(s)
    except ValueError:
        return s
    if n == 0:
        return "zero"
    if n > 9999:                       # long ids / phone numbers: read digit by digit
        return " ".join(_ONES[int(d)] for d in s)
    parts = []
    if n >= 1000:
        parts.append(_below_1000(n // 1000) + " thousand")
        n %= 1000
    if n:
        parts.append(_below_1000(n))
    return " ".join(parts)

def alignment_norm(disp):
    """The Latin a-z' token used to forced-align a display word. Digits are spelled out;
    returns '' for words the aligner can't handle (e.g. Korean/Japanese script), which the
    caller keeps for display and times by interpolation instead of dropping."""
    t = re.sub(r"\d+", lambda m: " " + num_to_words(m.group()) + " ", disp.lower())
    return WORD_RE.sub("", t)

def load_lines(text):
    """Split lyrics into lines; drop blanks and whole-line [section] markers.
    Recognises LRC '[mm:ss.xx] text' lines and returns (texts, anchors), where
    anchors[i] is that line's start time in seconds (absent if no timestamp)."""
    LRC = re.compile(r"^((?:\[\d+:\d+(?:\.\d+)?\])+)\s*(.*)$")
    texts, anchors = [], {}
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        m = LRC.match(s)
        if m:
            body = m.group(2).strip()
            if not body or re.fullmatch(r"[\[(].*[\])]", body):
                continue                                   # instrumental / section marker
            tm = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\]", m.group(1))
            anchors[len(texts)] = int(tm.group(1)) * 60 + float(tm.group(2))
            texts.append(body)
        elif not re.fullmatch(r"[\[(].*[\])]", s):
            texts.append(s)
    return texts, anchors

def line_words(line):
    """Split one line into (display, is_bg) words. Words inside (...) or [...] are
    flagged as background vocals (ad-libs)."""
    spans = [(m.start(), m.end()) for m in BRK.finditer(line)]
    in_brk = lambda p: any(s <= p < e for s, e in spans)
    out = []
    for m in re.finditer(r"\S+", line):
        bg = in_brk(m.start())
        disp = re.sub(r"[()\[\]]", "", m.group()) if bg else m.group()
        if disp:
            out.append((disp, bg))
    return out

def tokenize(lines):
    """-> (flat_norm_words, items) where items=[(line_idx, display, norm, is_bg)].
    Every display word is kept; `norm` is '' for words the aligner can't handle (non-Latin
    script) — those are timed by interpolation later rather than dropped, so they still show
    and don't shift the rest of the line. `flat` lists only the alignable (norm != '') words."""
    items = []
    for li, line in enumerate(lines):
        for disp, bg in line_words(line):
            items.append((li, disp, alignment_norm(disp), bg))
    flat = [it[2] for it in items if it[2]]
    return flat, items


def trim_holds(word_times, items):
    """Forced alignment lets a word's END absorb a following pause: its last phoneme
    self-loops across the blank frames until the next word, so the karaoke highlight
    *crawls* across that word (e.g. "goes" lit for 1.3s in a fast rap line). The onset
    is reliable; only the tail is bogus. Cap each word's lit duration to a length
    proportional to its letters, so it lights up crisply and then simply holds
    (fully lit) through the gap until the next word starts."""
    for i, it in enumerate(items):
        ws, we = word_times[i]
        L = len(it[2]) or len(it[1])                # non-Latin words have no norm; use display len
        cap = min(1.0, 0.22 + 0.08 * L)             # ~0.54s for "goes", ~0.94s for "blueberry"
        if we - ws > cap:
            word_times[i] = (ws, ws + cap)
    return word_times


def fill_unaligned(word_times, items):
    """Words with no alignment text (non-Latin script the MMS aligner can't tokenize) come
    back with word_times[i] == None. Spread the gap between the nearest aligned neighbours so
    they still display and stay roughly in sync, instead of being dropped."""
    n = len(items)
    i = 0
    while i < n:
        if word_times[i] is not None:
            i += 1
            continue
        j = i
        while j < n and word_times[j] is None:
            j += 1
        run = j - i
        prev_end = word_times[i - 1][1] if i > 0 and word_times[i - 1] else None
        next_start = word_times[j][0] if j < n and word_times[j] else None
        lo = prev_end if prev_end is not None else (next_start - 0.3 * (run + 1) if next_start is not None else 0.0)
        hi = next_start if next_start is not None else (prev_end + 0.3 * (run + 1) if prev_end is not None else 0.05)
        if hi <= lo:
            hi = lo + 0.05 * (run + 1)
        step = (hi - lo) / (run + 1)
        for m, k in enumerate(range(i, j)):
            ws = lo + step * (m + 1)
            word_times[k] = (ws, min(ws + step, hi))
        i = j
    return word_times


# --------------------------------------------------------------- ttml emit
def fmt(t):
    t = max(0.0, t)
    m = int(t // 60); s = t - 60 * m
    return f"{m:02d}:{s:06.3f}"

def build_ttml(lines, word_times, items, meta):
    """word_times[i] = (start,end) for items[i]. Group back into <p> per source line,
    emitting bracketed ad-libs as a nested <span ttm:role="x-bg"> background group."""
    # bucket words by line, split into main vocal vs background (ad-lib)
    per_line = {}
    for (li, disp, _, bg), (ws, we) in zip(items, word_times):
        d = per_line.setdefault(li, {"main": [], "bg": []})
        d["bg" if bg else "main"].append((disp, ws, we))

    allw = [w for d in per_line.values() for w in (d["main"] + d["bg"])]
    body_start = min((w[1] for w in allw), default=0.0)
    body_end = max((w[2] for w in allw), default=0.0)

    span = lambda disp, ws, we: f'<span begin="{fmt(ws)}" end="{fmt(we)}">{html.escape(disp)}</span>'
    meta_xml = "".join(
        f'<amll:meta key="{html.escape(k)}" value="{html.escape(v)}"/>'
        for k, v in meta if v
    )
    ps = []
    for li in sorted(per_line):
        main, bg = per_line[li]["main"], per_line[li]["bg"]
        words = main + bg
        if not words:
            continue
        p_start = min(w[1] for w in words)
        p_end = max(w[2] for w in words)
        inner = " ".join(span(*w) for w in main)
        if bg:
            inner += '<span ttm:role="x-bg">' + " ".join(span(*w) for w in bg) + "</span>"
        ps.append(f'<p begin="{fmt(p_start)}" end="{fmt(p_end)}" ttm:agent="v1">{inner}</p>')

    return (
        '<tt xmlns="http://www.w3.org/ns/ttml" '
        'xmlns:ttm="http://www.w3.org/ns/ttml#metadata" '
        'xmlns:itunes="http://music.apple.com/lyric-ttml-internal" '
        'xmlns:amll="http://www.example.com/ns/amll">'
        f'<head><metadata><ttm:agent type="person" xml:id="v1"/>{meta_xml}</metadata></head>'
        f'<body dur="{fmt(body_end)}"><div begin="{fmt(body_start)}" end="{fmt(body_end)}">'
        + "".join(ps) +
        "</div></body></tt>"
    )


# ----------------------------------------------------------------- stages
def separate_stage(audio, vocals_out, device):
    """Decode -> Hybrid-Demucs vocal stem -> 16k mono wav written to vocals_out."""
    wav_path = decode_to_wav(audio)
    waveform, sr = load_wav(wav_path)
    log(f"[audio] {waveform.shape[1]/sr:.1f}s @ {sr}Hz")
    log("[separate] loading Hybrid-Demucs ...")
    bundle = torchaudio.pipelines.HDEMUCS_HIGH_MUSDB_PLUS
    mix = waveform                      # stays on CPU; chunks stream to the GPU
    ref = mix.mean(0)
    mix = (mix - ref.mean()) / ref.std()

    def run(dev, seg):
        demucs = bundle.get_model().to(dev)
        try:
            return separate_vocals(demucs, mix[None], bundle.sample_rate, dev, segment=seg)[0]
        finally:
            del demucs
            if dev.type == "cuda":
                torch.cuda.empty_cache()

    # Try the GPU with a small chunk (low peak VRAM). Windows/WDDM can refuse a GPU
    # allocation even with plenty "free" when other apps churn VRAM, so on any GPU
    # error fall back to CPU separation — slower but it always finishes.
    try:
        log("[separate] running on GPU ...")
        vocals = run(device, 6.0) if device.type == "cuda" else run(device, 10.0)
    except Exception as ex:
        if device.type != "cuda":
            raise
        log(f"[separate] GPU failed ({type(ex).__name__}: {str(ex).splitlines()[0]}); "
            f"falling back to CPU — this is slower, hang tight ...")
        vocals = run(torch.device("cpu"), 10.0)
    vocals = vocals * ref.std() + ref.mean()
    voc16 = torchaudio.functional.resample(vocals.mean(0, keepdim=True).cpu(), sr, 16000)
    save_wav(vocals_out, voc16, 16000)
    log(f"[separate] wrote vocals -> {vocals_out}")
    try: os.remove(wav_path)
    except OSError: pass


def align_stage(vocals_path, lyrics_path, out, meta, device):
    """Forced-align lyrics to a 16k vocal wav and write TTML."""
    lines, anchors = load_lines(open(lyrics_path, encoding="utf-8").read())
    flat, items = tokenize(lines)
    log(f"[lyrics] {len(lines)} lines, {len(items)} words ({len(flat)} alignable, "
        f"{len(items) - len(flat)} interpolated), {len(anchors)} synced anchors")

    voc16, sr = load_wav(vocals_path)            # (1, N)
    if sr != 16000:
        voc16 = torchaudio.functional.resample(voc16, sr, 16000)

    fa = torchaudio.pipelines.MMS_FA
    tokenizer, aligner = fa.get_tokenizer(), fa.get_aligner()

    def emit(dev):
        # Process 30s windows with 5s of overlapping context on each side, then keep
        # only the central frames. Each kept frame had real context, so there are no
        # garbled chunk boundaries (which previously broke alignment at 30s/60s/...).
        model = fa.get_model().to(dev)
        N, WIN, CTX = voc16.size(1), 30 * 16000, 5 * 16000
        keep = []
        with torch.inference_mode():
            s = 0
            while s < N:
                e = min(N, s + WIN)
                p0, p1 = max(0, s - CTX), min(N, e + CTX)
                em = model(voc16[:, p0:p1].to(dev))[0][0].float().cpu()   # (frames, tokens)
                fps = em.size(0) / (p1 - p0)
                a, b = int(round((s - p0) * fps)), int(round((e - p0) * fps))
                keep.append(em[a:b])
                s = e
        del model
        if dev.type == "cuda":
            torch.cuda.empty_cache()
        return torch.cat(keep, dim=0).unsqueeze(0)   # (1, total_frames, tokens)

    try:
        log("[align] computing emission (30s chunks) ...")
        emission = emit(device)
    except RuntimeError as ex:
        log(f"[align] {type(ex).__name__} on {device}; retrying on CPU")
        emission = emit(torch.device("cpu"))

    ratio = voc16.size(1) / emission.size(1) / 16000.0   # seconds per emission frame
    to_times = lambda spans: [(s[0].start * ratio, max(s[-1].end * ratio, s[0].start * ratio + 0.05))
                              for s in spans]

    # Only words with alignment text (norm != '') go to the aligner; non-Latin words are
    # filled in afterwards by interpolation. Position map lets the bg pass index back.
    align_idx = [i for i, it in enumerate(items) if it[2]]
    align_pos = {i: k for k, i in enumerate(align_idx)}

    # Align the MAIN vocal on its own — excluding bracketed ad-libs from the token
    # sequence stops them dragging the main line's timing around (reduces drift).
    main_idx = [i for i in align_idx if not items[i][3]]
    with torch.inference_mode():
        main_spans = aligner(emission[0], tokenizer([items[i][2] for i in main_idx]))
    word_times = [None] * len(items)
    mt = to_times(main_spans)
    for k, i in enumerate(main_idx):
        word_times[i] = mt[k]

    # Background ad-libs get their timing from a full-sequence pass (same emission).
    bg_idx = [i for i in align_idx if items[i][3]]
    if bg_idx:
        with torch.inference_mode():
            all_times = to_times(aligner(emission[0], tokenizer(flat)))
        for i in bg_idx:
            word_times[i] = all_times[align_pos[i]]

    # Non-Latin words (Korean/Japanese/... the aligner can't tokenize) had no alignment text;
    # give them timing interpolated from their aligned neighbours so they still display.
    fill_unaligned(word_times, items)

    # Kill the "one word absorbs the pause and the highlight crawls" artifact before
    # we anchor (so an over-long word can't inflate a line's span and trigger spurious
    # compression of the whole line).
    trim_holds(word_times, items)

    # group word indices per line
    line_all, line_main = {}, {}
    for i, it in enumerate(items):
        line_all.setdefault(it[0], []).append(i)
        if not it[3]:
            line_main.setdefault(it[0], []).append(i)
    lids = sorted(line_main)

    if anchors:
        # Anchor each line's start to its human LRC timestamp (keeping the MMS word
        # spacing within the line, scaled to fit before the next line). Human line
        # timing means lines never land in instrumental gaps — kills the "falses".
        anc = 0
        for k, li in enumerate(lids):
            A = anchors.get(li)
            if A is None:
                continue
            idxs = line_all[li]
            cur = word_times[line_main[li][0]][0]
            span = max(0.05, max(word_times[i][1] for i in idxs) - cur)
            nxtA = next((anchors[j] for j in lids[k + 1:] if j in anchors), None)
            scale = 1.0
            if nxtA is not None and A + span > nxtA - 0.05:
                scale = max(0.0, nxtA - 0.05 - A) / span
            for i in idxs:
                a, b = word_times[i]
                word_times[i] = (A + (a - cur) * scale, A + (b - cur) * scale)
            anc += 1
        log(f"[align] anchored {anc} line(s) to LRCLIB synced timing")

        # Safety net: if the reference put line 1 in the intro (before any singing),
        # push the whole first line to the first sustained vocal onset in the stem.
        if lids:
            v = voc16[0].abs(); nf = v.numel() // 320
            if nf:
                rms = v[:nf * 320].reshape(nf, 320).pow(2).mean(1).sqrt()
                act = (rms > 0.12 * (rms.max().item() or 1.0)).tolist()
                g, i = None, 0
                while i < nf - 8:
                    if all(act[i:i + 8]):           # 160ms of continuous voice
                        g = i * 0.02; break
                    i += 1
                idxs = line_all[lids[0]]
                s0 = word_times[line_main[lids[0]][0]][0]
                if g is not None and g > s0 + 0.5:
                    d = g - s0
                    for j in idxs:
                        a, b = word_times[j]
                        word_times[j] = (a + d, b + d)
                    log(f"[align] pushed line 1 to first vocal onset (+{d:.1f}s)")
    else:
        # No synced timing available — fall back to snapping lines that start in
        # silence onto the next vocal onset (less reliable; uses the stem's energy).
        HOP = 0.02
        v = voc16[0].abs()
        nf = v.numel() // 320
        if nf:
            rms = v[:nf * 320].reshape(nf, 320).pow(2).mean(1).sqrt()
            thr = 0.12 * (rms.max().item() or 1.0)
            act = (rms > thr).tolist()
            is_active = lambda t: 0 <= int(t / HOP) < nf and act[int(t / HOP)]

            def next_onset(t):
                i = max(0, int(t / HOP))
                while i < nf - 2:
                    if act[i] and act[i + 1] and act[i + 2]:
                        return i * HOP
                    i += 1
                return None

            moved = 0
            for k, li in enumerate(lids):
                idxs = line_all[li]
                s = word_times[line_main[li][0]][0]
                e = max(word_times[i][1] for i in idxs)
                if is_active(s):
                    continue
                o = next_onset(s)
                if o is None or o <= s + 0.15:
                    continue
                if k + 1 < len(lids):
                    ns = word_times[line_main[lids[k + 1]][0]][0]
                    bound = max(ns, next_onset(ns) or ns)
                else:
                    bound = e + 999
                delta = min(o - s, max(0.0, bound - e - 0.05))
                if delta > 0.15:
                    for i in idxs:
                        a, b = word_times[i]
                        word_times[i] = (a + delta, b + delta)
                    moved += 1
            log(f"[align] moved {moved} line(s) off silence onto the vocal onset")

    log(f"[align] {len(items)} words aligned ({len(bg_idx)} background) over "
        f"{max(t[1] for t in word_times):.1f}s")

    ttml = build_ttml(lines, word_times, items, meta)
    open(out, "w", encoding="utf-8").write(ttml)
    log(f"[done] wrote {out} ({len(ttml)} bytes)")


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["all", "separate", "align"], default="all")
    ap.add_argument("--audio")
    ap.add_argument("--vocals", help="16k mono vocal wav (separate writes it, align reads it)")
    ap.add_argument("--lyrics")
    ap.add_argument("--out")
    ap.add_argument("--spotify-id", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--artist", default="")
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = ap.parse_args()

    device = torch.device(args.device)
    log(f"[device] {device} ({torch.cuda.get_device_name(0) if device.type=='cuda' else 'cpu'})")
    meta = [("spotifyId", args.spotify_id), ("musicName", args.title),
            ("artists", args.artist), ("ttmlAuthorGithubLogin", "auto-aligned")]

    if args.stage == "separate":
        separate_stage(args.audio, args.vocals, device)
    elif args.stage == "align":
        align_stage(args.vocals, args.lyrics, args.out, meta, device)
    else:  # all-in-one process (fine for short songs)
        vpath = args.vocals or tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
        separate_stage(args.audio, vpath, device)
        align_stage(vpath, args.lyrics, args.out, meta, device)


if __name__ == "__main__":
    main()
