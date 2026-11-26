#!/usr/bin/env bash
# ALPHA SHIELD watchdog — 5분 cron 권장.
# 10분 이상 로그 갱신이 없으면 서비스를 재시작하고 alerts.jsonl 에 남긴다.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/autostock}"
SERVICE="${SERVICE:-alpha-shield}"
LOG="$APP_DIR/logs/alpha-shield.log"
ALERT="$APP_DIR/logs/alerts.jsonl"
STALE_MIN="${STALE_MIN:-10}"

mkdir -p "$APP_DIR/logs"

if ! systemctl is-active --quiet "$SERVICE"; then
  echo "{\"at\":\"$(date -Iseconds)\",\"level\":\"error\",\"message\":\"watchdog: service inactive -> restart\"}" >> "$ALERT"
  systemctl restart "$SERVICE"
  exit 0
fi

if [[ -f "$LOG" ]]; then
  if find "$LOG" -mmin +"$STALE_MIN" | grep -q .; then
    echo "{\"at\":\"$(date -Iseconds)\",\"level\":\"error\",\"message\":\"watchdog: log stale ${STALE_MIN}m -> restart\"}" >> "$ALERT"
    systemctl restart "$SERVICE"
  fi
fi
