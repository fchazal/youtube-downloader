# AGENT.md — youtube-downloader

Everything needed to resume work on this project. Read this first.

## Overview

A Dockerized HTTP API around [yt-dlp](https://github.com/yt-dlp/yt-dlp) that lets
you fetch video metadata, list available formats/qualities, and download
videos/audio/subtitles into dedicated subdirectories of a configurable data directory.

YouTube requires EJS (External JS Scripts) for challenge solving and PO Tokens
(Proof of Origin) to avoid bot detection. Both are handled automatically via
[Deno](https://deno.com) and the
[bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
plugin running an HTTP server on port 4416 inside the container.

It is published as the **`youtube`** app of the CasaOS store
(`/Users/fchazal/Developer/casaos-store`), which builds the image on the server
from this repo's git URL.

## Tech stack

- Python 3.12 + **FastAPI** + **uvicorn** (synchronous endpoints; yt-dlp runs in a subprocess)
- **yt-dlp standalone binary** (installed in the image, self-updates at startup)
- **Deno** JS runtime (EJS challenge solving + bgutil PO token server)
- **bgutil-ytdlp-pot-provider** (PO Token generation via BotGuard attestation)
- **ffmpeg** (audio extraction, format merging, subtitle embedding)
- Docker (multi-stage build), Docker Compose for local dev

## Repo structure

```
youtube-downloader/
├── main.py              # FastAPI app: health, info, formats, download
├── Dockerfile           # multi-stage: ffmpeg + yt-dlp binary + Python deps + Deno + bgutil
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
- **EJS (External JS Scripts)**: YouTube now requires solving JavaScript challenges
  for video extraction. Deno is installed in the container as the JS runtime, and
  yt-dlp is configured with `--js-runtimes deno` + `--remote-components ejs:github`
  (fallback). EJS scripts are bundled with the yt-dlp standalone binary.
- **PO Tokens (Proof of Origin)**: YouTube enforces BotGuard attestation tokens to
  block automated access. The
  [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
  plugin is installed in `~/.config/yt-dlp/plugins/` and connects to an HTTP server
  (`http://127.0.0.1:4416`) running inside the container. The server uses Deno +
  BgUtils to generate PO tokens per-video. The `--extractor-args
  "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416"` flag is set in
  `/etc/yt-dlp.conf`. The health endpoint reports `pot_provider: "ok" | "unavailable"`.
- `normalize_target()` wraps a bare video ID into
  `https://www.youtube.com/watch?v=<id>`. **URLs are not accepted** — the routes
  use `{id}`, so only bare IDs work.
- Downloads go into dedicated subdirectories under `$DATA_DIR`:
  - `videos/` — video downloads (mp4, avc1 codec)
  - `audios/` — audio downloads (mp4)
  - `transcripts/` — plain-text transcripts
- `run_ytdlp()` has a 3600 s timeout and captures output; errors are surfaced as
  HTTP errors (404 for unknown videos, 502 for download failures).
- `GET /video/{id}` always produces **mp4 with avc1 codec**. The default format
  selector is `bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/best[ext=mp4]`.
  A custom selector can be passed via the `quality` query param (must stay avc1/mp4).
  Post-download, ffmpeg embeds the thumbnail as cover art and sets title/network/date
  metadata.
- `GET /audio/{id}` always produces **mp4 audio** via `--extract-audio --audio-format mp4`.
  The default format selector is `bestaudio[ext=m4a]/bestaudio`. A custom selector
  can be passed via the `quality` query param. Post-download, ffmpeg embeds the
  thumbnail as cover art (same as video).
- `GET /transcript/{id}` downloads the auto-generated subtitles in the original
  language (`--skip-download --sub-format ttml --sub-langs ".*orig"` +
  `--write-auto-subs`), strips the HTML/ttml tags, joins everything into a
  single line, and writes `$DATA_DIR/transcripts/<video_id>.txt`. That directory
  is meant to be served as public WebDAV. The response is
  `{ "video_id": ..., "transcript_file": "/transcripts/<id>.txt" }`.
- **WebDAV**: `wsgidav` is mounted on `/dav` via `WSGIMiddleware`, serving
  `$DATA_DIR` **read-only** (`FilesystemProvider(..., readonly=True)`). Files are
  addressed by their real name (`/dav/videos/<title>.mp4`,
  `/dav/audios/<title>.mp4`, `/dav/transcripts/<id>.txt`). Anonymous access is granted by
  `simple_dc.user_mapping: {"*": True}`; write methods return 403. The provider
  must be mapped to `/` (Starlette strips the `/dav` mount prefix).

## Endpoints

| Method | Path                    | Description                          |
| ------ | ----------------------- | ------------------------------------ |
| GET    | `/`                     | Health + yt-dlp version + data_dir + pot_provider status |
| GET    | `/info/{id}`           | Full yt-dlp metadata JSON            |
| GET    | `/formats/{id}`        | Clean table of available formats     |
| GET    | `/transcript/{id}`     | Plain-text transcript → `$DATA_DIR/transcripts/<video_id>.txt` |
| GET    | `/video/{id}`          | Download video (mp4/avc1) → `$DATA_DIR/videos/` (`?quality=`) |
| GET    | `/audio/{id}`          | Download audio (mp4) → `$DATA_DIR/audios/` (`?quality=`) |
| GET    | `/dav/*`               | Read-only WebDAV over `$DATA_DIR` (wsgidav) |
| GET    | `/docs`                 | Interactive OpenAPI UI               |

### GET /video and GET /audio query params

| Endpoint | Param     | Default   | Notes                                     |
| -------- | --------- | --------- | ----------------------------------------- |
| `/video` | `quality` | `bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/best[ext=mp4]` | **Raw yt-dlp format selector**, passed as `--format`. Must target avc1/mp4. |
| `/audio` | `quality` | `bestaudio[ext=m4a]/bestaudio` | Raw yt-dlp format selector for the source stream |

> `+` inside a selector must be URL-encoded (`%2B`) in a query string; a raw `+`
> decodes to a space. Use `curl -G --data-urlencode`.

Example:
```bash
curl -G "localhost:8000/video/dQw4w9WgXcQ" \
  --data-urlencode 'quality=bestvideo[width<=1920][height<=1080][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/(mp4)'
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
curl localhost:8000/                                  # health + version + pot_provider
curl localhost:8000/info/dQw4w9WgXcQ                  # metadata
curl localhost:8000/formats/dQw4w9WgXcQ               # available qualities
curl localhost:8000/transcript/dQw4w9WgXcQ            # plain-text transcript
curl -G "localhost:8000/audio/dQw4w9WgXcQ"            # audio mp4
curl -G "localhost:8000/video/dQw4w9WgXcQ" \
  --data-urlencode 'quality=bestvideo[width<=1920][height<=1080][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/(mp4)'
curl -X PROPFIND -H "Depth: 1" localhost:8000/dav/     # WebDAV read-only listing
ls data/videos/ data/audios/ data/transcripts/         # check output dirs
```

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
  straight to `--format`. A `+` must be `%2B`-encoded in the query string, else
  it decodes to a space. If a selector matches nothing, yt-dlp errors (502).
- **Video**: always mp4 with avc1 codec. The default selector includes
  `[vcodec^=avc1][ext=mp4]` constraints. Users can refine quality/resolution
  but not change the codec or container.
- **Audio**: always mp4. Uses `--extract-audio --audio-format mp4`.
- Both `/video` and `/audio` call **ffmpeg** post-download to embed the
  thumbnail + metadata. The remux uses `-c copy` (no re-encode), a temp file
  in the same dir then `os.replace`.
- **wsgidav** (deps: defusedxml, PyYAML) is a dependency for `/dav`; it is
  mounted **after** the API routes but its `/dav` prefix never collides with
  them. Logging is disabled via `logging.enable: False`.
- Container stop behavior is fine (tested ~1 s); the `sh -c` CMD works, but if
  slow stops ever appear, switch to `exec uvicorn …` or a tini entrypoint.
- `data/` is gitignored; a fresh `DATA_DIR` is auto-created at import time.
- **Subdirectories are fixed**: `videos/`, `audios/`, `transcripts/`. No
  user-configurable subdir param anymore.

## Current status & possible next steps

Done: info / formats / transcript endpoints, REST download interface
(`GET /video?quality=`, `GET /audio?quality=` with raw yt-dlp selectors),
thumbnail embedding + metadata on both video and audio, read-only WebDAV (`/dav`,
wsgidav), `DATA_DIR` env, Docker image, local compose, publish script, CasaOS
store app entry, EJS support (Deno JS runtime), PO Token provider
(bgutil-ytdlp-pot-provider HTTP server on port 4416), fixed output directories
(`videos/`, `audios/`, `transcripts/`).

Possible next steps (not started): async downloads with job status, web UI,
playlist export, per-request rate limiting, authentication, tests
(no test suite exists yet — `main.py` has no unit tests).
