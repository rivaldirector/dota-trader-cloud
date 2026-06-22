import os
import json
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
# DASHBOARD (HTML) — живая страница, не зависит от Мака.
# Источники: prematch_model_picks (бесплатный Elo-прогноз, см.
# scripts/prematch_free_predict.py) + elo_bankroll (банк Rule C контура,
# отдельного от других экспериментов в этой базе).
# -------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    now = datetime.now(timezone.utc)
    now_iso_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        picks = sb("GET", f"prematch_model_picks?starts_at=gte.{now_iso_str}&order=starts_at.asc&limit=100&select=*")
    except Exception as e:
        picks = []
    try:
        bankroll_rows = sb("GET", "elo_bankroll?select=*&limit=1")
    except Exception:
        bankroll_rows = []
    try:
        pipeline = sb("GET", "pipeline_status?select=*&order=checked_at.desc")
    except Exception:
        pipeline = []
    try:
        gate_count = sb("GET", "elo_paper_bets?strategy_name=eq.M05&settled=eq.true&select=id")
    except Exception:
        gate_count = []
    try:
        auto_bets = sb("GET", "elo_paper_bets?strategy_name=eq.AUTO_ELO_FLAT&order=start_time.desc&limit=20&select=*")
    except Exception:
        auto_bets = []

    bankroll = bankroll_rows[0] if bankroll_rows else {}

    rows_html = ""
    for p in picks:
        st = p.get("starts_at", "")
        try:
            dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
            st_fmt = dt.strftime("%d.%m %H:%M UTC")
        except Exception:
            st_fmt = st
        has_data = p.get("has_elo_data")
        fav = p.get("favorite") or "?"
        fav_prob = p.get("favorite_prob")
        fav_prob_s = f"{float(fav_prob) * 100:.1f}%" if fav_prob is not None else "—"
        elo_diff = p.get("elo_diff")
        elo_diff_s = f"{float(elo_diff):+.0f}" if elo_diff is not None else "—"
        warn = "" if has_data else " <span style='color:#f59e0b'>(нет Elo-истории)</span>"
        rows_html += f"""
        <tr>
          <td>{st_fmt}</td>
          <td>{p.get('league_name') or ''}</td>
          <td>{p.get('team_1')} vs {p.get('team_2')}</td>
          <td>{elo_diff_s}</td>
          <td><b>{fav}</b> ({fav_prob_s}){warn}</td>
        </tr>"""
    if not rows_html:
        rows_html = '<tr><td colspan="5" style="text-align:center;color:#888">Нет матчей в ближайшие часы (или прогноз ещё не обновлялся — обновляется 2 раза/день)</td></tr>'

    pipeline_html = ""
    for s in pipeline[:6]:
        cls = "ok" if s.get("status") == "ok" else "err"
        msg = (s.get("message") or "")[:90]
        pipeline_html += (
            f"<div class='stat'><div class='lbl'>{s.get('script')}</div>"
            f"<div class='val {cls}' style='font-size:14px'>{s.get('status')}</div>"
            f"<div class='note' style='margin-top:2px'>{msg}</div></div>"
        )

    bank_cur = float(bankroll.get("current_bank_usd", 1000) or 1000)
    bank_start = float(bankroll.get("start_bank_usd", 1000) or 1000)
    gate_settled = len(gate_count)

    auto_settled = [b for b in auto_bets if b.get("settled")]
    auto_pending = [b for b in auto_bets if not b.get("settled")]
    auto_pnl = sum(float(b.get("pnl") or 0) for b in auto_settled)
    auto_wins = sum(1 for b in auto_settled if b.get("outcome") == "win")
    auto_wr = (auto_wins / len(auto_settled) * 100) if auto_settled else None
    auto_bank = round(1000.0 + auto_pnl, 2)

    auto_rows_html = ""
    for b in auto_bets[:12]:
        st = b.get("start_time")
        try:
            st_fmt = datetime.fromtimestamp(st, tz=timezone.utc).strftime("%d.%m %H:%M UTC")
        except Exception:
            st_fmt = str(st)
        side = b.get("home_team") if b.get("bet_team") == "home" else b.get("away_team")
        if b.get("settled"):
            cls = "ok" if b.get("outcome") == "win" else "err"
            status_s = f"<span class='{cls}'>{b.get('outcome')} ({float(b.get('pnl') or 0):+.2f}$)</span>"
        else:
            status_s = "<span style='color:#9aa0ad'>ожидаем результат</span>"
        auto_rows_html += f"""
        <tr>
          <td>{st_fmt}</td>
          <td>{b.get('home_team')} vs {b.get('away_team')}</td>
          <td><b>{side}</b></td>
          <td>{b.get('odds')}</td>
          <td>{status_s}</td>
        </tr>"""
    if not auto_rows_html:
        auto_rows_html = '<tr><td colspan="5" style="text-align:center;color:#888">Машина пока не приняла ни одного решения — следующий прогон по расписанию (каждые 2ч)</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="300">
