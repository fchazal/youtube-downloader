# AGENT.md — youtube-downloader

Everything needed to resume work on this project. Read this first.

## Overview

A Dockerized HTTP API around [yt-dlp](https://github.com/yt-dlp/yt-dlp) that lets
you fetch video metadata, list available formats/qualities, and download
videos/audio/subtitles into a subdirectory of a configurable data directory.

It is published as the **`youtube`** app of the CasaOS store
(`/Users/fchazal/Developer/casaos-store`), which builds the image on the server
from this repo's git URL.

## Tech stack

- Python 3.12 + **FastAPI** + **uvicorn** (synchronous endpoints; yt-dlp runs in a subprocess)
- **yt-dlp standalone binary** (installed in the image, self-updates at startup)
- **ffmpeg** (audio extraction, format merging, subtitle embedding)
- Docker (multi-stage build), Docker Compose for local dev

## Repo structure

```
youtube-downloader/
├── main.py              # FastAPI app: health, info, formats, download
├── Dockerfile           # multi-stage: ffmpeg + yt-dlp binary + Python deps
├── requirements.txt     # fastapi, uvicorn[standard], python-multipart
├── docker-compose.yml   # local dev (build: ., port 8000, ./data:/data)
├── scripts/publish.sh   # build + push image to a registry
├── .dockerignore
├── .gitignore
├── README.md            # user-facing docs
└── AGENT.md             # this file
```

Remote: `https://github.com/fchazal/youtube-downloader.git` (branch `main`).
Current version: `1.0.0` (FastAPI app version + image tag `yt-dlp-api:latest`).

## How it works

- The API **shells out to the `yt-dlp` binary** via `subprocess.run` (not the
  Python module). This keeps the binary the single source of truth and lets it
  self-update with `yt-dlp -U`.
- `normalize_target()` wraps a bare video ID into
  `https://www.youtube.com/watch?v=<id>`. **URLs are not accepted** — the routes
  use `{id}`, so only bare IDs work.
- All downloads go into `$DATA_DIR/<subdir>/` (default subdir `downloads`).
- `run_ytdlp()` has a 3600 s timeout and captures output; errors are surfaced as
  HTTP errors (404 for unknown videos, 502 for download failures).
- `GET /video/{id}` re-remuxes the downloaded file with **ffmpeg**: the video
  thumbnail is attached as cover art (`-disposition:v:1 attached_pic`) and the
  tags `title`, `network` (watch URL) and `copyright` (upload date) are written.
  The thumbnail is fetched from the yt-dlp `thumbnail` URL via `urllib`. If the
  video has no thumbnail, the file is left untouched.
- `GET /transcript/{id}` downloads the auto-generated subtitles in the original
  language (`--skip-download --sub-format ttml --sub-langs ".*orig"` +
  `--write-auto-subs`), strips the HTML/ttml tags, joins everything into a
  single line, and writes `$DATA_DIR/transcripts/<video_id>.txt`. That directory
  is meant to be served as public WebDAV. The response is
  `{ "video_id": ..., "transcript_file": "/transcripts/<id>.txt" }`.
- **WebDAV**: `wsgidav` is mounted on `/dav` via `WSGIMiddleware`, serving
  `$DATA_DIR` **read-only** (`FilesystemProvider(..., readonly=True)`). Files are
  addressed by their real name (`/dav/downloads/<title>.mp4`,
  `/dav/transcripts/<id>.txt`). Anonymous access is granted by
  `simple_dc.user_mapping: {"*": True}`; write methods return 403. The provider
  must be mapped to `/` (Starlette strips the `/dav` mount prefix).

## Endpoints

| Method | Path                    | Description                          |
| ------ | ----------------------- | ------------------------------------ |
| GET    | `/`                     | Health + yt-dlp version + data_dir   |
| GET    | `/info/{id}`           | Full yt-dlp metadata JSON            |
| GET    | `/formats/{id}`        | Clean table of available formats     |
| GET    | `/transcript/{id}`     | Plain-text transcript → `$DATA_DIR/transcripts/<video_id>.txt` |
| GET    | `/video/{id}`          | Download video (`?quality=` `&ext=` `&subdir=`) |
| GET    | `/audio/{id}`          | Download audio (`?quality=` `&format=` `&subdir=`) |
| GET    | `/dav/*`               | Read-only WebDAV over `$DATA_DIR` (wsgidav) |
| GET    | `/docs`                 | Interactive OpenAPI UI               |

### GET /video and GET /audio query params

| Endpoint | Param     | Default   | Notes                                     |
| -------- | --------- | --------- | ----------------------------------------- |
| `/video` | `quality` | `""`      | **Raw yt-dlp format selector**, passed as `--format`. Empty = best. E.g. `bestvideo[width<=1280][height<=720][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/(mp4)` |
| `/video` | `ext`     | `""`      | Preferred container: mp4/webm/mkv…; adds `--merge-output-format` |
| `/audio` | `quality` | `"bestaudio[ext=m4a]"` | Raw yt-dlp format selector for the source stream |
| `/audio` | `format`  | `"mp3"`   | Output audio container: mp3/m4a/opus/wav… (`--audio-format`) |
| both     | `subdir`  | `"downloads"` | Subdir under `$DATA_DIR`               |

Response: `{ "ok": true, "directory": "/data/<subdir>", "files": [...] }`

> `+` inside a selector must be URL-encoded (`%2B`) in a query string; a raw `+`
> decodes to a space. Use `curl -G --data-urlencode`.

Example:
```bash
curl -G "localhost:8000/video/dQw4w9WgXcQ" \
  --data-urlencode 'quality=bestvideo[width<=1280][height<=720][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/(mp4)'
```

## Configuration (env vars)

| Env var        | Default   | Notes                                      |
| -------------- | --------- | ------------------------------------------ |
| `DATA_DIR`     | `./data`  | Base dir for downloads (default is the code default; image sets `/data`) |
| `PORT`         | `8000`    | uvicorn listen port                        |
| `YTDLP_UPDATE` | `1`       | If `1`, runs `yt-dlp -U` at startup        |

## Build & run

```bash
# Docker image
docker build -t yt-dlp-api:latest .

# Compose (local dev)
docker compose up -d --build

# Plain docker run
mkdir -p data
docker run -d --name yt-dlp-api -p 8000:8000 \
  -e DATA_DIR=/data -v "$(pwd)/data:/data" yt-dlp-api:latest
```

The image has `VOLUME ["/data"]` and the container default `DATA_DIR=/data`.
Outside the container, the Python default is `./data` (relative to cwd).

### Local (no Docker)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# needs the yt-dlp binary + ffmpeg on PATH
curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && chmod +x /usr/local/bin/yt-dlp
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Smoke test

```bash
curl localhost:8000/                                  # health + version
curl localhost:8000/info/dQw4w9WgXcQ                  # metadata
curl localhost:8000/formats/dQw4w9WgXcQ               # available qualities
curl localhost:8000/transcript/dQw4w9WgXcQ            # plain-text transcript
curl -G "localhost:8000/audio/dQw4w9WgXcQ" \
  --data-urlencode 'quality=bestaudio[ext=m4a]' --data-urlencode 'format=mp3'
curl -G "localhost:8000/video/dQw4w9WgXcQ" \
  --data-urlencode 'quality=bestvideo[width<=1280][height<=720][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/(mp4)'
curl -X PROPFIND -H "Depth: 1" localhost:8000/dav/     # WebDAV read-only listing
ls data/                                              # check output
```

Verified working: 720p mp4 video, mp3/opus audio, transcript, 404 on bad info
IDs (502 on failed downloads), files persist via the volume. The image
self-updated yt-dlp to `2026.07.04`.

## Publishing

### To a container registry

```bash
./scripts/publish.sh ghcr.io/<your-user>     # builds + pushes yt-dlp-api:latest
```

### CasaOS store integration

- The CasaOS store lives at `/Users/fchazal/Developer/casaos-store` (repo
  `https://github.com/fchazal/casaos-store.git`).
- `Apps/youtube/docker-compose.yml` builds this app **from this repo's git URL**
  (`build.context: https://github.com/fchazal/youtube-downloader.git`), so the
  CasaOS server builds the image itself — no registry needed. If the app repo
  moves, update that `build.context`.
- Store URL used in CasaOS (must be the archive, not the plain `.git` URL):
  `https://github.com/fchazal/casaos-store/archive/refs/heads/main.zip`
  (CasaOS's `go-getter` does not treat a plain `.git` URL as a git repo).
- To update what CasaOS serves: commit + **push** both repos, then in CasaOS
  **remove and re-add** the store (CasaOS caches the catalog; GitHub archives
  return no `Content-Length`, so in-place updates are skipped).

## Gotchas / decisions to keep in mind

- **yt-dlp must be the binary, not the pip module** — `main.py` imports only
  `subprocess` for it. Do not `import yt_dlp`.
- yt-dlp self-update via `pip` fails; the standalone binary (`yt-dlp -U`) is
  what works. That's why the Dockerfile downloads the binary with curl.
- `quality` on `/video` and `/audio` is a **raw yt-dlp format selector** passed
  straight to `--format` (no builder anymore — `build_video_format` was removed).
  A `+` must be `%2B`-encoded in the query string, else it decodes to a space. If
  a selector matches nothing, yt-dlp errors (502).
- `/video` and `/audio` are **GET** (replaced the old `POST /download`); `subdir`
  is now a query param (default `downloads`). The dropped features were: raw
  `format` selector (now `quality`), subtitle writing/embedding, playlist,
  audio bitrate (kbps). Subtitles-as-text are covered by `/transcript`.
- `/video` calls **ffmpeg** post-download to embed the thumbnail + metadata. The
  remux uses `-c copy` (no re-encode), a temp file in the same dir then
  `os.replace`, and the thumbnail is deleted afterwards. ffmpeg must be on PATH
  (bundled in the image).
- **wsgidav** (deps: defusedxml, PyYAML) is a new dependency for `/dav`; it is
  mounted **after** the API routes but its `/dav` prefix never collides with
  them. Logging is disabled via `logging.enable: False`.
- Container stop behavior is fine (tested ~1 s); the `sh -c` CMD works, but if
  slow stops ever appear, switch to `exec uvicorn …` or a tini entrypoint.
- `data/` is gitignored; a fresh `DATA_DIR` is auto-created at import time.

## Current status & possible next steps

Done: info / formats / transcript endpoints, REST download interface
(`GET /video?quality=&ext=`, `GET /audio?quality=&format=` with raw yt-dlp
selectors), thumbnail embedding + metadata on videos, read-only WebDAV (`/dav`,
wsgidav), `DATA_DIR` env, Docker image, local compose, publish script, CasaOS
store app entry.

Possible next steps (not started): async downloads with job status, web UI,
playlist export, per-request rate limiting, authentication, tests
(no test suite exists yet — `main.py` has no unit tests).
