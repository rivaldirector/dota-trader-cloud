import os
import json
import math
import urllib.request
import urllib.error
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(
    title="Dota Trader Orchestrator API",
    version="0.1.1",
)

# -------------------
# ENV (CRITICAL)
# -------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("WARNING: Missing Supabase env variables")


# -------------------
# SUPABASE CLIENT
# -------------------
def sb(method, path, payload=None):
    try:
        data = None if payload is None else json.dumps(payload).encode()

        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/{path}",
            data=data,
            method=method,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
        )

        raw = urllib.request.urlopen(req).read().decode()
        return json.loads(raw) if raw else []

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def sb_safe(path, default=None):
    """Supabase GET без исключений — возвращает default при ошибке."""
    try:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/{path}",
            method="GET",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
            },
        )
        raw = urllib.request.urlopen(req, timeout=15).read().decode()
        return json.loads(raw) if raw else (default if default is not None else [])
    except Exception:
        return default if default is not None else []


# -------------------
# HEALTH
# -------------------
@app.get("/health")
def health():
    return {"ok": True}


# -------------------
# TASKS
# -------------------
class TaskRequest(BaseModel):
    task: str
    priority: int = 1000
    assigned_to: str = "GPT"


@app.post("/tasks")
def create_task(body: TaskRequest):
    rows = sb("POST", "research_queue", [{
        "priority": body.priority,
        "status": "todo",
        "assigned_to": body.assigned_to,
        "task": body.task,
    }])
    return {"created": True, "task": rows[0] if rows else None}


@app.get("/tasks/latest")
def latest_tasks():
    return sb(
        "GET",
        "research_queue?select=id,created_at,priority,status,assigned_to,task&order=id.desc&limit=10",
    )


# -------------------
# DB DEBUG
# -------------------
@app.get("/db/summary")
def db_summary():
    return {
        "matches": sb("GET", "matches?select=*&limit=3"),
        "odds": sb("GET", "current_odds?select=*&limit=3"),
        "predictions": sb("GET", "predictions?select=*&limit=3"),
        "queue": sb("GET", "research_queue?select=*&limit=3"),
    }


# -------------------
# ELO EDGE (FIXED SAFE VERSION)
# -------------------
@app.get("/analysis/elo-edge")
def elo_edge():
    predictions = sb("GET", "predictions?select=*&limit=1000")
    odds = sb("GET", "current_odds?select=*&limit=1000")

    if not predictions or not odds:
        return {
            "analysis": "elo_edge",
            "error": "no_data",
            "predictions": len(predictions),
            "odds": len(odds),
        }

    odds_map = {}
    for o in odds:
        mid = o.get("match_id")
        if mid:
            odds_map.setdefault(str(mid), []).append(o)

    bins = {
        "0-2%": [],
        "2-4%": [],
        "4-6%": [],
        "6-8%": [],
        "8-10%": [],
        "10%+": [],
    }

    def norm(x):
        return str(x or "").lower().strip()

    def edge_bin(e):
        e = e * 100
        if e < 2: return "0-2%"
        if e < 4: return "2-4%"
        if e < 6: return "4-6%"
        if e < 8: return "6-8%"
        if e < 10: return "8-10%"
        return "10%+"

    for p in predictions:
        mid = p.get("match_id")
        if not mid:
            continue

        model_prob = p.get("predicted_probability")
        team = p.get("predicted_team")
        is_win = p.get("is_win")

        if model_prob is None or team is None or is_win is None:
            continue

        try:
            model_prob = float(model_prob)
        except:
            continue

        for o in odds_map.get(str(mid), []):
            odd = None

            if norm(team) == norm(o.get("team_1_name")):
                odd = o.get("team_1_odds")
            elif norm(team) == norm(o.get("team_2_name")):
                odd = o.get("team_2_odds")
            else:
                continue

            try:
                odd = float(odd)
            except:
                continue

            if odd <= 1:
                continue

            implied = 1 / odd
            edge = model_prob - implied

            b = edge_bin(edge)

            bins[b].append({
                "match_id": mid,
                "team": team,
                "odd": odd,
                "model_prob": model_prob,
                "implied": implied,
                "edge": edge,
                "win": bool(is_win),
                "profit": (odd - 1) if is_win else -1
            })

    result = {}
    for k, v in bins.items():
        if not v:
            result[k] = {"bets": 0, "roi": 0}
            continue

        bets = len(v)
        wins = sum(1 for x in v if x["win"])
        profit = sum(x["profit"] for x in v)

        result[k] = {
            "bets": bets,
            "winrate": round(wins / bets * 100, 2),
            "roi": round(profit / bets * 100, 2),
        }

    return {
        "analysis": "elo_edge_fixed",
        "predictions": len(predictions),
        "odds": len(odds),
        "bins": result
    }


