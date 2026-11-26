"""토스증권 Open API 클라이언트 (인증·레이트리밋·에러) + 시세 어댑터.

공식 스펙 `research/toss.json` (Open API 1.2.28) 기준.

핵심 제약
- 토큰은 클라이언트당 1개. 재발급하면 이전 토큰이 즉시 무효 → 토큰 파일 공유 + 파일 락.
- 계좌·자산·주문 API 는 `X-Tossinvest-Account` 헤더 필수.
- 허용 IP 미등록이면 403 `ip-not-allowed` / 토큰 발급은 `access_denied`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from ..cache import TTLCache
from ..clock import parse_iso
from ..models import Bar, Quote
from ..net import HttpResponse, HttpTransport, UrllibTransport
from ..numeric import dec

LOG = logging.getLogger(__name__)

try:  # POSIX 파일 락 (README 는 Linux/서버 기준)
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

TOKEN_REFRESH_MARGIN_SECONDS = 600


class TossApiError(RuntimeError):
    def __init__(
        self,
        status: int,
        code: str,
        message: str = "",
        data: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(f"[{status} {code}] {message}")
        self.status = status
        self.code = code
        self.message = message
        self.data = dict(data or {})
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        return self.status == 429 or 500 <= self.status < 600


class TossAuthError(TossApiError):
    """토큰 발급 실패 (허용 IP 미등록 포함)."""


class InsufficientBuyingPower(TossApiError):
    pass


def _error_code(response: HttpResponse) -> str:
    try:
        payload = response.json() or {}
    except ValueError:
        return ""
    return str(((payload.get("error") or {}).get("code")) or "")


class TossClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: Path | str,
        *,
        base_url: str = "https://openapi.tossinvest.com",
        account_seq: int | None = None,
        transport: HttpTransport | None = None,
        sleep=time.sleep,
        clock=time.time,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = Path(token_path)
        self._account_seq = account_seq
        self.transport = transport or UrllibTransport()
        self._sleep = sleep
        self._clock = clock
        self._client_tag = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:12]
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._account_cache: int | None = account_seq

    # ------------------------------------------------------------------ 토큰
    def _read_token_file(self) -> Mapping[str, Any] | None:
        try:
            payload = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("client") != self._client_tag:
            return None
        return payload

    def _write_token_file(self, token: str, expires_at: float) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "access_token": token,
            "expires_at": expires_at,
            "obtained_at": self._clock(),
            "client": self._client_tag,
        }
        tmp = self.token_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
        finally:
            try:
                os.chmod(tmp, 0o600)
            except OSError:  # pragma: no cover
                pass
        tmp.replace(self.token_path)

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.token_path.with_suffix(".lock")
        handle = open(lock_path, "a+")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _valid(self, payload: Mapping[str, Any] | None) -> bool:
        if not payload:
            return False
        try:
            return float(payload.get("expires_at", 0)) - TOKEN_REFRESH_MARGIN_SECONDS > self._clock()
        except (TypeError, ValueError):
            return False

    def _request_new_token(self) -> tuple[str, float]:
        response = self.transport.request(
            "POST",
            f"{self.base_url}/oauth2/token",
            form={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        if not response.ok:
            try:
                payload = response.json() or {}
            except ValueError:
                payload = {}
            code = str(payload.get("error") or f"http-{response.status}")
            message = str(payload.get("error_description") or payload.get("message") or "")
            raise TossAuthError(response.status, code, message or "토큰 발급 실패")
        payload = response.json() or {}
        token = payload.get("access_token")
        if not token:
            raise TossAuthError(500, "invalid-token-response", "access_token 이 없습니다")
        expires_in = float(payload.get("expires_in", 3600))
        return str(token), self._clock() + expires_in

    def access_token(self, force: bool = False) -> str:
        now = self._clock()
        if not force and self._token and self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS > now:
            return self._token
        with self._file_lock():
            payload = self._read_token_file()
            if not force and self._valid(payload):
                self._token = str(payload["access_token"])
                self._token_expires_at = float(payload["expires_at"])
                return self._token
            token, expires_at = self._request_new_token()
            self._write_token_file(token, expires_at)
            self._token = token
            self._token_expires_at = expires_at
            LOG.info("토스 토큰 재발급 완료 (만료 %.0f초 후)", expires_at - now)
            return token

    # ------------------------------------------------------------------ 요청
    def account_seq(self) -> int:
        if self._account_cache is not None:
            return self._account_cache
        accounts = self.request("GET", "/api/v1/accounts")
        items = accounts if isinstance(accounts, list) else []
        brokerage = [item for item in items if item.get("accountType") == "BROKERAGE"] or items
        if not brokerage:
            raise TossApiError(404, "account-not-found", "토스증권 계좌가 없습니다")
        self._account_cache = int(brokerage[0]["accountSeq"])
        return self._account_cache

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
        form: Mapping[str, str] | None = None,
        needs_account: bool = False,
        auth: bool = True,
        retry_auth: bool = True,
    ) -> Any:
        attempt_auth = True
        while True:
            headers: dict[str, str] = {"Accept": "application/json"}
            if auth:
                headers["Authorization"] = f"Bearer {self.access_token(force=not attempt_auth)}"
            if needs_account:
                headers["X-Tossinvest-Account"] = str(self.account_seq())
            response = self.transport.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                params=params,
                json_body=json_body,
                form=form,
            )
            if response.status == 401 and auth and retry_auth and attempt_auth:
                code = _error_code(response)
                if code in ("token-revoked", "invalid-token", "expired-token"):
                    LOG.warning("토스 토큰 무효(%s) — 재발급 후 1회 재시도", code)
                    attempt_auth = False
                    continue
            if not response.ok:
                self._raise_error(response)
            try:
                payload = response.json()
            except ValueError as exc:
                raise TossApiError(response.status, "invalid-json", str(exc)) from exc
            if isinstance(payload, Mapping) and "result" in payload:
                return payload["result"]
            return payload

    def _raise_error(self, response: HttpResponse) -> None:
        try:
            payload = response.json() or {}
        except ValueError:
            payload = {}
        error = payload.get("error") if isinstance(payload, Mapping) else None
        error = error if isinstance(error, Mapping) else {}
        code = str(error.get("code") or f"http-{response.status}")
        message = str(error.get("message") or response.text()[:200])
        data = error.get("data") if isinstance(error.get("data"), Mapping) else {}
        request_id = error.get("requestId") or response.header("X-Request-Id")
        cls = InsufficientBuyingPower if code == "insufficient-buying-power" else TossApiError
        raise cls(response.status, code, message, data, str(request_id) if request_id else None)

    def get(self, path: str, *, params: Mapping[str, object] | None = None, needs_account: bool = False) -> Any:
        return self.request("GET", path, params=params, needs_account=needs_account)

    def post(self, path: str, *, json_body: Mapping[str, object], needs_account: bool = False) -> Any:
        return self.request("POST", path, json_body=json_body, needs_account=needs_account)


class TossMarketData:
    """토스 시세·캔들. NQ/VIX 는 토스에 없으므로 None (Composite 가 Yahoo 로 위임)."""

    def __init__(self, client: TossClient, *, cache: TTLCache | None = None, page_limit: int = 3) -> None:
        self.client = client
        self.cache = cache if cache is not None else TTLCache()
        self.page_limit = page_limit

    def quotes(self, symbols: Sequence[str]) -> Mapping[str, Quote]:
        if not symbols:
            return {}
        key = "quotes:" + ",".join(sorted(symbols))
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        result = self.client.get("/api/v1/prices", params={"symbols": ",".join(symbols)})
        out: dict[str, Quote] = {}
        for item in result or []:
            try:
                timestamp = parse_iso(item["timestamp"]) if item.get("timestamp") else None
                out[item["symbol"]] = Quote(
                    symbol=item["symbol"],
                    last_price=dec(item["lastPrice"]),
                    timestamp=timestamp,
                    currency=item.get("currency", "USD"),
                )
            except (KeyError, TypeError, ValueError) as exc:
                LOG.warning("토스 시세 파싱 실패 %s: %s", item, exc)
        self.cache.set(key, out, ttl_seconds=20)
        return out

    def _candles(self, symbol: str, interval: str, count: int) -> list[Bar]:
        bars: list[Bar] = []
        before: str | None = None
        for _ in range(self.page_limit):
            params: dict[str, object] = {"symbol": symbol, "interval": interval, "count": 200}
            if before:
                params["before"] = before
            result = self.client.get("/api/v1/candles", params=params)
            page = (result or {}).get("candles") or []
            for item in page:
                try:
                    bars.append(
                        Bar(
                            timestamp=parse_iso(item["timestamp"]),
                            open=dec(item["openPrice"]),
                            high=dec(item["highPrice"]),
                            low=dec(item["lowPrice"]),
                            close=dec(item["closePrice"]),
                            volume=dec(item.get("volume", 0)),
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    LOG.warning("토스 캔들 파싱 실패 %s %s: %s", symbol, item, exc)
            before = (result or {}).get("nextBefore")
            if not before or len(bars) >= count:
                break
        bars.sort(key=lambda item: item.timestamp)
        return bars[-count:]

    def daily_bars(self, symbol: str, count: int = 250) -> list[Bar]:
        key = f"daily:{symbol}:{count}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        # SMA200 여유분을 위해 최대 400봉(2페이지)을 확보한다.
        bars = self._candles(symbol, "1d", max(count, 400))
        self.cache.set(key, bars[-count:], ttl_seconds=300)
        return self.cache.get(key) or []

    def intraday_bars(self, symbol: str, minutes: int = 390) -> list[Bar]:
        key = f"intraday:{symbol}:{minutes}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        bars = self._candles(symbol, "1m", max(minutes, 200))
        self.cache.set(key, bars, ttl_seconds=30)
        return bars

    def futures_change_pct(self, symbol: str) -> Decimal | None:
        return None

    def last_daily_close(self, symbol: str):
        return None

    def last_price(self, symbol: str) -> Decimal | None:
        return None
