"""
main.py — Polymarket Copy-Trade Bot v2 (Railway)
─────────────────────────────────────────────────
Corre dois processos em paralelo:
  • Thread A — bot de paper trading (ciclos de 30s)
  • Thread B — servidor HTTP na PORT (Railway injeta automaticamente)

Configuração via variáveis de ambiente (Railway → Variables):
  BUDGET           (default: 1000)
  INTERVAL         (default: 30)
  TOP_WALLETS      (default: 5)
  MARKETS_TO_SCAN  (default: 15)
  MAX_TRADES       (default: 500)
  TELEGRAM_TOKEN   (opcional)
  TELEGRAM_CHAT_ID (opcional)
  PORT             (injetado pelo Railway automaticamente)
"""

import os
import json
import time
import threading
import requests
from datetime import datetime
from dataclasses import dataclass
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIGURAÇÃO — via env vars ou defaults
# ─────────────────────────────────────────────

BUDGET          = float(os.environ.get("BUDGET",          1000))
INTERVAL        = int(os.environ.get("INTERVAL",          30))
TOP_WALLETS     = int(os.environ.get("TOP_WALLETS",       5))
MARKETS_TO_SCAN = int(os.environ.get("MARKETS_TO_SCAN",   15))
MAX_TRADES      = int(os.environ.get("MAX_TRADES",        500))
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN",        "")
TELEGRAM_CHAT_ID= os.environ.get("TELEGRAM_CHAT_ID",      "")
PORT            = int(os.environ.get("PORT",              8080))

DASHBOARD_INTERVAL = 2   # ciclos entre regenerações do HTML

# ─────────────────────────────────────────────

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
HEADERS   = {"User-Agent": "polymarket-bot/2.0"}

COPY_FRACTION = 0.05
MAX_PER_TRADE = 50.0

# Estado global partilhado entre threads (protegido por lock)
_lock         = threading.Lock()
_state: dict  = {
    "trades":    [],
    "budget":    {},
    "wallets":   [],
    "iteration": 0,
    "status":    "A inicializar…",
    "started_at": datetime.now().isoformat(),
}

# ══════════════════════════════════════════════
# UTILIDADES
# ══════════════════════════════════════════════

def now() -> str:
    return datetime.now().strftime("%H:%M:%S")

def now_iso() -> str:
    return datetime.now().isoformat()

def log(text: str):
    print(f"[{now()}] {text}", flush=True)

def _get(url, params={}, timeout=8):
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        if r.status_code in (400, 404):
            return []
        if r.status_code == 429:
            time.sleep(3)
            return []
        r.raise_for_status()
        return r.json()
    except Exception:
        return []

# ══════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════

def telegram_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception:
        pass

def telegram_trade_alert(pt: dict):
    icon = "🟢" if pt["side"] == "BUY" else "🔴"
    telegram_send(
        f"{icon} <b>NOVO TRADE</b>\n"
        f"⏰ {pt['timestamp']}\n"
        f"👛 <code>{pt['wallet'][:10]}...{pt['wallet'][-6:]}</code>\n"
        f"📌 {pt['title'][:60]}\n"
        f"💵 Preço: <b>{pt['price']:.3f}</b>  |  {pt['outcome']}\n"
        f"📋 [PAPER] {pt['usdc_size']:.2f} USDC"
    )

def telegram_stats(s: dict):
    icon = "📈" if s["pnl"] >= 0 else "📉"
    telegram_send(
        f"{icon} <b>STATS — ciclo {s['iteration']}</b>\n"
        f"Trades: {s['total']}  ✅{s['wins']}  ❌{s['losses']}\n"
        f"Win Rate: <b>{s['win_rate']:.1f}%</b>\n"
        f"P&amp;L: <b>{s['pnl']:+.2f} USDC</b>  ROI: <b>{s['roi']:+.1f}%</b>"
    )

