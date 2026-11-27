# ALPHA SHIELD v3.3 (6-1-F)

미국장 **2슬롯 고정 · Hark ATR 정규화 &amp; 깊이 순위(2.5배 캡) · VIX 가변 목표 익절 · 다중 방어막 · 계좌 Kill Switch** 소수점 자동매매 시스템.

토스증권 Open API(1.2.28)로 **금액 기반 소수점 매수 + 소수점 수량 매도**를 수행하고, 기본값은 절대 주문을 내지 않는 **dry-run** 입니다.

| 항목 | 값 |
|---|---|
| 유니버스 | NVDA(1.5) TSLA(1.4) AMZN(1.2) TSM(1.1) AVGO(1.0) QQQ(1.0) AAPL(0.9) GOOGL(0.9) |
| 자금 | 해외 약 50만 원($370~$382), **항상 2슬롯 고정** |
| 1회 진입 / 종목 상한 | 자산 < $400 → **$75 / $150** · $400~$500 미만 → **$85 / $170** · $500 이상 → **$120 / $240** (권장안 A) |
| 현금 안전 버퍼 | `max(자산 × 15%, $55.00)` · 최소 주문 $10 미만 금지 |
| 진입 | 장중 고점 대비 `-0.52 × ATR(20)%` 이상 눌림 + `RSI(2) ≤ 15` + `주가 ≥ SMA200 × 0.95` |
| 슬롯 점수 | `알파 가중치 × min(|낙폭| ÷ 진입 임계, 2.5)` (낙폭 비율 2.5배 상한 캡) |
| 비정상 급락 가드 | 장중 고점 대비 `-3.0 × ATR(20)%` 초과 폭락 종목은 **당일 신규 매수 제외** |
| 물타기 | 평단 대비 `max(1.5%, 0.8 × ATR(20)%)` 이상 하락 + `RSI(2) ≤ 35` + **1차 진입 후 2시간(또는 익거래일)** · 종목당 1회 |
| 익절 | 진입 시 VIX 종가 확정: `VIX < 18 → +8.0%`, `VIX ≥ 18 또는 미확인 → +5.0%` (물타기 후에도 유지) |
| 손절 | 단일 `-5.0%` / 물타기 포지션 `-3.5%` · 오버나잇 갭하락은 **09:32 ET 이후** 집행 |
| 시간청산 | 최초 진입일 기준 10거래일(달력 14일) · 만기 시 `+3.0%` 이상이면 **고점 대비 -2.0% 트레일링**으로 전환(옵션) |
| 쿨다운 | 일반 청산 후 15분(900초) · **동일 종목 손절 시 당일(EOD) 재진입 금지** |
| 15:00 프로토콜 | 14:50~14:58 ET(조기폐장 12:50~12:58) 선제 청산 · 프리마켓 매도 큐 · 유예는 신규 매수·물타기 전용 |
| 판단 우선순위 | **(1) 계좌 Kill Switch → (2) 매도 → (3) 매수(신규·물타기)** |

전략 원문: [`docs/SPEC-v3.3.md`](docs/SPEC-v3.3.md) (v3.2 원본은 [`docs/SPEC-v3.2.md`](docs/SPEC-v3.2.md))

---

## 1. 안전 원칙 (먼저 읽으세요)

1. **기본 모드는 dry-run** 입니다. 주문을 전혀 보내지 않고 "보냈을 주문"만 기록합니다.
2. **실주문 이중 잠금**: 아래 두 조건이 **모두** 참일 때만 실주문이 나갑니다.
   - 환경변수 `LIVE_ORDERS=true`
   - `state/ARMED` 파일 존재 (`touch state/ARMED`)
   둘 중 하나라도 없으면 `live` 모드 기동 자체가 **거부**됩니다.
3. **매수는 fail-closed**: 시세·지표·가드 중 하나라도 확인할 수 없으면 사지 않습니다.
   **매도는 fail-open**: 청산 신호가 나면 데이터 가드와 무관하게 팝니다.
   단, 매도 가격이 **직전 5분봉 대비 ±10%** 넘게 튀면 보조 시세로 교차 확인한 뒤 집행합니다(이상 틱 가드).
