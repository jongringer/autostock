"""알림 — JSONL + 선택 웹훅. 사람이 개입해야 하는 사건만 남긴다."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

from ..clock import now_et, stamp_kst
from ..net import HttpTransport, TransportError, UrllibTransport

LOG = logging.getLogger(__name__)


class Alerter:
    def __init__(
        self,
        log_path: Path | str | None = None,
        webhook_url: str = "",
        transport: HttpTransport | None = None,
        clock=now_et,
    ) -> None:
        self.log_path = Path(log_path) if log_path is not None else None
        self.webhook_url = webhook_url
        self.transport = transport or UrllibTransport()
        self._clock = clock

    def notify(self, level: str, message: str, **fields: Any) -> None:
        payload = {
            "at": self._clock().isoformat(),
            "kst": stamp_kst(self._clock()),
            "level": level,
            "message": message,
            **fields,
        }
        LOG.log(
            logging.ERROR if level in ("error", "critical") else logging.WARNING,
            "[ALERT:%s] %s %s",
            level,
            message,
            {key: value for key, value in fields.items() if key != "at"} or "",
        )
        if self.log_path is not None:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            except OSError as exc:  # pragma: no cover
                LOG.warning("알림 로그 기록 실패: %s", exc)
        if self.webhook_url:
            try:
                self.transport.request(
                    "POST",
                    self.webhook_url,
                    json_body={"text": f"[ALPHA SHIELD][{level}] {message}", "fields": fields},
                    timeout=8.0,
                )
            except (TransportError, Exception) as exc:  # noqa: BLE001 - 알림 실패가 매매를 막지 않는다
                LOG.warning("웹훅 알림 실패: %s", exc)


class DecisionLog:
    """사이클마다 판정 근거를 JSONL 로 남긴다 (감사·재현용)."""

    def __init__(self, path: Path | str | None) -> None:
        self.path = Path(path) if path is not None else None

    def write(self, record: Mapping[str, Any]) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:  # pragma: no cover
            LOG.warning("판정 로그 기록 실패: %s", exc)