# ══════════════════════════════════════════════
# P&L REAL
# ══════════════════════════════════════════════

_price_cache: dict = {}
CACHE_TTL = 30

def get_live_price(token_id: str):
    if not token_id:
        return None
    cached = _price_cache.get(token_id)
    if cached:
        price, ts = cached
        if time.time() - ts < CACHE_TTL:
            return price

    data = _get(f"{CLOB_API}/midpoint", {"token_id": token_id})
    if isinstance(data, dict):
        mid = data.get("mid")
        if mid is not None:
            try:
                p = float(mid)
                _price_cache[token_id] = (p, time.time())
                return p
            except Exception:
                pass

    book = _get(f"{CLOB_API}/book", {"token_id": token_id})
    if isinstance(book, dict):
        prices = []
        for side_key in ("bids", "asks"):
            entries = book.get(side_key, [])
            if entries:
                try:
                    prices.append(float(entries[0]["price"]))
                except Exception:
                    pass
        if prices:
            p = sum(prices) / len(prices)
            _price_cache[token_id] = (p, time.time())
            return p
    return None

def resolve_trade_pnl(trade: dict, budget: dict) -> dict:
    """
    Atualiza o P&L de um trade aberto com base no preço atual do mercado.
    Quando o mercado resolve (preco >= 0.97 ou <= 0.03), fecha o trade,
    calcula o retorno final e credita o saldo da carteira.

    Logica realista do Polymarket:
      - Compraste tokens a `entry` USDC cada
      - Se ganhas: cada token vale 1 USDC -> payout = tokens * 1.0
      - Se perdes: cada token vale 0 USDC -> payout = 0
    """
    if "CLOSED" in trade.get("status", ""):
        return trade

    # Tentar obter preco ao vivo via token_id
    token_id = trade.get("token_id", "")
    current_price = get_live_price(token_id) if token_id else None

    # Fallback: buscar via conditionId na Gamma API
    if current_price is None:
        cid = trade.get("condition_id", "")
        if cid:
            mkt = _get(f"{GAMMA_API}/markets", {"conditionId": cid})
            if isinstance(mkt, list) and mkt:
                mkt = mkt[0]
            if isinstance(mkt, dict):
                # Mercado resolvido?
                if mkt.get("closed") or mkt.get("resolved"):
                    winning = str(mkt.get("winningOutcome") or "").upper()
                    outcome = str(trade.get("outcome") or "").upper()
                    if winning and outcome and winning in outcome:
                        current_price = 1.0
                    elif winning:
                        current_price = 0.0
                # Mercado ainda aberto: ler preco do token
                if current_price is None:
                    tokens_raw = mkt.get("tokens") or mkt.get("outcomes") or []
                    outcome_trade = str(trade.get("outcome") or "").upper()
                    for tok in tokens_raw if isinstance(tokens_raw, list) else []:
                        tok_name = str(tok.get("outcome") or tok.get("name") or "").upper()
                        if tok_name and tok_name in outcome_trade:
                            p = tok.get("price") or tok.get("lastTradePrice")
                            if p is not None:
                                try:
                                    current_price = float(p)
                                except Exception:
                                    pass
                            break

    if current_price is None:
        trade["updated_at"] = now()
        return trade

    entry  = float(trade.get("price", 0))
    size   = float(trade.get("usdc_size", 0))
    wallet = trade.get("wallet", "")

    if entry <= 0 or size <= 0:
        return trade

    # Tokens comprados = USDC investidos / preco de entrada
    tokens_bought = size / entry

    # P&L nao realizado (mark-to-market)
    trade["pnl"]           = round(tokens_bought * current_price - size, 4)
    trade["current_price"] = round(current_price, 4)
    trade["updated_at"]    = now()

    # Verificar se o mercado resolveu
    market_resolved = current_price >= 0.97 or current_price <= 0.03

    if market_resolved:
        won = current_price >= 0.97

        if won:
            payout          = round(tokens_bought * 1.0, 4)
            trade["pnl"]    = round(payout - size, 4)
            trade["status"] = "CLOSED_WIN"
        else:
            payout          = 0.0
            trade["pnl"]    = round(-size, 4)
            trade["status"] = "CLOSED_LOSS"

        trade["payout"]    = payout
        trade["closed_at"] = now()

        # Creditar retorno no saldo da carteira
        if wallet and wallet in budget:
            budget[wallet] = round(budget[wallet] + payout, 4)
            log(f"  Fechado [{trade['status']}] {trade['title'][:35]}  "
                f"payout={payout:.2f}  P&L={trade['pnl']:+.2f}  saldo={budget[wallet]:.2f}")
            icon = "WIN" if won else "LOSS"
            telegram_send(
                f"{'✅' if won else '❌'} <b>TRADE FECHADO — {icon}</b>\n"
                f"📌 {trade['title'][:55]}\n"
                f"💵 Entrada: {entry:.3f} → Resolução: {current_price:.3f}\n"
                f"💰 Payout: <b>{payout:.2f} USDC</b>  "
                f"P&amp;L: <b>{trade['pnl']:+.2f}</b>\n"
                f"💼 Saldo: <b>{budget[wallet]:.2f} USDC</b>"
            )

    return trade

