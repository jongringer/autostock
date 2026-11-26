"""가벼운 TTL 캐시 — 외부 데이터 호출을 줄이고 '확인 불가' 상태를 명시한다."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generic, Iterator, TypeVar

T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    value: T
    expires_at: float


class TTLCache(Generic[T]):
    """메모리 TTL 캐시. `get_or_load` 로 실패를 캐시하지 않는다(예외는 그대로 전파)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._data: dict[str, _Entry[T]] = {}
        self._clock = clock

    def get(self, key: str) -> T | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            self._data.pop(key, None)
            return None
        return entry.value

    def peek(self, key: str) -> T | None:
        """만료됐어도 마지막 값을 돌려준다 (stale-while-error 표시용)."""
        entry = self._data.get(key)
        return None if entry is None else entry.value

    def set(self, key: str, value: T, ttl_seconds: float) -> T:
        self._data[key] = _Entry(value=value, expires_at=self._clock() + ttl_seconds)
        return value

    def get_or_load(self, key: str, ttl_seconds: float, loader: Callable[[], T]) -> T:
        cached = self.get(key)
        if cached is not None:
            return cached
        value = loader()
        return self.set(key, value, ttl_seconds)

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._data.clear()
        else:
            self._data.pop(key, None)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def __len__(self) -> int:
        return len(self._data)

    def items(self) -> Iterator[tuple[str, T]]:
        for key in list(self._data):
            value = self.get(key)
            if value is not None:
                yield key, value


class JsonFileCache:
    """프로세스 재시작 간 유지되는 작은 JSON 캐시 (일일 캘린더 등)."""

    def __init__(self, path: Path, ttl_seconds: float, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds
        self._clock = clock

    def read(self) -> object | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if float(payload.get("expires_at", 0)) <= self._clock():
            return None
        return payload.get("value")

    def write(self, value: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"expires_at": self._clock() + self.ttl_seconds, "value": value}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
