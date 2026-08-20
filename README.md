# youtube-downloader

Dockerized HTTP API around [yt-dlp](https://github.com/yt-dlp/yt-dlp) for fetching
video metadata and downloading media into a configurable data directory.

YouTube requires EJS (External JS Scripts) for challenge solving and PO Tokens
(Proof of Origin) to bypass bot detection. Both are handled automatically in the
Docker image via Deno and the
[bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
plugin.

> Published to CasaOS through the separate **casaos-store** repository, which
> builds this app from its git URL.

## Endpoints

| Method | Path                    | Description                                  |
| ------ | ----------------------- | -------------------------------------------- |
| GET    | `/`                     | Health check + yt-dlp version + PO token provider status |
| GET    | `/info/{id}`           | Full metadata/properties for a video ID     |
| GET    | `/formats/{id}`        | List of available formats/qualities         |
| GET    | `/transcript/{id}`     | Single-line plain-text transcript → `transcripts/<video_id>.txt` |
| GET    | `/video/{id}`          | Download video (mp4/avc1) → `videos/<title>.mp4` |
| GET    | `/audio/{id}`          | Download audio (mp4) → `audios/<title>.mp4` |
| GET    | `/dav/*`               | Read-only WebDAV server over `$DATA_DIR` |

All endpoints take a **bare YouTube video ID** (`/info/dQw4w9WgXcQ`).

### Directory structure

```
$DATA_DIR/
├── videos/       # video downloads (mp4, avc1 codec)
├── audios/       # audio downloads (mp4)
└── transcripts/  # plain-text transcripts
```

### GET /video/{id}

Downloads the video as **mp4 (avc1 codec)** with thumbnail embedded as cover art
and `title`, `network` (watch URL) and `copyright` (upload date) metadata.

| Query param | Default | Description                              |
| ----------- | ------- | ---------------------------------------- |
| `quality`   | `bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/best[ext=mp4]` | Raw yt-dlp format selector |

Example selectors:

```
bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/best[ext=mp4]
bestvideo[width<=1920][height<=1080][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/(mp4)
bestvideo[height<=480][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/(mp4)
```

> The `+` in a selector must be URL-encoded as `%2B` inside a query string (a raw
> `+` decodes to a space). Use `curl -G --data-urlencode` to build the URL safely:

```bash
curl -G "localhost:8000/video/dQw4w9WgXcQ" \
  --data-urlencode 'quality=bestvideo[width<=1920][height<=1080][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/(mp4)'
```

### GET /audio/{id}

Downloads the audio track only as **mp4**, with thumbnail embedded as cover art.

| Query param | Default           | Description                          |
| ----------- | ----------------- | ------------------------------------ |
| `quality`   | `bestaudio[ext=m4a]/bestaudio` | Raw yt-dlp format selector for the source stream |

```bash
curl -G "localhost:8000/audio/dQw4w9WgXcQ"
```

### GET /transcript/{id}

Downloads the auto-generated subtitles in the original language and writes them as
a single-line plain-text transcript to `$DATA_DIR/transcripts/<video_id>.txt`
(suitable for serving from a WebDAV-enabled directory).

```bash
curl localhost:8000/transcript/dQw4w9WgXcQ
# {"video_id":"dQw4w9WgXcQ","transcript_file":"/transcripts/dQw4w9WgXcQ.txt"}
```

### WebDAV (read-only) — `/dav/*`

A read-only [WebDAV](https://en.wikipedia.org/wiki/WebDAV) server (via
`wsgidav`) exposes `$DATA_DIR` so downloaded files can be browsed, mounted or
synced (Finder, rclone, davfs…). Files are served **by their real name**:

```
/dav/videos/<title>.mp4       # video downloads
/dav/audios/<title>.mp4       # audio downloads
/dav/transcripts/<video_id>.txt # plain-text transcripts
```

- Write methods (`PUT`, `DELETE`, `MKCOL`, …) return `403`.
- The directory listing at `/dav/` is a browsable HTML index.
- URL-encode spaces in file names (`%20`).

```bash
curl -X PROPFIND -H "Depth: 1" localhost:8000/dav/
curl localhost:8000/dav/videos/
```

## Configuration

| Env var        | Default   | Description                                  |
| -------------- | --------- | -------------------------------------------- |
| `DATA_DIR`     | `./data`  | Base directory for all downloads             |
| `PORT`         | `8000`    | uvicorn listen port                          |
| `YTDLP_UPDATE` | `1`       | Run `yt-dlp -U` at startup to self-update    |

## Run locally

```bash
docker build -t yt-dlp-api:latest .
mkdir -p data
docker run -d --name yt-dlp-api \
  -p 8000:8000 \
  -e DATA_DIR=/data \
  -v "$(pwd)/data:/data" \
  yt-dlp-api:latest
```

Or with the compose file:

```bash
docker compose up -d --build
```

Then:

```bash
curl localhost:8000/
curl localhost:8000/info/dQw4w9WgXcQ
curl localhost:8000/transcript/dQw4w9WgXcQ
curl -G "localhost:8000/audio/dQw4w9WgXcQ"
curl -G "localhost:8000/video/dQw4w9WgXcQ" \
  --data-urlencode 'quality=bestvideo[width<=1920][height<=1080][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/(mp4)'
```

Interactive API docs: http://localhost:8000/docs

## Notes

- **Up-to-date yt-dlp**: the image downloads the standalone yt-dlp binary
  (not pip), so `yt-dlp -U` at startup keeps it current without rebuilds.
- **EJS (External JS Scripts)**: YouTube now requires solving JavaScript
  challenges. [Deno](https://deno.com) is installed as the JS runtime, and yt-dlp
  is configured with `--js-runtimes deno` + `--remote-components ejs:github`.
- **PO Tokens (Proof of Origin)**: YouTube enforces BotGuard attestation tokens.
  The [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
  plugin runs an HTTP server on port 4416 inside the container, generating PO
  tokens automatically via BgUtils. The health endpoint reports
  `pot_provider: "ok" | "unavailable"`.
- **ffmpeg** is bundled for audio extraction, format merging, subtitle embedding,
  thumbnail cover-art.
- **WebDAV** is served by `wsgidav` (read-only), mounted at `/dav` over
  `$DATA_DIR`.
- The API shells out to the `yt-dlp` binary (subprocess) rather than importing
  the Python module, so the binary is the single source of truth.
