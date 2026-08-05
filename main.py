import json
import os
import re
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


def resolve_subdir(subdir: str) -> Path:
    """Normalize the destination subdir under $DATA_DIR and create it if needed."""
    subdir = subdir.strip() or "downloads"
    out_dir = DATA_DIR / subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def run_download(args: list[str], out_dir: Path) -> dict:
    """Run yt-dlp and return the file listing written to out_dir."""
    proc = run_ytdlp(args)
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=proc.stderr.strip())
    files = sorted(p.name for p in out_dir.iterdir())
    return {"ok": True, "directory": str(out_dir), "files": files}


def get_info_json(target: str) -> dict:
    """Fetch the full yt-dlp metadata JSON for a target."""
    proc = run_ytdlp(["-J", "--no-playlist", target])
    if proc.returncode != 0:
        raise HTTPException(status_code=404, detail=proc.stderr.strip())
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse yt-dlp output")


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

        out = video_file.parent / f".tmp.{video_file.name}"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_file),
            "-i", str(thumb),
            "-map", "0", "-map", "1",
            "-c", "copy",
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


@app.get("/")
def health():
    proc = run_ytdlp(["--version"])
    return {
        "status": "ok",
        "yt_dlp_version": proc.stdout.strip() or "unknown",
        "data_dir": str(DATA_DIR),
    }


@app.get("/info/{id}")
def get_info(id: str):
    """Return metadata/properties for a YouTube video ID."""
    target = normalize_target(id)
    return get_info_json(target)


@app.get("/formats/{id}")
def list_formats(id: str):
    """Return the available download formats/qualities for a video ID."""
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
    return {"video_id": info.get("id"), "title": info.get("title"), "formats": rows}


@app.get("/transcript/{id}")
def get_transcript(id: str):
    """Download auto-generated subtitles (original language) and store them as a
    single-line plain-text transcript in $DATA_DIR/transcripts/<video_id>.txt."""
    target = normalize_target(id)

    proc = run_ytdlp(["--skip-download", "--no-playlist", "--print", "%(id)s", target])
    if proc.returncode != 0:
        raise HTTPException(status_code=404, detail=proc.stderr.strip())
    video_id = proc.stdout.strip()
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

    return {
        "video_id": video_id,
        "transcript_file": f"/{TRANSCRIPT_SUBDIR}/{video_id}.txt",
    }


@app.get("/video/{id}")
def download_video(
    id: str,
    quality: str = Query("", description="Raw yt-dlp format selector. Ex: bestvideo[width<=1280][height<=720][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/(mp4). Empty = best."),
    ext: str = Query("", description="Preferred container: mp4, webm, mkv... Empty = yt-dlp default."),
    subdir: str = Query("", description="Subdirectory under $DATA_DIR. Empty -> downloads."),
):
    """Download a video at a yt-dlp format selector into a subdirectory of $DATA_DIR,
    then attach its thumbnail as cover art and set title/network/date metadata."""
    target = normalize_target(id)
    out_dir = resolve_subdir(subdir)
    before = {p.name for p in out_dir.iterdir()}

    args = [
        "--output", str(out_dir / "%(title).200B.%(ext)s"),
        "--no-playlist",
    ]
    if quality:
        args += ["--format", quality]
    if ext:
        args += ["--merge-output-format", ext]
    args += [target]
    result = run_download(args, out_dir)

    after = {p.name for p in out_dir.iterdir()}
    new_names = after - before
    if new_names:
        video_name = max(new_names, key=lambda n: (out_dir / n).stat().st_size)
    else:
        video_name = max(after, key=lambda n: (out_dir / n).stat().st_size)
    video_file = out_dir / video_name

    embed_thumbnail(video_file, get_info_json(target), target)
    result["files"] = sorted(p.name for p in out_dir.iterdir())
    return result


@app.get("/audio/{id}")
def download_audio(
    id: str,
    format: str = Query("mp3", description="Audio container: mp3, m4a, opus, wav..."),
    quality: str = Query("bestaudio[ext=m4a]", description="Raw yt-dlp format selector for the source stream."),
    subdir: str = Query("", description="Subdirectory under $DATA_DIR. Empty -> downloads."),
):
    """Download audio only into a subdirectory of $DATA_DIR."""
    target = normalize_target(id)
    out_dir = resolve_subdir(subdir)
    args = [
        "--output", str(out_dir / "%(title).200B.%(ext)s"),
        "--no-playlist",
        "--format", quality,
        "--extract-audio",
        "--audio-format", format,
        target,
    ]
    return run_download(args, out_dir)
