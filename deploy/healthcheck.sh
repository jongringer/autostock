#!/usr/bin/env bash
# ALPHA SHIELD healthcheck — cron 또는 모니터링에서 호출. 실패 시 exit 1.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/autostock}"
cd "$APP_DIR"

PY="${PY:-$APP_DIR/.venv/bin/python}"
[[ -x "$PY" ]] || PY="python3"

echo "[healthcheck] $(date -Iseconds) mode=$(grep -E '^ALPHA_MODE=' .env 2>/dev/null | cut -d= -f2 || echo unknown)"

"$PY" -m alpha_shield --check >/dev/null

if [[ ! -f state/alpha_shield.db ]]; then
  echo "[healthcheck] state DB 없음 — 아직 한 번도 실행되지 않았습니다"
  exit 0
fi

"$PY" - <<'PYCODE'
import sqlite3, sys, pathlib
path = pathlib.Path("state/alpha_shield.db")
conn = sqlite3.connect(path)
result = conn.execute("PRAGMA integrity_check").fetchone()[0]
if result != "ok":
    print(f"[healthcheck] DB integrity FAIL: {result}")
    sys.exit(1)
print("[healthcheck] DB ok")
PYCODE

# 최근 사이클이 30분 넘게 없으면 경고(장 마감 시간대는 제외)
if [[ -f logs/alpha-shield.log ]] && find logs/alpha-shield.log -mmin +30 | grep -q .; then
  echo "[healthcheck] WARN: 30분 이상 로그 갱신 없음 (장 마감/휴장일 수 있음)"
fi

if [[ -f state/ARMED && "${LIVE_ORDERS:-false}" != "true" ]]; then
  echo "[healthcheck] WARN: ARMED 파일은 있으나 LIVE_ORDERS!=true"
fi

echo "[healthcheck] ok"
