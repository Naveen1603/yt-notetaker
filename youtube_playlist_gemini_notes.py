#!/usr/bin/env python3
"""
Read all videos in a YouTube playlist, ask Gemini for structured notes per video,
then append raw notes (in playlist order) and produce one consolidated notes document.

Videos are processed **sequentially** in playlist order. After each successful Gemini
call, notes are **appended immediately** to ``notes_raw.md`` and ``manifest.json`` is
written so progress survives interruptions. ``finished_per_video_utc`` is set when
the per-video pass completes.

Requirements:
  - API keys in project `.env` (loaded automatically) or exported in the shell
  - pip install -r requirements.txt (``google.generativeai`` — legacy SDK, matches AI Studio snippets)
  - Prompts in `prompt/*.txt` (edit to tune behavior)
  - Optional: `GEMINI_REQUEST_TIMEOUT` (seconds) for ``RequestOptions`` timeouts on API calls

Example:
  python youtube_playlist_gemini_notes.py \\
    "https://www.youtube.com/playlist?list=PLxxxx"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import warnings

import google.api_core.exceptions as gcp_exceptions
import yt_dlp
from dotenv import load_dotenv

with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    import google.generativeai as genai
    from google.generativeai import protos
    from google.generativeai.types import RequestOptions

_PROJECT_ROOT = Path(__file__).resolve().parent
_PROMPT_DIR = _PROJECT_ROOT / "prompt"
DEFAULT_OUT_DIR = _PROJECT_ROOT / "results" / "ytnotes"
DEFAULT_MODEL = "gemini-2.5-flash"
# Per HTTP call (``RequestOptions``); overridable via ``--request-timeout-seconds`` / ``GEMINI_REQUEST_TIMEOUT``.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 480.0  # 8 minutes
# Total Gemini calls per operation (first try + retries on retryable errors).
DEFAULT_GENERATE_MAX_ATTEMPTS = 3  # first try + up to 2 retries on retryable errors

_LOG = logging.getLogger("yt_gemini_notes")


def _configure_run_logging(out_dir: Path) -> None:
    """stderr for INFO+; append ERROR+ with tracebacks to out_dir/errors.log."""
    if _LOG.handlers:
        return
    _LOG.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    _LOG.addHandler(sh)
    err_path = out_dir / "errors.log"
    fh = logging.FileHandler(err_path, encoding="utf-8")
    fh.setLevel(logging.ERROR)
    fh.setFormatter(fmt)
    _LOG.addHandler(fh)


def _load_env() -> None:
    """Load ``yt-notetaker/.env`` first; then parent ``.env`` for missing keys (monorepo layout)."""
    local = _PROJECT_ROOT / ".env"
    parent = _PROJECT_ROOT.parent / ".env"
    if local.is_file():
        load_dotenv(local)
    if parent.is_file():
        load_dotenv(parent, override=False)


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _read_prompt_file(name: str) -> str:
    path = _PROMPT_DIR / name
    if not path.is_file():
        print(f"Missing prompt file: {path}", file=sys.stderr)
        sys.exit(1)
    return path.read_text(encoding="utf-8").strip()


def load_prompt_bundle() -> tuple[str, str, str, str]:
    """Returns per_video_system, per_video_user_template, synthesis_system, synthesis_user_template."""
    return (
        _read_prompt_file("per_video_system.txt"),
        _read_prompt_file("per_video_user.txt"),
        _read_prompt_file("synthesis_system.txt"),
        _read_prompt_file("synthesis_user.txt"),
    )


def _api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        print(
            "Missing API key: set GEMINI_API_KEY or GOOGLE_API_KEY in `.env` "
            "(project root) or in the environment.",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def _looks_like_timeout(err: BaseException) -> bool:
    if isinstance(err, (TimeoutError, gcp_exceptions.DeadlineExceeded)):
        return True
    if type(err).__name__.endswith("Timeout") or type(err).__name__ == "ReadTimeout":
        return True
    s = str(err).lower()
    return "timeout" in s or "timed out" in s


def _should_retry_exception(err: BaseException) -> bool:
    """Avoid burning many attempts on client errors that won't succeed on retry."""
    if isinstance(err, gcp_exceptions.InvalidArgument):
        return False
    if isinstance(
        err,
        (
            gcp_exceptions.PermissionDenied,
            gcp_exceptions.NotFound,
            gcp_exceptions.Unauthenticated,
        ),
    ):
        return False
    if isinstance(
        err,
        (
            gcp_exceptions.ResourceExhausted,
            gcp_exceptions.DeadlineExceeded,
            gcp_exceptions.ServiceUnavailable,
            gcp_exceptions.InternalServerError,
            gcp_exceptions.GatewayTimeout,
            gcp_exceptions.Aborted,
        ),
    ):
        return True
    if isinstance(err, gcp_exceptions.GoogleAPICallError):
        return False
    if isinstance(err, RuntimeError) and "Empty model response" in str(err):
        return True
    return True


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def list_playlist_videos(playlist_url: str, max_videos: int | None) -> list[dict[str, Any]]:
    """Return [{id, title, url, index}], 1-based index in playlist order."""
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    out: list[dict[str, Any]] = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(playlist_url, download=False)
        entries = info.get("entries") or []
        for i, e in enumerate(entries, start=1):
            if not e:
                continue
            vid = e.get("id")
            if not vid:
                continue
            title = (e.get("title") or vid).strip()
            out.append(
                {
                    "index": i,
                    "id": vid,
                    "title": title,
                    "url": _watch_url(vid),
                }
            )
            if max_videos is not None and len(out) >= max_videos:
                break
    return out


