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
| GET    | `/transcript/{id}`     | Single-line plain-text transcript (auto subs, original language) |
| GET    | `/video/{id}`          | Download video (`?quality=` yt-dlp selector, `&ext=mp4`) |
| GET    | `/audio/{id}`          | Download audio (`?quality=` yt-dlp selector, `&format=mp3`) |
| GET    | `/dav/*`               | Read-only WebDAV server over `$DATA_DIR` |

All endpoints take a **bare YouTube video ID** (`/info/dQw4w9WgXcQ`).

### GET /transcript/{id}

Downloads the auto-generated subtitles in the original language and writes them as
a single-line plain-text transcript to `$DATA_DIR/transcripts/<video_id>.txt`
(suitable for serving from a WebDAV-enabled directory).

```bash
curl localhost:8000/transcript/dQw4w9WgXcQ
# {"video_id":"dQw4w9WgXcQ","transcript_file":"/transcripts/dQw4w9WgXcQ.txt"}
```

### GET /video/{id}

Downloads the video at a **yt-dlp format selector** into a subdirectory of
`$DATA_DIR`, then embeds the video thumbnail as cover art (attached picture) and
writes `title`, `network` (watch URL) and `copyright` (upload date) metadata into
the file.

| Query param | Default | Description                              |
| ----------- | ------- | ---------------------------------------- |
| `quality`   | `""`    | Raw yt-dlp format selector (see below); empty = best |
| `ext`       | `""`    | Preferred container: mp4, webm, mkv… (empty = yt-dlp default) |
| `subdir`    | `downloads` | Subdirectory under `$DATA_DIR`       |

Example selectors:

```
bestaudio[ext=m4a]
bestvideo[width<=1280][height<=720][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/(mp4)
bestvideo[width<=1920][height<=1080][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/(mp4)
bestvideo[height<=480][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/(mp4)
```

> The `+` in a selector must be URL-encoded as `%2B` inside a query string (a raw
> `+` decodes to a space). Use `curl -G --data-urlencode` to build the URL safely:

```bash
curl -G "localhost:8000/video/dQw4w9WgXcQ" \
  --data-urlencode 'quality=bestvideo[width<=1280][height<=720][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/(mp4)'
```

### GET /audio/{id}

Downloads the audio track only, selected with a yt-dlp format selector and
converted to the requested container.

| Query param | Default           | Description                          |
| ----------- | ----------------- | ------------------------------------ |
| `quality`   | `bestaudio[ext=m4a]` | Raw yt-dlp format selector for the source stream |
| `format`    | `mp3`             | Output audio container: mp3, m4a, opus, wav… |
| `subdir`    | `downloads`       | Subdirectory under `$DATA_DIR`       |

```bash
curl -G "localhost:8000/audio/dQw4w9WgXcQ" \
  --data-urlencode 'quality=bestaudio[ext=m4a]' --data-urlencode 'format=opus'
```

Downloads land in a **subdirectory** of `$DATA_DIR`. Response:
`{ "ok": true, "directory": "/data/<subdir>", "files": [...] }`

### WebDAV (read-only) — `/dav/*`

A read-only [WebDAV](https://en.wikipedia.org/wiki/WebDAV) server (via
`wsgidav`) exposes `$DATA_DIR` so downloaded files can be browsed, mounted or
synced (Finder, rclone, davfs…). Files are served **by their real name**:

```
/dav/downloads/<title>.mp4      # video/audio downloads
/dav/transcripts/<video_id>.txt # plain-text transcripts
```

- Write methods (`PUT`, `DELETE`, `MKCOL`, …) return `403`.
- The directory listing at `/dav/` is a browsable HTML index.
- URL-encode spaces in file names (`%20`).

```bash
curl -X PROPFIND -H "Depth: 1" localhost:8000/dav/
curl localhost:8000/dav/downloads/
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
curl -G "localhost:8000/audio/dQw4w9WgXcQ" \
  --data-urlencode 'quality=bestaudio[ext=m4a]' --data-urlencode 'format=mp3'
curl -G "localhost:8000/video/dQw4w9WgXcQ" \
  --data-urlencode 'quality=bestvideo[width<=1280][height<=720][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/(mp4)'
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
