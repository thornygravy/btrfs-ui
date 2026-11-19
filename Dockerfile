FROM python:3.13-slim

# btrfs-progs and snapper do the real work; tini reaps the background
# processes that scrub/balance leave behind; tzdata makes the TZ variable work.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends btrfs-progs snapper tini tzdata util-linux \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /var/lib/btrfs

WORKDIR /srv/btrfs-ui
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8008
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD \
  python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/api/session' % os.environ.get('BTRFS_UI_PORT','8008'), timeout=3)"

# Runs as root on purpose: btrfs ioctls need CAP_SYS_ADMIN (see docker-compose.yml).
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app"]
