import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

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

# Те же константы, что в elo_auto_bet.py — приблизительный коэффициент для
# карточки прогноза считается ТЕМ ЖЕ способом, что и в реальных ставках
# машины: 1/(model_prob * AVG_OVERROUND_HIST), где AVG_OVERROUND_HIST —
# реальный средний оверраунд букмекеров по 68 733 историческим строкам
# odds_summary в этой же базе. Не рыночная цена, а историческая оценка.
FORECAST_AVG_OVERROUND = 1.0585
FORECAST_STAKE_USD = 20.0


def clean_team_name(name):
    """Убирает мусор парсинга Liquipedia вида ' (page does not exist)'."""
    if not name:
        return name or "?"
    return name.split(" (page does not exist)")[0].strip()


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
# Маленький инлайн-SVG лайн-чарт — без внешних JS-библиотек, считается
# на сервере из cumulative P&L по урегулированным ставкам автономной машины.
# -------------------
def render_bank_chart(values: list, start: float, width: int = 760, height: int = 150) -> str:
    pts = [start] + list(values)
    if len(pts) < 2:
        return ('<div class="note" style="padding:24px 0;text-align:center">'
                'Пока нет урегулированных ставок — график появится после первых результатов.</div>')
    vmin, vmax = min(pts), max(pts)
    pad = max(2.0, (vmax - vmin) * 0.12)
    vmin -= pad
    vmax += pad
    n = len(pts)

    def X(i):
        return 8 + (width - 16) * i / (n - 1)

    def Y(v):
        return 8 + (height - 16) * (1 - (v - vmin) / (vmax - vmin or 1))

    poly = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(pts))
    y0 = Y(start)
    color = "#22c55e" if pts[-1] >= start else "#ef4444"
    return f"""<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" preserveAspectRatio="none">
      <line x1="8" y1="{y0:.1f}" x2="{width - 8}" y2="{y0:.1f}" stroke="#2a2e38" stroke-width="1" stroke-dasharray="4,4"/>
      <polyline points="{poly}" fill="none" stroke="{color}" stroke-width="2.5"/>
    </svg>"""


