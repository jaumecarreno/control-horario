#!/usr/bin/env sh
set -eu

log() {
  echo "[deploy-check] $1"
}

APP_DIR="${APP_DIR:-/app}"
if [ -d "$APP_DIR" ]; then
  cd "$APP_DIR"
fi

log "Applying migrations (alembic upgrade head)"
python -m alembic upgrade head

log "Current migration revision"
python -m alembic current

if [ "${RUN_SMOKE_CHECKS:-1}" != "1" ]; then
  log "RUN_SMOKE_CHECKS=0, skipping HTTP smoke checks"
  exit 0
fi

if [ -z "${APP_URL:-}" ]; then
  log "APP_URL is not set, skipping HTTP smoke checks"
  exit 0
fi

if ! command -v curl >/dev/null 2>&1; then
  log "curl is not installed, skipping HTTP smoke checks"
  exit 0
fi

ENDPOINTS="${SMOKE_ENDPOINTS:-/admin/turnos /admin/approvals /me/presence-control /me/pause-control /me/leaves}"
failed=0
base_url="${APP_URL%/}"

log "Running smoke checks against ${base_url}"
for endpoint in $ENDPOINTS; do
  url="${base_url}${endpoint}"
  code="$(curl -k -L -s -o /dev/null -w "%{http_code}" "$url" || true)"
  case "$code" in
    2*|3*)
      log "OK ${endpoint} -> ${code}"
      ;;
    *)
      log "FAIL ${endpoint} -> ${code}"
      failed=1
      ;;
  esac
done

if [ "$failed" -ne 0 ]; then
  log "Smoke checks failed"
  exit 1
fi

log "All checks passed"