# -------------------
# DEBUG LINKS
# -------------------
@app.get("/debug/data-links")
def debug_links():
    return {
        "predictions": sb("GET", "predictions?select=*&limit=5"),
        "odds": sb("GET", "current_odds?select=*&limit=5"),
        "matches": sb("GET", "matches?select=*&limit=5"),
    }


# -------------------
# LIVE DASHBOARD
# -------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── Bankroll ────────────────────────────────────────────────────────────
    START_BANK  = 1000.0
    bank_rows = sb_safe("bankroll_state?strategy=eq.AUTO_ELO_FLAT&select=balance,total_bets,updated_at&limit=1")
    bank = bank_rows[0] if bank_rows else {}
    curr_bank   = float(bank.get("balance") or START_BANK)
    total_pnl   = round(curr_bank - START_BANK, 2)
    roi_pct     = round(total_pnl / START_BANK * 100, 2)
    # Peak = max ever balance — не храним, используем curr как lower bound
    peak_bank   = curr_bank if total_pnl >= 0 else START_BANK
    drawdown    = round((peak_bank - curr_bank) / peak_bank * 100, 2) if peak_bank > curr_bank else 0

    # ── Model config / calibration ───────────────────────────────────────────
    cfg_rows = sb_safe("model_config?select=key,value")
    cfg = {r["key"]: r["value"] for r in cfg_rows} if cfg_rows else {}
    w_elo        = cfg.get("w_elo", "0.60")
    w_form       = cfg.get("w_form", "0.25")
    w_h2h        = cfg.get("w_h2h", "0.15")
    cal_at       = cfg.get("calibrated_at", "—")
    cal_n        = cfg.get("calibration_n", "—")
    cal_loss     = cfg.get("calibration_logloss", "—")
    cal_brier    = cfg.get("calibration_brier", "—")
    cal_acc      = cfg.get("calibration_acc", "—")

    # ── Активные ставки (72ч окно) ──────────────────────────────────────────
    from datetime import timedelta
    cutoff_72h = (datetime.now(timezone.utc) - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ")
    active_bets = sb_safe(
        "elo_paper_bets"
        "?strategy_name=eq.AUTO_ELO_FLAT"
        "&settled=eq.false"
        "&stake_usd=gt.0"
        f"&run_ts=gte.{cutoff_72h}"
        "&select=run_ts,home_team,away_team,bet_team,stake_usd,real_odds,"
        "composite_prob,kelly_f,form_score,h2h_score,league_tier,edge,start_time,league"
        "&order=run_ts.desc&limit=30"
    )

    # ── Auto bets (last 50) ──────────────────────────────────────────────────
    bets = sb_safe(
        "elo_paper_bets"
        "?strategy_name=eq.AUTO_ELO_FLAT"
        "&select=run_ts,home_team,away_team,bet_team,stake_usd,real_odds,"
        "composite_prob,kelly_f,form_score,h2h_score,closing_odds,clv,"
        "league_tier,edge,outcome,pnl,settled"
        "&order=run_ts.desc&limit=50"
    )
    real_bets = [b for b in bets if float(b.get("stake_usd") or 0) > 0]
    settled   = [b for b in real_bets if b.get("settled")]

    total_bets  = len(real_bets)
    wins        = sum(1 for b in settled if b.get("outcome") == "win")
    winrate     = round(wins / len(settled) * 100, 1) if settled else 0
    total_staked = sum(float(b.get("stake_usd") or 0) for b in settled)
    total_pnl_bets = sum(float(b.get("pnl") or 0) for b in settled)
    roi_bets    = round(total_pnl_bets / total_staked * 100, 2) if total_staked > 0 else 0

    # Brier score over settled bets with composite_prob
    brier_val = "—"
    clv_avg   = "—"
    try:
        brier_data = [(float(b["composite_prob"]), 1 if b["outcome"] == "win" else 0)
                      for b in settled if b.get("composite_prob") and b.get("outcome")]
        if brier_data:
            brier_val = round(sum((p-y)**2 for p,y in brier_data) / len(brier_data), 4)
        clv_data = [float(b["clv"]) for b in settled if b.get("clv")]
        if clv_data:
            clv_avg = round(sum(clv_data) / len(clv_data) * 100, 2)
    except Exception:
        pass

    # ── HTML helpers ─────────────────────────────────────────────────────────
    def pnl_cls(v):
        try: return "pos" if float(v) >= 0 else "neg"
        except: return ""

    def fmt_pnl(v):
        try: return f"${float(v):+.2f}"
        except: return "—"

    def fmt_pct(v):
        try: return f"{float(v)*100:.1f}%"
        except: return "—"

    def fmt_f(v, d=3):
        try: return f"{float(v):.{d}f}"
        except: return "—"

    def tier_badge(t):
        colors = {"1": "#ffd700", "2": "#5b8cff", "3": "#8a8f98"}
        c = colors.get(str(t), "#8a8f98")
        return f'<span style="color:{c};font-weight:700">T{t}</span>' if t else "—"

    def outcome_badge(o):
        if o == "win":  return '<span class="pos">✓ WIN</span>'
        if o == "loss": return '<span class="neg">✗ LOSS</span>'
        return '<span class="muted">⏳ pending</span>'

    # ── Build active bet rows (72h window) ───────────────────────────────────
    def fmt_ts(ts):
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d.%m %H:%M")
        except Exception:
            return "—"

    active_rows_html = '<div class="card"><h2>⏳ Активные ставки — сегодня и ближайшие 72ч</h2>'
    if active_bets:
        active_rows_html += '<table><thead><tr><th>Старт</th><th>Матч / Лига</th><th>Ставка</th><th>Prob / K</th><th>Edge / Тир</th></tr></thead><tbody>'
        for b in active_bets:
            edge_str = f"{float(b['edge'])*100:.1f}%" if b.get('edge') else "—"
            ts_display = fmt_ts(b.get("start_time")) if b.get("start_time") else (b.get("run_ts") or "")[:16].replace("T", " ")
            active_rows_html += (
                f'<tr>'
                f'<td class="muted">{ts_display}</td>'
                f'<td><b>{b.get("home_team","?")} vs {b.get("away_team","?")}</b>'
                f'<br><small class="muted">{b.get("league") or ""} · {b.get("bet_team","")}</small></td>'
                f'<td>${fmt_f(b.get("stake_usd"),1)} @ {fmt_f(b.get("real_odds"),2)}</td>'
                f'<td>{fmt_pct(b.get("composite_prob"))} / K:{fmt_f(b.get("kelly_f"),4)}</td>'
                f'<td>{edge_str} &nbsp; {tier_badge(b.get("league_tier"))}</td>'
                f'</tr>'
            )
        active_rows_html += '</tbody></table>'
    else:
        active_rows_html += '<p class="empty">Нет активных ставок в ближайшие 72ч</p>'

    # ── Build bet rows ───────────────────────────────────────────────────────
    rows_html = ""
    for b in real_bets:
        ts = (b.get("run_ts") or "")[:16].replace("T", " ")
        match = f"{b.get('home_team','?')} vs {b.get('away_team','?')}"
        team  = b.get("bet_team", "")
        stake = fmt_f(b.get("stake_usd"), 1)
        odds  = fmt_f(b.get("real_odds"), 2)
        prob  = fmt_pct(b.get("composite_prob"))
        kf    = fmt_f(b.get("kelly_f"), 4)
        edge  = f"{float(b['edge'])*100:.1f}%" if b.get("edge") else "—"
        form  = fmt_pct(b.get("form_score"))
        h2h   = fmt_pct(b.get("h2h_score"))
        clv   = f"{float(b['clv'])*100:+.1f}%" if b.get("clv") else "—"
        tier  = tier_badge(b.get("league_tier"))
        oc    = outcome_badge(b.get("outcome") if b.get("settled") else None)
        pnl   = fmt_pnl(b.get("pnl")) if b.get("settled") else "—"
        pc    = pnl_cls(b.get("pnl") or 0)
        rows_html += f"""
        <tr>
          <td class="muted">{ts}</td>
          <td>{match}<br><small class="muted">{team}</small></td>
          <td>${stake} @ {odds}</td>
          <td>{prob} / K:{kf}<br><small>Edge:{edge} {tier}</small></td>
          <td><small>Form:{form} H2H:{h2h}<br>CLV:{clv}</small></td>
          <td>{oc}</td>
          <td class="{pc}">{pnl}</td>
        </tr>"""

    if not rows_html:
        rows_html = '<tr><td colspan="7" class="empty">Нет ставок — пайплайн ещё не поставил</td></tr>'

    cal_at_short = cal_at[:16].replace("T", " ") if cal_at and cal_at != "—" else cal_at

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="1800">
<title>Dota Trader — Live Dashboard</title>
<style>
:root{{--bg:#0f1115;--card:#171a21;--border:#2a2e38;--text:#e6e8eb;
       --muted:#8a8f98;--pos:#3ddc84;--neg:#ff5c5c;--accent:#5b8cff;}}
*{{box-sizing:border-box;}}
body{{background:var(--bg);color:var(--text);
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      margin:0;padding:32px;}}
h1{{font-size:22px;margin:0 0 4px;}}
.upd{{color:var(--muted);font-size:13px;margin-bottom:28px;}}
.card{{background:var(--card);border:1px solid var(--border);
       border-radius:12px;padding:20px 24px;margin-bottom:24px;}}
.card h2{{font-size:14px;margin:0 0 16px;color:var(--muted);
          text-transform:uppercase;letter-spacing:.04em;}}
.stats{{display:flex;gap:28px;flex-wrap:wrap;}}
.stat .val{{font-size:24px;font-weight:700;}}
.stat .lbl{{font-size:12px;color:var(--muted);margin-top:2px;}}
table{{width:100%;border-collapse:collapse;font-size:12.5px;}}
th{{text-align:left;color:var(--muted);font-weight:500;
     padding:8px 10px;border-bottom:1px solid var(--border);}}
td{{padding:8px 10px;border-bottom:1px solid var(--border);vertical-align:top;}}
tr:last-child td{{border-bottom:none;}}
.pos{{color:var(--pos);font-weight:600;}}
.neg{{color:var(--neg);font-weight:600;}}
.muted{{color:var(--muted);}}
.empty{{text-align:center;color:var(--muted);padding:24px;}}
small{{font-size:11px;}}
</style>
</head>
<body>
<h1>🎮 Dota Trader — Live Dashboard</h1>
<div class="upd">Обновлено: {now_utc} &nbsp;·&nbsp; авто-обновление каждые 30 мин</div>

<div class="card">
  <h2>💰 Bankroll — AUTO_ELO_FLAT</h2>
  <div class="stats">
    <div class="stat"><div class="val">${curr_bank:,.2f}</div><div class="lbl">Текущий банк</div></div>
    <div class="stat"><div class="val {'pos' if total_pnl>=0 else 'neg'}">{fmt_pnl(total_pnl)}</div><div class="lbl">Total P&amp;L</div></div>
    <div class="stat"><div class="val {'pos' if roi_pct>=0 else 'neg'}">{roi_pct:+.2f}%</div><div class="lbl">ROI от старта</div></div>
    <div class="stat"><div class="val">${START_BANK:,.0f}</div><div class="lbl">Стартовый банк</div></div>
    <div class="stat"><div class="val">{bank.get('total_bets', '—')}</div><div class="lbl">Settled ставок</div></div>
  </div>
</div>

<div class="card">
  <h2>📊 Статистика ставок (settled)</h2>
  <div class="stats">
    <div class="stat"><div class="val">{total_bets}</div><div class="lbl">Всего ставок</div></div>
    <div class="stat"><div class="val">{len(settled)}</div><div class="lbl">Урегулировано</div></div>
    <div class="stat"><div class="val">{winrate}%</div><div class="lbl">Win rate</div></div>
    <div class="stat"><div class="val {'pos' if roi_bets>=0 else 'neg'}">{roi_bets:+.2f}%</div><div class="lbl">ROI (ставки)</div></div>
    <div class="stat"><div class="val">{brier_val}</div><div class="lbl">Brier score</div></div>
    <div class="stat"><div class="val">{'—' if clv_avg == '—' else f'{clv_avg:+.2f}%'}</div><div class="lbl">Avg CLV</div></div>
  </div>
</div>

<div class="card">
  <h2>🧠 Качество модели — калибровка</h2>
  <div class="stats">
    <div class="stat"><div class="val">{w_elo}</div><div class="lbl">Вес Elo</div></div>
    <div class="stat"><div class="val">{w_form}</div><div class="lbl">Вес формы</div></div>
    <div class="stat"><div class="val">{w_h2h}</div><div class="lbl">Вес H2H</div></div>
    <div class="stat"><div class="val">{cal_loss}</div><div class="lbl">Log-loss</div></div>
    <div class="stat"><div class="val">{cal_brier}</div><div class="lbl">Brier (cal)</div></div>
    <div class="stat"><div class="val">{cal_acc}</div><div class="lbl">Accuracy</div></div>
  </div>
  <div style="margin-top:12px;font-size:12px;color:var(--muted)">
    Откалибровано: {cal_at_short} &nbsp;·&nbsp; Выборка: {cal_n} матчей
  </div>
</div>

{active_rows_html}</div>

<div class="card">
  <h2>🎯 Последние ставки AUTO_ELO_FLAT</h2>
  <table>
    <thead>
      <tr>
        <th>Время</th><th>Матч / Команда</th><th>Ставка</th>
        <th>Prob / Kelly</th><th>Сигналы</th><th>Итог</th><th>P&amp;L</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>

</body>
</html>"""
    return HTMLResponse(content=html)
