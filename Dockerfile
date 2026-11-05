FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates ffmpeg unzip fonts-liberation fonts-noto-core \
      libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libgbm1 libasound2 libxkbcommon0 \
      libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir -r /app/requirements.txt gunicorn yt-dlp
ENV PATH=/venv/bin:$PATH DATA_DIR=/data

# Every deploy passes a new value, so HyperFrames and yt-dlp are the latest ones.
ARG FRESH=0
RUN npm install -g hyperframes@latest && /venv/bin/pip install --no-cache-dir -U yt-dlp

COPY . /app
WORKDIR /app
RUN mkdir -p /data && chown -R node:node /data /app
USER node
RUN hyperframes browser ensure

EXPOSE 8080
# One process: the job queue lives in it. Threads serve the pages while a video renders.
CMD ["gunicorn", "--workers", "1", "--threads", "16", "--timeout", "0", "--bind", "0.0.0.0:8080", "app:app"]
