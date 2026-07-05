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


# -------------------------------------------------- vocal-onset utilities (A + B)
def rms_envelope(voc16, hop=320):
    """RMS energy of the 16k mono vocal stem at ~50 fps -> (tensor, hop_seconds)."""
    v = voc16[0].abs()
    nf = v.numel() // hop
    if nf == 0:
        return torch.zeros(0), hop / 16000.0
    return v[:nf * hop].reshape(nf, hop).pow(2).mean(1).sqrt(), hop / 16000.0


def onset_times(rms, hop_s, rel=0.12):
    """Candidate vocal onsets: local peaks of positive energy rise above a relative floor.
    A cheap stand-in for spectral-flux onset detection — enough to pin word starts to where
    the voice actually re-enters, which is what bounds fast-verse drift."""
    if rms.numel() < 3:
        return []
    peak = rms.max().item() or 1.0
    rise = (rms[1:] - rms[:-1]).clamp(min=0)
    out = []
    for i in range(1, rise.numel() - 1):
        if rms[i + 1] > rel * peak and rise[i] >= rise[i - 1] and rise[i] > rise[i + 1] and rise[i] > 0.03 * peak:
            out.append((i + 1) * hop_s)
    return out


def nearest_within(sorted_ts, t, lo, hi):
    """Nearest value in sorted_ts lying in [t+lo, t+hi], else None."""
    best, bd = None, 1e9
    for x in sorted_ts:
        if x < t + lo:
            continue
        if x > t + hi:
            break
        d = abs(x - t)
        if d < bd:
            best, bd = x, d
    return best


def no_overlap(word_times, items, min_dur=0.03):
    """Final safety: within each line's lead/background stream, clamp each word's END so it
    never runs past the next word's onset (kills the two-words-lit overlap). Onsets are left
    exactly where alignment/anchoring/snapping put them — no spreading, so nothing drifts."""
    streams = {}
    for i, it in enumerate(items):
        if word_times[i] is not None:
            streams.setdefault((it[0], it[3]), []).append(i)
    for idxs in streams.values():
        for a, b in zip(idxs, idxs[1:]):
            sa, ea = word_times[a]
            sb = word_times[b][0]
            word_times[a] = (sa, max(min(ea, sb), min(sa + min_dur, sb)))
        sa, ea = word_times[idxs[-1]]
        word_times[idxs[-1]] = (sa, max(ea, sa + min_dur))
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