4. **수동 킬스위치**: `state/halt.flag` 파일을 만들면 신규 매수만 즉시 멈추고 청산은 계속합니다.
5. **계좌 Kill Switch(Safe Halt, v3.3)**: 일일 손실 `-4.0%` 도달 · 누적 MDD `-12.0%` 돌파 · 5분 내 동일 종목 중복 주문 시도 ·
   API 3회 연속 오류 · 장부·실잔고 불일치 중 하나라도 발생하면 **신규 주문(매수·매도)을 전면 정지**하고 알림을 보냅니다.
   원인을 확인한 뒤 대시보드의 `킬스위치 OFF`(또는 `kill_switch` 상태 초기화)로만 해제됩니다.
6. **계좌 규정 가드(v3.3)**:
   - 현금 계좌: 매도대금은 T+1 결제 — 미결제 대금을 뺀 **결제 완료 현금**으로만 사이징하고, 같은 종목 재매수도 막습니다(GFV 방지).
   - 마진 계좌($25,000 미만): 5영업일 내 데이트레이드 3회 도달 시 그날 신규 진입을 중단합니다(PDT 방지).
7. **당일 손실 한도**: 시작 자산 대비 -4% 가 되면 그날 신규 매수를 멈춥니다(Kill Switch 와 같은 임계의 1차 방벽).
8. **주문 폭주 방지**: 1분 6건 초과, 동일 종목·방향 60초 내 중복 주문을 차단합니다.
9. **기동 시 대조(reconcile)**: 브로커 보유 수량과 로컬 원장이 다르면 신규 매수를 멈추고 매도만 허용합니다.
   (v3.3 에서는 계좌 Kill Switch 도 함께 발동해 신규 주문 전체가 멈춥니다.)

> 이 프로그램은 투자 자문이 아닙니다. 실계좌 투입 전 **dry-run → paper 최소 2주 → 소액 1슬롯** 순서를 권장합니다.

---

## 2. 아키텍처

```
config/                     전략·가드·거시/시장 캘린더 (사람이 읽고 고치는 값)
src/alpha_shield/
  cli.py                    진입점 (--mode, --once, --loop, --check, --web ...)
  web.py                    봇 내장 웹 대시보드 (http.server, /api/status·/api/control·/api/run-once)
  webui.py                  대시보드 화면 (한글 반응형 HTML/CSS/JS, 외부 CDN 없음)
  clock.py                  America/New_York 기준, 로그는 KST 병기
  config.py                 YAML/환경변수 로딩·검증 (fail-fast, 3단 자금 구간)
  numeric.py                Decimal 강제 · 소수 2/6자리 규칙
  indicators.py             Wilder ATR(20), RSI(2), SMA(200), HOD 이상치 필터
  calendar.py               정규장·조기폐장·소수점 마감(15:00 ET)·선제 청산·프리마켓 창
  models.py                 Bar/Quote/OrderRequest/OrderResult/Position/GuardDecision
  net.py                    HTTP 전송 계층(+429/5xx 재시도)
  cache.py                  TTL 캐시 (외부 호출 절감)
  safety.py                 이중 안전잠금 · 수동 킬스위치 · 주문 rate · 당일 손실한도 · 계좌 Kill Switch
  state/                    SQLite(WAL): 포지션·신호·쿨다운·주문 원장·일일 상태
  marketdata/
    yahoo.py                NQ=F, ^VIX, 일봉/1분봉 (비공식, 캐시·재시도)
    toss.py                 토스 Open API 클라이언트(토큰 파일락) + 시세/캔들
    sec.py                  SEC EDGAR 8-K 구조적 악재
    nasdaq.py               Nasdaq 실적 캘린더
    news.py                 Yahoo Finance RSS 헤드라인
    macro.py                FOMC/CPI/NFP 고정 캘린더
  strategy/
    entry.py                Hark 1 진입 · 비정상 급락 가드 · Hark 2 점수 · 물타기 판정
    exit.py                 손절 · VIX 가변 목표 익절 · 시간청산 · 트레일링 전환
    sizing.py               2슬롯 3단 자금·버퍼·상한·최소주문
    slots.py                슬롯 경쟁 점수 정렬
    signals.py              BUY 신호 900초 만료 · RSI 미확인/회복 시 폐기(fail-closed)
    cooldown.py             일반 15분 · 손절 EOD 쿨다운
  guards/                   다중 방어막 (시장·세션·이벤트·공시·뉴스·거시·데이터·계좌 규정)
    account.py              현금 계좌 미결제 자금(GFV) · 마진 계좌 PDT
    data.py                 시세 지연/결측(fail-closed) · 이상 틱 가드(매도 크로스체크)
  broker/
    base.py                 Broker 인터페이스
    dryrun.py               주문 없음 (기본)
    paper.py                내장 모의 체결 (슬리피지·수수료)
    toss.py                 토스 주문 어댑터 (orderAmount/quantity/clientOrderId, 프리마켓 플래그)
  engine/
    loop.py                 5분 사이클 오케스트레이션 (Kill Switch → 매도 → 매수)
    reconcile.py            브로커 ↔ 로컬 원장 대조
    alerts.py               JSONL 알림 + 판정 로그
  reporting/ledger.py       세무용 거래 원장 CSV
tests/                      241개 unittest (전략·가드·v3.3 TC-806~813·영속화·모의 브로커·live 스택·웹)
deploy/                     systemd 유닛 · watchdog · healthcheck
docs/                       전략 스펙 · 운영 런북 + GitHub Pages 정적 웹앱(index.html)
```