# -------------------
# DASHBOARD (HTML) — живая страница, не зависит от Мака.
# Источники: prematch_model_picks (бесплатный Elo-прогноз, см.
# scripts/prematch_free_predict.py) + elo_paper_bets (и автономная
# AUTO_ELO_FLAT машина, и frozen Rule C family M05/M06/M36 — СТРОГО
# раздельный расчёт банка для каждой, без общих таблиц-банков).
# -------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    now = datetime.now(timezone.utc)
    now_iso_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)

    today_start_iso = today_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        # от начала СЕГОДНЯ (не от now) — иначе уже начавшиеся сегодня матчи
        # (на которые машина уже реально поставила) выпадают из карточки.
        picks = sb("GET", f"prematch_model_picks?starts_at=gte.{today_start_iso}&order=starts_at.asc&limit=150&select=*")
    except Exception:
        picks = []
    try:
        auto_bets = sb("GET", "elo_paper_bets?strategy_name=eq.AUTO_ELO_FLAT&order=start_time.desc&limit=300&select=*")
    except Exception:
        auto_bets = []
    auto_bets_by_event = {b.get("event_id"): b for b in auto_bets if b.get("event_id")}
    try:
        rule_c_bets = sb("GET", "elo_paper_bets?strategy_name=in.(M05,M06,M36)&select=id,settled,pnl")
    except Exception:
        rule_c_bets = []

    # ---- Rule C — полностью отдельный расчёт банка, НЕ из elo_bankroll
    # (та таблица принадлежит автономной машине, см. ниже) ----
    rule_c_settled = [b for b in rule_c_bets if b.get("settled")]
    rule_c_pnl = sum(float(b.get("pnl") or 0) for b in rule_c_settled)
    bank_start = 1000.0
    bank_cur = round(bank_start + rule_c_pnl, 2)
    gate_settled = len(rule_c_settled)

    def pick_row(p, time_cell, st_dt):
        t1 = clean_team_name(p.get("team_1"))
        t2 = clean_team_name(p.get("team_2"))
        has_data = bool(p.get("has_elo_data"))
        fav_prob = p.get("favorite_prob")
        real = auto_bets_by_event.get(f"liq_{p.get('match_hash')}") if p.get("match_hash") else None

        if real:
            # Машина уже реально поставила на этот матч — показываем
            # настоящие данные ставки, а не гипотетический прогноз.
            bet_team_name = clean_team_name(real.get("home_team") if real.get("bet_team") == "home" else real.get("away_team"))
            stake = float(real.get("stake_usd") or 0)
            odds = real.get("odds")
            bet_cell = f"<b>{bet_team_name}</b>"
            stake_cell = f"${stake:.0f}"
            odds_cell = f"{odds}"
            if real.get("settled"):
                cls = "ok" if real.get("outcome") == "win" else "err"
                result_cell = f"<span class='{cls}'>{real.get('outcome')} ({float(real.get('pnl') or 0):+.2f}$)</span>"
            elif st_dt is not None and st_dt <= now:
                elapsed_h = (now - st_dt).total_seconds() / 3600
                result_cell = f"<span style='color:#9aa0ad'>ожидаем результат ({elapsed_h:.0f}ч с начала)</span>"
            elif st_dt is not None:
                until_h = (st_dt - now).total_seconds() / 3600
                result_cell = f"<span style='color:#6b7280'>начнётся через {until_h:.0f}ч</span>"
            else:
                result_cell = "<span style='color:#9aa0ad'>ставка принята</span>"
        elif has_data and fav_prob is not None:
            # Прогноз есть, но машина ЕЩЁ не ставила (свежий матч — следующий
            # прогон по расписанию подхватит) — гипотетическая оценка.
            fav = clean_team_name(p.get("favorite"))
            fav_prob_f = float(fav_prob)
            odds = round(1.0 / (fav_prob_f * FORECAST_AVG_OVERROUND), 3)
            profit = round(FORECAST_STAKE_USD * (odds - 1), 2)
            bet_cell = f"<b>{fav}</b>"
            stake_cell = f"${FORECAST_STAKE_USD:.0f}"
            odds_cell = f"{odds}"
            result_cell = f"<span style='color:#6b7280'>потенциал +${profit:,.2f} (прогноз, бот ещё не ставил)</span>"
        else:
            bet_cell = "<span style='color:#6b7280'>нет данных</span>"
            stake_cell = odds_cell = result_cell = "—"
        return f"""
        <tr>
          <td>{time_cell}</td>
          <td>{t1} vs {t2}</td>
          <td>{bet_cell}</td>
          <td>{stake_cell}</td>
          <td>{odds_cell}</td>
          <td>{result_cell}</td>
        </tr>"""

    today_date = now.date()
    today_picks, later_picks = [], []
    for p in picks:
        st = p.get("starts_at", "")
        try:
            dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
        except Exception:
            dt = None
        (today_picks if (dt and dt.date() == today_date) else later_picks).append((p, dt))

    today_rows_html = "".join(
        pick_row(p, dt.strftime("%H:%M UTC") if dt else "—", dt) for p, dt in today_picks
    ) or '<tr><td colspan="6" style="text-align:center;color:#888">Сегодня матчей в прогнозе нет</td></tr>'

    later_rows_html = ""
    last_date = None
    for p, dt in later_picks:
        d = dt.date() if dt else None
        if d != last_date:
            _wd = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
            d_label = f"{dt.strftime('%d.%m')} ({_wd[dt.weekday()]})" if dt else "?"
            later_rows_html += f'<tr><td colspan="6" style="padding-top:14px;color:#9aa0ad;font-weight:600">{d_label}</td></tr>'
            last_date = d
        later_rows_html += pick_row(p, dt.strftime("%H:%M UTC") if dt else "—", dt)
    if not later_rows_html:
        later_rows_html = '<tr><td colspan="6" style="text-align:center;color:#888">Дальше матчей в прогнозе нет</td></tr>'

    auto_settled = [b for b in auto_bets if b.get("settled")]
    auto_pending = [b for b in auto_bets if not b.get("settled")]
    auto_pnl = sum(float(b.get("pnl") or 0) for b in auto_settled)
    auto_wins = sum(1 for b in auto_settled if b.get("outcome") == "win")
    auto_losses = sum(1 for b in auto_settled if b.get("outcome") == "loss")
    auto_wr = (auto_wins / len(auto_settled) * 100) if auto_settled else None
    auto_bank = round(1000.0 + auto_pnl, 2)

    gains_sum = sum(float(b.get("pnl") or 0) for b in auto_settled if float(b.get("pnl") or 0) > 0)
    losses_sum = sum(float(b.get("pnl") or 0) for b in auto_settled if float(b.get("pnl") or 0) < 0)

    def started_after(b, dt):
        st = b.get("start_time")
        if st is None:
            return False
        try:
            return datetime.fromtimestamp(st, tz=timezone.utc) >= dt
        except Exception:
            return False

    today_bets = [b for b in auto_bets if started_after(b, today_start)]
    week_bets = [b for b in auto_bets if started_after(b, week_start)]
    today_settled = [b for b in today_bets if b.get("settled")]
    week_settled = [b for b in week_bets if b.get("settled")]
    today_staked = sum(float(b.get("stake_usd") or 0) for b in today_bets)
    week_staked = sum(float(b.get("stake_usd") or 0) for b in week_bets)
    today_pnl = sum(float(b.get("pnl") or 0) for b in today_settled)
    week_pnl = sum(float(b.get("pnl") or 0) for b in week_settled)

    # ---- график банка: хронологически по settled_ts (fallback start_time) ----
    chrono_settled = sorted(
        auto_settled,
        key=lambda b: b.get("settled_ts") or datetime.fromtimestamp(b.get("start_time", 0), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    bank_curve, running = [], 1000.0
    for b in chrono_settled:
        running = round(running + float(b.get("pnl") or 0), 2)
        bank_curve.append(running)
    bank_chart_svg = render_bank_chart(bank_curve, start=1000.0)

    auto_rows_html = ""
    for b in auto_bets[:20]:
        st = b.get("start_time")
        try:
            st_dt = datetime.fromtimestamp(st, tz=timezone.utc)
            st_fmt = st_dt.strftime("%d.%m %H:%M UTC")
        except Exception:
            st_dt, st_fmt = None, str(st)
        side = b.get("home_team") if b.get("bet_team") == "home" else b.get("away_team")
        if b.get("settled"):
            cls = "ok" if b.get("outcome") == "win" else "err"
            status_s = f"<span class='{cls}'>{b.get('outcome')} ({float(b.get('pnl') or 0):+.2f}$)</span>"
        elif st_dt is not None and st_dt <= now:
            elapsed_h = (now - st_dt).total_seconds() / 3600
            status_s = f"<span style='color:#9aa0ad'>ожидаем результат ({elapsed_h:.0f}ч с начала)</span>"
        elif st_dt is not None:
            until_h = (st_dt - now).total_seconds() / 3600
            status_s = f"<span style='color:#6b7280'>начнётся через {until_h:.0f}ч</span>"
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
    <h2>Прогноз — сегодня</h2>
    <table>
      <tr><th>Время</th><th>Матч</th><th>Ставка</th><th>Размер</th><th>Коэф</th><th>Итог / потенциал</th></tr>
      {today_rows_html}
    </table>
  </div>

  <div class="card">
    <h2>Прогноз — далее</h2>
    <table>
      <tr><th>Время</th><th>Матч</th><th>Ставка</th><th>Размер</th><th>Коэф</th><th>Итог / потенциал</th></tr>
      {later_rows_html}
    </table>
    <div class="note">
      Коэф — НЕ рыночная цена (BetsAPI мёртв, живых одсов нет): это приблизительная оценка 1/(model_prob × 1.0585)
      по тому же среднему историческому оверраунду, что и у автономной машины ниже. "Нет данных" — для команд без
      Elo-истории прогноз ненадёжен (коинфлип), ставку не показываем. Если по матчу машина уже реально поставила
      (видно по "Итог/потенциал": win/loss/ожидаем результата) — показаны настоящие данные ставки, а не прогноз.
      "Потенциал (прогноз, бот ещё не ставил)" — гипотетическая оценка для матчей, до которых бот пока не добрался
      (прогоняется раз в 2ч). Обновляется 2 раза/день через GitHub Actions (prematch_free_predict.yml).
    </div>
  </div>

  <div class="card">
    <h2>Автономная Elo-машина (решает сама, без ревью)</h2>
    <div class="stats">
      <div class="stat"><div class="val">${auto_bank:,.2f}</div><div class="lbl">условный банк (старт $1000)</div></div>
      <div class="stat"><div class="val">{len(auto_settled)}</div><div class="lbl">решений урегулировано</div></div>
      <div class="stat"><div class="val">{f'{auto_wr:.1f}%' if auto_wr is not None else '—'}</div><div class="lbl">winrate Elo-фаворита</div></div>
      <div class="stat"><div class="val">{len(auto_pending)}</div><div class="lbl">ожидают результата</div></div>
    </div>
    <div style="margin-top:16px">{bank_chart_svg}</div>
    <table style="margin-top:14px">
      <tr><th>Старт</th><th>Матч</th><th>Решение</th><th>Условные odds</th><th>Итог</th></tr>
      {auto_rows_html}
    </table>
    <div class="note">
      Машина сама выбирает фаворита по Elo и фиксирует флэт $20 на каждый матч в окне 72ч — каждые 2 часа, без участия человека.
      "Условные odds" — НЕ рыночная цена (её сейчас просто нет, BetsAPI мёртв): это 1/(model_prob × 1.0585), где 1.0585 —
      реальный средний оверраунд букмекеров, посчитанный по 68 733 историческим строкам в этой же базе. Главная метрика
      здесь — <b>winrate</b> (угадывает ли Elo фаворита), а не $ — деньги тут иллюстративные, пока не вернутся живые одсы.
      Сеттлинг — через бесплатный OpenDota (без BetsAPI/ключа): иногда матч (особенно квалы с неопределённым соперником,
      типа "Inner Circle x Insanity") просто отсутствует в бесплатной выдаче OpenDota — тогда ставка честно висит
      "ожидаем результата" дольше обычного, это не баг, а предел бесплатного источника.
    </div>
  </div>

  <div class="card">
    <h2>Объём ставок и P&amp;L — сегодня / за 7 дней</h2>
    <div class="stats">
      <div class="stat"><div class="val">{len(today_bets)}</div><div class="lbl">ставок сегодня (${today_staked:,.0f} stake)</div></div>
      <div class="stat"><div class="val">{len(week_bets)}</div><div class="lbl">ставок за 7 дней (${week_staked:,.0f} stake)</div></div>
      <div class="stat"><div class="val {'ok' if today_pnl >= 0 else 'err'}">{today_pnl:+,.2f}$</div><div class="lbl">P&amp;L сегодня ({len(today_settled)} settled)</div></div>
      <div class="stat"><div class="val {'ok' if week_pnl >= 0 else 'err'}">{week_pnl:+,.2f}$</div><div class="lbl">P&amp;L за 7 дней ({len(week_settled)} settled)</div></div>
    </div>
    <div class="stats" style="margin-top:18px">
      <div class="stat"><div class="val ok">+{gains_sum:,.2f}$</div><div class="lbl">сумма приростов ({auto_wins} win)</div></div>
      <div class="stat"><div class="val err">{losses_sum:,.2f}$</div><div class="lbl">сумма лузов ({auto_losses} loss)</div></div>
      <div class="stat"><div class="val {'ok' if auto_pnl >= 0 else 'err'}">{auto_pnl:+,.2f}$</div><div class="lbl">чистый P&amp;L (всего settled)</div></div>
    </div>
    <div class="note">Считается по тем же settled-ставкам автономной Elo-машины (elo_paper_bets, strategy_name=AUTO_ELO_FLAT). "Сегодня"/"7 дней" — по времени старта матча (UTC), а не по времени сеттла.</div>
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
    <h2>Виртуальный банк (Rule C контур — заморожен, нужны живые одсы)</h2>
    <div class="stats">
      <div class="stat"><div class="val">${bank_cur:,.2f}</div><div class="lbl">текущий банк</div></div>
      <div class="stat"><div class="val">${bank_start:,.2f}</div><div class="lbl">старт</div></div>
      <div class="stat"><div class="val">{gate_settled}/30</div><div class="lbl">гейт Rule C (settled signals)</div></div>
    </div>
    <div class="note">
      Считается СТРОГО по своим ставкам (elo_paper_bets, strategy_name IN M05/M06/M36) — не путать с автономной
      машиной выше, у них разные банки и разная логика. Rule C требует рыночные коэффициенты для расчёта edge —
      сигналы не генерируются и банк honestly не двигается, пока не подключён источник одсов (сейчас 0/30 — это
      ожидаемо, контур не сломан, просто пуст). Отдельно от других таблиц в этой базе (paper_trades/daily_bankroll
      и т.п. — другие эксперименты, не трогаем).
    </div>
  </div>
</body>
</html>"""
    return html
