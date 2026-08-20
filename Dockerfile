# Build stage
FROM python:3.12-slim AS build

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    unzip \
    git \
    build-essential \
    libcairo2-dev \
    libjpeg-dev \
    libpango1.0-dev \
    libgif-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp \
    && chmod +x /usr/local/bin/yt-dlp

# Install Deno JS runtime (required for EJS + bgutil server)
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh

# Install bgutil-ytdlp-pot-provider (PO token generation)
RUN git clone --single-branch --branch 1.3.1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-ytdlp-pot-provider \
    && rm -rf /opt/bgutil-ytdlp-pot-provider/.git

WORKDIR /opt/bgutil-ytdlp-pot-provider/server
RUN deno install --allow-scripts=npm:canvas

# Install yt-dlp plugin from cloned repo
RUN mkdir -p /root/.config/yt-dlp/plugins \
    && cp -r /opt/bgutil-ytdlp-pot-provider/plugin /root/.config/yt-dlp/plugins/bgutil-ytdlp-pot-provider

# Runtime stage
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    libcairo2 \
    libjpeg62-turbo \
    libpango-1.0-0 \
    libgif7 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin /usr/local/bin

# Copy bgutil server (source + node_modules for PO token HTTP server)
COPY --from=build /opt/bgutil-ytdlp-pot-provider /opt/bgutil-ytdlp-pot-provider

# Copy yt-dlp plugin
COPY --from=build /root/.config/yt-dlp /root/.config/yt-dlp

COPY main.py .

# Configure yt-dlp for EJS + PO token support:
# - js-runtimes deno: use Deno as the JS runtime for YouTube challenge solving
# - remote-components ejs:github: auto-download EJS scripts from GitHub as fallback
# - youtubepot-bgutilhttp: connect to local bgutil HTTP server for PO tokens
RUN cat > /etc/yt-dlp.conf <<'EOF'
--js-runtimes deno
--remote-components ejs:github
--extractor-args "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416"
EOF

ENV DATA_DIR=/data
ENV YTDLP_UPDATE=1
ENV PORT=8000

VOLUME ["/data"]

EXPOSE 8000

CMD ["sh", "-c", "\
  if [ \"$YTDLP_UPDATE\" = \"1\" ]; then yt-dlp -U || true; fi; \
  cd /opt/bgutil-ytdlp-pot-provider/server/node_modules && \
  deno run --allow-env --allow-net --allow-ffi=. --allow-read=. ../src/main.ts & \
  sleep 3; \
  uvicorn main:app --host 0.0.0.0 --port $PORT"]
