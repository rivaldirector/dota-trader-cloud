import os
import re
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


def sb_paginate(base_path: str, page_size: int = 1000) -> list:
    """Пагинированный GET — обходит лимит Supabase 1000 строк."""
    result, offset = [], 0
    sep = "&" if "?" in base_path else "?"
    while True:
        chunk = sb_safe(f"{base_path}{sep}limit={page_size}&offset={offset}")
        if not chunk:
            break
        result.extend(chunk)
        if len(chunk) < page_size:
            break
        offset += page_size
    return result


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
# BETSAPI DEBUG
# -------------------
@app.get("/debug/betsapi")
def debug_betsapi():
    """Проверяет BetsAPI: токен, sport_id, первые events."""
    import urllib.parse
    token = os.getenv("BETSAPI_TOKEN", "")
    if not token:
        return {"error": "BETSAPI_TOKEN не задан в Railway env"}

    results = {}
    # Проверяем несколько sport_id для Dota 2
    for sid in [151, 12, 161]:
        try:
            url = f"https://api.betsapi.com/v3/events/upcoming?token={token}&sport_id={sid}&page=1"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=10).read().decode()
            data = json.loads(raw)
            events = data.get("results", [])
            results[f"sport_{sid}"] = {
                "total": data.get("pager", {}).get("total", "?"),
                "returned": len(events),
                "sample": [
                    {"home": e.get("home",{}).get("name"), "away": e.get("away",{}).get("name"),
                     "time": e.get("time"), "league": e.get("league",{}).get("name")}
                    for e in events[:5]
                ],
            }
        except Exception as ex:
            results[f"sport_{sid}"] = {"error": str(ex)}

    return {"token_prefix": token[:6] + "...", "betsapi": results}


# -------------------
# LIVE DASHBOARD
# -------------------
def _fetch_haglund_schedule(hours: int = 72):
    """Получает расписание матчей из haglund.dev на ближайшие N часов."""
    try:
        import urllib.parse
        from datetime import timedelta
        req = urllib.request.Request(
            "https://dota.haglund.dev/v1/matches",
            headers={"User-Agent": "DotaTrader/2.0"}
        )
        raw = urllib.request.urlopen(req, timeout=10).read().decode()
        matches = json.loads(raw)
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)
        result = []
        for m in matches:
            sa = m.get("startsAt")
            if not sa:
                continue
            try:
                ts = datetime.fromisoformat(sa.replace("Z", "+00:00"))
            except Exception:
                continue
            if now <= ts <= cutoff:
                teams = m.get("teams") or [{}, {}]
                t1 = (teams[0] or {}).get("name", "TBD")
                t2 = (teams[1] or {}).get("name", "TBD")
                if t1 == "TBD" or t2 == "TBD":
                    continue
                result.append({
                    "startsAt": sa,
                    "ts": ts,
                    "t1": t1, "t2": t2,
                    "matchType": m.get("matchType", ""),
                    "league": (m.get("leagueName") or ""),
                })
        result.sort(key=lambda x: x["ts"])
        return result
    except Exception:
        return []


