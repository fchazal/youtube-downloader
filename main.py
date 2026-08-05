import json
import os
import re
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="yt-dlp API", version="1.0.0")

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def run_ytdlp(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["yt-dlp", "--no-warnings"] + args,
        capture_output=True,
        text=True,
        timeout=3600,
    )


def normalize_target(url_or_id: str) -> str:
    """yt-dlp accepts a full URL or a bare video ID."""
    target = url_or_id.strip()
    if not target:
        raise HTTPException(status_code=400, detail="Empty url or id")
    if URL_RE.match(target):
        return target
    return f"https://www.youtube.com/watch?v={target}"


class DownloadRequest(BaseModel):
    url_or_id: str
    subdir: str = Field(default="", description="Subdirectory under $DATA_DIR. Empty -> video ID.")
    audio_only: bool = False
    format: str = Field(default="", description="Advanced: raw yt-dlp format selector. Overrides video_quality/video_ext.")
    video_quality: int = Field(default=0, ge=0, le=2160, description="Max video height in pixels: 2160/1440/1080/720/480/360/240/144. 0 = best available.")
    video_ext: str = Field(default="", description="Preferred container: mp4, webm, mkv... Empty = yt-dlp default.")
    audio_format: str = Field(default="mp3", description="Audio extension when audio_only")
    audio_quality: int = Field(default=192, ge=0, le=320, description="Audio bitrate when audio_only")
    write_subtitles: bool = False
    subtitle_langs: list[str] = Field(default=["en"])
    embed_subtitles: bool = False
    write_auto_subs: bool = False
    playlist: bool = False


def build_video_format(req: DownloadRequest) -> str:
    """Build a yt-dlp format selector from the high-level quality/extension fields."""
    if req.format:
        return req.format

    q = f"[height<={req.video_quality}]" if req.video_quality else ""
    ext = f"[ext={req.video_ext}]" if req.video_ext else ""

    merged = f"bestvideo{q}{ext}+bestaudio"
    fallback = f"best{q}{ext}"
    last = f"best{q}" if ext else f"best{q}"
    if ext:
        return f"{merged}/{fallback}/{last}"
    return f"best{q}"


def build_audio_format(req: DownloadRequest) -> str:
    return "bestaudio/best"


@app.get("/")
def health():
    proc = run_ytdlp(["--version"])
    return {
        "status": "ok",
        "yt_dlp_version": proc.stdout.strip() or "unknown",
        "data_dir": str(DATA_DIR),
    }


@app.get("/info/{url_or_id:path}")
def get_info(url_or_id: str):
    """Return metadata/properties for a YouTube URL or video ID."""
    target = normalize_target(url_or_id)
    proc = run_ytdlp(["-J", "--no-playlist", target])
    if proc.returncode != 0:
        raise HTTPException(status_code=404, detail=proc.stderr.strip())
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse yt-dlp output")


@app.get("/formats/{url_or_id:path}")
def list_formats(url_or_id: str):
    """Return the available download formats/qualities for a URL or video ID."""
    target = normalize_target(url_or_id)
    proc = run_ytdlp(["-J", "--no-playlist", target])
    if proc.returncode != 0:
        raise HTTPException(status_code=404, detail=proc.stderr.strip())
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse yt-dlp output")

    rows = []
    for f in info.get("formats", []):
        rows.append(
            {
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "height": f.get("height"),
                "width": f.get("width"),
                "fps": f.get("fps"),
                "video_codec": f.get("vcodec"),
                "audio_codec": f.get("acodec"),
                "bitrate_kbps": f.get("tbr"),
                "filesize_mb": round(f["filesize"] / 1_000_000, 1) if f.get("filesize") else None,
                "note": f.get("format_note"),
            }
        )
    rows = [r for r in rows if r["video_codec"] != "none" or r["audio_codec"] != "none"]
    return {"video_id": info.get("id"), "title": info.get("title"), "formats": rows}


@app.post("/download")
def download(req: DownloadRequest):
    """Download a video/audio/subtitles into a subdirectory of $DATA_DIR."""
    target = normalize_target(req.url_or_id)

    subdir = req.subdir.strip() or "downloads"
    out_dir = DATA_DIR / subdir
    out_dir.mkdir(parents=True, exist_ok=True)

    args = [
        "--output", str(out_dir / "%(title).200B.%(ext)s"),
        "--no-playlist" if not req.playlist else "--yes-playlist",
    ]

    if req.audio_only:
        args += ["--format", build_audio_format(req)]
        args += [
            "--extract-audio",
            "--audio-format", req.audio_format,
            "--audio-quality", str(req.audio_quality),
        ]
    else:
        args += ["--format", build_video_format(req)]
        if req.video_ext:
            args += ["--merge-output-format", req.video_ext]

    if req.write_subtitles or req.embed_subtitles:
        args += ["--write-subs", "--sub-langs", ",".join(req.subtitle_langs)]
        if req.write_auto_subs:
            args += ["--write-auto-subs"]
        if req.embed_subtitles:
            args += ["--embed-subs", "--embed-thumbnail"]

    args += [target]

    proc = run_ytdlp(args)
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=proc.stderr.strip())

    files = sorted(p.name for p in out_dir.iterdir())
    return {
        "ok": True,
        "directory": str(out_dir),
        "files": files,
    }
