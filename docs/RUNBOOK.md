# ALPHA SHIELD v3.2 운영 런북

## 0. 일일 점검 (장 시작 전, 한국 시간 22:30 전후)

```bash
cd /opt/autostock
python -m alpha_shield --check          # 설정·이중잠금 상태
python -m alpha_shield --status         # 포지션·최근 주문
tail -n 30 logs/alpha-shield.log
```

- 미국 정규장: 한국 겨울 23:30~06:00, 서머타임 22:30~05:00.
- 소수점 주문 마감: 15:00 ET (한국 겨울 05:00, 서머타임 04:00).
- 조기폐장일(추수감사절 다음날, 크리스마스 이브)은 13:00 ET 마감.

## 1. 실행 모드 전환

| 목적 | 방법 |
|---|---|
| 판정만 확인 | `python -m alpha_shield --mode dry-run --once` |
| 모의 체결 | `python -m alpha_shield --mode paper --loop` |
| 실주문 | `.env` 에 `LIVE_ORDERS=true` + `touch state/ARMED` + `python -m alpha_shield --mode live --loop` |
| 실주문 즉시 중단 | `rm state/ARMED` (이미 낸 주문은 취소되지 않음) |
| 신규 매수만 중단 | `touch state/halt.flag` (청산은 계속) |
| 정지 해제 | `rm state/halt.flag` |

## 2. 알림 대응

`logs/alerts.jsonl` 과 웹훅(`ALERT_WEBHOOK_URL`)으로 들어옵니다.

| 알림 | 의미 | 조치 |
|---|---|---|
| `보유 수량 불일치` | 브로커 보유 ↔ 로컬 원장 불일치 → 신규 매수 중단 | `--reconcile` 로 상세 확인, 토스 앱 보유와 대조, 필요 시 로컬 포지션 수동 정리 후 재기동 |
| `청산 주문 거부` | 손절/익절 매도가 거부됨 | 사유 코드 확인. `insufficient-sellable-quantity` 면 대조 불일치, `stock-restricted` 면 해당 종목 제한 |
| `청산 신호가 났지만 소수점 매도 마감 이후` | 15:00~16:00 ET 에 청산 신호 발생 | 다음 거래일 개장 직후 청산. 손실 확대 가능성 인지 |
| `시세 조회 실패` | Yahoo/토스 장애 | 매수는 자동 중단. 매도는 가격을 못 받아 지연될 수 있음 |
| `토스 토큰 무효` | 다른 프로세스가 토큰 재발급 | 프로세스가 하나인지 확인. `state/toss_token.json` 삭제 후 재기동 |
| `daily-loss-limit` | 당일 -5% | 그날은 매수 정지. 익일 자동 해제 |

## 2-1. 웹 대시보드 (`--web`)

```bash
python -m alpha_shield --web                  # http://127.0.0.1:8080
python -m alpha_shield --web --port 8090 --web-refresh 30
```

| 화면 버튼 | 하는 일 | 주의 |
|---|---|---|
| 백그라운드 시작 · dry-run | 주문 없이 5분(기본) 사이클 루프 | 언제든 정지 가능 |
| 백그라운드 시작 · paper | 내장 모의 계좌로 체결까지 시뮬레이션 | `state/alpha_shield.db` 의 `paper_state` 에 이어서 저장 |
| 1회 즉시 실행 | 현재 모드로 사이클 1회 | 장 마감이면 `skipped` 만 남음 |
| 정지 | 루프 종료(진행 중 사이클은 끝까지) | — |
| 킬스위치 ON/OFF | `state/halt.flag` 생성/삭제 | ON 이면 신규 매수만 멈추고 청산은 계속 |
| 실주문 전환 | live 루프 시작 | `LIVE_ORDERS=true` + `state/ARMED` + 토스 키가 모두 있어야 활성화. 웹은 ARMED 를 만들지 않음 |

- 화면은 10초마다 `/api/status` 를 다시 읽습니다. 시세 스냅샷은 서버가 `--web-refresh`(기본 60초) 주기로 백그라운드에서 갱신하므로,
  첫 접속 직후에는 "시세 수집 중"이 잠깐 보일 수 있습니다.
- 접속이 안 되면: ① 프로세스가 살아 있는지 ② 다른 프로그램이 포트를 쓰는지(`--port` 변경) ③ `--host` 를 바꿨는지 확인하세요.
- 이 대시보드는 **로컬 전용**입니다. 외부에 열어야 한다면 SSH 터널(`ssh -L 8080:127.0.0.1:8080 서버`)을 권합니다.

## 3. 토큰·IP 문제

- **403 `ip-not-allowed`**: 서버 공인 IP 가 토스 WTS > 설정 > Open API > 허용 IP 에 없습니다. 고정 IP 를 등록하세요. 유동 IP 환경이면 상시 서버를 쓰거나 프록시 고정 IP 가 필요합니다.
- **OAuth2 `access_denied`**: 토큰 발급 단계의 IP 차단입니다.
- **401 `token-revoked`**: 같은 client_id 로 다른 프로세스가 토큰을 재발급했습니다. 토큰 파일을 공유하도록 프로세스를 1개만 실행하세요.

## 4. 데이터·상태 백업

```bash
sqlite3 state/alpha_shield.db ".backup state/backup-$(date +%F).db"
cp -a state/toss_token.json state/backup-token.json    # 선택 (0600 유지)
python -m alpha_shield --export-ledger $(date +%Y-%m)  # 월별 원장
```

## 5. 장애 복구 순서

1. 서비스 상태 확인: `systemctl status alpha-shield`
2. 로그 확인: `journalctl -u alpha-shield -n 100 --no-pager`, `tail -n 100 logs/alpha-shield.log`
3. 프로세스가 죽어 있었다면 → 재기동 후 `--reconcile` 로 보유 대조 → 불일치면 매수 중단 상태로 매도만 허용
4. 15:00 ET 이후라면 소수점 매도가 불가하므로, 다음 개장까지 손절 공백을 인지하고 필요하면 토스 앱에서 수동 청산

## 6. systemd 배포

```bash
sudo cp deploy/alpha-shield.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alpha-shield
sudo systemctl status alpha-shield
```

- 서비스 파일의 `User`, `WorkingDirectory`, `EnvironmentFile` 을 서버 경로에 맞게 수정하세요.
- watchdog 은 10분 이상 로그 갱신이 없으면 서비스를 재시작합니다(`deploy/watchdog.sh`, cron 5분 권장).
- healthcheck 는 `--check` 와 DB 무결성만 확인합니다(`deploy/healthcheck.sh`).

## 7. 성능·호출량

5분 사이클 기준 대략:

- 시세: 종목 8~10개 1회(`prices` 일괄)
- 캔들: 일봉 8회(5분 캐시) + 1분봉 8회(30초 캐시)
- 계좌: 1~2회
- 외부: NQ/VIX 2회(60초/300초 캐시), 실적 1~2회(12시간 캐시), SEC(6시간/24시간 캐시), 뉴스(5분 캐시)

토스 한도(MARKET_DATA 15/s, CHART 20/s) 대비 여유가 큽니다. 429 를 받으면 `Retry-After` 만큼 대기 후 재시도합니다.

## 8. 변경 금지 권장

- `config/strategy.yaml` 의 수치: 백테스트 기준값이 무효가 됩니다.
- `capital.slots`: v3.2 는 2슬롯 고정(설정 검증에서 거부).
- `entry.signal_ttl_seconds`: 900초 고정(설정 검증에서 거부).
- `add.max_adds`: 1회 고정(설정 검증에서 거부).