def _market_label(bet_market):
    m = (bet_market or "moneyline").lower()
    if m == "series":       return "📊 Серия"
    if m == "map1":         return "🗺️ Карта 1"
    if m == "map2":         return "🗺️ Карта 2"
    if m == "map3":         return "🗺️ Карта 3"
    if m == "kills":        return "⚔️ Тотал убийств"
    if m == "duration":     return "⏱️ Длительность"
    return "💰 Монейлайн"


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    from datetime import timedelta
    from collections import defaultdict

    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    START_BANK = 1000.0

    # ── ВСЕ settled ставки (BACKTEST + LIVE) ─────────────────────────────────
    # Единая история: division=BACKTEST (симуляция 3 мес) + division=FREE (live)
    all_settled = sb_paginate(
        "elo_paper_bets"
        "?strategy_name=in.(AUTO_ELO_FLAT,SIM_3M)"
        "&settled=eq.true"
        "&stake_usd=gt.0"
        "&select=pnl,outcome,stake_usd,run_ts,home_team,away_team,bet_team,"
        "real_odds,odds,composite_prob,clv,edge,bet_market,form_score,h2h_score,division"
        "&order=run_ts.asc"
    )

    # ── Статистика (все settled) ─────────────────────────────────────────────
    wins_all    = sum(1 for b in all_settled if b.get("outcome") == "win")
    losses_all  = sum(1 for b in all_settled if b.get("outcome") == "loss")
    staked_all  = sum(float(b.get("stake_usd") or 0) for b in all_settled)
    pnl_all     = round(sum(float(b.get("pnl") or 0) for b in all_settled), 2)
    winrate_all = round(wins_all / len(all_settled) * 100, 1) if all_settled else 0
    roi_all     = round(pnl_all / staked_all * 100, 2) if staked_all > 0 else 0
    curr_bank   = round(START_BANK + pnl_all, 2)
    total_pnl   = pnl_all
    roi_pct     = roi_all

    # Дата первой и последней ставки
    hist_from = all_settled[0]["run_ts"][:10] if all_settled else "—"
    hist_to   = all_settled[-1]["run_ts"][:10] if all_settled else "—"

    # Peak bankroll
    peak_bank = START_BANK
    _running  = START_BANK
    for b in all_settled:
        _running += float(b.get("pnl") or 0)
        peak_bank = max(peak_bank, _running)
    max_dd = round((peak_bank - curr_bank) / peak_bank * 100, 1) if peak_bank > 0 else 0

    # ── Последние 100 ставок (история, все div) ──────────────────────────────
    last100 = sb_safe(
        "elo_paper_bets"
        "?strategy_name=in.(AUTO_ELO_FLAT,SIM_3M)"
        "&stake_usd=gt.0"
        "&select=run_ts,home_team,away_team,bet_team,stake_usd,odds,real_odds,"
        "outcome,pnl,settled,league,bet_market,edge,composite_prob,form_score,"
        "h2h_score,kelly_f,division"
        "&order=run_ts.desc&limit=100"
    )

    # ── Живые ставки (не BACKTEST) ───────────────────────────────────────────
    live_settled = [b for b in all_settled if b.get("division") != "BACKTEST"]
    live_wins    = sum(1 for b in live_settled if b.get("outcome") == "win")
    live_losses  = sum(1 for b in live_settled if b.get("outcome") == "loss")
    live_pnl     = round(sum(float(b.get("pnl") or 0) for b in live_settled), 2)
    live_staked  = sum(float(b.get("stake_usd") or 0) for b in live_settled)
    live_wr      = round(live_wins / (live_wins + live_losses) * 100, 1) if (live_wins + live_losses) > 0 else 0
    live_roi     = round(live_pnl / live_staked * 100, 1) if live_staked > 0 else 0

    # ── Недельная статистика (7 дней, все div) ───────────────────────────────
    week_ago  = (now_utc - timedelta(days=7)).isoformat()
    week_bets = [b for b in all_settled
                 if (b.get("run_ts") or "") >= week_ago]
    w_staked  = sum(float(b.get("stake_usd") or 0) for b in week_bets)
    w_pnl     = round(sum(float(b.get("pnl") or 0) for b in week_bets), 2)
    w_earned  = sum(float(b.get("pnl") or 0) for b in week_bets if float(b.get("pnl") or 0) > 0)
    w_lost    = sum(float(b.get("pnl") or 0) for b in week_bets if float(b.get("pnl") or 0) < 0)
    w_count   = len(week_bets)
    w_wins    = sum(1 for b in week_bets if b.get("outcome") == "win")
    w_wr      = round(w_wins / w_count * 100, 1) if w_count > 0 else 0
    w_roi     = round(w_pnl / w_staked * 100, 1) if w_staked > 0 else 0

    # ── Ставки за сегодня ───────────────────────────────────────────────────
    today_start_str = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    today_bets = sb_safe(
        "elo_paper_bets"
        "?strategy_name=eq.AUTO_ELO_FLAT"
        "&or=(division.eq.FREE,division.is.null)"
        "&stake_usd=gt.0"
        f"&run_ts=gte.{today_start_str}"
        "&select=run_ts,home_team,away_team,bet_team,stake_usd,odds,real_odds,"
        "outcome,pnl,settled,start_time,league,bet_market,edge,composite_prob"
        "&order=run_ts.desc&limit=30"
    )
    # ── Пропущенные матчи за сегодня (все оценённые но не поставленные) ──────
    today_skipped = sb_safe(
        "elo_paper_bets"
        "?strategy_name=eq.AUTO_ELO_FLAT"
        "&or=(division.eq.FREE,division.is.null)"
        "&stake_usd=eq.0"
        f"&run_ts=gte.{today_start_str}"
        "&skip_reason=not.is.null"
        "&select=run_ts,home_team,away_team,bet_team,start_time,league,"
        "composite_prob,edge,real_odds,skip_reason,bet_market"
        "&order=start_time.asc&limit=50"
    )
    today_staked    = sum(float(b.get("stake_usd") or 0) for b in today_bets)
    today_pnl       = round(sum(float(b.get("pnl") or 0) for b in today_bets if b.get("settled")), 2)
    today_settled_n = sum(1 for b in today_bets if b.get("settled"))

    # ── Активные ставки (unsettled live) ─────────────────────────────────────
    active_bets = sb_safe(
        "elo_paper_bets"
        "?strategy_name=eq.AUTO_ELO_FLAT"
        "&division=neq.BACKTEST"
        "&settled=eq.false"
        "&stake_usd=gt.0"
        "&select=start_time,home_team,away_team,bet_team,stake_usd,odds,real_odds,"
        "league,bet_market,edge,composite_prob,form_score,h2h_score"
        "&order=start_time.asc&limit=30"
    )

    # ── Расписание haglund.dev (72ч) ─────────────────────────────────────────
    upcoming_schedule = _fetch_haglund_schedule(72)

    # ── Tracking rows (оценены пайплайном, ставки нет — нет реальных одсов) ──
    tracking_rows = sb_safe(
        "elo_paper_bets"
        "?strategy_name=eq.AUTO_ELO_FLAT"
        "&division=neq.BACKTEST"
        "&settled=eq.false"
        "&stake_usd=eq.0"
        "&select=home_team,away_team,composite_prob,model_prob,form_score,"
        "h2h_score,bet_team,league_tier,run_ts"
        "&order=run_ts.desc&limit=200"
    )
    def _nt2(s): return re.sub(r"[^a-z0-9]", "", (s or "").lower())
    tracking_map: dict = {}
    for _tr in (tracking_rows or []):
        _key = (_nt2(_tr.get("home_team", "")), _nt2(_tr.get("away_team", "")))
        if _key not in tracking_map:
            tracking_map[_key] = _tr

    # ── Model config ─────────────────────────────────────────────────────────
    cfg_rows = sb_safe("model_config?select=key,value")
    cfg      = {r["key"]: r["value"] for r in cfg_rows} if cfg_rows else {}
    def _pct(v, d=0):
        try: return f"{float(v)*100:.{d}f}%"
        except: return v
    w_elo    = _pct(cfg.get("w_elo", "0.60"))
    w_form   = _pct(cfg.get("w_form", "0.25"))
    w_h2h    = _pct(cfg.get("w_h2h", "0.15"))
    cal_at   = cfg.get("calibrated_at", "—")
    cal_n    = cfg.get("calibration_n", "—")
    cal_loss = cfg.get("calibration_logloss", "—")
    cal_brier= cfg.get("calibration_brier", "—")
    cal_acc  = cfg.get("calibration_acc", "—")

    brier_val = "—"
    clv_avg   = "—"
    try:
        bd = [(float(b["composite_prob"]), 1 if b["outcome"] == "win" else 0)
              for b in all_settled if b.get("composite_prob") and b.get("outcome")]
        if bd:
            brier_val = round(sum((p - y) ** 2 for p, y in bd) / len(bd), 4)
        cd = [float(b["clv"]) for b in all_settled if b.get("clv")]
        if cd:
            clv_avg = round(sum(cd) / len(cd) * 100, 2)
    except Exception:
        pass

    # ── График банка по дням (ВСЯ история) ───────────────────────────────────
    daily_pnl_map: dict = defaultdict(float)
    for b in all_settled:
        try:
            raw_ts = b.get("run_ts") or ""
            dt     = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            dk     = dt.strftime("%Y-%m-%d")
            daily_pnl_map[dk] += float(b.get("pnl") or 0)
        except Exception:
            pass
    sorted_days = sorted(daily_pnl_map.keys())
    chart_points = []
    running = START_BANK
    if sorted_days:
        chart_points.append(("старт", START_BANK))
    for d in sorted_days:
        running += daily_pnl_map[d]
        chart_points.append((d[5:], round(running, 2)))  # MM-DD

    # SVG chart (700×160)
    def build_chart(points):
        if not points:
            return '<div style="text-align:center;color:var(--muted);padding:40px 0;font-size:13px">Недостаточно данных для графика (нужны settled ставки)</div>'
        W, H, PAD = 680, 140, 10
        vals = [p[1] for p in points]
        mn, mx = min(min(vals), START_BANK) - 20, max(max(vals), START_BANK) + 20
        rng = mx - mn or 1
        def px(v): return H - PAD - (v - mn) / rng * (H - 2*PAD)
        def xi(i): return PAD + i * (W - 2*PAD) / max(len(points)-1, 1)
        baseline = px(START_BANK)
        pts = " ".join(f"{xi(i):.1f},{px(v):.1f}" for i,(l,v) in enumerate(points))
        path = "M " + " L ".join(f"{xi(i):.1f},{px(v):.1f}" for i,(l,v) in enumerate(points))
        fill_path = f"{path} L {xi(len(points)-1):.1f},{H-PAD} L {PAD},{H-PAD} Z"
        col = "#3ddc84" if vals[-1] >= START_BANK else "#ff5c5c"
        labels_html = ""
        step = max(1, len(points)//8)
        for i,(l,v) in enumerate(points):
            if i % step == 0 or i == len(points)-1:
                labels_html += f'<text x="{xi(i):.1f}" y="{H+4}" fill="#8a8f98" font-size="9" text-anchor="middle">{l}</text>'
        return f'''<svg viewBox="0 0 {W} {H+16}" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-height:160px">
  <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="{col}" stop-opacity="0.3"/>
    <stop offset="100%" stop-color="{col}" stop-opacity="0"/>
  </linearGradient></defs>
  <line x1="{PAD}" y1="{baseline:.1f}" x2="{W-PAD}" y2="{baseline:.1f}" stroke="#2a2e38" stroke-width="1" stroke-dasharray="4,3"/>
  <path d="{fill_path}" fill="url(#g)"/>
  <polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2" stroke-linejoin="round"/>
  {"".join(f'<circle cx="{xi(i):.1f}" cy="{px(v):.1f}" r="3" fill="{col}"/>' for i,(l,v) in enumerate(points))}
  {labels_html}
  <text x="{PAD}" y="14" fill="#8a8f98" font-size="9">${mx-20:.0f}</text>
  <text x="{PAD}" y="{H-PAD:.0f}" fill="#8a8f98" font-size="9">${mn+20:.0f}</text>
</svg>'''

    chart_svg = build_chart(chart_points)

    # ── HTML helpers ─────────────────────────────────────────────────────────
    def pc(v):
        try: return "pos" if float(v) >= 0 else "neg"
        except: return ""

    def fp(v):
        try: return f"${float(v):+.2f}"
        except: return "—"

    def ff(v, d=1):
        try: return f"{float(v):.{d}f}"
        except: return "—"

    def fmt_ts(ts):
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d.%m %H:%M")
        except Exception:
            return "—"

    def oc_badge(o, settled):
        if not settled: return '<span class="muted">⏳</span>'
        if o == "win":  return '<span class="pos">✓ WIN</span>'
        if o == "loss": return '<span class="neg">✗ LOSS</span>'
        return "—"

    def clean_league(raw):
        """Конвертирует Liquipedia-путь в читаемое название."""
        if not raw:
            return ""
        # Убираем "#fragment" и лишние части
        s = raw.split("#")[0].strip()
        # Если это путь типа "The International/2026/Europe/Closed Qualifier"
        # берём последние 2 части
        parts = [p.strip() for p in s.split("/") if p.strip()]
        if len(parts) >= 3:
            return parts[0] + " · " + " · ".join(parts[2:])
        return " · ".join(parts) if parts else s

    # ── Today bets HTML ───────────────────────────────────────────────────────
    today_wins    = sum(1 for b in today_bets if b.get("outcome") == "win")
    today_losses  = sum(1 for b in today_bets if b.get("outcome") == "loss")
    today_pending = sum(1 for b in today_bets if not b.get("settled"))
    now_ts        = now_utc.timestamp()

    if today_bets:
        td_rows = ""
        for b in today_bets:
            home  = b.get("home_team", "?")
            away  = b.get("away_team", "?")
            side  = b.get("bet_team", "home")
            fav   = home if side == "home" else away
            stake = ff(b.get("stake_usd"))
            _ro   = b.get("real_odds") or b.get("odds")
            odds  = ff(_ro, 2) if _ro else "—"
            start = fmt_ts(b.get("start_time")) if b.get("start_time") else "—"
            oc    = oc_badge(b.get("outcome"), b.get("settled"))
            pnl_v = fp(b.get("pnl")) if b.get("settled") else "⏳"
            pcls  = pc(b.get("pnl") or 0) if b.get("settled") else "muted"
            mkt   = _market_label(b.get("bet_market"))
            edg   = f"{float(b['edge'])*100:+.1f}%" if b.get("edge") is not None else "—"
            cpb   = ff(b.get("composite_prob"), 3) if b.get("composite_prob") else "—"
            # in-play flag
            st_ts = b.get("start_time")
            inplay = st_ts and float(st_ts) < now_ts
            ip_badge = ' <span style="background:#ff5c5c;color:#fff;font-size:10px;padding:1px 5px;border-radius:4px;font-weight:700">LIVE</span>' if inplay else ""
            td_rows += f"""<tr>
              <td class="muted">{start} UTC{ip_badge}</td>
              <td><b>{home} vs {away}</b><br><small class="muted">{clean_league(b.get("league",""))}</small></td>
              <td><span style="color:var(--accent);font-weight:600">→ {fav}</span><br><small class="muted">{mkt}</small></td>
              <td><b>${stake}</b> <span class="muted">@ {odds}</span><br>
                  <small class="muted">edge {edg} · p={cpb}</small></td>
              <td>{oc}</td>
              <td class="{pcls}">{pnl_v}</td>
            </tr>"""
        pnl_color = "pos" if today_pnl >= 0 else "neg"
        today_html = f"""<div class="card">
  <h2>📅 Отчёт за день — {now_utc.strftime("%d.%m.%Y")}</h2>
  <div style="display:flex;gap:24px;margin-bottom:14px;flex-wrap:wrap">
    <div class="stat"><div class="val">{len(today_bets)}</div><div class="lbl">Ставок</div></div>
    <div class="stat"><div class="val">${today_staked:,.0f}</div><div class="lbl">Поставлено</div></div>
    <div class="stat"><div class="val pos">{today_wins}W</div><div class="lbl">Победы</div></div>
    <div class="stat"><div class="val neg">{today_losses}L</div><div class="lbl">Поражения</div></div>
    <div class="stat"><div class="val muted">{today_pending}⏳</div><div class="lbl">Ожидают</div></div>
    <div class="stat"><div class="val {pnl_color}">{fp(today_pnl)}</div><div class="lbl">P&amp;L сегодня</div></div>
  </div>
  <table><thead><tr>
    <th>Начало UTC</th><th>Матч / Турнир</th><th>Ставка / Тип</th>
    <th>Сумма @ Коэф / Сигналы</th><th>Итог</th><th>P&amp;L</th>
  </tr></thead><tbody>{td_rows}</tbody></table>
</div>"""
    else:
        today_html = f"""<div class="card">
  <h2>📅 Отчёт за день — {now_utc.strftime("%d.%m.%Y")}</h2>
  <p class="empty">Сегодня ставок ещё не было — пайплайн запускается каждые 10 мин</p>
</div>"""

    # ── Пропущенные матчи HTML ────────────────────────────────────────────────
    if today_skipped:
        sk_rows = ""
        for b in today_skipped:
            home   = b.get("home_team", "?")
            away   = b.get("away_team", "?")
            side   = b.get("bet_team", "home")
            fav    = home if side == "home" else away
            start  = fmt_ts(b.get("start_time")) if b.get("start_time") else "—"
            st_ts  = b.get("start_time")
            inplay = st_ts and float(st_ts) < now_ts
            ip_badge = ' <span style="background:#ff5c5c;color:#fff;font-size:10px;padding:1px 5px;border-radius:4px;font-weight:700">LIVE</span>' if inplay else ""
            cpb    = f"{float(b['composite_prob']):.0%}" if b.get("composite_prob") else "—"
            edg    = f"{float(b['edge'])*100:+.1f}%" if b.get("edge") is not None else "—"
            ro     = ff(b.get("real_odds"), 2) if b.get("real_odds") else "нет"
            reason = b.get("skip_reason") or "—"
            sk_rows += f"""<tr>
              <td class="muted">{start} UTC{ip_badge}</td>
              <td><b>{home} vs {away}</b><br><small class="muted">{clean_league(b.get("league",""))}</small></td>
              <td style="color:var(--accent)">{fav}</td>
              <td><small>p={cpb} · edge {edg} · odds {ro}</small></td>
              <td><span style="color:var(--neg);font-size:12px">✗ {reason}</span></td>
            </tr>"""
        skipped_html = f"""<div class="card">
  <h2>🔍 Пропущенные матчи сегодня — {len(today_skipped)} оценено, не поставлено</h2>
  <table><thead><tr>
    <th>Начало UTC</th><th>Матч / Турнир</th><th>Фаворит (Elo)</th>
    <th>Сигнал</th><th>Причина пропуска</th>
  </tr></thead><tbody>{sk_rows}</tbody></table>
</div>"""
    else:
        skipped_html = ""

    # ── Active bets + 72h schedule HTML ──────────────────────────────────────
    ab_rows = ""
    if active_bets:
        for b in active_bets:
            ts   = fmt_ts(b.get("start_time")) if b.get("start_time") else "—"
            home = b.get("home_team", "?")
            away = b.get("away_team", "?")
            side = b.get("bet_team", "home")
            fav  = home if side == "home" else away
            stake= ff(b.get("stake_usd"))
            _ro  = b.get("real_odds") or b.get("odds")
            odds = ff(_ro, 2) if _ro else "—"
            mkt  = _market_label(b.get("bet_market"))
            edg  = f"{float(b['edge'])*100:+.1f}%" if b.get("edge") is not None else "—"
            cpb  = ff(b.get("composite_prob"), 3) if b.get("composite_prob") else "—"
            frm  = ff(b.get("form_score"), 3) if b.get("form_score") else "—"
            h2h  = ff(b.get("h2h_score"), 3) if b.get("h2h_score") else "—"
            ab_rows += f"""<tr style="background:rgba(91,140,255,.05)">
              <td class="muted">{ts} UTC</td>
              <td><b>{home} vs {away}</b><br><small class="muted">{clean_league(b.get("league",""))}</small></td>
              <td><span style="color:var(--accent);font-weight:600">→ {fav}</span><br>
                  <small class="muted">{mkt}</small></td>
              <td><b>${stake}</b> @ {odds}<br>
                  <small class="muted">edge {edg} · p={cpb} · form={frm} · H2H={h2h}</small></td>
            </tr>"""

    # Upcoming schedule from haglund.dev
    sch_rows = ""
    placed_keys = {(b.get("home_team",""), b.get("away_team","")) for b in active_bets}
    for m in upcoming_schedule:
        k = (m["t1"], m["t2"])
        tk = (_nt2(m["t1"]), _nt2(m["t2"]))
        tr_row = tracking_map.get(tk)
        already = "✓ В игре" if k in placed_keys else "⏳ Ожидает"
        color   = "pos" if k in placed_keys else "muted"
        ts_str  = m["ts"].strftime("%d.%m %H:%M")
        # Signal data from tracking row
        sig_html = ""
        if tr_row and k not in placed_keys:
            cp  = tr_row.get("composite_prob")
            mp  = tr_row.get("model_prob")
            frm = tr_row.get("form_score")
            h2h = tr_row.get("h2h_score")
            side = "дом." if tr_row.get("bet_team") == "home" else "гость"
            parts = []
            if mp  is not None: parts.append(f"elo={float(mp):.2f}")
            if cp  is not None: parts.append(f"p={float(cp):.2f}")
            if frm is not None: parts.append(f"form={float(frm):.2f}")
            if h2h is not None: parts.append(f"H2H={float(h2h):.2f}")
            if parts:
                sig_html = f'<br><small class="muted">🔍 модель {side}: {" · ".join(parts)}</small>'
        sch_rows += f"""<tr>
          <td class="muted">{ts_str} UTC</td>
          <td><b>{m["t1"]} vs {m["t2"]}</b><br>
              <small class="muted">{m["matchType"]} · {clean_league(m["league"])}</small></td>
          <td>{sig_html if sig_html else '<span class="muted">нет сигнала</span>'}</td>
          <td class="{color}">{already}</td>
        </tr>"""

    active_inner = ab_rows + sch_rows if (ab_rows or sch_rows) else ""
    if active_inner:
        active_html = f"""<div class="card">
  <h2>⏳ Активные ставки и расписание — ближайшие 72ч</h2>
  <table><thead><tr>
    <th>Начало UTC</th><th>Матч / Тип</th><th>Ставка / Сигналы</th><th>Статус</th>
  </tr></thead><tbody>{active_inner}</tbody></table>
</div>"""
    else:
        active_html = f"""<div class="card">
  <h2>⏳ Активные ставки — ближайшие 72ч</h2>
  <p class="empty">Нет матчей в расписании или все ставки уже сделаны (пайплайн каждые 10 мин)</p>
</div>"""

    # ── History rows HTML (последние 100, первые 15 видны) ───────────────────
    hist_rows = ""
    for i, b in enumerate(last100):
        ts    = (b.get("run_ts") or "")[:16].replace("T", " ")
        home  = b.get("home_team", "?")
        away  = b.get("away_team", "?")
        side  = b.get("bet_team", "home")
        fav   = home if side == "home" else away
        stake = ff(b.get("stake_usd"))
        _ro   = b.get("real_odds") or b.get("odds")
        odds  = ff(_ro, 2) if _ro else "—"
        oc    = oc_badge(b.get("outcome"), b.get("settled"))
        pnl_v = fp(b.get("pnl")) if b.get("settled") else "—"
        pcls  = pc(b.get("pnl") or 0) if b.get("settled") else ""
        mkt   = _market_label(b.get("bet_market"))
        edg   = f"{float(b['edge'])*100:+.1f}%" if b.get("edge") is not None else "—"
        cpb   = ff(b.get("composite_prob"), 3) if b.get("composite_prob") else "—"
        div_badge = '<span style="font-size:9px;color:#5b8cff;border:1px solid #5b8cff;border-radius:3px;padding:0 3px">BT</span> ' if b.get("division") == "BACKTEST" else ""
        extra = ' class="hist-extra" style="display:none"' if i >= 15 else ''
        hist_rows += f"""<tr{extra}>
          <td class="muted">{ts}</td>
          <td>{div_badge}<b>{home} vs {away}</b><br>
              <small class="muted">{clean_league(b.get("league",""))}</small></td>
          <td><span style="color:var(--accent)">→ {fav}</span><br>
              <small class="muted">{mkt}</small></td>
          <td>${stake} <span class="muted">@ {odds}</span><br>
              <small class="muted">edge {edg} · p={cpb}</small></td>
          <td>{oc}</td>
          <td class="{pcls}">{pnl_v}</td>
        </tr>"""

    if not hist_rows:
        hist_rows = '<tr><td colspan="6" class="empty">Нет ставок — пайплайн ещё не поставил</td></tr>'

    show_more = ""
    if len(last100) > 15:
        show_more = f"""<div style="text-align:center;margin-top:12px">
  <button onclick="toggleHist(this)" style="background:var(--card);border:1px solid var(--border);
    color:var(--muted);padding:8px 20px;border-radius:8px;cursor:pointer;font-size:13px">
    Показать все {len(last100)} ставок ▼
  </button>
</div>"""

    cal_at_short = cal_at[:16].replace("T", " ") if cal_at and cal_at != "—" else cal_at

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="600">
<title>Dota Trader — Live Dashboard</title>
<style>
:root{{--bg:#0f1115;--card:#171a21;--border:#2a2e38;--text:#e6e8eb;
       --muted:#8a8f98;--pos:#3ddc84;--neg:#ff5c5c;--accent:#5b8cff;}}
*{{box-sizing:border-box;}}
body{{background:var(--bg);color:var(--text);
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      margin:0;padding:24px 32px;max-width:1200px;}}
h1{{font-size:22px;margin:0 0 4px;}}
.upd{{color:var(--muted);font-size:13px;margin-bottom:24px;}}
.card{{background:var(--card);border:1px solid var(--border);
       border-radius:12px;padding:20px 24px;margin-bottom:20px;}}
.card h2{{font-size:13px;margin:0 0 14px;color:var(--muted);
          text-transform:uppercase;letter-spacing:.05em;}}
.stats{{display:flex;gap:24px;flex-wrap:wrap;}}
.stat .val{{font-size:22px;font-weight:700;}}
.stat .lbl{{font-size:11px;color:var(--muted);margin-top:2px;}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:20px;}}
.grid3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:20px;}}
table{{width:100%;border-collapse:collapse;font-size:12.5px;}}
th{{text-align:left;color:var(--muted);font-weight:500;
     padding:7px 10px;border-bottom:1px solid var(--border);font-size:11px;}}