def _retry_delay_seconds(err: BaseException, attempt: int) -> float:
    """Honor Gemini 429 'retry in Ns' hints when present."""
    if _looks_like_timeout(err):
        return min(90.0, 8.0 * attempt + random.uniform(0.0, 5.0))
    err_s = str(err)
    if "429" in err_s or "RESOURCE_EXHAUSTED" in err_s:
        m = re.search(r"retry in ([\d.]+)\s*s", err_s, re.I)
        if m:
            return min(120.0, float(m.group(1)) + random.uniform(2.0, 8.0))
        return min(90.0, 12.0 * attempt + random.uniform(0.0, 6.0))
    return min(60.0, (2**attempt) + random.uniform(0, 1.5))


def _generate_with_retries(
    model_name: str,
    contents: Any,
    *,
    system_instruction: str | None = None,
    max_attempts: int = DEFAULT_GENERATE_MAX_ATTEMPTS,
    log_label: str = "generate_content",
    request_timeout_seconds: float | None = None,
) -> str:
    """Uses ``google.generativeai`` (``GenerativeModel`` + ``RequestOptions``), like AI Studio samples."""
    last_err: Exception | None = None
    model = genai.GenerativeModel(model_name, system_instruction=system_instruction)
    req_opts = (
        RequestOptions(timeout=float(request_timeout_seconds))
        if request_timeout_seconds is not None
        else None
    )
    to = request_timeout_seconds
    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            ts = f" http_timeout={to}s" if to is not None else ""
            print(
                f"  [api] {log_label}: calling Gemini (up to {max_attempts} attempts{ts})",
                flush=True,
            )
        try:
            resp = model.generate_content(contents, request_options=req_opts)
            try:
                text = (resp.text or "").strip()
            except ValueError as ve:
                raise RuntimeError(
                    "No text in model response (blocked, empty, or no candidates). "
                    f"prompt_feedback={getattr(resp, 'prompt_feedback', None)!r}"
                ) from ve
            if not text:
                raise RuntimeError("Empty model response (no text).")
            return text
        except Exception as e:  # noqa: BLE001 — broad for network/API flakiness
            last_err = e
            print(f"  [api] {log_label}: attempt {attempt} failed: {e!s}", flush=True)
            _LOG.error(
                "%s: attempt %d/%d failed: %s",
                log_label,
                attempt,
                max_attempts,
                e,
                exc_info=True,
            )
            if not _should_retry_exception(e):
                print(
                    f"  [api] {log_label}: non-retryable error, stopping",
                    flush=True,
                )
                break
            if attempt >= max_attempts:
                break
            delay = _retry_delay_seconds(e, attempt)
            print(f"  [api] {log_label}: sleeping {delay:.1f}s before retry", flush=True)
            time.sleep(delay)
    assert last_err is not None
    _LOG.error(
        "%s: stopped after attempt %d/%d; last error: %s",
        log_label,
        attempt,
        max_attempts,
        last_err,
    )
    raise last_err


def notes_for_video(
    model_name: str,
    video_url: str,
    video_title: str,
    *,
    per_video_system: str,
    per_video_user_template: str,
    special_instructions: str,
    request_timeout_seconds: float,
) -> str:
    user_text = (
        per_video_user_template.replace("{video_title}", video_title).replace(
            "{video_url}", video_url
        )
    )
    extra = special_instructions.strip()
    if extra:
        user_text += (
            "\n\n---\n## Additional instructions for this run\n\n"
            f"{extra}\n"
        )
    youtube_part = protos.Part(
        file_data=protos.FileData(
            file_uri=video_url,
            mime_type="video/youtube",
        )
    )
    # Same shape as AI Studio samples: YouTube part then text prompt.
    contents: list[Any] = [youtube_part, user_text]
    return _generate_with_retries(
        model_name,
        contents,
        system_instruction=per_video_system,
        log_label=f"video notes ({video_title[:60]}{'…' if len(video_title) > 60 else ''})",
        request_timeout_seconds=request_timeout_seconds,
    )


