import html
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="yt-dlp API", version="2.0.0")

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
HTML_TAG_RE = re.compile(r"<[^>]*>")
SUBTITLE_TIMING_RE = re.compile(r"(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}")
SIMPLE_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,4})?$")

PARTS = ("video", "audio", "cover", "transcript", "info")
FILE_NAMES = {
    "video": "video.m4v",
    "audio": "audio.m4a",
    "cover": "cover.jpg",
    "transcript": "transcript.txt",
    "info": "info.json",
}
MIME_TYPES = {
    "video": "video/mp4",
    "audio": "audio/mp4",
    "cover": "image/jpeg",
    "transcript": "text/plain",
    "info": "application/json",
}
DEFAULT_VIDEO_QUALITY = "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/best[ext=mp4]"
DEFAULT_AUDIO_QUALITY = "bestaudio[ext=m4a]/bestaudio"

USER_AGENT = "Mozilla/5.0 yt-dlp-api"


def run_ytdlp(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["yt-dlp", "--no-warnings"] + args,
        capture_output=True,
        text=True,
        timeout=3600,
    )


def sanitize_id(video_id: str) -> str:
    """Validate and return a bare YouTube video ID."""
    video_id = video_id.strip()
    if not ID_RE.fullmatch(video_id or ""):
        raise HTTPException(status_code=400, detail=f"Invalid video id: {video_id!r}")
    return video_id


def normalize_target(video_id: str) -> str:
    """Validate a bare YouTube video ID and wrap it into a watch URL."""
    return f"https://www.youtube.com/watch?v={sanitize_id(video_id)}"


def video_dir(video_id: str) -> Path:
    path = DATA_DIR / video_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_path(video_id: str, part: str) -> Path:
    return DATA_DIR / video_id / FILE_NAMES[part]


def file_ready(video_id: str, part: str) -> bool:
    p = file_path(video_id, part)
    return p.exists() and p.stat().st_size > 0


def run_download(args: list[str], out_dir: Path) -> str:
    """Run yt-dlp and return the name of the newly created (or largest) file."""
    before = {p.name for p in out_dir.iterdir()}
    proc = run_ytdlp(args)
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=proc.stderr.strip())
    after = {p.name for p in out_dir.iterdir()}
    new_names = after - before
    if new_names:
        return max(new_names, key=lambda n: (out_dir / n).stat().st_size)
    existing = [p.name for p in out_dir.iterdir()]
    if existing:
        return max(existing, key=lambda n: (out_dir / n).stat().st_size)
    raise HTTPException(status_code=502, detail="yt-dlp produced no output file")


def get_info_json(target: str) -> dict:
    """Fetch the full yt-dlp metadata JSON for a target."""
    proc = run_ytdlp(["-J", "--no-playlist", target])
    if proc.returncode != 0:
        raise HTTPException(status_code=404, detail=proc.stderr.strip())
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse yt-dlp output")


def _check_pot_server() -> bool:
    """Check if the bgutil PO token HTTP server is reachable on port 4416."""
    try:
        with socket.create_connection(("127.0.0.1", 4416), timeout=2) as sock:
            return True
    except (OSError, ConnectionRefusedError):
        return False


# ---------------------------------------------------------------------------
# Per-part downloaders
# ---------------------------------------------------------------------------

def download_video(video_id: str, target: str, quality: str) -> None:
    out_dir = video_dir(video_id)
    args = [
        "--output", str(out_dir / "%(id)s.%(ext)s"),
        "--no-playlist",
        "--format", quality,
        "--merge-output-format", "mp4",
        target,
    ]
    produced = run_download(args, out_dir)
    src = out_dir / produced
    dest = file_path(video_id, "video")
    os.replace(src, dest)


def download_audio(video_id: str, target: str, quality: str) -> None:
    out_dir = video_dir(video_id)
    args = [
        "--output", str(out_dir / "%(id)s.%(ext)s"),
        "--no-playlist",
        "--format", quality,
        "--extract-audio",
        "--audio-format", "m4a",
        target,
    ]
    produced = run_download(args, out_dir)
    src = out_dir / produced
    dest = file_path(video_id, "audio")
    os.replace(src, dest)


