#!/usr/bin/env bash
set -e

# wait for postgres to accept connections before starting (compose starts both
# at once; the api shouldn't crash-loop while pg is still coming up)
if [[ "$DATABASE_URL" == postgresql* ]]; then
  echo "waiting for postgres..."
  python - <<'PY'
import os, time
from sqlalchemy import create_engine, text
url = os.environ["DATABASE_URL"]
for i in range(30):
    try:
        create_engine(url).connect().execute(text("SELECT 1"))
        print("postgres is up"); break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("postgres did not come up in time")
PY
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