td{{padding:7px 10px;border-bottom:1px solid var(--border);vertical-align:middle;}}
tr:last-child td{{border-bottom:none;}}
.pos{{color:var(--pos);font-weight:600;}}
.neg{{color:var(--neg);font-weight:600;}}
.muted{{color:var(--muted);}}
.empty{{text-align:center;color:var(--muted);padding:24px;font-size:13px;}}
small{{font-size:11px;}}
.explain{{margin-top:14px;border-top:1px solid var(--border);padding-top:14px;}}
.explain dl{{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:12px;margin:0;}}
.explain dt{{color:var(--accent);font-weight:600;white-space:nowrap;}}
.explain dd{{color:var(--muted);margin:0;}}
</style>
</head>
<body>
<h1>🎮 Dota Trader — Live Dashboard</h1>
<div class="upd">Обновлено: {now_str} &nbsp;·&nbsp; авто-обновление каждые 10 мин</div>

<!-- ══ БЛОК 1: БАНК — ЕДИНАЯ ИСТОРИЯ (BACKTEST + LIVE) ════════════════════ -->
<div class="card">
  <h2>💰 Банк — AUTO_ELO_FLAT &nbsp;·&nbsp; {hist_from} → {hist_to} &nbsp;·&nbsp;
      {len(all_settled)} settled ставок</h2>
  <div class="stats">
    <div class="stat"><div class="val">${curr_bank:,.2f}</div><div class="lbl">Текущий банк ($1 000 старт)</div></div>
    <div class="stat"><div class="val {pc(total_pnl)}">{fp(total_pnl)}</div><div class="lbl">Итоговый P&amp;L</div></div>
    <div class="stat"><div class="val {pc(roi_pct)}">{roi_pct:+.1f}%</div><div class="lbl">ROI (весь период)</div></div>
    <div class="stat"><div class="val">{winrate_all}%</div><div class="lbl">Win rate ({wins_all}W/{losses_all}L)</div></div>
    <div class="stat"><div class="val">${staked_all:,.0f}</div><div class="lbl">Всего поставлено</div></div>
    <div class="stat"><div class="val">${peak_bank:,.0f}</div><div class="lbl">Пик банка</div></div>
    <div class="stat"><div class="val {pc(-max_dd)}">{max_dd:.1f}%</div><div class="lbl">Макс. просадка</div></div>
  </div>
  <div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--border)">
    <div class="stats">
      <div class="stat"><div class="val" style="font-size:16px;color:var(--accent)">{live_wins}W/{live_losses}L</div><div class="lbl">Live win/loss</div></div>
      <div class="stat"><div class="val {pc(live_pnl)}" style="font-size:16px">{fp(live_pnl)}</div><div class="lbl">Live P&amp;L</div></div>
      <div class="stat"><div class="val {pc(live_roi)}" style="font-size:16px">{live_roi:+.1f}%</div><div class="lbl">Live ROI</div></div>
      <div class="stat"><div class="val" style="font-size:16px">{live_wr}%</div><div class="lbl">Live win rate</div></div>
      <div style="font-size:11px;color:var(--muted);align-self:flex-end;padding-bottom:4px">
        BT = симуляция апр-июн на исторических данных (нотион. коэффы)<br>
        Без BT = живые ставки с реальными коэффами BetsAPI
      </div>
    </div>
  </div>
