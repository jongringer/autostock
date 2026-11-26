"""SEC EDGAR — 8-K 구조적 악재 가드.

- `data.sec.gov/submissions/CIK##########.json` 으로 최근 공시 목록
- 8-K 원문(primary document)에서 구조적 악재 키워드 검사
- SEC 는 User-Agent 에 연락처를 요구하고 초당 10회 이하를 권고한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..cache import TTLCache
from ..net import HttpTransport, TransportError

LOG = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}/{document}"


@dataclass(frozen=True)
class Filing:
    form: str
    filing_date: date
    accession: str
    primary_document: str

    def url(self, cik: str) -> str:
        return ARCHIVE_URL.format(
            cik_int=str(int(cik)),
            accession=self.accession.replace("-", ""),
            document=self.primary_document,
        )


@dataclass(frozen=True)
class FilingCheck:
    checked: bool
    blocked: bool = False
    reason: str = ""
    detail: dict = field(default_factory=dict)


class SecFilingsProvider:
    def __init__(
        self,
        transport: HttpTransport,
        user_agent: str,
        *,
        cache: TTLCache | None = None,
    ) -> None:
        self.transport = transport
        self.user_agent = user_agent
        self.cache = cache if cache is not None else TTLCache()

    def _get(self, url: str, ttl: float) -> str | None:
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        try:
            response = self.transport.request(
                "GET", url, headers={"User-Agent": self.user_agent, "Accept": "application/json,text/html"}
            )
        except TransportError as exc:
            LOG.warning("SEC 요청 실패 %s: %s", url, exc)
            return None
        if not response.ok:
            LOG.warning("SEC 응답 %s %s", response.status, url)
            return None
        return self.cache.set(url, response.text(), ttl)

    def recent_8k(self, cik: str, today: date, lookback_days: int = 3) -> list[Filing] | None:
        url = SUBMISSIONS_URL.format(cik=str(cik).zfill(10))
        raw = self._get(url, ttl=6 * 3600)
        if raw is None:
            return None
        import json

        try:
            payload = json.loads(raw)
            recent = payload["filings"]["recent"]
            forms = recent["form"]
            dates = recent["filingDate"]
            accessions = recent["accessionNumber"]
            documents = recent["primaryDocument"]
        except (ValueError, KeyError, TypeError) as exc:
            LOG.warning("SEC submissions 파싱 실패 cik=%s: %s", cik, exc)
            return None
        cutoff = today - timedelta(days=lookback_days)
        filings: list[Filing] = []
        for index, form in enumerate(forms):
            if form != "8-K":
                continue
            try:
                filing_date = date.fromisoformat(dates[index])
            except (ValueError, IndexError):
                continue
            if filing_date < cutoff or filing_date > today:
                continue
            try:
                filings.append(
                    Filing(
                        form=form,
                        filing_date=filing_date,
                        accession=accessions[index],
                        primary_document=documents[index],
                    )
                )
            except IndexError:
                continue
        return filings

    def check(
        self,
        symbol: str,
        cik: str | None,
        keywords: tuple[str, ...],
        today: date,
        lookback_days: int = 3,
    ) -> FilingCheck:
        if not cik:
            return FilingCheck(checked=True, reason="no-cik")
        filings = self.recent_8k(cik, today, lookback_days)
        if filings is None:
            return FilingCheck(checked=False, reason="sec-unavailable", detail={"symbol": symbol})
        hits: list[str] = []
        for filing in filings[:2]:  # 최근 2건만 원문 확인 (호출량 제한)
            raw = self._get(filing.url(cik), ttl=24 * 3600)
            if raw is None:
                return FilingCheck(checked=False, reason="sec-document-unavailable", detail={"symbol": symbol})
            text = raw.lower()
            for keyword in keywords:
                if keyword in text:
                    hits.append(f"{filing.filing_date.isoformat()}:{keyword}")
        if hits:
            return FilingCheck(
                checked=True,
                blocked=True,
                reason="sec-8k-adverse",
                detail={"symbol": symbol, "hits": hits},
            )
        return FilingCheck(checked=True, detail={"symbol": symbol, "filings": len(filings)})