**의존성 원칙**: 전략·가드·원장은 브로커를 모릅니다. `Broker` / `MarketData` 인터페이스만 의존하므로, 증권사나 데이터 소스를 바꿔도 전략 코드는 그대로입니다.
**정밀도**: 수량·금액·가격 전 구간 `Decimal`. Yahoo float 은 소수 4자리로 정리하고, 주문 금액은 소수 2자리(내림), 수량은 소수 6자리(내림)입니다.

---

## 3. 설치

```bash
cd autostock
python3 -m venv .venv && source .venv/bin/activate     # 선택
pip install -e ".[dev]"                                # PyYAML + tzdata (+ pytest)
cp .env.example .env
```

- **Python 3.10 이상**, 의존성은 `PyYAML>=6.0` 과 `tzdata>=2024.1` 입니다.
  Windows 처럼 시스템 tz 데이터베이스가 없는 환경에서도 `America/New_York` 세션 판정이 되도록 `tzdata` 를 함께 설치합니다.
- 개발·검증까지 하려면 `pip install -e ".[dev]"` (pytest 포함).

## 4. 설정

`.env` (커밋 금지):

```ini
ALPHA_MODE=dry-run                    # dry-run | paper | live
ALPHA_DATA_DIR=state
LIVE_ORDERS=false                     # 실주문 1차 잠금
TOSS_CLIENT_ID=
TOSS_CLIENT_SECRET=
TOSS_ACCOUNT_SEQ=                     # 비우면 기동 시 자동 조회
SEC_USER_AGENT=alpha-shield/3.3 (you@example.com)
PAPER_STARTING_CASH=380.00
ALLOW_FRACTIONAL_PREMARKET_SELL=false # 프리마켓 소수점 매도 지원 여부(기본 미지원 → 09:30~09:32 예약 큐)
```

- 전략 수치는 `config/strategy.yaml`, 방어막·계좌 규정·Kill Switch 는 `config/guards.yaml` 입니다. **숫자를 바꾸면 백테스트 기준값(2년 +48.68%, MDD -10.18%, 샤프 2.05)이 무효**가 됩니다.
- `capital.plan`: `A`(권장, 3단 사이징) 또는 `B`($500 미만은 $85/$170 유지 + 가용현금 한도 내 부분 물타기).
- `capital.tiers`: 자산 구간별 `max_equity_usd` / `entry_usd` / `symbol_cap_usd`. 마지막 구간은 반드시 `max_equity_usd: null`.
- `entry.sma_floor_ratio`(기본 0.95), `entry.score_cap_multiple`(2.5), `entry.crash_atr_multiple`(3.0), `add.min_interval_hours`(2) 모두 설정값입니다.
- `guards.session`: 선제 청산 창(14:50~14:58), 오버나잇 손절 집행(09:32 이후), 프리마켓 큐 집행(09:30~09:32) 창.
- `guards.account.settlement`: `cash`(현금 계좌, GFV 가드) 또는 `margin`(PDT 가드).
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

