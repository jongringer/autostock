# ALPHA SHIELD v3.2 (6-1-F)

미국장 **2슬롯 고정 · Hark ATR 정규화 & 깊이 순위 · VIX 가변 목표 익절 · 5중 다중 방어막** 소수점 자동매매 시스템.

토스증권 Open API(1.2.28)로 **금액 기반 소수점 매수 + 소수점 수량 매도**를 수행하고, 기본값은 절대 주문을 내지 않는 **dry-run** 입니다.

| 항목 | 값 |
|---|---|
| 유니버스 | NVDA(1.5) TSLA(1.4) AMZN(1.2) TSM(1.1) AVGO(1.0) QQQ(1.0) AAPL(0.9) GOOGL(0.9) |
| 자금 | 해외 약 50만 원($370~$382), **항상 2슬롯 고정** |
| 1회 진입 / 종목 상한 | 자산 < $500 → $85 / $170 · 자산 ≥ $500 → $120 / $240 |
| 현금 안전 버퍼 | `max(자산 × 15%, $55.00)` · 최소 주문 $10 미만 금지 |
| 진입 | 장중 고점 대비 `-0.52 × ATR(20)%` 이상 눌림 + `RSI(2) ≤ 15` + `주가 ≥ SMA200 × 0.80` |
| 물타기 | 평단 대비 `-1.5%` 이상 추가 하락 + `RSI(2) ≤ 35`, 종목당 1회 |
| 익절 | 진입 시 VIX 종가 확정: `VIX < 18 → +8.0%`, `VIX ≥ 18 또는 미확인 → +5.0%` (물타기 후에도 유지) |
| 손절 | 단일 `-5.0%` / 물타기 포지션 `-3.5%` |
| 시간청산 | 최초 진입일 기준 10거래일(달력 14일) |
| 쿨다운 | 청산 후 15분(900초) |
| 판단 우선순위 | **(1) 매도 → (2) 매수(신규·물타기)** |

전략 원문: [`docs/SPEC-v3.2.md`](docs/SPEC-v3.2.md)

---

## 1. 안전 원칙 (먼저 읽으세요)

1. **기본 모드는 dry-run** 입니다. 주문을 전혀 보내지 않고 "보냈을 주문"만 기록합니다.
2. **실주문 이중 잠금**: 아래 두 조건이 **모두** 참일 때만 실주문이 나갑니다.
   - 환경변수 `LIVE_ORDERS=true`
   - `state/ARMED` 파일 존재 (`touch state/ARMED`)
   둘 중 하나라도 없으면 `live` 모드 기동 자체가 **거부**됩니다.
3. **매수는 fail-closed**: 시세·지표·가드 중 하나라도 확인할 수 없으면 사지 않습니다.
   **매도는 fail-open**: 청산 신호가 나면 데이터 가드와 무관하게 팝니다.
4. **킬스위치**: `state/halt.flag` 파일을 만들면 신규 매수만 즉시 멈추고 청산은 계속합니다.
5. **당일 손실 한도**: 시작 자산 대비 -5% 가 되면 그날 신규 매수를 멈춥니다.
6. **주문 폭주 방지**: 1분 6건 초과, 동일 종목·방향 60초 내 중복 주문을 차단합니다.
7. **기동 시 대조(reconcile)**: 브로커 보유 수량과 로컬 원장이 다르면 신규 매수를 멈추고 매도만 허용합니다.

> 이 프로그램은 투자 자문이 아닙니다. 실계좌 투입 전 **dry-run → paper 최소 2주 → 소액 1슬롯** 순서를 권장합니다.

---

## 2. 아키텍처