<title>Dota Trader — Cloud Dashboard</title>
<style>
  body {{ background:#0f1115; color:#e6e6e6; font-family:-apple-system,Segoe UI,Roboto,sans-serif; margin:0; padding:24px; }}
  h1 {{ font-size:20px; color:#fff; }}
  .card {{ background:#1a1d24; border-radius:12px; padding:20px; margin-bottom:20px; border:1px solid #2a2e38; }}
  .card h2 {{ margin-top:0; font-size:15px; color:#9aa0ad; text-transform:uppercase; letter-spacing:.04em; }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; }}
  th, td {{ text-align:left; padding:8px 6px; border-bottom:1px solid #2a2e38; }}
  th {{ color:#9aa0ad; font-weight:500; }}
  .stats {{ display:flex; gap:24px; flex-wrap:wrap; }}
  .stat {{ min-width:140px; }}
  .stat .val {{ font-size:22px; font-weight:600; color:#fff; }}
  .stat .lbl {{ font-size:12px; color:#9aa0ad; }}
  .ok {{ color:#22c55e; }}
  .err {{ color:#ef4444; }}
  .note {{ font-size:12px; color:#6b7280; margin-top:10px; line-height:1.5; }}
  a {{ color:#60a5fa; }}
</style>
</head>
<body>
  <h1>Dota Trader — облачный дэшборд (работает без Мака, GitHub Actions + Railway + Supabase)</h1>
  <div class="note">Обновлено: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC · автообновление страницы каждые 5 мин</div>

  <div class="card">
    <h2>Elo-прогноз — ближайшие матчи (бесплатные источники: Liquipedia + история в Supabase)</h2>
    <table>
      <tr><th>Время</th><th>Лига</th><th>Матч</th><th>Elo Δ</th><th>Фаворит по модели</th></tr>
      {rows_html}
    </table>
    <div class="note">Без рыночных коэффициентов (BetsAPI отключён) — чисто модельный Elo-прогноз, без edge/value, без ставок. Обновляется 2 раза/день через GitHub Actions (prematch_free_predict.yml).</div>
  </div>

  <div class="card">
    <h2>Автономная Elo-машина (решает сама, без ревью)</h2>
    <div class="stats">
      <div class="stat"><div class="val">${auto_bank:,.2f}</div><div class="lbl">условный банк (старт $1000)</div></div>
      <div class="stat"><div class="val">{len(auto_settled)}</div><div class="lbl">решений урегулировано</div></div>
      <div class="stat"><div class="val">{f'{auto_wr:.1f}%' if auto_wr is not None else '—'}</div><div class="lbl">winrate Elo-фаворита</div></div>
      <div class="stat"><div class="val">{len(auto_pending)}</div><div class="lbl">ожидают результата</div></div>
    </div>
    <table style="margin-top:14px">
      <tr><th>Старт</th><th>Матч</th><th>Решение</th><th>Условные odds</th><th>Итог</th></tr>
      {auto_rows_html}
    </table>
    <div class="note">
      Машина сама выбирает фаворита по Elo и фиксирует флэт $20 на каждый матч в окне 72ч — каждые 2 часа, без участия человека.
      "Условные odds" — НЕ рыночная цена (её сейчас просто нет, BetsAPI мёртв): это 1/(model_prob × 1.0585), где 1.0585 —
      реальный средний оверраунд букмекеров, посчитанный по 68 733 историческим строкам в этой же базе. Главная метрика
      здесь — <b>winrate</b> (угадывает ли Elo фаворита), а не $ — деньги тут иллюстративные, пока не вернутся живые одсы.
      Сеттлинг — через бесплатный OpenDota (без BetsAPI/ключа).
    </div>
  </div>

  <div class="card">
    <h2>Историческая симуляция — чистый Elo на реальных одсах, 60 дней</h2>
    <div class="stats">
      <div class="stat"><div class="val">377</div><div class="lbl">ставок (flat $20)</div></div>
      <div class="stat"><div class="val">68.44%</div><div class="lbl">winrate (258/377)</div></div>
      <div class="stat"><div class="val pos" style="color:#22c55e">+$570.46</div><div class="lbl">P&amp;L (ROI +7.57%)</div></div>
      <div class="stat"><div class="val">$1,000 → $1,570.46</div><div class="lbl">банк в этой симуляции</div></div>
    </div>
    <div class="note">
      Бэктест на РЕАЛЬНЫХ исторических котировках (закэшированы до смерти BetsAPI 17 июня), посчитан 21.06.2026
      на Mac-пайплайне (scripts/backtest_elo_pure_60d.py). Это задним числом, отдельно от живого банка выше —
      не смешиваем, чтобы не подменять честный live-трек симуляцией.
    </div>
  </div>

  <div class="card">
    <h2>Виртуальный банк (Elo + H2H + Rule C контур)</h2>
    <div class="stats">
      <div class="stat"><div class="val">${bank_cur:,.2f}</div><div class="lbl">текущий банк</div></div>
      <div class="stat"><div class="val">${bank_start:,.2f}</div><div class="lbl">старт</div></div>
      <div class="stat"><div class="val">{gate_settled}/30</div><div class="lbl">гейт Rule C (settled signals)</div></div>
    </div>
    <div class="note">Rule C требует рыночные коэффициенты для расчёта edge — сигналы не генерируются и банк не меняется, пока не подключён источник одсов. Это отдельный контур от других таблиц в этой базе (paper_trades/daily_bankroll и т.п. — другие эксперименты).</div>
  </div>

  <div class="card">
    <h2>Статус пайплайна (последние прогоны)</h2>
    <div class="stats">
      {pipeline_html or "<div class='note'>Нет данных pipeline_status</div>"}
    </div>
  </div>
</body>
</html>"""
    return html
