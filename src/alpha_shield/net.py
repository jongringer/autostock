"""HTTP 전송 계층.

테스트는 이 `HttpTransport` 프로토콜을 가짜로 구현해 실제 네트워크 없이
토스증권·Yahoo·SEC·Nasdaq 요청 형식을 검증한다.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0
DEFAULT_UA = "alpha-shield/3.2 (+https://github.com/jongringer/autostock)"


class TransportError(RuntimeError):
    """네트워크 자체 실패 (연결·타임아웃)."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    url: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        charset = "utf-8"
        content_type = self.headers.get("Content-Type", "")
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        return self.body.decode(charset, errors="replace")

    def header(self, name: str, default: str | None = None) -> str | None:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return default


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        json_body: Any | None = None,
        form: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HttpResponse: ...


class UrllibTransport:
    """표준 라이브러리만 쓰는 실제 전송 구현."""

    def __init__(self, user_agent: str = DEFAULT_UA, sleep=time.sleep) -> None:
        self.user_agent = user_agent
        self._sleep = sleep
        self.calls = 0

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        json_body: Any | None = None,
        form: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HttpResponse:
        if params:
            encoded = urllib.parse.urlencode(
                {key: value for key, value in params.items() if value is not None}, doseq=True
            )
            url = f"{url}{'&' if '?' in url else '?'}{encoded}"
        data: bytes | None = None
        final_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            final_headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            final_headers["Content-Type"] = "application/x-www-form-urlencoded"
        if headers:
            final_headers.update({k: v for k, v in headers.items()})
        request = urllib.request.Request(url, data=data, headers=final_headers, method=method.upper())
        self.calls += 1
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                return HttpResponse(
                    status=response.status,
                    headers=dict(response.headers.items()),
                    body=body,
                    url=url,
                )
        except urllib.error.HTTPError as exc:  # 4xx/5xx 도 응답으로 돌려준다
            body = exc.read() if exc.fp else b""
            return HttpResponse(
                status=exc.code,
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=body,
                url=url,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"{method} {url} 실패: {exc}") from exc


class RetryingTransport:
    """429/5xx/네트워크 오류에 지수 백오프를 적용하는 데코레이터.

    `Retry-After` 헤더가 있으면 그 값을 우선한다.
    """

    def __init__(
        self,
        inner: HttpTransport,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        sleep=time.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self.inner = inner
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._sleep = sleep
        self.log = logger or LOG

    def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.inner.request(method, url, **kwargs)
            except TransportError:
                if attempt > self.max_retries:
                    raise
                self._sleep(self._delay(attempt, None))
                continue
            if response.status == 429 or 500 <= response.status < 600:
                if attempt > self.max_retries:
                    return response
                retry_after = response.header("Retry-After")
                self._sleep(self._delay(attempt, retry_after))
                continue
            return response

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(self.max_delay, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