```
config/                     전략·가드·거시/시장 캘린더 (사람이 읽고 고치는 값)
src/alpha_shield/
  cli.py                    진입점 (--mode, --once, --loop, --check ...)
  clock.py                  America/New_York 기준, 로그는 KST 병기
  config.py                 YAML/환경변수 로딩·검증 (fail-fast)
  numeric.py                Decimal 강제 · 소수 2/6자리 규칙
  indicators.py             Wilder ATR(20), RSI(2), SMA(200), HOD 이상치 필터
  calendar.py               정규장·조기폐장·소수점 마감(15:00 ET) 세션 상태
  models.py                 Bar/Quote/OrderRequest/OrderResult/Position/GuardDecision
  net.py                    HTTP 전송 계층(+429/5xx 재시도)
  cache.py                  TTL 캐시 (외부 호출 절감)
  safety.py                 이중 안전잠금 · 킬스위치 · 주문 rate · 당일 손실한도
  state/                    SQLite(WAL): 포지션·신호·쿨다운·주문 원장·일일 상태
  marketdata/
    yahoo.py                NQ=F, ^VIX, 일봉/1분봉 (비공식, 캐시·재시도)
    toss.py                 토스 Open API 클라이언트(토큰 파일락) + 시세/캔들
    sec.py                  SEC EDGAR 8-K 구조적 악재
    nasdaq.py               Nasdaq 실적 캘린더
    news.py                 Yahoo Finance RSS 헤드라인
    macro.py                FOMC/CPI/NFP 고정 캘린더
  strategy/
    entry.py                Hark 1 진입 · 물타기 판정
    exit.py                 손절 · VIX 가변 목표 익절 · 시간청산
    sizing.py               2슬롯 자금·버퍼·상한·최소주문
    slots.py                슬롯 경쟁 점수 정렬
    signals.py              BUY 신호 900초 만료 · RSI>25 폐기
    cooldown.py             재진입 15분 쿨다운
  guards/                   5중 다중 방어막 (시장·세션·이벤트·공시·뉴스·거시·데이터)
  broker/
    base.py                 Broker 인터페이스
    dryrun.py               주문 없음 (기본)
    paper.py                내장 모의 체결 (슬리피지·수수료)
    toss.py                 토스 주문 어댑터 (orderAmount/quantity/clientOrderId)
  engine/
    loop.py                 5분 사이클 오케스트레이션
    reconcile.py            브로커 ↔ 로컬 원장 대조
    alerts.py               JSONL 알림 + 판정 로그
  reporting/ledger.py       세무용 거래 원장 CSV
tests/                      184개 unittest (전략·가드·영속화·이중잠금·모의 브로커·live 스택)
deploy/                     systemd 유닛 · watchdog · healthcheck
docs/                       전략 스펙 · 운영 런북
```

**의존성 원칙**: 전략·가드·원장은 브로커를 모릅니다. `Broker` / `MarketData` 인터페이스만 의존하므로, 증권사나 데이터 소스를 바꿔도 전략 코드는 그대로입니다.
**정밀도**: 수량·금액·가격 전 구간 `Decimal`. Yahoo float 은 소수 4자리로 정리하고, 주문 금액은 소수 2자리(내림), 수량은 소수 6자리(내림)입니다.

---

## 3. 설치

```bash
cd autostock
python3 -m venv .venv && source .venv/bin/activate     # 선택
pip install -e .                                       # PyYAML 1개만 필요
cp .env.example .env
```

Python 3.10 이상. 그 외 외부 패키지는 없습니다(표준 라이브러리 + PyYAML).

## 4. 설정

`.env` (커밋 금지):

```ini
ALPHA_MODE=dry-run          # dry-run | paper | live
ALPHA_DATA_DIR=state
LIVE_ORDERS=false           # 실주문 1차 잠금
TOSS_CLIENT_ID=
TOSS_CLIENT_SECRET=
TOSS_ACCOUNT_SEQ=           # 비우면 기동 시 자동 조회
SEC_USER_AGENT=alpha-shield/3.2 (you@example.com)
PAPER_STARTING_CASH=380.00
```

- 전략 수치는 `config/strategy.yaml`, 방어막은 `config/guards.yaml` 입니다. **숫자를 바꾸면 백테스트 기준값(수익률 +48.68%, MDD -10.18%, 샤프 2.05)이 무효**가 됩니다.
- `config/macro_calendar_2026.json` 은 FOMC·CPI·NFP 발표일 고정 캘린더입니다. 2027년에는 새 파일을 만들어 `guards.yaml` 의 `macro.calendar_file` 을 바꾸세요.
- `config/market_calendar_2026.json` 은 휴장일·조기폐장 폴백입니다. live 모드에서는 토스 `market-calendar/US` 가 우선합니다.

## 5. 실행

```bash
# 1) 설정 점검 (네트워크 없음)
python -m alpha_shield --check

# 2) dry-run: 실제 시세로 판정만, 주문 없음
python -m alpha_shield --mode dry-run --once
python -m alpha_shield --mode dry-run --loop          # 5분 주기

# 3) paper: 내장 모의 계좌로 체결까지 시뮬레이션 (state/alpha_shield.db 에 이어서 저장)
python -m alpha_shield --mode paper --loop

# 4) live: 실계좌 (아래 6번 준비 후)
export LIVE_ORDERS=true
touch state/ARMED
python -m alpha_shield --mode live --loop

# 기타
python -m alpha_shield --status                       # 포지션·최근 주문·테이블 현황
python -m alpha_shield --export-ledger 2026-12        # 월별 거래 원장 CSV
python -m alpha_shield --reconcile                    # 브로커 ↔ 로컬 대조
```

