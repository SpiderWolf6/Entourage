#!/bin/sh
# fix /data ownership on every startup so the non-root appuser can write to the
# persistent volume regardless of how it was originally created
chown -R appuser:appuser /data 2>/dev/null || true
exec gosu appuser uvicorn server.main:app --host 0.0.0.0 --port 8000 --workers 1