</div>

<!-- ══ БЛОК 2: НЕДЕЛЯ + СИГНАЛЫ МОДЕЛИ ═══════════════════════════════════ -->
<div class="grid2">
  <div class="card">
    <h2>📅 За последние 7 дней</h2>
    <div class="stats">
      <div class="stat"><div class="val">{w_count}</div><div class="lbl">Ставок</div></div>
      <div class="stat"><div class="val {pc(w_pnl)}">{fp(w_pnl)}</div><div class="lbl">P&amp;L</div></div>
      <div class="stat"><div class="val {pc(w_roi)}">{w_roi:+.1f}%</div><div class="lbl">ROI</div></div>
      <div class="stat"><div class="val">{w_wr}%</div><div class="lbl">Win rate</div></div>
    </div>
    <div class="stats" style="margin-top:14px">
      <div class="stat"><div class="val pos" style="font-size:16px">{fp(w_earned)}</div><div class="lbl">Выигрыши</div></div>
      <div class="stat"><div class="val neg" style="font-size:16px">{fp(w_lost)}</div><div class="lbl">Проигрыши</div></div>
      <div class="stat"><div class="val" style="font-size:16px">${w_staked:,.0f}</div><div class="lbl">Поставлено</div></div>
    </div>
  </div>
  <div class="card">
    <h2>🧠 Модель: сигналы и качество</h2>
    <div class="stats">
      <div class="stat"><div class="val">{w_elo}</div><div class="lbl">Вес Elo</div></div>
      <div class="stat"><div class="val">{w_form}</div><div class="lbl">Вес формы</div></div>
      <div class="stat"><div class="val">{w_h2h}</div><div class="lbl">Вес H2H</div></div>
      <div class="stat"><div class="val">{brier_val}</div><div class="lbl">Brier (live)</div></div>
      <div class="stat"><div class="val">{'—' if clv_avg == '—' else f'{clv_avg:+.2f}%'}</div><div class="lbl">Avg CLV</div></div>
    </div>
    <div style="font-size:11px;color:var(--muted);margin-top:10px">
      Стратегия: elo 60% + form 25% + H2H 15% + fatigue adj · Kelly fraction=0.25 ·
      edge-фильтр ≥2-5% (по тиру лиги) · дневной лимит 20% банка
    </div>
  </div>