# 5) 웹 대시보드 (로컬 전용, 표준 라이브러리만 사용)
python -m alpha_shield --web                          # http://127.0.0.1:8080
python -m alpha_shield --web --port 8090              # 포트 변경
python -m alpha_shield --web --host 0.0.0.0           # 같은 공유기 안에서만 열 때 (권장하지 않음)
python -m alpha_shield --web --web-refresh 30         # 시세 갱신 주기(초)
```

- `--interval 300` 으로 사이클 간격을 바꿀 수 있습니다. **5분(300초)이 스펙 값**입니다.
- 모드는 CLI 인자가 `.env` 보다 우선합니다.
- 로그: `logs/alpha-shield.log`(회전), `logs/alerts.jsonl`, `logs/decisions-YYYY-MM-DD.jsonl`(사이클별 판정 근거).
- 상태: `state/alpha_shield.db` (WAL), `state/toss_token.json`(0600).

---

## 5-1. 봇 내장 웹 대시보드 (`--web`)

`python3 -m alpha_shield --web` 은 **표준 라이브러리 `http.server`** 만 쓰는 로컬 대시보드입니다.
기본 주소는 `http://127.0.0.1:8080` 이고, `--host`/`--port`/`--web-refresh` 로 바꿀 수 있습니다.

| 화면 | 내용 |
|---|---|
| 상단 요약 | 실행 모드, 총자산·현금·안전 버퍼 `max(15%, $55)`, 2슬롯 사용 현황(현재 구간의 1회 진입·상한), 당일 손익, 실주문 잠금, **계좌 Kill Switch** 상태 |
| 운용 제어 | 백그라운드 루프 **시작(dry-run/paper)**, **1회 즉시 실행**, **정지**, **킬스위치 ON/OFF**(계좌 Kill Switch 해제 포함) |
| 다중 방어막 (v3.3) | ① 시장(NQ·VIX) ② 세션 ③ 이벤트·공시 ④ 뉴스 ⑤ 거시 ⑥ 데이터(이상 틱 가드) ⑦ 계좌 규정(GFV·PDT) ⑧ 계좌 Kill Switch |
| 8종목 판정표 | 현재가, 고점 대비 낙폭, ATR(20)·진입 임계, RSI(2), SMA200, 슬롯 점수, 진입/대기 사유(비정상 급락 포함) |
| 보유 슬롯 | 평단·현재 수익률·VIX 목표 익절가·손절가·보유 거래일·트레일링 고점 |
| 로그 | 최근 주문 25건, 알림 15건 |

**API**

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/` | 대시보드 화면 (CSRF 토큰 내장) |
| GET | `/api/status` | 요약·방어막·판정표·보유·주문·알림 JSON + `kill_switch` · `account_rules` |
| POST | `/api/control` | `{"action":"start","mode":"dry-run"\|"paper"\|"live"}`, `{"action":"stop"}`, `{"action":"halt-on"\|"halt-off"}` |
| POST | `/api/run-once` | `{"mode":"paper"}` — 사이클 1회 즉시 실행 |
| GET | `/api/health` | `{"ok":true,"version":...}` |

**안전**
- 기본 바인딩은 `127.0.0.1` 입니다. `--host 0.0.0.0` 은 본인 책임 하에만 쓰세요.
- POST 는 `X-Alpha-Token` 헤더(화면에 자동 내장)와 로컬 Origin 검사를 통과해야 합니다.
- **live 전환 버튼은 `LIVE_ORDERS=true` 와 `state/ARMED` 가 모두 있을 때만 활성화**되고, 그마저도 토스 키가 없으면 거부됩니다.
  웹은 `state/ARMED` 를 대신 만들어 주지 않습니다.
- SQLite 연결은 요청·엔진마다 따로 열어 스레드 간 공유를 피합니다.

## 5-2. GitHub Pages 정적 웹앱 (`docs/index.html`)

`docs/` 는 GitHub Pages 루트로도 쓸 수 있습니다. `docs/index.html` + `docs/manifest.webmanifest` + `docs/icon.svg` 구성입니다.

- **브라우저 단독 실행**: Yahoo Finance 공개 차트 API(무료·비공식)를 공개 CORS 프록시 체인으로 읽어
  8종목 + `NQ=F` + `^VIX` 의 시세·일봉(2년)·1분봉을 받고, **ATR(20)·RSI(2)·SMA(200)·장중 고점(HOD)·낙폭·슬롯 점수(2.5배 캡)·VIX 목표 익절률**을
  브라우저에서 직접 계산합니다. 계산식은 `src/alpha_shield/indicators.py` · `strategy/` 와 같습니다.
- **모의매매 시뮬레이터**: 시작 자금 **$380**, **2슬롯 고정**, **v3.3 3단 사이징($75/$85/$120)**,
  자동/수동 매수·물타기(max(1.5%, 0.8×ATR) & RSI≤35 · 2시간 후 1회)·익절(VIX<18 → +8%, 아니면 +5%)·
  손절(−5% / 물타기 −3.5%, 손절 시 당일 재진입 금지)·시간청산(10거래일/14일, +3% 이상은 고점 대비 −2% 트레일링)·
  14:50~14:58 ET 선제 청산. 체결 가정은 슬리피지 5bp·수수료 10bp 이고, 계좌·체결·로그는 `localStorage` 에 남습니다.
- **가드**: 시장(NQ/VIX)·세션(개장 15분·마감 15분·선제 청산 창·소수점 마감)·거시(FOMC/CPI/NFP 2026 캘린더, 발표일 낙폭 +0.5%p)·
  데이터(수신 여부)·비정상 급락 가드를 표시합니다. 실적·8-K·뉴스 가드와 계좌 규정(GFV·PDT)·계좌 Kill Switch 는 브라우저에서
  안전하게 판정할 수 없어 **봇 대시보드에서 확인**하도록 명시했습니다.
- **PWA**: `manifest.webmanifest` + 아이콘 + `apple-mobile-web-app-*` 메타태그. 홈 화면에 추가하면 앱처럼 열립니다.
- **실주문 없음**: 이 페이지에는 주문 권한도, API 키도, 실계좌 연동도 없습니다.

공개 주소(저장소 Settings → Pages 에서 `main` / `/docs` 를 선택하면): `https://jongringer.github.io/autostock/`