def synthesize_notes(
    model_name: str,
    combined_raw: str,
    *,
    synthesis_system: str,
    synthesis_user_template: str,
    request_timeout_seconds: float | None = None,
) -> str:
    prompt = synthesis_user_template.replace("{combined_raw}", combined_raw)
    return _generate_with_retries(
        model_name,
        prompt,
        system_instruction=synthesis_system,
        log_label="synthesis (full raw notes)",
        request_timeout_seconds=request_timeout_seconds,
    )


def raw_block_markdown(
    *,
    index: int,
    title: str,
    url: str,
    notes: str,
    captured_utc: str | None = None,
) -> str:
    ts = captured_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"\n\n---\n"
        f"### Playlist video #{index}\n"
        f"**Title:** {title}\n"
        f"**URL:** {url}\n"
        f"**Captured (UTC):** {ts}\n\n"
        f"{notes.strip()}\n"
    )


def append_raw_block(
    path: Path,
    *,
    index: int,
    title: str,
    url: str,
    notes: str,
) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(raw_block_markdown(index=index, title=title, url=url, notes=notes))


def write_manifest(path: Path, meta: dict[str, Any]) -> None:
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def main(special_instructions: str = "") -> None:
    _load_env()

    parser = argparse.ArgumentParser(
        description="YouTube playlist → Gemini per-video notes → consolidated notes."
    )
    parser.add_argument(
        "playlist_url",
        nargs="?",
        default=None,
        help="YouTube playlist URL (or set YT_PLAYLIST_URL in .env)",
    )
    parser.add_argument(
        "--out-dir",
        default=os.fspath(DEFAULT_OUT_DIR),
        help=f"Directory for raw notes, final notes, and manifest (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL),
        help=f"Gemini model id (default: {DEFAULT_MODEL} or GEMINI_MODEL env)",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=2.0,
        help="Pause after each per-video API call to reduce rate-limit issues (default: 2)",
    )
    parser.add_argument(
        "--max-videos",
        type=int,
        default=None,
        help="Process only the first N videos in playlist order",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip videos already listed in manifest as completed",
    )
    parser.add_argument(
        "--only-synthesize",
        action="store_true",
        help="Do not call per-video; only build final notes from existing raw file",
    )
    parser.add_argument(
        "--special-instructions",
        default="",
        help=(
            "Text appended to every per-video prompt. When non-empty, overrides "
            "SPECIAL_INSTRUCTIONS from the __main__ block."
        ),
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=_env_float("GEMINI_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT_SECONDS),
        help=(
            "HTTP timeout (seconds) for each per-video generate_content call; "
            f"prevents indefinite hangs (default: {int(DEFAULT_REQUEST_TIMEOUT_SECONDS)} "
            "or GEMINI_REQUEST_TIMEOUT)."
        ),
    )
    parser.add_argument(
        "--synthesis-request-timeout-seconds",
        type=float,
        default=None,
        help=(
            "HTTP timeout (seconds) for the final synthesis call only "
            "(default: same as --request-timeout-seconds)."
        ),
    )
    args = parser.parse_args()

    run_extra = (args.special_instructions or "").strip() or special_instructions.strip()

    playlist_url = (args.playlist_url or os.environ.get("YT_PLAYLIST_URL") or "").strip()
    if not args.only_synthesize and not playlist_url:
        parser.error(
            "playlist URL required: pass as first argument or set YT_PLAYLIST_URL in .env"
        )

    per_video_system, per_video_user_t, synthesis_system, synthesis_user_t = (
        load_prompt_bundle()
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _configure_run_logging(out_dir)
    raw_path = out_dir / "notes_raw.md"
    final_path = out_dir / "notes_comprehensive.md"
    manifest_path = out_dir / "manifest.json"

    api_key = _api_key()
    genai.configure(api_key=api_key)
    req_to = max(30.0, float(args.request_timeout_seconds))
    synth_to = (
        max(30.0, float(args.synthesis_request_timeout_seconds))
        if args.synthesis_request_timeout_seconds is not None
        else req_to
    )
    model = args.model

    manifest: dict[str, Any] = {
        "playlist_url": playlist_url,
        "model": model,
        "videos": [],
        "started_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            _LOG.warning("Could not parse manifest.json, starting fresh: %s", e)

    if run_extra:
        manifest["special_instructions"] = run_extra

    completed_ids = {
        v["id"]
        for v in manifest.get("videos", [])
        if isinstance(v, dict) and v.get("status") == "ok"
    }

    if not args.only_synthesize:
        try:
            videos = list_playlist_videos(playlist_url, args.max_videos)
        except Exception as e:  # noqa: BLE001
            _LOG.exception("yt-dlp failed to read playlist: %s", playlist_url)
            print(f"Playlist fetch failed: {e}", file=sys.stderr, flush=True)
            sys.exit(2)
        if not videos:
            print("No videos found for that playlist URL.", file=sys.stderr)
            sys.exit(2)

        manifest["playlist_url"] = playlist_url
        manifest["model"] = model
        manifest.setdefault("videos", [])

        # Ensure raw file exists
        if not raw_path.exists():
            raw_path.write_text(
                f"# Raw per-video notes\n\nPlaylist: {playlist_url}\n",
                encoding="utf-8",
            )

        id_to_manifest_idx = {
            entry["id"]: i
            for i, entry in enumerate(manifest["videos"])
            if isinstance(entry, dict) and entry.get("id")
        }

        for v in videos:
            vid = v["id"]
            if args.skip_existing and vid in completed_ids:
                print(f"[skip] #{v['index']} {v['title']}", flush=True)
                continue

            print(f"[notes] #{v['index']} {v['title']}", flush=True)
            entry: dict[str, Any] = {
                "index": v["index"],
                "id": vid,
                "title": v["title"],
                "url": v["url"],
                "status": "pending",
            }
            try:
                text = notes_for_video(
                    model,
                    v["url"],
                    v["title"],
                    per_video_system=per_video_system,
                    per_video_user_template=per_video_user_t,
                    special_instructions=run_extra,
                    request_timeout_seconds=req_to,
                )
                append_raw_block(
                    raw_path,
                    index=v["index"],
                    title=v["title"],
                    url=v["url"],
                    notes=text,
                )
                entry["status"] = "ok"
                entry["raw_saved"] = True
                completed_ids.add(vid)
            except Exception as e:  # noqa: BLE001
                entry["status"] = "error"
                entry["error"] = str(e)
                print(f"  ERROR: {e}", file=sys.stderr, flush=True)
                _LOG.exception(
                    "Per-video notes failed index=%s id=%s title=%s",
                    v.get("index"),
                    vid,
                    v.get("title"),
                )

            if vid in id_to_manifest_idx:
                manifest["videos"][id_to_manifest_idx[vid]] = entry
            else:
                manifest["videos"].append(entry)
                id_to_manifest_idx[vid] = len(manifest["videos"]) - 1

            write_manifest(manifest_path, manifest)
            time.sleep(max(0.0, float(args.delay_seconds)))

        manifest["finished_per_video_utc"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        write_manifest(manifest_path, manifest)

    if not raw_path.exists():
        print(f"Raw notes not found: {raw_path}", file=sys.stderr)
        sys.exit(3)

    combined = raw_path.read_text(encoding="utf-8")
    if len(combined.strip()) < 50:
        print("Raw notes file is too small to synthesize.", file=sys.stderr)
        sys.exit(4)
    if "### Playlist video #" not in combined:
        print(
            "No completed per-video notes in the raw file (nothing to synthesize).",
            file=sys.stderr,
        )
        sys.exit(6)

    print(
        "[synthesize] Building comprehensive notes (this can take a while)...",
        flush=True,
    )
    try:
        final_text = synthesize_notes(
            model,
            combined,
            synthesis_system=synthesis_system,
            synthesis_user_template=synthesis_user_t,
            request_timeout_seconds=synth_to,
        )
    except Exception as e:  # noqa: BLE001
        print(f"Synthesis failed: {e}", file=sys.stderr, flush=True)
        _LOG.exception("Synthesis step failed")
        sys.exit(5)

    final_path.write_text(final_text.strip() + "\n", encoding="utf-8")
    manifest["final_notes_path"] = str(final_path)
    manifest["synthesized_at_utc"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    write_manifest(manifest_path, manifest)
    print(f"Done. Raw: {raw_path}\nFinal: {final_path}", flush=True)


if __name__ == "__main__":
    # Default extra instructions when `--special-instructions` is not passed on the CLI
    # (CLI wins when non-empty). Tailor for Striver Graph playlist: Java/C++ -> Python 3.
    SPECIAL_INSTRUCTIONS = (
        "This playlist uses Java/C++ in the source material. For every code example and "
        "algorithm walkthrough, provide an equivalent Python 3 solution instead (idiomatic "
        "where reasonable, type hints optional). Preserve logic, complexity discussion, and "
        "edge cases; do not omit steps—translate faithfully."
    )

    main(special_instructions=SPECIAL_INSTRUCTIONS)
