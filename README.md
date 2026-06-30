# Lyric Converter

Turn a song's audio + its lyrics into **word-by-word time-synced karaoke** —
exported as **TTML** (word-level timing) plus a self-contained HTML viewer to check
the result. It separates the vocals (Demucs) and forced-aligns the words (MMS) on
your GPU.

Numbers are spelled out for alignment, and lines in other scripts (e.g. Korean) are
kept and timed by interpolation instead of being dropped.

## Requirements

- **Python 3.10+**
- **ffmpeg** (and `ffprobe`) on your PATH — see `requirements.txt` for install commands
- **PyTorch + torchaudio** — a CUDA build is strongly recommended (CPU works, just slow).
  Get the right command for your GPU at <https://pytorch.org/get-started/locally/>.
- Model weights download automatically on first run (~1.5 GB) and are cached after.

```bash
pip install -r requirements.txt
```

## Run the web app

```bash
python server.py
```

Then open <http://127.0.0.1:8770>. Upload a song file, give it a Spotify track ID
and/or a "song + artist" search (or paste the lyrics yourself), and it generates the
karaoke page. Results land in `out/` (`<id>.ttml` + `<id>.html`).

## Command line (one song)

```bash
python lyrics.py "C:\path\to\song.mp3" --search "Song Name Artist" --id <spotifyTrackId>
```

Useful flags:

| Flag | Meaning |
|---|---|
| `--search "text"` | what to look up on [LRCLIB](https://lrclib.net) (song + artist works best) |
| `--lyrics file.txt` | use your own lyrics instead of fetching them |
| `--id <id>` | Spotify track id — names the `.ttml` output |
| `--title` / `--artist` | override the display name |
| `--no-open` | don't auto-open the browser |

## How it works

1. **Lyrics** — fetched from LRCLIB (synced `[mm:ss]` lines preferred) or supplied directly.
2. **Separate** — Hybrid-Demucs isolates the vocal stem.
3. **Align** — the MMS forced aligner places each word in time; LRCLIB line timestamps,
   when present, anchor each line so words never drift into instrumental gaps.
4. **View** — a self-contained HTML karaoke page is written next to the `.ttml`.

## Files

| File | Purpose |
|---|---|
| `server.py` | the web app (upload → karaoke) |
| `lyrics.py` | one-command CLI wrapper |
| `align_lyrics.py` | vocal separation + forced alignment → TTML |
| `make_view.py` | TTML → self-contained karaoke HTML |
| `retrim_existing.py` | re-tidy timing on already-generated TTML |

## Notes

- Text in `(parentheses)` / `[brackets]` is treated as background vocals (ad-libs) and
  shown separately.
- Auto-alignment isn't perfect: fast/dense vocals can drift a beat then re-sync, and
  non-Latin lines are timed approximately. A manual nudge pass cleans those up.
- Talks only to LRCLIB and Spotify's public embed page — no API keys required.

## Contributing

PRs welcome — this is a small hobby tool and there's plenty to improve (better timing,
romanized alignment for non-Latin lyrics, a nicer UI). Open an issue or PR.

## License

MIT