# ══════════════════════════════════════════════
# ESTATÍSTICAS
# ══════════════════════════════════════════════

def compute_stats(trades: list, budget: dict, iteration: int = 0) -> dict:
    wins = losses = open_count = closed_count = 0
    pnl = apostado = 0.0
    for t in trades:
        size   = t["usdc_size"]
        apostado += size
        status = t.get("status", "OPEN")
        if "CLOSED" in status:
            closed_count += 1
            if "WIN" in status:
                wins += 1
                pnl += t.get("pnl", 0)
            else:
                losses += 1
                pnl += t.get("pnl", -size)
        else:
            open_count += 1
            pnl += t.get("pnl", 0.0)

    total    = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0
    roi      = (pnl / apostado * 100) if apostado > 0 else 0

    return {
        "iteration": iteration,
        "total":     len(trades),
        "wins":      wins,
        "losses":    losses,
        "open":      open_count,
        "closed":    closed_count,
        "win_rate":  round(win_rate, 2),
        "pnl":       round(pnl, 4),
        "apostado":  round(apostado, 2),
        "roi":       round(roi, 2),
        "updated_at": now_iso(),
    }

# ══════════════════════════════════════════════
# DASHBOARD HTML
# ══════════════════════════════════════════════

def generate_dashboard(trades, budget, wallets, iteration) -> str:
    s = compute_stats(trades, budget, iteration)
    pnl_color = "#00e676" if s["pnl"] >= 0 else "#ff5252"
    roi_sign  = "+" if s["roi"] >= 0 else ""

    rows = ""
    for t in reversed(trades[-50:]):
        st = t.get("status", "OPEN")
        if "WIN" in st:
            badge = '<span class="badge win">WIN ✅</span>'
        elif "LOSS" in st:
            badge = '<span class="badge loss">LOSS ❌</span>'
        else:
            badge = '<span class="badge open">OPEN</span>'
        pnl_val  = t.get("pnl", 0.0)
        pnl_cls  = "pos" if pnl_val >= 0 else "neg"
        cur_p    = t.get("current_price", t["price"])
        side_cls = "buy" if t["side"] == "BUY" else "sell"
        payout   = t.get("payout", "—")
        payout_str = f"{payout:.2f}" if isinstance(payout, float) else "—"
        closed_t = t.get("closed_at", "")
        time_str = closed_t if closed_t else t["timestamp"]
        rows += f"""
        <tr>
          <td>{time_str}</td>
          <td class="mono">{t['wallet'][:8]}…{t['wallet'][-4:]}</td>
          <td>{t['title'][:42]}</td>
          <td class="{side_cls}">{t['side']}</td>
          <td class="mono">{t['price']:.3f} → {cur_p:.3f}</td>
          <td class="mono">{t['usdc_size']:.2f}</td>
          <td class="mono">{payout_str}</td>
          <td class="mono {pnl_cls}">{pnl_val:+.4f}</td>
          <td>{badge}</td>
        </tr>"""

    balances = ""
    for w, b in budget.items():
        pct   = (b / BUDGET) * 100
        bar_w = max(0, min(100, pct))
        color = "#00e676" if pct > 70 else "#ffab40" if pct > 40 else "#ff5252"
        balances += f"""
        <div class="wallet-card">
          <div class="wallet-addr">{w[:10]}…{w[-6:]}</div>
          <div class="wallet-balance">{b:.2f} <span class="unit">USDC</span></div>
          <div class="bar-bg"><div class="bar-fill" style="width:{bar_w}%;background:{color}"></div></div>
          <div class="bar-label">{pct:.1f}% restante</div>
        </div>"""

    uptime_since = _state.get("started_at", "")[:19].replace("T", " ")

    return f"""<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polymarket Bot v2</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#0a0e14;--surface:#111720;--border:#1e2a38;
    --text:#c8d6e5;--muted:#4a6080;--accent:#00e5ff;
    --green:#00e676;--red:#ff5252;--yellow:#ffab40;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;font-size:14px;line-height:1.6}}
  header{{background:var(--surface);border-bottom:1px solid var(--border);padding:16px 32px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}}
  .logo{{font-family:'Space Mono',monospace;font-size:14px;color:var(--accent);letter-spacing:2px;text-transform:uppercase}}
  .meta{{color:var(--muted);font-size:12px}}.meta span{{color:var(--text)}}
  .live-dot{{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);margin-right:6px;animation:pulse 2s infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
  .container{{max-width:1200px;margin:0 auto;padding:24px}}
  .grid-4{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px}}
  .kpi{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:18px;position:relative;overflow:hidden}}
  .kpi::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--accent)}}
  .kpi-label{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}}
  .kpi-value{{font-family:'Space Mono',monospace;font-size:24px;font-weight:700}}
  .kpi-sub{{font-size:11px;color:var(--muted);margin-top:4px}}
  h2{{font-size:11px;text-transform:uppercase;letter-spacing:2px;color:var(--muted);margin-bottom:12px}}
  .wallets-row{{display:flex;gap:10px;margin-bottom:24px;flex-wrap:wrap}}
  .wallet-card{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px;min-width:170px;flex:1}}
  .wallet-addr{{font-family:'Space Mono',monospace;font-size:10px;color:var(--muted);margin-bottom:4px}}
  .wallet-balance{{font-family:'Space Mono',monospace;font-size:18px;font-weight:700}}
  .unit{{font-size:10px;color:var(--muted)}}
  .bar-bg{{background:var(--border);border-radius:4px;height:4px;margin-top:8px}}
  .bar-fill{{height:4px;border-radius:4px}}
  .bar-label{{font-size:10px;color:var(--muted);margin-top:3px}}
  .table-wrap{{background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden}}
  table{{width:100%;border-collapse:collapse}}
  thead tr{{background:#0d1520}}
  th{{padding:9px 13px;text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);font-weight:400;border-bottom:1px solid var(--border)}}
  td{{padding:9px 13px;border-bottom:1px solid #151f2c;font-size:12px}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:#131d2a}}
  .mono{{font-family:'Space Mono',monospace;font-size:11px}}
  .buy{{color:var(--green);font-weight:600}}.sell{{color:var(--red);font-weight:600}}
  .pos{{color:var(--green)}}.neg{{color:var(--red)}}
  .badge{{font-size:10px;font-family:'Space Mono',monospace;padding:2px 7px;border-radius:3px;font-weight:700}}
  .badge.win{{background:rgba(0,230,118,.15);color:var(--green)}}
  .badge.loss{{background:rgba(255,82,82,.15);color:var(--red)}}
  .badge.open{{background:rgba(0,229,255,.12);color:var(--accent)}}
  .empty{{text-align:center;color:var(--muted);padding:40px;font-size:13px}}
  .status-bar{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px 16px;margin-bottom:24px;font-size:12px;color:var(--muted)}}
  .status-bar span{{color:var(--text)}}
  footer{{text-align:center;padding:20px;color:var(--muted);font-size:11px;border-top:1px solid var(--border);margin-top:28px}}
  @media(max-width:768px){{.grid-4{{grid-template-columns:repeat(2,1fr)}}}}
</style>
</head>
<body>
<header>
  <div class="logo"><span class="live-dot"></span>Polymarket Bot v2</div>
  <div class="meta">
    Ciclo <span>{iteration}</span> &nbsp;·&nbsp;
    Atualizado <span>{now()}</span> &nbsp;·&nbsp;
    Desde <span>{uptime_since}</span>
  </div>
</header>
<div class="container">

  <div class="status-bar">
    🤖 Status: <span>{_state.get('status','…')}</span>
    &nbsp;·&nbsp; Intervalo: <span>{INTERVAL}s</span>
    &nbsp;·&nbsp; Carteiras seguidas: <span>{len(wallets)}</span>
    &nbsp;·&nbsp; Dashboard: atualizado a cada <span>{DASHBOARD_INTERVAL}</span> ciclos
  </div>

  <div class="grid-4">
    <div class="kpi">
      <div class="kpi-label">P&amp;L Total</div>
      <div class="kpi-value" style="color:{pnl_color}">{s['pnl']:+.2f}</div>
      <div class="kpi-sub">USDC (preços reais)</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">ROI</div>
      <div class="kpi-value" style="color:{pnl_color}">{roi_sign}{s['roi']:.1f}%</div>
      <div class="kpi-sub">sobre {s['apostado']:.0f} USDC apostados</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Win Rate</div>
      <div class="kpi-value">{s['win_rate']:.1f}%</div>
      <div class="kpi-sub">{s['wins']}W / {s['losses']}L &middot; {s['closed']} fechados</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Trades</div>
      <div class="kpi-value">{s['total']}</div>
      <div class="kpi-sub">{s['open']} abertos &middot; {s['closed']} fechados</div>
    </div>
  </div>

  <h2>Saldos por carteira</h2>
  <div class="wallets-row">{balances or '<div class="wallet-card"><div class="empty">Sem carteiras ainda</div></div>'}</div>

  <h2>Últimos {min(50, len(trades))} trades</h2>
  <div class="table-wrap">
    {'<table><thead><tr><th>Hora</th><th>Carteira</th><th>Mercado</th><th>Side</th><th>Preço entrada→atual</th><th>Investido</th><th>Payout</th><th>P&L</th><th>Status</th></tr></thead><tbody>' + rows + '</tbody></table>' if trades else '<div class="empty">Aguardando trades…</div>'}
  </div>

</div>
<footer>
  Polymarket Copy-Trade Bot v2 &nbsp;·&nbsp; Railway &nbsp;·&nbsp; Paper Trading &nbsp;·&nbsp;
  {', '.join(w[:8]+'…' for w in wallets) or 'sem carteiras'}
</footer>
</body>
</html>"""