def align_stage(vocals_path, lyrics_path, out, meta, device, lead=0.12, blend=0.55):
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

    rms, hop_s = rms_envelope(voc16)
    onsets = onset_times(rms, hop_s)
    peak = rms.max().item() if rms.numel() else 0.0

    if anchors:
        # (B) Global sync — LRC line stamps are often systematically a bit late/early vs the
        # actual vocal. Measure (nearest vocal onset − anchor) per line and shift EVERY anchor
        # by the median, so the whole song locks onto the singing instead of the tab's bias.
        devs = []
        for li in lids:
            A = anchors.get(li)
            if A is not None:
                o = nearest_within(onsets, A, -0.40, 0.40)
                if o is not None:
                    devs.append(o - A)
        gshift = 0.0
        if len(devs) >= 4:
            devs.sort()
            gshift = max(-0.35, min(0.35, devs[len(devs) // 2]))
            anchors = {li: A + gshift for li, A in anchors.items()}
        if abs(gshift) > 0.02:
            log(f"[align] global sync {gshift * 1000:+.0f}ms (median LRC->vocal of {len(devs)} lines)")

        # (A) Place each line against its human LRC stamp. When MMS looks sane for the line
        # (words in order, no implausible internal gap, fits the window) keep its relative
        # spacing — the accurate case. When MMS has fallen apart (fast outro: words 10-20s
        # apart, lines crossing over each other, the last word flung to the song's end) its
        # spacing is garbage, so lay the words out evenly across the line's window instead —
        # not perfectly synced, but readable and in order. Guardrails, not blind trust. Absolute
        # placement of a sane line is set by the stamp/MMS blend below (then a small global LEAD).
        audio_dur = voc16.size(1) / 16000.0

        # Where to land each sane line's first word. The human LRC stamp leads the vocal (tab
        # authors cue the line a beat early); MMS lands late (on the vowel, past the consonant).
        # The truth is between them, so translate the line so its first word sits a fraction
        # BLEND of the way from MMS's onset back toward the stamp (0 = pin hard to the stamp, the
        # old behaviour that dragged whole lines ~300 ms early; 1 = trust MMS as-is). Measured
        # against the hand-authored Blinding Lights reference, ~0.55 centres the error.
        BLEND = blend

        def natural(idxs):                                # rough sung length per word, by letters
            return [0.14 + 0.05 * (len(items[i][2]) or len(items[i][1]) or 1) for i in idxs]

        def even_layout(idxs, start, end):
            durs = natural(idxs); sc = max(0.1, end - start) / (sum(durs) or 1.0)
            t = start
            for i, d in zip(idxs, durs):
                w = d * sc
                word_times[i] = (t, t + w * 0.85)         # 15% gap between words
                t += w

        def active_frac(a, b):                            # fraction of [a,b] carrying vocal energy
            if rms.numel() == 0 or b <= a:
                return 0.0
            i0, i1 = max(0, int(a / hop_s)), min(rms.numel(), int(b / hop_s))
            return float((rms[i0:i1] > 0.12 * (peak or 1.0)).float().mean()) if i1 > i0 else 0.0

        sane = 0
        for k, li in enumerate(lids):
            A = anchors.get(li)
            if A is None:
                continue
            idxs, mids = line_all[li], line_main[li]
            nxtA = next((anchors[j] for j in lids[k + 1:] if j in anchors), None)
            W = (nxtA - 0.06) if nxtA is not None else None
            first = word_times[mids[0]][0]
            last = max(word_times[i][1] for i in idxs)
            # A big internal gap only means "MMS drifted" if it isn't a sustained held note
            # (energy, no re-attacks): held notes legitimately leave a long gap and must NOT
            # trigger an even re-lay, or the words after the note get spread far too early
            # (that was throwing "…ooohh, I'm blinded by the lights" ~2s early every chorus).
            drift_gap = 0.0
            for j in range(len(mids) - 1):
                ce2, ns2 = word_times[mids[j]][1], word_times[mids[j + 1]][0]
                if ns2 - ce2 <= 1.2:
                    continue
                ons_in = sum(1 for o in onsets if ce2 + 0.05 < o < ns2 - 0.05)
                if not (active_frac(ce2, ns2) > 0.6 and ons_in < 2):   # not a held note -> real drift
                    drift_gap = max(drift_gap, ns2 - ce2)
            avail = (W - A) if W is not None else None
            # A line re-lays (words spaced evenly across its window) when MMS's spacing is
            # untrustworthy: a catastrophic gap (10-20s fast-outro collapse) OR a line that
            # over-spreads its window (usually overlapping background vocals fooling MMS). A held
            # note ("oooh", a 2-3s gap of continuous energy) is NOT drift and must be kept, or the
            # words after it spread early (whole choruses). In the over-spread case MMS's onset is
            # corrupted, so the blend below must NOT trust it — even-relay anchors the first word to
            # the stamp instead, which is far closer.
            if drift_gap > 5.0 or (avail is not None and (last - first) > avail * 1.6):
                end = A + sum(natural(idxs)) * 1.6        # MMS drift -> even, readable-paced layout
                if W is not None:
                    end = min(end, W)
                even_layout(idxs, A, max(A + 0.2, min(end, audio_dur)))
            else:
                # Blend MMS's onset with the stamp, but CLAMP how far MMS is trusted to sit after
                # the stamp. A mild lag (up to ~0.7s: MMS lands on the vowel, past the consonant)
                # is real and worth following; a larger gap is MMS drift, not singing, and trusting
                # it would drag the whole line seconds late (FEAR "On the verge" went +2s). Cap the
                # trusted lag so drift lines fall back near the stamp instead.
                lag = max(-0.5, min(0.7, first - A))
                dev = (A + BLEND * lag) - first           # land first word at A + BLEND*lag
                for i in idxs:
                    a, b = word_times[i]
                    word_times[i] = (a + dev, b + dev)
                if W is not None and max(word_times[i][1] for i in idxs) > W:
                    s0 = word_times[mids[0]][0]
                    ln = max(word_times[i][1] for i in idxs)
                    sc = max(0.4, (W - s0) / max(0.05, ln - s0))
                    for i in idxs:
                        a, b = word_times[i]
                        word_times[i] = (s0 + (a - s0) * sc, s0 + (b - s0) * sc)
                # Internal-gap handling. MMS leaves two kinds of big gap, told apart by length:
                #  - SHORT (~0.5-1.8s): it over-held a word while the voice ARTICULATED the next
                #    words — pull the rest of the line earlier onto the vocal (FEAR "Knocking…at").
                #  - LONG (>1.8s of continuous energy): it labelled a HELD note ("oooh") at the
                #    END of the sustain — light that one word at the START of the sustain and let
                #    it hold, WITHOUT dragging the words after it (they're already right). Pulling
                #    across a held note was throwing whole choruses ~2s early.
                for j in range(len(mids) - 1):
                    ce, ns = word_times[mids[j]][1], word_times[mids[j + 1]][0]
                    gap = ns - ce
                    if gap <= 0.5:
                        continue
                    if gap <= 1.8 and active_frac(ce, ns) > 0.5 and \
                            sum(1 for o in onsets if ce + 0.05 < o < ns - 0.05) >= 2:
                        sh = gap - 0.5
                        for jj in range(j + 1, len(mids)):
                            a, b = word_times[mids[jj]]
                            word_times[mids[jj]] = (a - sh, b - sh)
                    elif gap > 1.8 and active_frac(ce, ns) > 0.4:
                        a, b = word_times[mids[j + 1]]
                        na = ce + 0.15
                        word_times[mids[j + 1]] = (na, max(b, na + 0.2))
                sane += 1
        log(f"[align] placed {len(lids)} line(s): {sane} kept MMS spacing, "
            f"{len(lids) - sane} re-laid (MMS drift)")

        # Line-1 intro guard: if the first word still sits well before the singing, slide the
        # whole first line onto the first sustained (160 ms) vocal onset.
        if lids and rms.numel() >= 8:
            thr, g = 0.12 * (peak or 1.0), None
            for i in range(rms.numel() - 8):
                if bool((rms[i:i + 8] > thr).all()):
                    g = i * hop_s
                    break
            s0 = word_times[line_main[lids[0]][0]][0]
            if g is not None and g > s0 + 0.5:
                d = g - s0
                for j in line_all[lids[0]]:
                    a, b = word_times[j]
                    word_times[j] = (a + d, b + d)
                log(f"[align] pushed line 1 to first vocal onset (+{d:.1f}s)")
    else:
        # No synced timing — snap lines that start in silence onto the next vocal onset.
        HOP = hop_s
        act = (rms > 0.12 * (peak or 1.0)).tolist() if rms.numel() else []
        nf = len(act)
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

    no_overlap(word_times, items)                         # remove any word overlaps; onsets untouched

    if lead:
        # Global lead: forced aligners (and hand LRC stamps) sit a touch late — MMS lands on
        # the vowel, past the consonant attack — so the highlight feels a beat behind, worst on
        # fast lines. Nudge every word earlier by `lead` seconds to cancel that systematic bias.
        for i, wt in enumerate(word_times):
            if wt is not None:
                ns = max(0.0, wt[0] - lead)
                word_times[i] = (ns, ns + (wt[1] - wt[0]))
        log(f"[align] applied global lead -{lead * 1000:.0f}ms")

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
    ap.add_argument("--lead", type=float, default=0.12,
                    help="shift all words earlier by this many seconds to cancel the aligner's late bias")
    ap.add_argument("--blend", type=float, default=0.55,
                    help="0=pin line starts to the LRC stamp, 1=trust MMS onset as-is")
    args = ap.parse_args()

    device = torch.device(args.device)
    log(f"[device] {device} ({torch.cuda.get_device_name(0) if device.type=='cuda' else 'cpu'})")
    meta = [("spotifyId", args.spotify_id), ("musicName", args.title),
            ("artists", args.artist), ("ttmlAuthorGithubLogin", "auto-aligned"),
            ("aligner", "v2-blend")]   # stamps the alignment version so viewers can label it

    if args.stage == "separate":
        separate_stage(args.audio, args.vocals, device)
    elif args.stage == "align":
        align_stage(args.vocals, args.lyrics, args.out, meta, device, args.lead, args.blend)
    else:  # all-in-one process (fine for short songs)
        vpath = args.vocals or tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
        separate_stage(args.audio, vpath, device)
        align_stage(vpath, args.lyrics, args.out, meta, device, args.lead, args.blend)


if __name__ == "__main__":
    main()
