"""
run.py
------
Script principal — faz tudo automaticamente:
1. Busca carteiras ativas dos mercados com mais volume
2. Ranqueia as carteiras por ROI, win rate e P&L
3. Monitoriza as top carteiras em paper trading
4. P&L REAL baseado em preços de mercado ao vivo
5. Alertas via Telegram
6. Dashboard HTML gerado automaticamente

Basta correr:
    python3 run.py

Para ativar Telegram, preenche TELEGRAM_TOKEN e TELEGRAM_CHAT_ID abaixo.
"""

import sys
import os
import json
import requests
import time
import random
from datetime import datetime
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIGURAÇÃO — altera aqui se quiseres
# ─────────────────────────────────────────────

BUDGET          = 1000.0   # USDC simulados por carteira
INTERVAL        = 30       # segundos entre cada verificação
TOP_WALLETS     = 5        # nº de carteiras top a seguir
MARKETS_TO_SCAN = 15       # nº de mercados a analisar para encontrar traders
MAX_TRADES      = 500      # máximo de trades a analisar por carteira

# Telegram (opcional) — deixa "" para desativar
TELEGRAM_TOKEN   = ""      # ex: "123456789:AAxxxxxx"
TELEGRAM_CHAT_ID = ""      # ex: "987654321"

# Dashboard HTML — gerado a cada N ciclos
DASHBOARD_FILE     = "dashboard.html"
DASHBOARD_INTERVAL = 2     # ciclos entre cada atualização do HTML

# ─────────────────────────────────────────────

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
HEADERS   = {"User-Agent": "polymarket-bot/1.0"}

COPY_FRACTION = 0.05
MAX_PER_TRADE = 50.0

# ══════════════════════════════════════════════
# UTILIDADES
# ══════════════════════════════════════════════

def now():
    return datetime.now().strftime("%H:%M:%S")

def now_iso():
    return datetime.now().isoformat()

def banner(text):
    w = 60
    print("\n" + "═" * w)
    print(f"  {text}")
    print("═" * w)

def ok(text):
    print(f"  ✅ {text}")

def warn(text):
    print(f"  ⚠️  {text}")

def info(text):
    print(f"  ℹ️  {text}")