- `--interval 300` 으로 사이클 간격을 바꿀 수 있습니다. **5분(300초)이 스펙 값**입니다.
- 모드는 CLI 인자가 `.env` 보다 우선합니다.
- 로그: `logs/alpha-shield.log`(회전), `logs/alerts.jsonl`, `logs/decisions-YYYY-MM-DD.jsonl`(사이클별 판정 근거).
- 상태: `state/alpha_shield.db` (WAL), `state/toss_token.json`(0600).

## 6. 토스증권 Open API 실전 준비

1. **계좌**: 토스증권 종합매매 계좌(BROKERAGE)를 준비합니다.
2. **키 발급**: 토스증권 WTS 로그인 → **설정 > Open API** 에서 `client_id` / `client_secret` 발급.
3. **허용 IP 등록 (필수)**: 같은 메뉴의 **허용 IP 관리** 에 상시 서버의 **고정 IP**를 등록합니다. 미등록 IP는 REST 403 `ip-not-allowed`, 토큰 발급은 OAuth2 `access_denied` 로 거절됩니다.
4. **달러 환전**: API로 환전·입출금이 불가능합니다. 토스 앱에서 원화 → 달러로 **$380 내외**를 미리 환전해 둡니다. 부족하면 주문이 `422 insufficient-buying-power` 로 거절됩니다.
5. **토큰 1개 제약**: 토큰은 클라이언트당 1개만 유효하고, 재발급하면 직전 토큰이 즉시 무효가 됩니다. 이 프로그램은 `state/toss_token.json` 을 공유하고 파일 락으로 발급을 직렬화하므로 **프로세스를 하나만** 띄우세요.
6. **주문 가능 창**: 금액·소수점 주문은 **정규장 개장 ~ 종료 1시간 전(09:30~15:00 ET)** 만 접수됩니다. 이 프로그램은 14:45 ET 부터 신규 매수를 멈추고 청산에 집중하며, 15:00 이후 청산 신호는 경고를 남기고 다음 거래일로 넘깁니다.
7. **2주 모의 검증 권장**: `dry-run` 1주 → `paper` 1주 → `live` 소액 1슬롯 2주 → 정상 규모.
8. **실행 환경**: 24시간 켜져 있는 서버(고정 IP)에서 `deploy/alpha-shield.service` 로 상시 실행. 봇이 꺼지면 소수점 포지션의 서버측 손절이 없으므로 watchdog 을 함께 쓰세요.

## 7. 5중 다중 방어막

| 층 | 내용 | 실패 시 |
|---|---|---|
| 시장 | NQ 선물 ≤ -1.5% 또는 VIX ≥ 30 → **신규 매수 전면 차단** | fail-closed |
| 세션 | 개장 15분(09:30~09:45 ET) 유예, 마감 15분(14:45~15:00 / 조기 12:45~13:00) 유예 | fail-closed |
| 이벤트·공시·뉴스 | 실적 D-1~당일(Nasdaq), SEC 8-K 구조적 악재, 헤드라인 BLOCK 키워드 | fail-closed |
| 거시 | FOMC/CPI/NFP 당일 요구 낙폭 **-0.5%p 강화** (`config/macro_calendar_2026.json`) | fail-closed |
| 데이터 | 시세 15분 이상 지연·결측, SMA200 미확인 → 매수 보류 | 매도는 정상 |

부가: 기동 시 대조, 당일 손실 한도, 킬스위치, 주문 rate 제한, 미체결·거부 주문 알림.

