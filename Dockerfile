# ff-trade-analyzer — production image for the Raspberry Pi (ARM64).
#
# One image, two jobs. It runs as a long-lived gunicorn web service, and the
# homelab systemd timers `docker compose exec` into that same running container
# to run `cli.py sync`. Sharing one container is deliberate: the sync writes the
# SQLite file the dashboard reads, so they must be looking at the same volume.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Pure-Python dependencies only (requests + flask), so no compiler toolchain is
# needed for aarch64 wheels.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .
RUN chmod +x docker-entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["./docker-entrypoint.sh"]

# Two workers is right for a Pi 400: the pages are SQLite reads and a small
# lineup solve, so they are quick, but a scheduled sync running via `exec` in
# the same container should never be able to block a page load.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", \
     "--timeout", "60", "app:app"]