# ══════════════════════════════════════════════
# SERVIDOR HTTP (Railway precisa de uma porta)
# ══════════════════════════════════════════════

_dashboard_html = "<html><body>A inicializar…</body></html>"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/dashboard"):
            body = _dashboard_html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            with _lock:
                data = json.dumps({
                    "iteration": _state["iteration"],
                    "status":    _state["status"],
                    "stats":     compute_stats(_state["trades"], _state["budget"], _state["iteration"]),
                }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # silencia logs HTTP no terminal

def run_server():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log(f"🌐 Servidor HTTP a correr em 0.0.0.0:{PORT}")
    server.serve_forever()

# ══════════════════════════════════════════════
# FASE 1 — BUSCAR CARTEIRAS
# ══════════════════════════════════════════════

def fase1_buscar_carteiras():
    log("FASE 1 — A buscar carteiras ativas…")
    with _lock:
        _state["status"] = "Fase 1 — A buscar carteiras…"

    markets = _get(f"{GAMMA_API}/markets", {
        "active": "true", "closed": "false",
        "limit": MARKETS_TO_SCAN, "order": "volume24hr", "ascending": "false"
    }, timeout=12)

    fallback = [
        {"address": "0x6af75d4e4aaf700450efbac3708cce1665810ff1", "name": "gopfan"},
        {"address": "0xd48165a42bb4eeb5971e5e830c068eef0890af35", "name": ""},
        {"address": "0xe7ef052f94ef4217c7078e9e2b40f84c64e56d8a", "name": ""},
        {"address": "0x1fcabd63b75e0ba18b7af9af8d0f74fc63e1b906", "name": ""},
        {"address": "0x5b5fca8ae94e3988a0b856c74284a1bc70069c01", "name": ""},
    ]

    if not markets:
        log("⚠️  Sem mercados — a usar fallback")
        return fallback

    seen, wallets = set(), []
    for m in markets:
        cid = m.get("conditionId") or ""
        if not cid:
            continue
        trades = _get(f"{DATA_API}/trades", {"market": cid, "limit": 20})
        for t in trades if isinstance(trades, list) else []:
            addr = t.get("proxyWallet") or t.get("maker") or ""
            name = t.get("name") or t.get("pseudonym") or ""
            if addr and addr.startswith("0x") and len(addr) > 20 and addr not in seen:
                seen.add(addr)
                wallets.append({"address": addr, "name": name})
        time.sleep(0.3)

    if not wallets:
        log("⚠️  Sem carteiras — a usar fallback")
        return fallback

    log(f"✅ {len(wallets)} carteiras encontradas")
    return wallets

# ══════════════════════════════════════════════
# FASE 2 — RANQUEAR
# ══════════════════════════════════════════════

@dataclass
class WalletStats:
    address: str
    name:    str   = ""
    total_trades:    int   = 0
    markets_entered: int   = 0
    markets_won:     int   = 0
    markets_lost:    int   = 0
    total_invested:  float = 0.0
    total_returned:  float = 0.0
    pnl:             float = 0.0
    roi_pct:         float = 0.0
    win_rate_pct:    float = 0.0
    score:           float = 0.0

def analyse_wallet(address, name="") -> WalletStats:
    stats  = WalletStats(address=address, name=name)
    trades = []
    offset = 0
    while len(trades) < MAX_TRADES:
        batch = _get(f"{DATA_API}/trades", {"user": address, "limit": 100, "offset": offset})
        if not batch or not isinstance(batch, list):
            break
        trades.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        time.sleep(0.1)

    activity = _get(f"{DATA_API}/activity", {"user": address, "limit": 100, "type": "REDEEM"})
    if not isinstance(activity, list):
        activity = []
    if not trades:
        return stats

    stats.total_trades = len(trades)
    markets = {}
    for t in trades:
        cid  = t.get("conditionId") or t.get("market", "unknown")
        side = (t.get("side") or "").upper()
        size = float(t.get("usdcSize") or t.get("size") or 0)
        if cid not in markets:
            markets[cid] = {"invested": 0.0, "returned": 0.0, "resolved": False}
        if side == "BUY":
            markets[cid]["invested"] += size
            stats.total_invested += size
        elif side == "SELL":
            markets[cid]["returned"] += size
            stats.total_returned += size

    for act in activity:
        cid  = act.get("conditionId") or act.get("market", "")
        usdc = float(act.get("usdcSize") or act.get("size") or 0)
        if cid in markets:
            markets[cid]["returned"] += usdc
            markets[cid]["resolved"]  = True
            stats.total_returned     += usdc

    for cid, m in markets.items():
        net = m["returned"] - m["invested"]
        if m["resolved"]:
            if net > 0:
                stats.markets_won += 1
            elif net < -0.5:
                stats.markets_lost += 1

    stats.markets_entered = len(markets)
    stats.pnl = stats.total_returned - stats.total_invested
    if stats.total_invested > 0:
        stats.roi_pct = (stats.pnl / stats.total_invested) * 100
    resolved = stats.markets_won + stats.markets_lost
    if resolved > 0:
        stats.win_rate_pct = (stats.markets_won / resolved) * 100
    confidence = min(resolved / 10, 1.0)
    stats.score = (
        stats.roi_pct      * 0.40 +
        stats.win_rate_pct * 0.35 +
        min(stats.pnl, 5000) / 50 * 0.25
    ) * confidence
    return stats

def fase2_ranquear(wallets):
    log(f"FASE 2 — A ranquear {len(wallets)} carteiras…")
    with _lock:
        _state["status"] = f"Fase 2 — A ranquear {len(wallets)} carteiras…"

    results = []
    for i, w in enumerate(wallets, 1):
        if i % 10 == 0:
            log(f"  [{i}/{len(wallets)}] a analisar…")
            with _lock:
                _state["status"] = f"Fase 2 — {i}/{len(wallets)} carteiras analisadas"
        stats = analyse_wallet(w["address"], w.get("name", ""))
        results.append(stats)
        time.sleep(0.2)

    ranked = sorted(results, key=lambda w: w.score, reverse=True)
    top    = [w for w in ranked[:TOP_WALLETS] if w.total_trades > 0]

    log(f"✅ Top {len(top)} carteiras selecionadas:")
    for w in top:
        log(f"   • {w.address[:12]}…  score={w.score:.1f}  ROI={w.roi_pct:.1f}%  win={w.win_rate_pct:.1f}%")

    return [w.address for w in top]

# ══════════════════════════════════════════════
# FASE 3 — MONITORIZAR
# ══════════════════════════════════════════════

def simulate_copy(budget, wallet, trade):
    side     = (trade.get("side") or "BUY").upper()
    price    = float(trade.get("price") or 0)
    cid      = trade.get("conditionId") or trade.get("market", "")
    title    = trade.get("title") or cid[:40] or "Mercado desconhecido"
    outcome  = trade.get("outcome", "")
    token_id = trade.get("tokenId") or trade.get("token_id") or ""

    if price <= 0:
        return None
    available = budget.get(wallet, BUDGET)
    size = min(available * COPY_FRACTION, MAX_PER_TRADE)
    if side == "BUY":
        if size < 1.0:
            return None
        budget[wallet] = available - size

    return {
        "wallet":        wallet,
        "condition_id":  cid,
        "token_id":      token_id,
        "title":         title,
        "side":          side,
        "price":         price,
        "current_price": price,
        "usdc_size":     round(size, 2),
        "outcome":       outcome,
        "timestamp":     now(),
        "status":        "OPEN",
        "pnl":           0.0,
    }

def fase3_monitorizar(wallets):
    global _dashboard_html

    log(f"FASE 3 — A monitorizar {len(wallets)} carteiras (intervalo {INTERVAL}s)")
    with _lock:
        _state["wallets"]  = wallets
        _state["budget"]   = {w: BUDGET for w in wallets}
        _state["status"]   = "A monitorizar…"

    budget   = _state["budget"]
    seen_ids = {w: set() for w in wallets}
    iteration = 0

    # Ignorar trades já existentes
    for w in wallets:
        existing = _get(f"{DATA_API}/trades", {"user": w, "limit": 10})
        for t in existing if isinstance(existing, list) else []:
            tid = t.get("id") or t.get("transactionHash") or ""
            if tid:
                seen_ids[w].add(tid)
        time.sleep(0.5)

    log("✅ Inicialização completa — a monitorizar!")

    # Dashboard inicial
    with _lock:
        _dashboard_html = generate_dashboard([], budget, wallets, 0)

    while True:
        iteration += 1
        novos = 0

        for w in wallets:
            recent = _get(f"{DATA_API}/trades", {"user": w, "limit": 10})
            seen   = seen_ids[w]
            for t in recent if isinstance(recent, list) else []:
                tid = t.get("id") or t.get("transactionHash") or ""
                if tid and tid in seen:
                    continue
                if tid:
                    seen.add(tid)
                novos += 1
                with _lock:
                    pt = simulate_copy(budget, w, t)
                    if pt:
                        _state["trades"].append(pt)
                        log(f"🟢 NOVO TRADE: {pt['title'][:40]}  {pt['side']}  {pt['price']:.3f}  {pt['usdc_size']:.2f} USDC")
                        telegram_trade_alert(pt)
            seen_ids[w] = seen
            time.sleep(0.4)

        # Atualizar P&L real e fechar trades resolvidos
        with _lock:
            for i, t in enumerate(_state["trades"]):
                if t.get("status") == "OPEN":
                    _state["trades"][i] = resolve_trade_pnl(t, budget)
            _state["iteration"] = iteration
            _state["status"] = f"A monitorizar… ciclo {iteration}"

        if novos == 0:
            log(f"Ciclo {iteration} — sem novos trades")
        else:
            log(f"Ciclo {iteration} — {novos} novo(s) trade(s)!")

        # Dashboard a cada DASHBOARD_INTERVAL ciclos
        if iteration % DASHBOARD_INTERVAL == 0:
            with _lock:
                _dashboard_html = generate_dashboard(
                    _state["trades"], budget, wallets, iteration
                )
            log(f"🌐 Dashboard atualizado (ciclo {iteration})")

        # Stats + Telegram a cada 10 ciclos
        if iteration % 10 == 0:
            with _lock:
                s = compute_stats(_state["trades"], budget, iteration)
            log(f"📊 Stats: {s['total']} trades | P&L {s['pnl']:+.2f} | ROI {s['roi']:+.1f}% | Win {s['win_rate']:.1f}%")
            telegram_stats(s)

        time.sleep(INTERVAL)

# ══════════════════════════════════════════════
# ARRANQUE
# ══════════════════════════════════════════════

def bot_thread():
    """Corre as 3 fases em loop."""
    try:
        wallets     = fase1_buscar_carteiras()
        top_wallets = fase2_ranquear(wallets)
        if not top_wallets:
            log("⚠️  Nenhuma carteira com dados suficientes.")
            with _lock:
                _state["status"] = "⚠️ Sem carteiras — aumenta MARKETS_TO_SCAN"
            return
        fase3_monitorizar(top_wallets)
    except Exception as e:
        log(f"❌ Erro fatal no bot: {e}")
        with _lock:
            _state["status"] = f"❌ Erro: {e}"

def main():
    log("=" * 55)
    log("  POLYMARKET COPY-TRADE BOT v2 — Railway")
    log(f"  Budget: {BUDGET} USDC | Interval: {INTERVAL}s | Top: {TOP_WALLETS}")
    log(f"  HTTP porta: {PORT}")
    tg = "✅" if TELEGRAM_TOKEN else "❌ não configurado"
    log(f"  Telegram: {tg}")
    log("=" * 55)

    # Servidor HTTP em thread separada (daemon → termina com o processo)
    t_server = threading.Thread(target=run_server, daemon=True)
    t_server.start()

    # Bot em thread separada
    t_bot = threading.Thread(target=bot_thread, daemon=True)
    t_bot.start()

    # Thread principal fica viva para o Railway não matar o processo
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log("A parar…")

if __name__ == "__main__":
    main()