</div>

<!-- ══ БЛОК 3: ГРАФИК РОСТА БАНКА ════════════════════════════════════════ -->
<div class="card">
  <h2>📈 Рост банка — по дням &nbsp;·&nbsp; {hist_from} → {hist_to}</h2>
  {chart_svg}
  <div style="font-size:11px;color:var(--muted);margin-top:8px">
    Пунктир — стартовый банк $1 000. Каждая точка — конец дня.
    Серые точки = историческая симуляция [BT], цветные = live ставки.
  </div>
</div>

<!-- ══ БЛОК 4: АКТИВНЫЕ СТАВКИ + РАСПИСАНИЕ 72Ч ══════════════════════════ -->
{active_html}

<!-- ══ БЛОК 5: ОТЧЁТ ЗА ДЕНЬ ════════════════════════════════════════════ -->
{today_html}

<!-- ══ БЛОК 5б: ПРОПУЩЕННЫЕ МАТЧИ ══════════════════════════════════════ -->
{skipped_html}

<!-- ══ БЛОК 6: ИСТОРИЯ СТАВОК ════════════════════════════════════════════ -->
<div class="card">
  <h2>🎯 История ставок &nbsp;·&nbsp; {len(last100)} последних</h2>
  <div style="font-size:11px;color:var(--muted);margin-bottom:10px">
    <span style="color:#5b8cff;border:1px solid #5b8cff;border-radius:3px;padding:0 3px;font-size:9px">BT</span>
    = Backtest (нотион. коэффы, симуляция) &nbsp;·&nbsp; без значка = live ставка (реальные коэффы BetsAPI)
  </div>
  <table>
    <thead><tr>
      <th>Время</th><th>Матч / Турнир</th><th>Ставка / Тип рынка</th>
      <th>Сумма @ Коэф / Edge · Prob</th><th>Итог</th><th>P&amp;L</th>
    </tr></thead>
    <tbody>{hist_rows}</tbody>
  </table>
  {show_more}
</div>

<script>
function toggleHist(btn) {{
  var rows = document.querySelectorAll('.hist-extra');
  var hidden = rows[0] && rows[0].style.display === 'none';
  rows.forEach(function(r){{ r.style.display = hidden ? '' : 'none'; }});
  btn.textContent = hidden ? 'Скрыть ▲' : 'Показать все {len(last100)} ставок ▼';
}}
</script>
</body>
</html>"""
    return HTMLResponse(content=html)