> Yahoo 는 CORS 헤더를 주지 않아 공개 프록시(`codetabs` → `allorigins` → `cors.lol` → `r.jina.ai` → 직접)를 순서대로 시도하고,
> 성공한 경로를 기억합니다. 프록시가 모두 막히면 값이 비거나 지연될 수 있습니다.

## 5-3. 검증

- 단위 테스트: **241개 전부 PASS** (기존 201 + v3.3 TC-806~813 및 신규 규칙 40).
- `python -m pytest -q` 또는 `PYTHONPATH=src:tests python -m unittest discover -s tests -p "test_*.py"`.

## 6. 토스증권 Open API 실전 준비

1. **계좌**: 토스증권 종합매매 계좌(BROKERAGE)를 준비합니다.
2. **키 발급**: 토스증권 WTS 로그인 → **설정 > Open API** 에서 `client_id` / `client_secret` 발급.
3. **허용 IP 등록 (필수)**: 같은 메뉴의 **허용 IP 관리** 에 상시 서버의 **고정 IP**를 등록합니다. 미등록 IP는 REST 403 `ip-not-allowed`, 토큰 발급은 OAuth2 `access_denied` 로 거절됩니다.
4. **달러 환전**: API로 환전·입출금이 불가능합니다. 토스 앱에서 원화 → 달러로 **$380 내외**를 미리 환전해 둡니다. 부족하면 주문이 `422 insufficient-buying-power` 로 거절됩니다.
5. **토큰 1개 제약**: 토큰은 클라이언트당 1개만 유효하고, 재발급하면 직전 토큰이 즉시 무효가 됩니다. 이 프로그램은 `state/toss_token.json` 을 공유하고 파일 락으로 발급을 직렬화하므로 **프로세스를 하나만** 띄우세요.
6. **주문 가능 창**: 금액·소수점 주문은 **정규장 개장 ~ 종료 1시간 전(09:30~15:00 ET)** 만 접수됩니다.
   이 프로그램은 14:45 ET 부터 신규 매수를 멈추고, 14:50~14:58 ET 에 만기·근접 손절 종목을 선제 청산하며,
   15:00 이후 청산 신호는 경고를 남기고 다음 거래일로 넘깁니다.
7. **프리마켓 매도**: 토스는 소수점 프리마켓 매도를 기본 지원하지 않습니다(`ALLOW_FRACTIONAL_PREMARKET_SELL=false`).
   프리마켓에 손절·익절 신호가 나면 **09:30~09:32 ET 예약 매도 큐**에 넣고 개장 직후 집행합니다.
8. **2주 모의 검증 권장**: `dry-run` 1주 → `paper` 1주 → `live` 소액 1슬롯 2주 → 정상 규모.
9. **실행 환경**: 24시간 켜져 있는 서버(고정 IP)에서 `deploy/alpha-shield.service` 로 상시 실행. 봇이 꺼지면 소수점 포지션의 서버측 손절이 없으므로 watchdog 을 함께 쓰세요.