def download_cover(video_id: str, info: dict) -> None:
    thumb_url = info.get("thumbnail")
    if not thumb_url:
        raise HTTPException(status_code=502, detail="No thumbnail available for this video")
    out_dir = video_dir(video_id)
    dest = file_path(video_id, "cover")
    # Temp files live on the same filesystem as dest so os.replace stays atomic
    # (DATA_DIR may be a different mount than the system /tmp).
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        raw = Path(tmp) / "thumb.raw"
        req = urllib.request.Request(thumb_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw.write_bytes(resp.read())

        jpg = Path(tmp) / "cover.jpg"
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(raw), "-frames:v", "1", "-q:v", "2", str(jpg)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise HTTPException(status_code=502, detail=proc.stderr.strip())
        os.replace(jpg, dest)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def original_language_candidates(info: dict) -> list[str]:
    """Best-effort list of the video's original audio language codes.

    yt-dlp used to expose the original auto-caption as ``<lang>-orig`` but that
    is no longer guaranteed (some clients now return only the plain language
    code). Use every signal available in the metadata, most reliable first.
    """
    candidates = []
    language = (info.get("language") or "").strip()
    if language:
        candidates.append(language)

    automatic = info.get("automatic_captions") or {}
    candidates.extend(key[:-5] for key in automatic if key.endswith("-orig"))

    # yt-dlp sets language_preference=10 on the original audio track.
    audio_formats = sorted(
        (f for f in info.get("formats") or []
         if f.get("language") and f.get("acodec") != "none"),
        key=lambda f: f.get("language_preference") or -1,
        reverse=True,
    )
    candidates.extend(f["language"] for f in audio_formats)
    return _dedupe(candidates)


def select_transcript_source(info: dict) -> tuple[str, str] | None:
    """Return ``(language, kind)`` for the original-language subtitle track.

    ``kind`` is ``manual`` when an uploaded subtitle track is available and
    ``auto`` for a YouTube-generated one. Manual subtitles are preferred since
    they are more accurate, but only when they are in the original language.
    """
    subtitles = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}

    for language in original_language_candidates(info):
        if language in subtitles:
            return language, "manual"
        if f"{language}-orig" in automatic:
            return f"{language}-orig", "auto"
        if language in automatic:
            return language, "auto"

        # Regional/script variants such as fr-CA or zh-Hans.
        variant_re = re.compile(rf"{re.escape(language)}(?:-[A-Za-z0-9]{{2,4}})?")
        for key in subtitles:
            if variant_re.fullmatch(key):
                return key, "manual"
        for key in automatic:
            if variant_re.fullmatch(key):
                return key, "auto"

    # Legacy marker, and fallback when no language metadata was extracted.
    for key in automatic:
        if key.endswith("-orig"):
            return key, "auto"
    return None


def subtitle_to_text(raw: str) -> str:
    """Strip TTML/VTT/SRT markup and timestamps from a subtitle file."""
    lines = []
    for line in html.unescape(raw).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "WEBVTT" or stripped.startswith(("NOTE", "Kind:", "Language:", "Region:")):
            continue
        if SUBTITLE_TIMING_RE.search(stripped) and "-->" in stripped:
            continue
        if stripped.isdigit():
            continue
        lines.append(line)
    return " ".join(HTML_TAG_RE.sub("", "\n".join(lines)).split())


def download_transcript(video_id: str, target: str, info: dict) -> None:
    """Download the subtitles of the video's original language as a single-line
    plain-text transcript file."""
    automatic = info.get("automatic_captions") or {}
    source = select_transcript_source(info)

    attempts: list[tuple[str, str]] = []
    if source:
        attempts.append(source)
        language, kind = source
        # If the uploaded track cannot be fetched, fall back to the
        # auto-generated track for the same language.
        if kind == "manual":
            if f"{language}-orig" in automatic:
                attempts.append((f"{language}-orig", "auto"))
            elif language in automatic:
                attempts.append((language, "auto"))
    else:
        # Old yt-dlp versions exposed the original track as "<lang>-orig".
        attempts.append((".*orig", "auto"))
        # Last resort: a plain automatic-caption language (no translation hash).
        fallback = next((key for key in automatic if SIMPLE_LANG_RE.fullmatch(key)), None)
        if fallback:
            attempts.append((fallback, "auto"))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        last_error: str | None = None
        parts: list[Path] = []

        for language, kind in attempts:
            for stale in tmp_dir.iterdir():
                if stale.is_file():
                    stale.unlink()

            lang_pattern = language if language == ".*orig" else f"^{re.escape(language)}$"
            args = [
                "--skip-download",
                "--no-playlist",
                "--sub-format", "ttml/vtt/best",
                "--sub-langs", lang_pattern,
                "--write-subs" if kind == "manual" else "--write-auto-subs",
                "--output", str(tmp_dir / "_SUBS_"),
                target,
            ]
            proc = run_ytdlp(args)
            if proc.returncode != 0:
                last_error = proc.stderr.strip()
                continue

            parts = [p for p in sorted(tmp_dir.glob("_SUBS_.*")) if p.name != "_SUBS_"]
            if parts:
                break

        if not parts:
            raise HTTPException(status_code=502, detail=last_error or "No subtitles found")

        text = "".join(subtitle_to_text(p.read_text(errors="replace")) for p in parts)

        video_dir(video_id)
        dest = file_path(video_id, "transcript")
        dest.write_text(text)



def download_info(video_id: str, info: dict) -> None:
    video_dir(video_id)
    dest = file_path(video_id, "info")
    dest.write_text(json.dumps(info, indent=2))


DOWNLOADERS = {
    "video": lambda vid, target, info, quality: download_video(vid, target, quality),
    "audio": lambda vid, target, info, quality: download_audio(vid, target, quality),
    "cover": lambda vid, target, info, quality: download_cover(vid, info),
    "transcript": lambda vid, target, info, quality: download_transcript(vid, target, info),
    "info": lambda vid, target, info, quality: download_info(vid, info),
}