## 8. 테스트

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests -p "test_*.py" -v
# 또는
pip install pytest && PYTHONPATH=src pytest -q tests
```

**184개 테스트 / 전부 PASS** (2026-11-27 기준). 범위:

- 지표: Wilder ATR/RSI 고정값, SMA, HOD 이상치 필터
- 자금: $499.99/$500.00/$500.01 경계, 버퍼 교차점, 최소주문, 종목 상한
- 진입/청산: ATR 결측 폴백(종목별), RSI 게이트, SMA 바닥, 물타기 1회, 손절 -5/-3.5%, VIX 익절 +8/+5%, 시간청산
- 신호: 900초 만료, 재전달 RSI>25 폐기, 소비
- 방어막: NQ/VIX, 개장/마감/조기폐장 창, 실적 D-1, 8-K, 뉴스, 거시 강화, 시세 15분
- 브로커: PaperBroker 체결·수수료·슬리피지, TossBroker 주문 본문(`orderAmount`, 소수 6자리 `quantity`, `clientOrderId`), 계좌 헤더, 409 재시도, 422 거부, 이중 잠금
- 토스 클라이언트: client_credentials 폼, 토큰 파일 공유/0600/만료 재발급, `token-revoked` 1회 재시도, 캔들 페이지네이션
- 영속화: Decimal 왕복, 쿨다운, 일일 상태, 원장 CSV
- 엔진: 진입→보유→익절/손절/시간청산, 매도 우선, 물타기, 가드/킬스위치/불일치 차단, 15:00 이후 청산 불가, dry-run 무주문

네트워크는 테스트에 쓰지 않습니다. 토스·Yahoo·SEC·Nasdaq 응답은 모두 가짜 전송 계층으로 대체합니다.

## 9. 원장·세무

`python -m alpha_shield --export-ledger YYYY-MM` → `ledger/ledger-YYYY-MM.csv` (UTF-8 BOM, 엑셀 호환).
열: 체결시각(KST), 종목, 매수/매도, 수량, 가격, 금액, 수수료, 세금, 통화, 환율, 사유, 멱등키, 브로커 주문ID, 모드.
해외주식 양도소득세(연 250만 원 공제, 22%) 신고에 필요한 체결 단위 기록입니다. 환율 열은 비어 있으며, 필요하면 토스 `GET /api/v1/exchange-rate` 로 체결 시점 환율을 보강하세요.

## 10. 구현 범위와 알려진 제약

- **웹소켓 미구현**: 체결 통보(`personal:order`) 대신 REST 5분 폴링 + 주문 직후 상세 조회로 상태를 맞춥니다. 시장가 주문은 대부분 수초 내 체결됩니다.
- **조건주문 미사용**: 소수점 수량은 조건주문(정수 기반)으로 위임할 수 없어 손절은 봇이 시장가로 수행합니다. 봇 다운타임 동안 서버측 손절이 없다는 뜻이므로 watchdog 이 필수입니다.
- **데이터 출처**: 종목 시세·캔들은 live 에서 토스(일부 거래소 기준, NBBO 아님), NQ/VIX 는 Yahoo(비공식)입니다. 1분봉 이상 체결은 HOD 산출 시 필터링합니다.
- **뉴스 가드**: Yahoo Finance RSS 헤드라인 키워드 방식입니다. `config/guards.yaml` 에서 끌 수 있지만, 끄면 그 층의 보호가 사라집니다.
- **Yahoo 실패**: 15분 지연·결측이면 매수를 멈춥니다. 3회 연속 실패 시 당일 매수 중단을 권장합니다(알림으로 확인).
- **환율·수수료**: 페이퍼는 0.10% 수수료·5bp 슬리피지를 가정합니다. 실제 토스 수수료·환전 스프레드와 다를 수 있습니다.
- **공개 저장소 주의**: 이 저장소에는 키·체결 데이터가 커밋되지 않습니다(`.env`, `state/`, `*.db`, `logs/`, `*.key` 차단). 거래 데이터는 본인 매매 목적에만 사용하세요.

## 11. 운영

`docs/RUNBOOK.md` 참고. 요약:

| 상황 | 조치 |
|---|---|
| 즉시 매수 중단 | `touch state/halt.flag` (청산은 계속) |
| 실주문 중단 | `rm state/ARMED` → 다음 주문부터 `LiveOrdersDisabled` |
| 보유 불일치 알림 | `python -m alpha_shield --reconcile`, 필요 시 수동 정리 후 `state` 의 `reconcile_state` 확인 |
| 토큰 오류 | `state/toss_token.json` 삭제 후 재기동 (프로세스는 하나만) |
| 장 마감 후 청산 실패 | 다음 거래일 개장 직후 청산 시도. 15:00~16:00 ET 에는 소수점 매도 불가 |

## 12. 라이선스·면책

개인 사용 목적. 투자 손실에 대한 책임은 사용자에게 있습니다. 토스증권 Open API 이용약관(본인 매매 목적, 제3자 배포 금지)을 준수하세요.