## 7. 다중 방어막 (v3.3)

| 층 | 내용 | 실패 시 |
|---|---|---|
| 시장 | NQ 선물 ≤ -1.5% 또는 VIX ≥ 30 → **신규 매수 전면 차단** | fail-closed |
| 세션 | 개장 15분(09:30~09:45 ET) 유예, 마감 15분(14:45~15:00 / 조기 12:45~13:00) 유예 — **신규 매수·물타기 전용** | fail-closed |
| 이벤트·공시·뉴스 | 실적 D-1~당일(Nasdaq), SEC 8-K 구조적 악재, 헤드라인 BLOCK 키워드 | fail-closed |
| 거시 | FOMC/CPI/NFP 당일 요구 낙폭 **-0.5%p 강화** (`config/macro_calendar_2026.json`) | fail-closed |
| 데이터 | 시세 15분 이상 지연·결측, SMA200 미확인 → 매수 보류 / 매도는 이상 틱 가드(±10% 크로스체크) | 매도는 fail-open |
| 계좌 규정 | 현금 계좌 미결제 자금(GFV) 재매수 차단 · 마진 계좌 PDT(5영업일 3회) 중단 | fail-closed |
| 계좌 Kill Switch | 일손실 -4% · MDD -12% · 5분 중복 주문 · API 3연속 오류 · 장부 불일치 → **신규 주문 전면 정지** | Safe Halt |

부가: 기동 시 대조, 당일 손실 한도, 수동 킬스위치, 주문 rate 제한, 미체결·거부 주문 알림.