# ---------------------------------------------------------------------------
# Response payload helpers
# ---------------------------------------------------------------------------

def _part_state(video_id: str, part: str) -> dict:
    p = file_path(video_id, part)
    if not file_ready(video_id, part):
        return {"present": False}
    return {
        "present": True,
        "path": f"{video_id}/{p.name}",
        "size_bytes": p.stat().st_size,
    }


def _download_part_state(video_id: str, part: str, status: str, error: str | None = None) -> dict:
    entry: dict = {"status": status}
    if error:
        entry["error"] = error
    if file_ready(video_id, part):
        p = file_path(video_id, part)
        entry["path"] = f"{video_id}/{p.name}"
        entry["size_bytes"] = p.stat().st_size
    return entry


class DownloadRequest(BaseModel):
    parts: list[str] | None = None
    video_quality: str | None = None
    audio_quality: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def health():
    proc = run_ytdlp(["--version"])
    pot_ok = _check_pot_server()
    return {
        "status": "ok",
        "yt_dlp_version": proc.stdout.strip() or "unknown",
        "data_dir": str(DATA_DIR),
        "pot_provider": "ok" if pot_ok else "unavailable",
    }


@app.post("/download/{video_id}")
def download(video_id: str, refresh: bool = False, req: DownloadRequest | None = None):
    """Download the requested parts for a video in a single call.

    Parts default to everything (video, audio, cover, transcript, info). Already
    present files are skipped unless `refresh=1`. Each part is isolated, so a
    failure on one (e.g. no subtitles) does not affect the others.
    """
    target = normalize_target(video_id)
    req = req or DownloadRequest()
    parts = req.parts or list(PARTS)
    unknown = set(parts) - set(PARTS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown part(s): {', '.join(sorted(unknown))}")

    video_quality = req.video_quality or DEFAULT_VIDEO_QUALITY
    audio_quality = req.audio_quality or DEFAULT_AUDIO_QUALITY

    info = get_info_json(target)
    video_id = info.get("id") or video_id
    title = info.get("title")

    results: dict[str, dict] = {}
    for part in parts:
        if not refresh and file_ready(video_id, part):
            results[part] = _download_part_state(video_id, part, "skipped")
        else:
            quality = video_quality if part == "video" else audio_quality
            try:
                DOWNLOADERS[part](video_id, target, info, quality)
                results[part] = _download_part_state(video_id, part, "downloaded")
            except HTTPException as exc:
                results[part] = _download_part_state(video_id, part, "error", exc.detail)
            except Exception as exc:  # noqa: BLE001 - keep one bad part from breaking the rest
                results[part] = _download_part_state(video_id, part, "error", str(exc))

    return {"id": video_id, "title": title, "requested": parts, "parts": results}


@app.get("/{video_id}")
def list_parts(video_id: str):
    """Report which parts are already stored for a video (no download)."""
    video_id = sanitize_id(video_id)
    title = None
    info_file = file_path(video_id, "info")
    if info_file.exists():
        try:
            title = json.loads(info_file.read_text()).get("title")
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "id": video_id,
        "title": title,
        "parts": {part: _part_state(video_id, part) for part in PARTS},
    }


@app.get("/{video_id}/{part}")
def get_part(video_id: str, part: str, download: bool = False):
    """Serve a stored part file (video/audio/cover/transcript/info).

    Video/audio/cover are streamed inline by default (range requests supported).
    Pass `?download=1` to force an attachment download instead.
    """
    video_id = sanitize_id(video_id)
    if part not in PARTS:
        raise HTTPException(status_code=404, detail=f"Unknown part: {part!r}")
    p = file_path(video_id, part)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"{part} not downloaded for {video_id}")
    if download:
        return FileResponse(p, media_type=MIME_TYPES[part], filename=p.name)
    return FileResponse(p, media_type=MIME_TYPES[part], content_disposition_type="inline")


@app.delete("/{video_id}")
def delete_all(video_id: str):
    """Delete every downloaded part for a video at once."""
    video_id = sanitize_id(video_id)
    dir_path = DATA_DIR / video_id
    removed = [part for part in PARTS if file_path(video_id, part).exists()]
    if not removed and not dir_path.exists():
        raise HTTPException(status_code=404, detail=f"No data for {video_id}")
    shutil.rmtree(dir_path, ignore_errors=True)
    return {"id": video_id, "deleted": removed}


@app.delete("/{video_id}/{part}")
def delete_part(video_id: str, part: str):
    """Delete a single part for a video."""
    video_id = sanitize_id(video_id)
    if part not in PARTS:
        raise HTTPException(status_code=404, detail=f"Unknown part: {part!r}")
    p = file_path(video_id, part)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"{part} not downloaded for {video_id}")
    p.unlink()
    return {"id": video_id, "part": part, "deleted": True}
