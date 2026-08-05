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
- `normalize_target()` accepts a full URL **or** a bare video ID (bare IDs are
  wrapped into `https://www.youtube.com/watch?v=<id>`).
- All downloads go into `$DATA_DIR/<subdir>/` (default subdir `downloads`).
- `run_ytdlp()` has a 3600 s timeout and captures output; errors are surfaced as
  HTTP errors (404 for unknown videos, 502 for download failures).

## Endpoints

| Method | Path                    | Description                          |
| ------ | ----------------------- | ------------------------------------ |
| GET    | `/`                     | Health + yt-dlp version + data_dir   |
| GET    | `/info/{url_or_id}`     | Full yt-dlp metadata JSON            |
| GET    | `/formats/{url_or_id}`  | Clean table of available formats     |
| POST   | `/download`             | Download video/audio/subtitles       |
| GET    | `/docs`                 | Interactive OpenAPI UI               |

### POST /download body

| Field             | Type     | Default       | Notes                                    |
| ----------------- | -------- | ------------- | ---------------------------------------- |
| `url_or_id`       | string   | required      | URL or bare video ID                     |
| `subdir`          | string   | `"downloads"` | Subdir under `$DATA_DIR`                 |
| `audio_only`      | bool     | `false`       | Extract audio only                       |
| `video_quality`   | int      | `0`           | Max height: 144/240/360/480/720/1080/1440/2160; `0` = best |
| `video_ext`       | string   | `""`          | Preferred container: mp4/webm/mkv…       |
| `format`          | string   | `""`          | Raw yt-dlp selector; overrides quality/ext |
| `audio_format`    | string   | `"mp3"`       | mp3/m4a/opus/wav… when `audio_only`      |
| `audio_quality`   | int      | `192`         | kbps when `audio_only`                   |
| `write_subtitles` | bool     | `false`       |                                          |
| `subtitle_langs`  | list     | `["en"]`      |                                          |
| `write_auto_subs` | bool     | `false`       |                                          |
| `embed_subtitles` | bool     | `false`       | Also embeds thumbnail                    |
| `playlist`        | bool     | `false`       | Allow multi-entry URLs                   |

Example:
```json
{ "url_or_id": "dQw4w9WgXcQ", "subdir": "music", "audio_only": true, "audio_format": "opus" }
```

Response: `{ "ok": true, "directory": "/data/music", "files": [...] }`

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
curl -X POST localhost:8000/download -H 'Content-Type: application/json' \
  -d '{"url_or_id":"dQw4w9WgXcQ","subdir":"music","audio_only":true}'
ls data/music/                                        # check output
```

Verified working: 720p mp4 download, opus audio, subtitles, 404 on bad IDs,
files persist via the volume. The image self-updated yt-dlp to `2026.07.04`.

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
- Energy/format selectors: `build_video_format()` builds a yt-dlp selector from
  `video_quality`/`video_ext`; raw `format` overrides it.
- Container stop behavior is fine (tested ~1 s); the `sh -c` CMD works, but if
  slow stops ever appear, switch to `exec uvicorn …` or a tini entrypoint.
- `data/` is gitignored; a fresh `DATA_DIR` is auto-created at import time.

## Current status & possible next steps

Done: info / formats / download endpoints, quality+extension selection, audio,
subtitles, `DATA_DIR` env, Docker image, local compose, publish script, CasaOS
store app entry.

Possible next steps (not started): async downloads with job status, web UI,
playlist export, per-request rate limiting, authentication, tests
(no test suite exists yet — `main.py` has no unit tests).
