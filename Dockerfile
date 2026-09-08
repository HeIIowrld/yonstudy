FROM python:3.12-slim-bookworm

ARG PLAYWRIGHT_VERSION=1.62.0

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        cron \
        ffmpeg \
        rclone \
        tini \
        tzdata \
        util-linux \
    && python -m pip install --no-cache-dir \
        "playwright==${PLAYWRIGHT_VERSION}" \
        "pypdf==6.14.2" \
    && python -m playwright install --with-deps chromium \
    && python -m playwright install chrome \
    && install -d /root/.cache \
    && ln -sfn /ms-playwright /root/.cache/ms-playwright \
    && ln -snf /usr/share/zoneinfo/Asia/Seoul /etc/localtime \
    && echo Asia/Seoul >/etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY cli.py README.md ./
COPY yonstudy ./yonstudy
COPY deploy ./deploy

RUN chmod 0755 /app/deploy/container-entrypoint.sh /app/deploy/run-job.sh \
        /app/deploy/run_job.py /app/deploy/update-loop.sh \
        /app/deploy/update-once.sh \
    && install -m 0644 /app/deploy/yonstudy.cron /etc/cron.d/yonstudy \
    && /usr/local/bin/python /app/deploy/check_browser.py

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/deploy/container-entrypoint.sh"]
