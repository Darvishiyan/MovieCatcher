FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --no-install-recommends --yes build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --requirement /tmp/requirements.txt


FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 moviecatcher \
    && useradd --system --uid 1000 --gid moviecatcher --home-dir /app moviecatcher \
    && mkdir -p /app /data /downloads \
    && chown -R moviecatcher:moviecatcher /app /data /downloads

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=moviecatcher:moviecatcher bot.py ./

USER moviecatcher
STOPSIGNAL SIGTERM

CMD ["python", "bot.py"]