## 8. 테스트

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests -p "test_*.py" -v
# 또는
pip install pytest && PYTHONPATH=src pytest -q tests
```

**241개 테스트 / 전부 PASS** (2026-11-27 v3.3 기준). 범위:

- 지표: Wilder ATR/RSI 고정값, SMA, HOD 이상치 필터
- 자금: 3단 구간 경계($399.99/$400/$499.99/$500), 버퍼 교차점, 최소주문, 종목 상한, 대안 B 단일 구간
- 진입/청산: ATR 결측 폴백(종목별), RSI 게이트, SMA200×0.95 바닥, **점수 2.5배 캡**, **비정상 급락 가드**,
  물타기 max(1.5%, 0.8×ATR)·2시간·1회, 손절 -5/-3.5%, VIX 익절 +8/+5%, 시간청산·**트레일링 전환**
- 신호: 900초 만료, 재전달 RSI>25 또는 미확인 시 폐기(fail-closed), 소비
- 방어막: NQ/VIX, 개장/마감/조기폐장 창, 실적 D-1, 8-K, 뉴스, 거시 강화, 시세 15분, **이상 틱 가드**
- 계좌 규정: 미결제 자금 T+1 정리·동일 종목 재매수 차단, 마진 계좌 PDT 3회 중단, 자산 $25,000 이상 미적용
- 계좌 Kill Switch: 일손실 -4.1% · MDD -12.1% · API 3연속 오류 · 5분 중복 주문 · 수동 해제
- v3.3 전수 감사 TC-806~813: EOD 쿨다운, 사이징·버퍼 정합성, 물타기 최소 대기·ATR, 급락 가드,
  이상 틱 가드, 14:52 선제 청산, 프리마켓 예약 매도 큐, Safe Halt
- 브로커: PaperBroker 체결·수수료·슬리피지, TossBroker 주문 본문(`orderAmount`, 소수 6자리 `quantity`, `clientOrderId`), 계좌 헤더, 409 재시도, 422 거부, 이중 잠금, 프리마켓 매도 플래그
- 토스 클라이언트: client_credentials 폼, 토큰 파일 공유/0600/만료 재발급, `token-revoked` 1회 재시도, 캔들 페이지네이션
- 영속화: Decimal 왕복, 쿨다운(사유 포함), 일일 상태, 원장 CSV
- 엔진: 진입→보유→익절/손절/시간청산, 매도 우선, 물타기, 가드/킬스위치/불일치 차단, 15:00 이후 청산 불가, dry-run 무주문
- 웹 대시보드: `/api/status` 응답 구조(`kill_switch`·`account_rules` 포함), 토큰·Origin 검사, 시작/정지/1회 실행/킬스위치 토글, live 이중 잠금 거부, 시세 실패 시 degrade, CLI `--web` 옵션

네트워크는 테스트에 쓰지 않습니다. 토스·Yahoo·SEC·Nasdaq 응답은 모두 가짜 전송 계층으로 대체합니다.

## 9. 원장·세무

`python -m alpha_shield --export-ledger YYYY-MM` → `ledger/ledger-YYYY-MM.csv` (UTF-8 BOM, 엑셀 호환).
열: 체결시각(KST), 종목, 매수/매도, 수량, 가격, 금액, 수수료, 세금, 통화, 환율, 사유, 멱등키, 브로커 주문ID, 모드.
해외주식 양도소득세(연 250만 원 공제, 22%) 신고에 필요한 체결 단위 기록입니다. 환율 열은 비어 있으며, 필요하면 토스 `GET /api/v1/exchange-rate` 로 체결 시점 환율을 보강하세요.

## 10. 구현 범위와 알려진 제약

- **웹소켓 미구현**: 체결 통보(`personal:order`) 대신 REST 5분 폴링 + 주문 직후 상세 조회로 상태를 맞춥니다. 시장가 주문은 대부분 수초 내 체결됩니다.
- **조건주문 미사용**: 소수점 수량은 조건주문(정수 기반)으로 위임할 수 없어 손절은 봇이 시장가로 수행합니다. 봇 다운타임 동안 서버측 손절이 없다는 뜻이므로 watchdog 이 필수입니다.
- **프리마켓 매도**: 기본은 `ALLOW_FRACTIONAL_PREMARKET_SELL=false` — 프리마켓 신호는 09:30~09:32 예약 큐로 넘깁니다.
  브로커가 소수점 프리마켓 매도를 지원하지 않는데 플래그를 켜면 주문이 `amount-order-outside-regular-hours` 로 거절됩니다.
- **데이터 출처**: 종목 시세·캔들은 live 에서 토스(일부 거래소 기준, NBBO 아님), NQ/VIX 는 Yahoo(비공식)입니다.
  1분봉 이상 체결은 HOD 산출 시 필터링하고, 매도 직전 ±10% 급변은 Yahoo 보조 시세로 교차 확인합니다.
- **뉴스 가드**: Yahoo Finance RSS 헤드라인 키워드 방식입니다. `config/guards.yaml` 에서 끌 수 있지만, 끄면 그 층의 보호가 사라집니다.
- **Yahoo 실패**: 15분 지연·결측이면 매수를 멈춥니다. 3회 연속 실패는 계좌 Kill Switch 로 이어집니다.
- **환율·수수료**: 페이퍼는 0.10% 수수료·5bp 슬리피지를 가정합니다. 실제 토스 수수료·환전 스프레드와 다를 수 있습니다.
- **공개 저장소 주의**: 이 저장소에는 키·체결 데이터가 커밋되지 않습니다(`.env`, `state/`, `*.db`, `logs/`, `*.key` 차단). 거래 데이터는 본인 매매 목적에만 사용하세요.

## 11. 운영

`docs/RUNBOOK.md` 참고. 요약:

| 상황 | 조치 |
|---|---|
| 즉시 매수 중단 | `touch state/halt.flag` (청산은 계속) |
| 계좌 Kill Switch 발동 | 알림의 `reason`(daily-loss/max-drawdown/duplicate-order/api-errors/book-mismatch) 확인 → 원인 해결 후 대시보드에서 킬스위치 OFF |
| 실주문 중단 | `rm state/ARMED` → 다음 주문부터 `LiveOrdersDisabled` |
| 보유 불일치 알림 | `python -m alpha_shield --reconcile`, 필요 시 수동 정리 후 `state` 의 `reconcile_state` 확인 |
| 토큰 오류 | `state/toss_token.json` 삭제 후 재기동 (프로세스는 하나만) |
| 장 마감 후 청산 실패 | 다음 거래일 개장 직후(09:30~09:32 예약 큐) 청산 시도. 15:00~16:00 ET 에는 소수점 매도 불가 |
| 손절한 종목이 같은 날 다시 조건 충족 | 설계상 당일 재진입 금지(EOD 쿨다운) — 정상 동작 |

## 12. 라이선스·면책

개인 사용 목적. 투자 손실에 대한 책임은 사용자에게 있습니다. 토스증권 Open API 이용약관(본인 매매 목적, 제3자 배포 금지)을 준수하세요.
