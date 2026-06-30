FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC \
    PYTGCALL_LOG_LEVEL=warning \
    TMPDIR=/tmp/singerbot_cache

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
       ca-certificates \
       curl \
       ffmpeg \
       libopus0 \
       libopus-dev \
       build-essential \
       gcc \
       python3-dev \
       git \
       procps \
  && rm -rf /var/lib/apt/lists/* \
  \
  && useradd --create-home --shell /bin/bash botuser \
  && mkdir -p /tmp/singerbot_cache \
  && chown -R botuser:botuser /tmp/singerbot_cache \
  && chmod 700 /tmp/singerbot_cache

USER botuser
WORKDIR /home/botuser/app

COPY --chown=botuser:botuser requirements.txt ./

RUN python -m pip install --upgrade pip setuptools wheel \
  && python -m pip install -r requirements.txt

COPY --chown=botuser:botuser . .

ENV TMPDIR=/tmp/singerbot_cache

CMD ["python", "bot.py"]

HEALTHCHECK --interval=1m --timeout=10s --start-period=30s --retries=3 \
  CMD pgrep -f "python.*bot.py" || exit 1
