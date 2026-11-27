"""봇 내장 웹 대시보드의 화면(HTML/CSS/JS).

`web.py` 가 이 문자열을 그대로 내보내고 `__TOKEN__` 자리에 CSRF 토큰을 넣는다.
외부 CDN 없이 단독으로 뜨도록 폰트는 시스템 폰트 스택을 쓴다(Pretendard 가 설치돼 있으면 우선).
"""

PAGE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b1220">
<meta name="robots" content="noindex, nofollow">
<title>ALPHA SHIELD v3.2 · 봇 대시보드</title>
<style>
  :root{
    --bg:#0b1220; --bg2:#0e1727; --card:#131d31; --card2:#18243c; --line:#243350;
    --fg:#f3f7ff; --fg2:#c7d3e8; --muted:#93a4c3;
    --green:#34d399; --red:#fb7185; --amber:#fbbf24; --blue:#60a5fa; --violet:#a78bfa;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{
    background:radial-gradient(1200px 600px at 20% -10%, #16233b 0%, var(--bg) 55%) no-repeat, var(--bg);
    color:var(--fg);
    font-family:Pretendard,"Pretendard Variable",-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR","Segoe UI",Roboto,sans-serif;
    font-size:17px; line-height:1.62; -webkit-text-size-adjust:100%;
    padding-bottom:48px;
  }
  .topbar{
    position:sticky; top:0; z-index:20; display:flex; flex-wrap:wrap; gap:14px;
    align-items:center; justify-content:space-between;
    padding:16px clamp(14px,3vw,32px);
    background:rgba(9,15,27,.94); backdrop-filter:blur(8px);
    border-bottom:1px solid var(--line);
  }
  .brand{display:flex; align-items:center; gap:12px}
  .logo{
    width:44px;height:44px;border-radius:14px;display:grid;place-items:center;
    background:linear-gradient(145deg,#1d4ed8,#0ea5e9); color:#fff;font-size:20px;font-weight:800;
    box-shadow:0 6px 20px rgba(14,165,233,.28)
  }
  .brand h1{margin:0;font-size:24px;letter-spacing:.06em;font-weight:800}
  .brand p{margin:2px 0 0;color:var(--muted);font-size:14px}
  .topmeta{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  #clock{color:var(--fg2);font-size:15px;font-variant-numeric:tabular-nums}
  main{max-width:1280px;margin:0 auto;padding:18px clamp(12px,3vw,28px) 0}
  section{margin-bottom:22px}
  h2{font-size:21px;margin:0 0 14px;letter-spacing:.01em}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px clamp(14px,2vw,22px)}
  .panel.two{display:grid;grid-template-columns:1fr 1fr;gap:22px}
  .chip{
    display:inline-flex;align-items:center;gap:7px;border-radius:999px;
    padding:6px 13px;font-size:14.5px;font-weight:700;border:1px solid var(--line);
    background:var(--card2);color:var(--fg2);white-space:nowrap
  }
  .chip .dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}
  .chip.ok{color:#bff5dd;border-color:#1f6f52;background:#0f2e25}
  .chip.ok .dot{background:var(--green)}
  .chip.warn{color:#ffe7ae;border-color:#7a5a12;background:#2e2410}
  .chip.warn .dot{background:var(--amber)}
  .chip.bad{color:#ffd3da;border-color:#7c2334;background:#2f1420}
  .chip.bad .dot{background:var(--red)}
  .chip.calm .dot{background:var(--blue)}
  button{
    font:inherit;font-weight:700;border-radius:12px;border:1px solid var(--line);
    background:var(--card2);color:var(--fg);padding:11px 18px;cursor:pointer;
    transition:transform .06s ease, filter .15s ease; min-height:46px
  }
  button:hover:not(:disabled){filter:brightness(1.15)}
  button:active:not(:disabled){transform:translateY(1px)}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.primary{background:linear-gradient(145deg,#1d4ed8,#2563eb);border-color:#2b58b8}
  button.go{background:#0f3d2e;border-color:#1c6b50;color:#c7f9e5}
  button.stop{background:#3a1520;border-color:#7c2334;color:#ffd6dc}
  button.ghost{background:transparent}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px 18px}
  .kpi .label{color:var(--muted);font-size:15px;font-weight:600}
  .kpi .value{font-size:31px;font-weight:800;margin-top:5px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
  .kpi .sub{color:var(--fg2);font-size:14.5px;margin-top:4px;font-variant-numeric:tabular-nums}
  .up{color:var(--green)} .down{color:var(--red)} .flat{color:var(--fg2)}
  .modebar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:16px}
  .modebig{font-size:19px;font-weight:800}
  .controls{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:6px}
  .hint{color:var(--muted);font-size:15px;margin:12px 0 0}
  .guardgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(252px,1fr));gap:12px}
  .guard{background:var(--card2);border:1px solid var(--line);border-radius:15px;padding:14px 15px;border-left-width:6px}
  .guard.pass{border-left-color:#1f9d6f}
  .guard.block{border-left-color:#d4425a}
  .guard.unknown{border-left-color:#7a8800}
  .guard.armed{border-left-color:#3b6fd8}
  .guard h3{margin:0 0 6px;font-size:16.5px;display:flex;justify-content:space-between;gap:8px;align-items:center}
  .guard .st{font-size:13.5px;font-weight:800;padding:2px 9px;border-radius:999px;background:#0c1626;border:1px solid var(--line);color:var(--fg2);white-space:nowrap;flex:0 0 auto}
  .guard .num{font-size:22px;font-weight:800;font-variant-numeric:tabular-nums;margin:2px 0 2px}
  .guard .desc{color:var(--muted);font-size:14.5px;line-height:1.5}
  .symgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(268px,1fr));gap:13px}
  .sym{background:var(--card2);border:1px solid var(--line);border-radius:16px;padding:15px 16px}
  .sym.ready{border-color:#1f7a5a;box-shadow:0 0 0 1px rgba(52,211,153,.15) inset}
  .sym.held{border-color:#355c9c}
  .sym .head{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:8px}
  .sym .tick{font-size:21px;font-weight:800;letter-spacing:.02em}
  .sym .px{font-size:20px;font-weight:800;font-variant-numeric:tabular-nums}
  .sym .chg{font-size:14.5px;font-weight:700;font-variant-numeric:tabular-nums}
  .kv{display:grid;grid-template-columns:1fr 1fr;gap:6px 14px;font-size:15px;color:var(--fg2);font-variant-numeric:tabular-nums}
  .kv div{display:flex;justify-content:space-between;gap:8px;border-bottom:1px dotted #223050;padding-bottom:3px}
  .kv b{color:var(--fg);font-weight:700}
  .sym .state{margin-top:10px;font-size:15px;font-weight:700}
  .bar{height:7px;border-radius:99px;background:#0c1626;overflow:hidden;margin-top:8px}
  .bar > i{display:block;height:100%;background:linear-gradient(90deg,#2563eb,#34d399)}
  ul.log{list-style:none;margin:0;padding:0;font-size:15px}
  ul.log li{display:grid;grid-template-columns:132px 74px 1fr;gap:10px;padding:9px 2px;border-bottom:1px solid #1d2a44;align-items:baseline}
  ul.log li .t{color:var(--muted);font-variant-numeric:tabular-nums;font-size:13.5px}
  ul.log li .m{color:var(--fg2)}
  ul.log li .s{font-weight:800;font-size:13.5px;letter-spacing:.02em}
  .empty{color:var(--muted);font-size:15px;padding:10px 2px}
  .foot{color:var(--muted);font-size:14px;margin:12px 0 0}
  .pos{background:var(--card2);border:1px solid var(--line);border-radius:16px;padding:15px 16px;margin-bottom:12px}
  .pos .head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
  .pos .tick{font-size:22px;font-weight:800}
  .pos .pnl{font-size:22px;font-weight:800;font-variant-numeric:tabular-nums}
  .pos .kv{grid-template-columns:1fr 1fr 1fr;margin-top:10px}
  .toast{
    position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(20px);
    background:#0f1b2f;border:1px solid var(--line);color:var(--fg);
    padding:13px 22px;border-radius:14px;font-weight:700;box-shadow:0 12px 34px rgba(0,0,0,.5);
    opacity:0;pointer-events:none;transition:all .22s ease;z-index:60;max-width:92vw;text-align:center
  }
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
  .toast.bad{border-color:#7c2334;background:#2a1420}
  .toast.good{border-color:#1c6b50;background:#0f2e25}
  footer{max-width:1280px;margin:0 auto;padding:8px clamp(14px,3vw,28px) 30px;color:var(--muted);font-size:13.5px;line-height:1.7}
  @media (max-width:860px){
    .panel.two{grid-template-columns:1fr}
    .cards{grid-template-columns:1fr 1fr}
    .kpi .value{font-size:26px}
    ul.log li{grid-template-columns:1fr;gap:2px}
    .pos .kv{grid-template-columns:1fr 1fr}
  }
  @media (max-width:520px){
    body{font-size:16.5px}
    .brand h1{font-size:20px}
    .cards{grid-template-columns:1fr 1fr;gap:10px}
    .kpi{padding:13px 14px}
    .kpi .label{font-size:13.5px}
    .kpi .value{font-size:24px}
    .kpi .sub{font-size:12.8px}
    .kv{grid-template-columns:1fr}
    .controls button{flex:1 1 44%}
  }
  @media (max-width:380px){
    .cards{grid-template-columns:1fr}
  }
</style>
</head>
<body>
<header class="topbar">
  <div class="brand">
    <div class="logo">▲</div>
    <div>
      <h1>ALPHA SHIELD</h1>
      <p>v3.2 · 2슬롯 소수점 자동매매 · 봇 내장 대시보드</p>
    </div>
  </div>
  <div class="topmeta">
    <span id="clock">–</span>
    <span class="chip" id="conn"><span class="dot"></span><span>연결 중</span></span>
    <button id="refresh" class="ghost">새로고침</button>
  </div>
</header>

<main>
  <section class="modebar" id="modebar"></section>

  <section>
    <div class="cards" id="summary"></div>
  </section>

  <section class="panel">
    <h2>운용 제어</h2>
    <div class="controls">
      <button id="btn-dry" class="go">백그라운드 시작 · dry-run</button>
      <button id="btn-paper" class="primary">백그라운드 시작 · paper</button>
      <button id="btn-once">1회 즉시 실행</button>
      <button id="btn-stop" class="stop">정지</button>
      <button id="btn-halt">킬스위치 ON</button>
      <button id="btn-live" disabled>실주문 전환 (잠김)</button>
    </div>
    <p class="hint" id="controlhint">–</p>
  </section>

  <section class="panel">
    <h2>5중 다중 방어막</h2>
    <div class="guardgrid" id="guards"></div>
    <p class="foot">매수는 fail-closed(확인 불가 → 정지), 매도는 fail-open(청산은 계속).</p>
  </section>

  <section class="panel">
    <h2>8종목 유니버스 판정</h2>
    <div class="symgrid" id="universe"></div>
    <p class="foot" id="universeFoot">–</p>
  </section>

  <section class="panel">
    <h2>보유 슬롯 (최대 2)</h2>
    <div id="positions"></div>
  </section>

  <section class="panel two">
    <div>
      <h2>최근 주문</h2>
      <ul class="log" id="orders"></ul>
    </div>
    <div>
      <h2>알림 · 판정 로그</h2>
      <ul class="log" id="alerts"></ul>
    </div>
  </section>
</main>

<footer>
  <div>이 화면은 <b>127.0.0.1</b> 에만 열리는 로컬 대시보드입니다. 실주문은 <code>LIVE_ORDERS=true</code> 와 <code>state/ARMED</code> 파일이 모두 있을 때만 열립니다.</div>
  <div>전략 수치의 단일 출처는 <code>config/strategy.yaml</code> · <code>config/guards.yaml</code> 입니다. 이 페이지는 투자 자문이 아닙니다.</div>
</footer>

<div class="toast" id="toast"></div>

<script>
(function(){
  "use strict";
  var TOKEN = "__TOKEN__";
  var state = null;
  var busy = false;

  function $(id){ return document.getElementById(id); }
  function el(tag, cls, text){
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function num(v){ return (v === null || v === undefined || v === "") ? "–" : String(v); }
  function usd(v){
    if (v === null || v === undefined || v === "") return "–";
    var n = Number(v);
    if (isFinite(n)) return "$" + n.toLocaleString("en-US", {minimumFractionDigits:2, maximumFractionDigits:2});
    return "$" + v;
  }
  function pct(v, digits){
    if (v === null || v === undefined || v === "") return "–";
    var n = Number(v);
    if (!isFinite(n)) return v + "%";
    var d = (digits === undefined) ? 2 : digits;
    return (n > 0 ? "+" : "") + n.toFixed(d) + "%";
  }
  function cls(n){ return n > 0 ? "up" : (n < 0 ? "down" : "flat"); }
  function mag(v, digits){
    if (v === null || v === undefined || v === "") return "–";
    var n = Number(v);
    if (!isFinite(n)) return v + "%";
    return Math.abs(n).toFixed(digits === undefined ? 2 : digits) + "%";
  }
  function toast(msg, kind){
    var t = $("toast");
    t.textContent = msg;
    t.className = "toast show" + (kind ? " " + kind : "");
    clearTimeout(toast._t);
    toast._t = setTimeout(function(){ t.className = "toast"; }, 4200);
  }
  function chip(text, kind){
    var c = el("span", "chip " + (kind || ""));
    c.appendChild(el("span", "dot"));
    c.appendChild(el("span", null, text));
    return c;
  }
  function api(path, opts){
    opts = opts || {};
    opts.headers = Object.assign({"X-Alpha-Token": TOKEN, "Accept": "application/json"}, opts.headers || {});
    if (opts.body) opts.headers["Content-Type"] = "application/json";
    return fetch(path, opts).then(function(r){
      return r.json().catch(function(){ return {ok:false, error:"bad-json"}; }).then(function(body){
        if (!r.ok || body.ok === false) {
          var msg = body.detail || body.error || ("HTTP " + r.status);
          throw new Error(msg);
        }
        return body;
      });
    });
  }

  function renderMode(s){
    var box = $("modebar");
    box.textContent = "";
    var mode = s.runner && s.runner.state === "running" ? (s.runner.mode || s.mode) : s.mode;
    var label = { "dry-run":"DRY-RUN (주문 없음)", "paper":"PAPER (모의 체결)", "live":"LIVE (실주문)" }[mode] || mode;
    box.appendChild(el("span", "modebig", "현재 모드 · " + label));
    box.appendChild(chip(
      s.runner.state === "running" ? "루프 실행 중" : "루프 정지",
      s.runner.state === "running" ? "ok" : "calm"
    ));
    if (s.runner.cycles) box.appendChild(chip("완료 사이클 " + s.runner.cycles + "회", "calm"));
    box.appendChild(chip(
      s.safety.halted ? "킬스위치 ON" : "킬스위치 OFF",
      s.safety.halted ? "bad" : "ok"
    ));
    box.appendChild(chip(
      s.safety.live_allowed ? "실주문 잠금 해제" : "실주문 잠금",
      s.safety.live_allowed ? "warn" : "ok"
    ));
  }

  function kpi(label, value, sub, tone){
    var box = el("div", "kpi");
    box.appendChild(el("div", "label", label));
    var v = el("div", "value" + (tone ? " " + tone : ""), value);
    box.appendChild(v);
    if (sub) box.appendChild(el("div", "sub", sub));
    return box;
  }

  function renderSummary(s){
    var box = $("summary");
    box.textContent = "";
    var a = s.account || {}, t = s.today || {}, sl = s.slots || {};
    box.appendChild(kpi("총자산", usd(a.equity_usd), "현금 " + usd(a.cash_usd) + " · 평가 " + usd(a.positions_value)));
    box.appendChild(kpi("현금", usd(a.cash_usd), "안전 버퍼 " + usd(a.buffer_usd) + " · 주문가능 " + usd(a.spendable_usd)));
    box.appendChild(kpi("안전 버퍼", usd(a.buffer_usd), "max(자산×15%, $55) 상시 보존"));
    box.appendChild(kpi("슬롯", (sl.used || 0) + " / " + (sl.total || 2), sl.free + "개 비어 있음 · 1회 " + usd((sl.tier || {}).entry_usd) + " · 종목상한 " + usd((sl.tier || {}).cap_usd)));
    var pnl = Number(t.pnl_usd || 0);
    box.appendChild(kpi("당일 손익", (pnl > 0 ? "+" : "") + usd(pnl), (t.equity_open ? "기준자산 " + usd(t.equity_open) : "당일 기준자산 미기록") + " · " + pct(t.pnl_pct), cls(pnl)));
    var safetyText = s.safety.live_allowed ? "잠금 해제됨" : "잠김";
    box.appendChild(kpi("실주문 잠금", safetyText, "LIVE_ORDERS " + (s.safety.live_orders_env ? "ON" : "OFF") + " · ARMED " + (s.safety.armed ? "있음" : "없음"), s.safety.live_allowed ? "down" : "flat"));
    if (s.session) box.appendChild(kpi("장 상태", s.session.label || "–", s.session.now_et + " ET · " + s.session.open_et + "~" + s.session.close_et));
  }

  function renderControls(s){
    var r = s.runner || {};
    var running = r.state === "running";
    $("btn-dry").disabled = running || busy;
    $("btn-paper").disabled = running || busy;
    $("btn-once").disabled = busy;
    $("btn-stop").disabled = !running || busy;
    $("btn-halt").disabled = busy;
    $("btn-halt").textContent = s.safety.halted ? "킬스위치 OFF" : "킬스위치 ON";
    $("btn-halt").className = s.safety.halted ? "go" : "stop";
    var liveBtn = $("btn-live");
    liveBtn.disabled = !s.safety.live_allowed || running || busy;
    liveBtn.textContent = s.safety.live_allowed
      ? (running ? "실주문 전환 (실행 중)" : "실주문 전환 (이중 잠금 해제됨)")
      : "실주문 전환 (잠김)";
    liveBtn.className = s.safety.live_allowed ? "stop" : "";
    var parts = [];
    if (r.last_error) parts.push("마지막 오류: " + r.last_error);
    if (r.last_cycle_at) parts.push("마지막 사이클: " + r.last_cycle_at);
    parts.push("사이클 간격 " + (r.interval_seconds || 300) + "초");
    if (!s.safety.live_allowed) parts.push("실주문은 LIVE_ORDERS=true + state/ARMED 가 모두 있어야 열립니다.");
    $("controlhint").textContent = parts.join(" · ");
  }

  function guardCard(title, g, numText, desc){
    var box = el("div", "guard " + (g.status || "unknown"));
    var h = el("h3");
    h.appendChild(el("span", null, title));
    h.appendChild(el("span", "st", ({pass:"통과", block:"차단", unknown:"확인 불가", armed:"대기", boost:"강화"})[g.status] || g.status));
    box.appendChild(h);
    if (numText) box.appendChild(el("div", "num", numText));
    if (desc) box.appendChild(el("div", "desc", desc));
    return box;
  }

  function renderGuards(s){
    var box = $("guards");
    box.textContent = "";
    var g = s.guards || {};
    var m = g.market || {};
    box.appendChild(guardCard("① 시장 (NQ·VIX)", m,
      "NQ " + pct(m.nq_change_pct, 2) + " · VIX " + num(m.vix),
      "차단 기준 NQ ≤ −" + num(m.nq_block_pct) + "% 또는 VIX ≥ " + num(m.vix_block)));
    var se = g.session || {};
    box.appendChild(guardCard("② 세션 (주문 가능 창)", se, se.label || "", se.detail || ""));
    var ev = g.events || {};
    box.appendChild(guardCard("③ 이벤트·공시", ev,
      (ev.blocked && ev.blocked.length ? "차단 " + ev.blocked.join(", ") : "차단 종목 없음"),
      ev.detail || ""));
    var nw = g.news || {};
    box.appendChild(guardCard("④ 뉴스", nw,
      (nw.blocked && nw.blocked.length ? "차단 " + nw.blocked.join(", ") : (nw.enabled ? "헤드라인 정상" : "가드 꺼짐")),
      nw.detail || ""));
    var mc = g.macro || {};
    var evs = (mc.today_events || []);
    box.appendChild(guardCard("⑤ 거시 (FOMC·CPI·NFP)", mc,
      evs.length ? "오늘 " + evs.join("·") : "오늘 발표 없음",
      (mc.detail || "") + (mc.next ? " · 다음 " + mc.next : "")));
    var dt = g.data || {};
    box.appendChild(guardCard("＋ 데이터 안전장치", dt, dt.label || "",
      dt.detail || ""));
  }

  var REASON = {
    "ok":"진입 조건 충족", "no-dip":"눌림 부족", "rsi-too-high":"RSI(2) 높음",
    "chase-block":"추격 차단(RSI≥70)", "below-sma-floor":"SMA200×0.80 미달",
    "rsi-unavailable":"RSI 결측", "sma200-unavailable":"SMA200 결측",
    "hod-unavailable":"장중고점 결측", "invalid-price":"가격 이상",
    "quote-missing":"시세 없음", "quote-stale":"시세 지연",
    "quote-timestamp-missing":"시세 시각 없음", "daily-bars-stale":"일봉 지연",
    "held":"보유 중", "no-data":"데이터 없음", "signal-pending":"신호 유효"
  };
  function reasonText(r){ return REASON[r] || r; }

  function renderUniverse(s){
    var box = $("universe");
    box.textContent = "";
    var list = s.universe || [];
    if (!list.length) {
      box.appendChild(el("div", "empty", "시세를 아직 받지 못했습니다. 잠시 후 새로고침하세요."));
    }
    list.forEach(function(u){
      var card = el("div", "sym " + (u.status === "ready" ? "ready" : (u.held ? "held" : "")));
      var head = el("div", "head");
      var left = el("div");
      left.appendChild(el("div", "tick", u.symbol));
      left.appendChild(el("div", "chg " + cls(Number(u.change_pct)), pct(u.change_pct) + " · 가중 " + num(u.weight)));
      head.appendChild(left);
      var right = el("div", null);
      right.appendChild(el("div", "px", usd(u.last_price)));
      right.appendChild(el("div", "chg", "고점대비 " + pct(u.drawdown_pct)));
      head.appendChild(right);
      card.appendChild(head);

      var kv = el("div", "kv");
      function row(k, v){ var d = el("div"); d.appendChild(el("span", null, k)); d.appendChild(el("b", null, v)); kv.appendChild(d); }
      row("ATR(20)", mag(u.atr_pct));
      row("진입 임계", mag(u.threshold_pct));
      row("RSI(2)", num(u.rsi2));
      row("SMA200", usd(u.sma200));
      row("슬롯 점수", num(u.score));
      row("장중 고점", usd(u.hod));
      card.appendChild(kv);

      var score = Number(u.score || 0);
      var bar = el("div", "bar");
      var fill = el("i");
      fill.style.width = Math.max(0, Math.min(100, score * 33)) + "%";
      bar.appendChild(fill);
      card.appendChild(bar);

      var st = el("div", "state");
      var label = u.status_label || reasonText(u.reason);
      st.textContent = (u.status === "ready" ? "● " : (u.held ? "◆ " : "○ ")) + label;
      st.className = "state " + (u.status === "ready" ? "up" : (u.held ? "flat" : "flat"));
      card.appendChild(st);
      box.appendChild(card);
    });
    var foot = s.universe_note || "";
    $("universeFoot").textContent = foot;
  }

  function renderPositions(s){
    var box = $("positions");
    box.textContent = "";
    var list = s.positions || [];
    if (!list.length) {
      box.appendChild(el("div", "empty", "보유 포지션 없음 — " + ((s.slots || {}).free || 0) + "슬롯 비어 있음"));
      return;
    }
    list.forEach(function(p){
      var card = el("div", "pos");
      var head = el("div", "head");
      var l = el("div");
      l.appendChild(el("div", "tick", p.symbol));
      l.appendChild(el("div", "chg", "평단 " + usd(p.avg_price) + " → 현재 " + usd(p.last_price)));
      head.appendChild(l);
      var pnl = Number(p.pnl_pct || 0);
      head.appendChild(el("div", "pnl " + cls(pnl), pct(p.pnl_pct) + " (" + usd(p.pnl_usd) + ")"));
      card.appendChild(head);

      var kv = el("div", "kv");
      function row(k, v, tone){ var d = el("div"); d.appendChild(el("span", null, k)); var b = el("b", null, v); if (tone) b.className = tone; d.appendChild(b); kv.appendChild(d); }
      row("수량", p.qty);
      row("목표 익절", "+" + num(p.tp_target_pct) + "% · " + usd(p.tp_price));
      row("손절", "−" + num(p.stop_loss_pct) + "% · " + usd(p.stop_price));
      row("물타기", (p.add_count ? "1회 완료" : "가능") );
      row("보유", p.trading_days + "거래일 / " + p.calendar_days + "일");
      row("진입 VIX", num(p.entry_vix));
      card.appendChild(kv);
      box.appendChild(card);
    });
  }

  function renderOrders(s){
    var ul = $("orders");
    ul.textContent = "";
    var list = s.orders || [];
    if (!list.length) { ul.appendChild(el("li", null, "주문 없음")); return; }
    list.forEach(function(o){
      var li = el("li");
      li.appendChild(el("span", "t", o.at_kst));
      var side = el("span", "s " + (o.side === "BUY" ? "up" : "down"), o.side === "BUY" ? "매수" : "매도");
      li.appendChild(side);
      li.appendChild(el("span", "m", o.symbol + " · " + o.status + " · " + (o.qty || "-") + "주 @ " + usd(o.avg_price) + " · " + (o.reason || "")));
      ul.appendChild(li);
    });
  }

  function renderAlerts(s){
    var ul = $("alerts");
    ul.textContent = "";
    var list = s.alerts || [];
    if (!list.length) { ul.appendChild(el("li", null, "알림 없음")); return; }
    list.forEach(function(a){
      var li = el("li");
      li.appendChild(el("span", "t", a.at_kst));
      li.appendChild(el("span", "s " + (a.level === "error" ? "down" : (a.level === "info" ? "up" : "flat")), a.level));
      li.appendChild(el("span", "m", a.message));
      ul.appendChild(li);
    });
  }

  function render(s){
    state = s;
    $("clock").textContent = (s.clock || {}).kst || "–";
    var conn = $("conn");
    conn.className = "chip ok";
    conn.lastChild.textContent = "실시간 연결";
    renderMode(s); renderSummary(s); renderControls(s);
    renderGuards(s); renderUniverse(s); renderPositions(s);
    renderOrders(s); renderAlerts(s);
  }

  function fail(err){
    var conn = $("conn");
    conn.className = "chip bad";
    if (conn.lastChild) conn.lastChild.textContent = "연결 끊김";
    toast("대시보드를 불러오지 못했습니다: " + (err && err.message ? err.message : err), "bad");
  }

  function load(){
    return api("/api/status").then(render).catch(fail);
  }

  function action(body, okMsg){
    if (busy) return;
    busy = true;
    var running = state && state.runner && state.runner.state === "running";
    renderControls(state || {runner:{}, safety:{}});
    api("/api/control", {method:"POST", body: JSON.stringify(body)})
      .then(function(res){
        toast(res.message || okMsg || "완료했습니다.", "good");
        return load();
      })
      .catch(function(e){ toast(e.message || "실행하지 못했습니다.", "bad"); })
      .then(function(){ busy = false; if (state) renderControls(state); });
  }

  function once(){
    if (busy) return;
    busy = true;
    renderControls(state || {runner:{}, safety:{}});
    api("/api/run-once", {method:"POST", body: JSON.stringify({})})
      .then(function(res){
        var r = res.report || {};
        toast("1회 실행 완료" + (r.skipped ? " · " + r.skipped : "") + (r.orders ? " · 주문 " + r.orders + "건" : ""));
        return load();
      })
      .catch(function(e){ toast(e.message || "실행하지 못했습니다.", "bad"); })
      .then(function(){ busy = false; if (state) renderControls(state); });
  }

  $("btn-dry").addEventListener("click", function(){ action({action:"start", mode:"dry-run"}, "dry-run 루프를 시작했습니다."); });
  $("btn-paper").addEventListener("click", function(){ action({action:"start", mode:"paper"}, "paper 루프를 시작했습니다."); });
  $("btn-live").addEventListener("click", function(){
    if (!confirm("실계좌 실주문 모드로 전환합니다. 계속할까요?")) return;
    action({action:"start", mode:"live"}, "live 루프를 시작했습니다.");
  });
  $("btn-stop").addEventListener("click", function(){ action({action:"stop"}, "루프를 정지했습니다."); });
  $("btn-once").addEventListener("click", once);
  $("btn-halt").addEventListener("click", function(){
    var on = !(state && state.safety && state.safety.halted);
    action({action: on ? "halt-on" : "halt-off"}, on ? "킬스위치를 켰습니다." : "킬스위치를 해제했습니다.");
  });
  $("refresh").addEventListener("click", function(){ load().then(function(){ toast("새로고침 완료", "good"); }).catch(function(){}); });

  load();
  setInterval(function(){ if (!document.hidden) load(); }, 10000);
})();
</script>
</body>
</html>
"""