def save_json(data, path):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

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
    """Envia mensagem Telegram. Silencioso se não configurado."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=5)
    except Exception:
        pass  # nunca bloqueia o bot por falha de Telegram


def telegram_trade_alert(pt: dict):
    icon = "🟢" if pt["side"] == "BUY" else "🔴"
    msg = (
        f"{icon} <b>NOVO TRADE DETETADO</b>\n"
        f"⏰ {pt['timestamp']}\n"
        f"👛 <code>{pt['wallet'][:10]}...{pt['wallet'][-6:]}</code>\n"
        f"📌 {pt['title'][:60]}\n"
        f"💵 Preço: <b>{pt['price']:.3f}</b>  |  {pt['outcome']}\n"
        f"📋 [PAPER] Simulado: <b>{pt['usdc_size']:.2f} USDC</b>"
    )
    telegram_send(msg)


def telegram_stats_alert(stats: dict):
    pnl_icon = "📈" if stats["pnl"] >= 0 else "📉"
    msg = (
        f"{pnl_icon} <b>ESTATÍSTICAS PAPER TRADING</b>\n"
        f"Trades: {stats['total']}  |  ✅ {stats['wins']}  ❌ {stats['losses']}\n"
        f"Win Rate: <b>{stats['win_rate']:.1f}%</b>\n"
        f"P&amp;L real: <b>{stats['pnl']:+.2f} USDC</b>\n"
        f"ROI: <b>{stats['roi']:+.1f}%</b>"
    )
    telegram_send(msg)

# ══════════════════════════════════════════════
# P&L REAL — preços ao vivo via CLOB API
# ══════════════════════════════════════════════

_price_cache: dict = {}  # token_id → (price, timestamp)
CACHE_TTL = 30  # segundos


def get_live_price(token_id: str) -> float | None:
    """
    Busca o melhor preço de compra (mid-point) para um token no CLOB.
    Retorna None se não conseguir.
    Cache de 30s para não sobrecarregar a API.
    """
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
            except (ValueError, TypeError):
                pass

    # fallback: orderbook
    book = _get(f"{CLOB_API}/book", {"token_id": token_id})
    if isinstance(book, dict):
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        prices = []
        if bids:
            try:
                prices.append(float(bids[0]["price"]))
            except Exception:
                pass
        if asks:
            try:
                prices.append(float(asks[0]["price"]))
            except Exception:
                pass
        if prices:
            p = sum(prices) / len(prices)
            _price_cache[token_id] = (p, time.time())
            return p

    return None


def get_market_resolution(condition_id: str) -> str | None:
    """
    Verifica se um mercado já foi resolvido via Gamma API.
    Devolve 'YES', 'NO', ou None se ainda não resolvido / sem dados.
    """
    if not condition_id:
        return None
    data = _get(f"{GAMMA_API}/markets", {"conditionId": condition_id})
    markets = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    for m in markets:
        resolution = m.get("resolution") or m.get("resolvedOutcome") or ""
        if resolution:
            return str(resolution).upper()
        # campo closed=True + winner
        if m.get("closed") or m.get("resolved"):
            winner = m.get("winner") or m.get("resolvedOutcome") or ""
            if winner:
                return str(winner).upper()
    return None


def resolve_trade_pnl(trade: dict, budget: dict | None = None) -> dict:
    """
    Calcula o P&L real de um trade aberto com base no preço atual.
    Atualiza os campos pnl, current_price, status no dicionário.

    Se budget for fornecido, atualiza o saldo da wallet quando o trade fecha:
      - WIN  → devolve o capital apostado + lucro (size + pnl)
      - LOSS → o dinheiro já saiu quando o trade foi aberto, não faz nada

    Fallbacks para mercados sem preço CLOB (ex: mercados de curta duração):
      1. Verifica resolução via Gamma API
      2. Se o trade tem mais de 30 minutos sem preço, tenta resolver por P&L estimado
    """
    prev_status = trade.get("status", "OPEN")

    if prev_status == "CLOSED":
        return trade

    # Se já estava fechado numa chamada anterior, não volta a creditar
    already_closed = "CLOSED" in prev_status

    token_id     = trade.get("token_id", "")
    condition_id = trade.get("condition_id", "")
    side         = trade.get("side", "BUY")
    entry        = trade.get("price", 0)
    size         = trade.get("usdc_size", 0)
    outcome      = (trade.get("outcome") or "").upper()  # ex: "YES" / "UP" / "NO" / "DOWN"

    if entry <= 0:
        return trade

    current_price = get_live_price(token_id)

    # ── Fallback 1: resolução via Gamma API ──────────────────────────────
    if current_price is None and condition_id:
        resolution = get_market_resolution(condition_id)
        if resolution:
            # Determinar se ganhámos: o resultado resolvido bate com o outcome apostado?
            # Normaliza: YES/UP/1 → ganhou quem comprou YES; NO/DOWN/0 → ganhou quem comprou NO
            won_outcomes = {"YES", "UP", "1", "TRUE"}
            lost_outcomes = {"NO", "DOWN", "0", "FALSE"}
            resolved_win = (
                (resolution in won_outcomes and outcome in won_outcomes) or
                (resolution in lost_outcomes and outcome in lost_outcomes)
            )
            if resolved_win:
                # tokens comprados valem ~1 agora
                tokens = size / entry if entry > 0 else 0
                trade["pnl"]          = round(tokens - size, 4)
                trade["current_price"] = 1.0
                new_status = "CLOSED_WIN" if side == "BUY" else "CLOSED_LOSS"
            else:
                # tokens valem ~0
                trade["pnl"]          = round(-size, 4)
                trade["current_price"] = 0.0
                new_status = "CLOSED_LOSS" if side == "BUY" else "CLOSED_WIN"
            trade["status"]     = new_status
            trade["updated_at"] = now()
            trade["resolved_by"] = "gamma_api"
            _apply_budget(budget, trade, already_closed, new_status, size, wallet=trade.get("wallet",""))
            return trade

    # ── Fallback 2: sem preço após 30 min → fecha como LOSS (capital perdido) ──
    if current_price is None:
        ts_str = trade.get("timestamp", "")
        try:
            trade_time = datetime.strptime(ts_str, "%H:%M:%S").replace(
                year=datetime.now().year,
                month=datetime.now().month,
                day=datetime.now().day
            )
            elapsed = (datetime.now() - trade_time).total_seconds()
        except Exception:
            elapsed = 0

        if elapsed > 1800:  # 30 minutos sem preço → assume expirado
            trade["pnl"]          = round(-size, 4)
            trade["current_price"] = 0.0
            new_status = "CLOSED_LOSS"
            trade["status"]     = new_status
            trade["updated_at"] = now()
            trade["resolved_by"] = "timeout"
            _apply_budget(budget, trade, already_closed, new_status, size, wallet=trade.get("wallet",""))
        # sem preço e < 30 min → mantém OPEN
        return trade

    # ── Resolução normal via preço CLOB ──────────────────────────────────
    if side == "BUY":
        tokens = size / entry
        current_value = tokens * current_price
        trade["pnl"] = round(current_value - size, 4)
    else:
        tokens = size / (1 - entry) if entry < 1 else size
        current_value = tokens * (1 - current_price)
        trade["pnl"] = round(current_value - size, 4)

    # mercado resolvido: preço próximo de 0 ou 1
    if current_price >= 0.97:
        new_status = "CLOSED_WIN" if side == "BUY" else "CLOSED_LOSS"
    elif current_price <= 0.03:
        new_status = "CLOSED_LOSS" if side == "BUY" else "CLOSED_WIN"
    else:
        new_status = "OPEN"

    trade["status"]        = new_status
    trade["current_price"] = round(current_price, 4)
    trade["updated_at"]    = now()
    trade.pop("resolved_by", None)  # limpa se havia fallback anterior

    _apply_budget(budget, trade, already_closed, new_status, size, wallet=trade.get("wallet",""))
    return trade


def _apply_budget(budget, trade, already_closed, new_status, size, wallet):
    """Credita saldo da wallet quando um trade fecha pela primeira vez."""
    if budget is None or already_closed or "CLOSED" not in new_status:
        return
    if wallet not in budget:
        return
    if "WIN" in new_status:
        payout = round(size + trade["pnl"], 4)
        budget[wallet] = round(budget[wallet] + payout, 4)
        print(f"  💰 WIN  {wallet[:8]}... +{payout:.2f} USDC  (saldo: {budget[wallet]:.2f})")
    else:
        print(f"  💸 LOSS {wallet[:8]}... -{size:.2f} USDC já descontado  (saldo: {budget[wallet]:.2f})")

# ══════════════════════════════════════════════
# FASE 1 — BUSCAR CARTEIRAS ATIVAS
# ══════════════════════════════════════════════

def fase1_buscar_carteiras():
    banner("FASE 1 — A buscar carteiras ativas")
    print(f"  A analisar os {MARKETS_TO_SCAN} mercados com mais volume...")

    markets = _get(f"{GAMMA_API}/markets", {
        "active": "true",
        "closed": "false",
        "limit": MARKETS_TO_SCAN,
        "order": "volume24hr",
        "ascending": "false"
    }, timeout=12)

    if not markets:
        warn("Não foi possível obter mercados. A usar carteiras de fallback.")
        return [
            {"address": "0x6af75d4e4aaf700450efbac3708cce1665810ff1", "name": "gopfan"},
            {"address": "0xd48165a42bb4eeb5971e5e830c068eef0890af35", "name": ""},
            {"address": "0xe7ef052f94ef4217c7078e9e2b40f84c64e56d8a", "name": ""},
            {"address": "0x1fcabd63b75e0ba18b7af9af8d0f74fc63e1b906", "name": ""},
            {"address": "0x5b5fca8ae94e3988a0b856c74284a1bc70069c01", "name": ""},
        ]

    seen    = set()
    wallets = []

    for i, m in enumerate(markets):
        cid   = m.get("conditionId") or ""
        title = (m.get("question") or m.get("title") or "")[:45]
        vol   = float(m.get("volume24hr") or 0)

        print(f"  {i+1:>2}/{len(markets)}  {title:<45}  ${vol:>10,.0f}", end="\r")

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

    print()

    if not wallets:
        warn("Nenhuma carteira encontrada nos mercados. A usar fallback.")
        return [{"address": "0x6af75d4e4aaf700450efbac3708cce1665810ff1", "name": "gopfan"}]

    save_json(wallets, "active_wallets.json")
    ok(f"{len(wallets)} carteiras encontradas → active_wallets.json")
    return wallets

# ══════════════════════════════════════════════
# FASE 2 — RANQUEAR CARTEIRAS
# ══════════════════════════════════════════════

@dataclass
class WalletStats:
    address: str
    name:    str    = ""
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


def analyse_wallet(address, name=""):
    stats = WalletStats(address=address, name=name)
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
            stats.total_returned += usdc

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
    banner("FASE 2 — A ranquear carteiras")
    print(f"  A analisar {len(wallets)} carteiras...\n")

    results = []
    for i, w in enumerate(wallets, 1):
        addr = w["address"]
        name = w.get("name", "")
        print(f"  [{i:>3}/{len(wallets)}] {addr[:12]}...  {name[:20]}", end="\r")
        stats = analyse_wallet(addr, name)
        results.append(stats)
        time.sleep(0.2)

    print()
    ranked = sorted(results, key=lambda w: w.score, reverse=True)

    print(f"\n  {'#':<4} {'Endereço':<16} {'Score':>6} {'ROI%':>7} {'Win%':>6} {'P&L':>9} {'Trades':>7}")
    print(f"  {'─'*4} {'─'*16} {'─'*6} {'─'*7} {'─'*6} {'─'*9} {'─'*7}")
    for i, w in enumerate(ranked[:15], 1):
        short = f"{w.address[:6]}...{w.address[-4:]}"
        print(f"  {i:<4} {short:<16} {w.score:>6.1f} {w.roi_pct:>6.1f}% {w.win_rate_pct:>5.1f}% ${w.pnl:>8.2f} {w.total_trades:>7}")

    top = [w for w in ranked[:TOP_WALLETS] if w.total_trades > 0]
    print(f"\n  🎯 Top {len(top)} carteiras para seguir:")
    for w in top:
        print(f"     • {w.address}  score {w.score:.1f}  ROI {w.roi_pct:.1f}%  win {w.win_rate_pct:.1f}%")

    output = [{"address": w.address, "name": w.name, "score": round(w.score,2),
               "roi_pct": round(w.roi_pct,2), "win_rate_pct": round(w.win_rate_pct,2),
               "pnl": round(w.pnl,2), "total_trades": w.total_trades,
               "markets_entered": w.markets_entered} for w in ranked]
    save_json(output, "ranked_wallets.json")
    ok(f"Ranking guardado → ranked_wallets.json")

    return [w.address for w in top]

# ══════════════════════════════════════════════
# FASE 3 — MONITORIZAR + PAPER TRADING REAL
# ══════════════════════════════════════════════

def simulate_copy(budget, wallet, trade, budget_total):
    side     = (trade.get("side") or "BUY").upper()
    price    = float(trade.get("price") or 0)
    cid      = trade.get("conditionId") or trade.get("market", "")
    title    = trade.get("title") or cid[:40] or "Mercado desconhecido"
    outcome  = trade.get("outcome", "")
    token_id = trade.get("tokenId") or trade.get("token_id") or ""

    if price <= 0:
        return None

    available = budget.get(wallet, budget_total)
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
        "current_price": price,   # atualizado em cada ciclo
        "usdc_size":     round(size, 2),
        "outcome":       outcome,
        "timestamp":     now(),
        "status":        "OPEN",
        "pnl":           0.0,
    }


def print_alert(pt):
    icon = "🟢 COMPRA" if pt["side"] == "BUY" else "🔴 VENDA"
    print(f"\n  ┌─ NOVO MOVIMENTO ──────────────────────────────────────")
    print(f"  │  ⏰ {pt['timestamp']}  👛 {pt['wallet'][:8]}...{pt['wallet'][-6:]}")
    print(f"  │  {icon}  {pt['title'][:50]}")
    print(f"  │  💰 Preço entrada: {pt['price']:.3f}  Resultado: {pt['outcome']}")
    print(f"  │  📋 [PAPER] Simulado: {pt['usdc_size']:.2f} USDC")
    print(f"  └───────────────────────────────────────────────────────\n")
    telegram_trade_alert(pt)


def compute_stats(trades: list, budget: dict) -> dict:
    """Calcula estatísticas reais com base nos preços atuais dos trades."""
    wins = losses = 0
    pnl = apostado = 0.0
    open_count = closed_count = 0

    for t in trades:
        size  = t["usdc_size"]
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
            pnl += t.get("pnl", 0.0)  # P&L não realizado

    total    = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0
    roi      = (pnl / apostado * 100) if apostado > 0 else 0

    return {
        "total":        len(trades),
        "wins":         wins,
        "losses":       losses,
        "open":         open_count,
        "closed":       closed_count,
        "win_rate":     round(win_rate, 2),
        "pnl":          round(pnl, 4),
        "apostado":     round(apostado, 2),
        "roi":          round(roi, 2),
        "updated_at":   now_iso(),
    }


def print_stats(trades: list, budget: dict):
    if not trades:
        info("Ainda sem trades simulados.")
        return

    s = compute_stats(trades, budget)
    bar_filled = int(s["win_rate"] / 5)
    bar = "█" * bar_filled + "░" * (20 - bar_filled)
    pnl_icon = "📈" if s["pnl"] >= 0 else "📉"

    print(f"\n  ══════════════════════════════════════════════════════")
    print(f"  {pnl_icon}  ESTATÍSTICAS PAPER TRADING  (preços reais)")
    print(f"  ══════════════════════════════════════════════════════")
    print(f"  Trades simulados  : {s['total']}  ({s['open']} abertos, {s['closed']} fechados)")
    print(f"  Wins  ✅          : {s['wins']}")
    print(f"  Losses ❌         : {s['losses']}")
    print(f"  Win Rate          : {s['win_rate']:.1f}%  [{bar}]")
    print(f"  Capital apostado  : {s['apostado']:.2f} USDC")
    print(f"  P&L (real/estimado): {s['pnl']:+.4f} USDC")
    print(f"  ROI               : {s['roi']:+.1f}%")
    print(f"  ──────────────────────────────────────────────────────")
    for w, b in budget.items():
        print(f"  Saldo {w[:8]}... : {b:.2f} USDC restantes")
    print(f"  ══════════════════════════════════════════════════════\n")
    return s


def save_session(trades, budget):
    save_json({
        "saved_at": now_iso(),
        "budget_remaining": {k: round(v,2) for k,v in budget.items()},
        "trades": trades
    }, "paper_session.json")

# ══════════════════════════════════════════════
# DASHBOARD HTML
# ══════════════════════════════════════════════

def generate_dashboard(trades: list, budget: dict, wallets: list, iteration: int):
    """Gera um ficheiro HTML com o estado atual do paper trading."""
    s = compute_stats(trades, budget)
    pnl_color = "#00e676" if s["pnl"] >= 0 else "#ff5252"
    roi_sign  = "+" if s["roi"] >= 0 else ""

    # Tabela de trades (últimos 30)
    rows = ""
    for t in reversed(trades[-30:]):
        st = t.get("status", "OPEN")
        if "WIN" in st:
            badge = '<span class="badge win">WIN</span>'
        elif "LOSS" in st:
            badge = '<span class="badge loss">LOSS</span>'
        else:
            badge = '<span class="badge open">OPEN</span>'

        pnl_val = t.get("pnl", 0.0)
        pnl_cls = "pos" if pnl_val >= 0 else "neg"
        cur_p   = t.get("current_price", t["price"])
        side_cls = "buy" if t["side"] == "BUY" else "sell"

        rows += f"""
        <tr>
          <td>{t['timestamp']}</td>
          <td class="mono">{t['wallet'][:8]}…{t['wallet'][-4:]}</td>
          <td>{t['title'][:40]}</td>
          <td class="{side_cls}">{t['side']}</td>
          <td class="mono">{t['price']:.3f} → {cur_p:.3f}</td>
          <td class="mono">{t['usdc_size']:.2f}</td>
          <td class="mono {pnl_cls}">{pnl_val:+.4f}</td>
          <td>{badge}</td>
        </tr>"""

    # Saldos
    balances = ""
    for w, b in budget.items():
        pct = (b / BUDGET) * 100
        bar_w = max(0, min(100, pct))
        color = "#00e676" if pct > 70 else "#ffab40" if pct > 40 else "#ff5252"
        balances += f"""
        <div class="wallet-card">
          <div class="wallet-addr">{w[:10]}…{w[-6:]}</div>
          <div class="wallet-balance">{b:.2f} <span class="unit">USDC</span></div>
          <div class="bar-bg"><div class="bar-fill" style="width:{bar_w}%;background:{color}"></div></div>
          <div class="bar-label">{pct:.1f}% restante</div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Polymarket Paper Trading</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:       #0a0e14;
    --surface:  #111720;
    --border:   #1e2a38;
    --text:     #c8d6e5;
    --muted:    #4a6080;
    --accent:   #00e5ff;
    --green:    #00e676;
    --red:      #ff5252;
    --yellow:   #ffab40;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'DM Sans', sans-serif;
    font-size: 14px;
    line-height: 1.6;
  }}
  header {{
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 18px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }}
  .logo {{
    font-family: 'Space Mono', monospace;
    font-size: 15px;
    color: var(--accent);
    letter-spacing: 2px;
    text-transform: uppercase;
  }}
  .meta {{ color: var(--muted); font-size: 12px; }}
  .meta span {{ color: var(--text); }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 28px 24px; }}
  .grid-4 {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    margin-bottom: 28px;
  }}
  .kpi {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
    position: relative;
    overflow: hidden;
  }}
  .kpi::before {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: var(--accent);
  }}
  .kpi-label {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }}
  .kpi-value {{ font-family: 'Space Mono', monospace; font-size: 26px; font-weight: 700; }}
  .kpi-sub {{ font-size: 11px; color: var(--muted); margin-top: 4px; }}
  .green {{ color: var(--green); }}
  .red   {{ color: var(--red); }}

  h2 {{
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 2px;
    color: var(--muted);
    margin-bottom: 14px;
  }}
  .wallets-row {{
    display: flex;
    gap: 12px;
    margin-bottom: 28px;
    flex-wrap: wrap;
  }}
  .wallet-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    min-width: 180px;
    flex: 1;
  }}
  .wallet-addr {{ font-family: 'Space Mono', monospace; font-size: 11px; color: var(--muted); margin-bottom: 6px; }}
  .wallet-balance {{ font-family: 'Space Mono', monospace; font-size: 20px; font-weight: 700; }}
  .unit {{ font-size: 11px; color: var(--muted); }}
  .bar-bg {{ background: var(--border); border-radius: 4px; height: 4px; margin-top: 10px; }}
  .bar-fill {{ height: 4px; border-radius: 4px; transition: width 0.5s; }}
  .bar-label {{ font-size: 10px; color: var(--muted); margin-top: 4px; }}

  .table-wrap {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }}
  table {{ width: 100%; border-collapse: collapse; }}
  thead tr {{ background: #0d1520; }}
  th {{
    padding: 10px 14px;
    text-align: left;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--muted);
    font-weight: 400;
    border-bottom: 1px solid var(--border);
  }}
  td {{
    padding: 10px 14px;
    border-bottom: 1px solid #151f2c;
    font-size: 13px;
  }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #131d2a; }}
  .mono {{ font-family: 'Space Mono', monospace; font-size: 12px; }}
  .buy  {{ color: var(--green); font-weight: 600; }}
  .sell {{ color: var(--red);   font-weight: 600; }}
  .pos  {{ color: var(--green); }}
  .neg  {{ color: var(--red); }}
  .badge {{
    font-size: 10px;
    font-family: 'Space Mono', monospace;
    padding: 2px 8px;
    border-radius: 3px;
    font-weight: 700;
  }}
  .badge.win  {{ background: rgba(0,230,118,0.15); color: var(--green); }}
  .badge.loss {{ background: rgba(255,82,82,0.15);  color: var(--red); }}
  .badge.open {{ background: rgba(0,229,255,0.12); color: var(--accent); }}
  .empty {{ text-align: center; color: var(--muted); padding: 40px; font-size: 13px; }}
  footer {{
    text-align: center;
    padding: 24px;
    color: var(--muted);
    font-size: 11px;
    border-top: 1px solid var(--border);
    margin-top: 32px;
  }}
  @media (max-width: 768px) {{
    .grid-4 {{ grid-template-columns: repeat(2, 1fr); }}
  }}
</style>
</head>
<body>
<header>
  <div class="logo">⬡ Polymarket Bot</div>
  <div class="meta">
    Ciclo <span>{iteration}</span> &nbsp;·&nbsp;
    Atualizado <span>{now()}</span> &nbsp;·&nbsp;
    Auto-refresh 30s
  </div>
</header>

<div class="container">

  <!-- KPIs -->
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
      <div class="kpi-sub">{s['wins']}W / {s['losses']}L  ({s['closed']} fechados)</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Trades</div>
      <div class="kpi-value">{s['total']}</div>
      <div class="kpi-sub">{s['open']} abertos &middot; {s['closed']} fechados</div>
    </div>
  </div>

  <!-- Saldos -->
  <h2>Saldos por carteira</h2>
  <div class="wallets-row">{balances if balances else '<div class="wallet-card"><div class="empty">Sem carteiras</div></div>'}</div>

  <!-- Tabela de trades -->
  <h2>Últimos {min(30, len(trades))} trades</h2>
  <div class="table-wrap">
    {'<table><thead><tr><th>Hora</th><th>Carteira</th><th>Mercado</th><th>Side</th><th>Preço (entrada→atual)</th><th>USDC</th><th>P&L</th><th>Status</th></tr></thead><tbody>' + rows + '</tbody></table>' if trades else '<div class="empty">Aguardando trades…</div>'}
  </div>

</div>
<footer>
  Polymarket Copy-Trade Bot &nbsp;·&nbsp; Paper Trading &nbsp;·&nbsp;
  Carteiras seguidas: {', '.join(w[:8]+'…' for w in wallets)}
</footer>
</body>
</html>"""

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)


# ══════════════════════════════════════════════
# LOOP PRINCIPAL — FASE 3
# ══════════════════════════════════════════════

def fase3_monitorizar(wallets):
    banner("FASE 3 — A monitorizar em paper trading (P&L real)")
    print(f"  Carteiras a seguir : {len(wallets)}")
    print(f"  Orçamento simulado : {BUDGET:.0f} USDC por carteira")
    print(f"  Intervalo          : {INTERVAL}s")
    print(f"  Dashboard          : {DASHBOARD_FILE}  (atualizado a cada {DASHBOARD_INTERVAL} ciclos)")
    tg_status = "✅ configurado" if TELEGRAM_TOKEN else "❌ não configurado (preenche TELEGRAM_TOKEN)"
    print(f"  Telegram           : {tg_status}")
    print(f"  Para parar         : Ctrl+C\n")

    for w in wallets:
        print(f"    • {w}")

    budget   = {w: BUDGET for w in wallets}
    seen_ids = {w: set() for w in wallets}
    trades: list = []
    iteration = 0

    print(f"\n  [{now()}] A inicializar...")
    for w in wallets:
        existing = _get(f"{DATA_API}/trades", {"user": w, "limit": 10})
        for t in existing if isinstance(existing, list) else []:
            tid = t.get("id") or t.get("transactionHash") or ""
            if tid:
                seen_ids[w].add(tid)
        print(f"    ✓ {w[:12]}... — {len(seen_ids[w])} trades existentes ignorados")
        time.sleep(0.5)

    save_session(trades, budget)
    generate_dashboard(trades, budget, wallets, 0)
    ok(f"paper_session.json criado")
    ok(f"{DASHBOARD_FILE} criado — abre no browser para o dashboard")
    print(f"\n  [{now()}] ✅ A monitorizar! Stats a cada 10 ciclos.\n")

    try:
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
                    pt = simulate_copy(budget, w, t, BUDGET)
                    if pt:
                        trades.append(pt)
                        print_alert(pt)

                seen_ids[w] = seen
                time.sleep(0.4)

            # Atualizar P&L real em todos os trades abertos
            for i, t in enumerate(trades):
                if t.get("status") == "OPEN":
                    trades[i] = resolve_trade_pnl(t, budget)

            if novos == 0:
                print(f"  [{now()}] Ciclo {iteration} — sem novos trades nas {len(wallets)} carteiras")
            else:
                print(f"  [{now()}] Ciclo {iteration} — {novos} novo(s) trade(s) detetado(s)!")

            save_session(trades, budget)

            if iteration % DASHBOARD_INTERVAL == 0:
                generate_dashboard(trades, budget, wallets, iteration)
                print(f"  [{now()}] 🌐 Dashboard atualizado → {DASHBOARD_FILE}")

            if iteration % 10 == 0:
                s = print_stats(trades, budget)
                if s:
                    telegram_stats_alert(s)

            time.sleep(INTERVAL)

    except KeyboardInterrupt:
        print("\n\n  A parar...")
        # atualizar P&L final
        for i, t in enumerate(trades):
            trades[i] = resolve_trade_pnl(t, budget)
        save_session(trades, budget)
        generate_dashboard(trades, budget, wallets, iteration)
        print_stats(trades, budget)
        ok(f"Sessão guardada em paper_session.json")
        ok(f"Dashboard final guardado em {DASHBOARD_FILE}")
        print("  Até logo!\n")

# ══════════════════════════════════════════════
# ARRANQUE
# ══════════════════════════════════════════════

def main():
    print("\n" + "█" * 60)
    print("  POLYMARKET COPY-TRADE BOT  —  PAPER TRADING")
    print("  P&L real · Alertas Telegram · Dashboard HTML")
    print("█" * 60)
    print(f"\n  Configuração atual:")
    print(f"    Orçamento simulado  : {BUDGET:.0f} USDC")
    print(f"    Carteiras a seguir  : top {TOP_WALLETS}")
    print(f"    Intervalo           : {INTERVAL}s")
    print(f"    Mercados a analisar : {MARKETS_TO_SCAN}")
    tg = "✅ ativo" if TELEGRAM_TOKEN else "❌ não configurado"
    print(f"    Telegram            : {tg}")
    print(f"    Dashboard           : {DASHBOARD_FILE}")
    print(f"\n  Para alterar configurações, edita a secção CONFIGURAÇÃO")
    print(f"  no topo deste ficheiro.\n")
    time.sleep(2)

    wallets      = fase1_buscar_carteiras()
    top_wallets  = fase2_ranquear(wallets)

    if not top_wallets:
        warn("Nenhuma carteira com dados suficientes para seguir.")
        warn("Tenta aumentar MARKETS_TO_SCAN na configuração.")
        return

    fase3_monitorizar(top_wallets)


if __name__ == "__main__":
    main()