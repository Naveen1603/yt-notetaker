# yt-notetaker

Generate structured study notes from a **YouTube playlist** using the **Gemini API**. The tool lists videos with `yt-dlp`, requests notes per video, writes **`notes_raw.md`**, then produces a single synthesized **`notes_comprehensive.md`**. Optional **Markdown → PDF** conversion is included (`md_to_pdf.py`).

## Requirements

- Python 3.10+
- [Gemini API key](https://aistudio.google.com/apikey) (Google AI Studio)
- Network access for YouTube and Google APIs

## Setup

```bash
git clone git@github-naveen1603.com:Naveen1603/yt-notetaker.git
cd yt-notetaker
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit **`.env`** and set `GEMINI_API_KEY` or `GOOGLE_API_KEY`. Optional: `GEMINI_MODEL`, `GEMINI_REQUEST_TIMEOUT`, `YT_PLAYLIST_URL`. If `.env` is missing here, a **parent directory** `.env` is used only for variables not already defined.

## Usage

Run from the `yt-notetaker` directory (or pass paths accordingly):

```bash
python youtube_playlist_gemini_notes.py "https://www.youtube.com/playlist?list=PLxxxx" --out-dir results/my_playlist
```

Use a **separate `--out-dir` per playlist** so outputs do not overwrite each other. Resume after interruption:

```bash
python youtube_playlist_gemini_notes.py "PLAYLIST_URL" --out-dir results/my_playlist --skip-existing
```

Regenerate only the final document from an existing raw file:

```bash
python youtube_playlist_gemini_notes.py --only-synthesize --out-dir results/my_playlist
```

**Common options:** `--model`, `--max-videos`, `--delay-seconds`, `--request-timeout-seconds`, `--synthesis-request-timeout-seconds`, `--special-instructions`. See `python youtube_playlist_gemini_notes.py --help`.

## Outputs

All paths are relative to **`--out-dir`** (default: `results/ytnotes` under this repo):

| File | Purpose |
|------|---------|
| `notes_raw.md` | Per-video sections |
| `notes_comprehensive.md` | Synthesized notes |
| `manifest.json` | Run metadata and per-video status |
| `errors.log` | Error-level logs |

Generated content and `.env` are gitignored.

## Customization

- **`prompt/`** — system/user templates for per-video calls and for synthesis (`{video_title}`, `{video_url}`, `{combined_raw}`).
- **`youtube_playlist_gemini_notes.py`** — default per-run instructions live in `SPECIAL_INSTRUCTIONS` in the `__main__` block; override with `--special-instructions` when needed.

**PDF:** `python md_to_pdf.py path/to/notes.md -o path/to/out.pdf` — use `--help` for layout options.
