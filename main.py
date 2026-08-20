import json
import os
import re
import socket
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from starlette.middleware.wsgi import WSGIMiddleware
from wsgidav.fs_dav_provider import FilesystemProvider
from wsgidav.wsgidav_app import WsgiDAVApp

app = FastAPI(title="yt-dlp API", version="1.0.0")

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

app.mount(
    "/dav",
    WSGIMiddleware(
        WsgiDAVApp(
            {
                "provider_mapping": {"/": FilesystemProvider(str(DATA_DIR), readonly=True, fs_opts={})},
                "http_authenticator": {"domain_controller": None},
                "simple_dc": {"user_mapping": {"*": True}},
                "dir_browser": {"enable": True},
                "verbose": 1,
                "logging": {"enable": False},
            }
        )
    ),
)

HTML_TAG_RE = re.compile(r"<[^>]*>")

VIDEO_SUBDIR = "videos"
AUDIO_SUBDIR = "audios"
TRANSCRIPT_SUBDIR = "transcripts"


def run_ytdlp(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["yt-dlp", "--no-warnings"] + args,
        capture_output=True,
        text=True,
        timeout=3600,
    )


def normalize_target(video_id: str) -> str:
    """Wrap a bare YouTube video ID into a watch URL."""
    video_id = video_id.strip()
    if not video_id:
        raise HTTPException(status_code=400, detail="Empty video id")
    return f"https://www.youtube.com/watch?v={video_id}"


def run_download(args: list[str], out_dir: Path) -> str:
    """Run yt-dlp and return the name of the newly created file."""
    before = {p.name for p in out_dir.iterdir()}
    proc = run_ytdlp(args)
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=proc.stderr.strip())
    after = {p.name for p in out_dir.iterdir()}
    new_names = after - before
    if new_names:
        return max(new_names, key=lambda n: (out_dir / n).stat().st_size)
    return max(after, key=lambda n: (out_dir / n).stat().st_size)


def get_info_json(target: str) -> dict:
    """Fetch the full yt-dlp metadata JSON for a target."""
    proc = run_ytdlp(["-J", "--no-playlist", target])
    if proc.returncode != 0:
        raise HTTPException(status_code=404, detail=proc.stderr.strip())
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse yt-dlp output")


def _build_response(info: dict, dav_path: str) -> dict:
    """Build a standardized response from yt-dlp info and the dav path."""
    return {
        "title": info.get("title"),
        "upload_date": info.get("upload_date"),
        "channel_id": info.get("channel_id"),
        "channel": info.get("channel"),
        "dav_path": dav_path,
    }


def embed_thumbnail(video_file: Path, info: dict, url: str) -> None:
    """Attach the video thumbnail as cover art and set title/network/date metadata."""
    thumb_url = info.get("thumbnail")
    if not thumb_url:
        return
    title = info.get("title", "")
    date = info.get("upload_date", "")

    with tempfile.TemporaryDirectory() as tmp:
        thumb = Path(tmp) / "thumb.jpg"
        req = urllib.request.Request(
            thumb_url, headers={"User-Agent": "Mozilla/5.0 yt-dlp-api"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            thumb.write_bytes(resp.read())

        thumb_png = Path(tmp) / "thumb.png"
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(thumb), str(thumb_png)],
            capture_output=True, text=True,
        )

        out = video_file.parent / f".tmp.{video_file.name}"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_file),
            "-i", str(thumb_png),
            "-map", "0", "-map", "1",
            "-c:a", "copy",
            "-c:v:0", "copy",
            "-c:v:1", "png",
            "-disposition:v:1", "attached_pic",
            "-metadata", f"title={title}",
            "-metadata", f"network={url}",
            "-metadata", f"copyright={date}",
            str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            out.unlink(missing_ok=True)
            raise HTTPException(status_code=502, detail=proc.stderr.strip())
        out.replace(video_file)


def _check_pot_server() -> bool:
    """Check if the bgutil PO token HTTP server is reachable on port 4416."""
    try:
        with socket.create_connection(("127.0.0.1", 4416), timeout=2) as sock:
            return True
    except (OSError, ConnectionRefusedError):
        return False


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


@app.get("/about/{id}")
def get_about(id: str):
    """Return metadata and available formats for a YouTube video ID."""
    target = normalize_target(id)
    info = get_info_json(target)

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

    return {
        "title": info.get("title"),
        "upload_date": info.get("upload_date"),
        "channel_id": info.get("channel_id"),
        "channel": info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "formats": rows,
    }


@app.get("/transcript/{id}")
def get_transcript(id: str):
    """Download auto-generated subtitles (original language) and store them as a
    single-line plain-text transcript in $DATA_DIR/transcripts/<id>.txt."""
    target = normalize_target(id)
    info = get_info_json(target)
    video_id = info.get("id")
    if not video_id:
        raise HTTPException(status_code=404, detail="Failed to extract video id")

    transcripts_dir = DATA_DIR / TRANSCRIPT_SUBDIR
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        proc = run_ytdlp(
            [
                "--skip-download",
                "--no-playlist",
                "--sub-format", "ttml",
                "--sub-langs", ".*orig",
                "--write-auto-subs",
                "--output", str(tmp_dir / "_SUBS_"),
                target,
            ]
        )
        if proc.returncode != 0:
            raise HTTPException(status_code=502, detail=proc.stderr.strip())

        parts = []
        for p in sorted(tmp_dir.glob("_SUBS_.*")):
            if p.name == "_SUBS_":
                continue
            parts.append(p.read_text(errors="replace"))
        if not parts:
            raise HTTPException(status_code=502, detail="No subtitles found")

        text = HTML_TAG_RE.sub("", "".join(parts)).replace("&#39;", "'")
        transcript = " ".join(text.split())

        transcript_file = transcripts_dir / f"{video_id}.txt"
        transcript_file.write_text(transcript)

    return _build_response(info, f"/{TRANSCRIPT_SUBDIR}/{video_id}.txt")


@app.get("/video/{id}")
def download_video(
    id: str,
    quality: str = Query("bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/best[ext=mp4]", description="Raw yt-dlp format selector. Ex: bestvideo[width<=1920][height<=1080][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/(mp4)."),
):
    """Download a video (always mp4, avc1 codec) into $DATA_DIR/videos/,
    then attach its thumbnail as cover art and set title/network/date metadata."""
    target = normalize_target(id)
    info = get_info_json(target)
    video_id = info.get("id")
    out_dir = DATA_DIR / VIDEO_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)

    args = [
        "--output", str(out_dir / "%(id)s.%(ext)s"),
        "--no-playlist",
        "--format", quality,
        "--merge-output-format", "mp4",
        target,
    ]
    file_name = run_download(args, out_dir)
    video_file = out_dir / file_name

    embed_thumbnail(video_file, info, target)

    return _build_response(info, f"/{VIDEO_SUBDIR}/{video_id}.mp4")


@app.get("/audio/{id}")
def download_audio(
    id: str,
    quality: str = Query("bestaudio[ext=m4a]/bestaudio", description="Raw yt-dlp format selector for the source audio stream."),
):
    """Download audio only (m4a) into $DATA_DIR/audios/."""
    target = normalize_target(id)
    info = get_info_json(target)
    video_id = info.get("id")
    out_dir = DATA_DIR / AUDIO_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)

    args = [
        "--output", str(out_dir / "%(id)s.%(ext)s"),
        "--no-playlist",
        "--format", quality,
        "--extract-audio",
        "--audio-format", "m4a",
        target,
    ]
    run_download(args, out_dir)

    return _build_response(info, f"/{AUDIO_SUBDIR}/{video_id}.m4a")
