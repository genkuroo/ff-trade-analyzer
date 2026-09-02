#!/bin/sh
# Prepares the container's data directory, then hands off to the real command.
set -e

mkdir -p "$(dirname "${FFTA_DB:-/data/ffta.db}")" "${FFTA_CACHE:-/data/cache}"

# A fresh volume means an empty database, and an empty database means the
# dashboard serves a 503 until the first scheduled sync fires — which could be
# fifteen minutes away. Seeding on first boot only (guarded on the file not
# existing) makes a cold start immediately useful without re-syncing on every
# restart.
if [ ! -f "${FFTA_DB:-/data/ffta.db}" ]; then
	echo "no database present — running an initial sync"
	python cli.py sync || echo "initial sync failed; the timer will retry"
fi

exec "$@"
