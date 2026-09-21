# syntax=docker/dockerfile:1.7

FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        gcc \
        g++ \
        libffi-dev \
        libxml2-dev \
        libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv

COPY requirements.txt requirements.lock ./
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --no-deps -r requirements.lock \
    && python -m pip check


FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}" \
    TRADINGBUFFETT_CACHE_DIR=/app/tradingagents/dataflows/data_cache \
    TRADINGBUFFETT_RESULTS_DIR=/app/.tradingbuffett/results \
    TRADINGBUFFETT_MEMORY_LOG_PATH=/app/.tradingbuffett/memory/trading_memory.md \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libgomp1 \
        libxml2 \
        libxslt1.1 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY . .

RUN python -m pip install --no-deps -e . \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p \
        /app/tradingagents/dataflows/data_cache \
        /app/.tradingbuffett/memory \
        /tmp/matplotlib \
    && chown -R app:app /app /tmp/matplotlib

USER app

ENTRYPOINT ["tradingagents"]
CMD ["--help"]
