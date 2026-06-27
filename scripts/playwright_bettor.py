#!/usr/bin/env python3
"""
Playwright-бот для размещения ставок на Pinco.
Читает pending-ставки из Supabase (placed_on_site=false, stake_usd>0)
и размещает их на сайте букмекера.

Запуск:
  python3 scripts/playwright_bettor.py

Первый запуск (сохранение сессии):
  python3 scripts/playwright_bettor.py --login

Требования (VPS):
  pip install playwright python-dotenv requests
  playwright install chromium
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Конфиг ────────────────────────────────────────────────────────────────────
PINCO_URL        = "https://pinco.bet"                  # базовый URL
SESSION_FILE     = Path(__file__).parent.parent / ".pinco_session.json"
SUPABASE_URL     = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY     = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")
STRATEGY_NAME    = "AUTO_ELO_FLAT"
# Сколько секунд до начала матча ещё принимаем ставку (не ставим если матч начался > 30 мин назад)
BET_WINDOW_SECS  = 30 * 60
# Пауза между действиями (человекоподобная)
DELAY_MIN        = 0.8
DELAY_MAX        = 2.2

# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def sb_get(query: str) -> list:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{query}",
        headers=_sb_headers(), timeout=15,
    )
    r.raise_for_status()
    return r.json()

def sb_patch(table: str, row_id: int, data: dict) -> None:
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}?id=eq.{row_id}",
        headers=_sb_headers(),
        json=data, timeout=15,
    )
    r.raise_for_status()

# ── Человекоподобные задержки ─────────────────────────────────────────────────

def human_delay(mn=DELAY_MIN, mx=DELAY_MAX):
    time.sleep(random.uniform(mn, mx))

def human_type(page, selector: str, text: str):
    """Печатает текст посимвольно с задержками."""
    page.click(selector)
    for ch in text:
        page.keyboard.type(ch)
        time.sleep(random.uniform(0.05, 0.18))

# ── Получаем pending ставки из Supabase ───────────────────────────────────────

def get_pending_bets() -> list:
    now_ts = int(datetime.now(timezone.utc).timestamp())
    cutoff = now_ts - BET_WINDOW_SECS  # не ставим на матчи начавшиеся >30 мин назад

    rows = sb_get(
        "elo_paper_bets"
        f"?strategy_name=eq.{STRATEGY_NAME}"
        "&placed_on_site=eq.false"
        "&stake_usd=gt.0"
        "&settled=eq.false"
        f"&start_time=gte.{cutoff}"
        "&select=id,home_team,away_team,bet_team,stake_usd,real_odds,start_time,league,bet_market"
        "&order=start_time.asc&limit=10"
    )
    return rows

# ── Нормализация названий команд ──────────────────────────────────────────────

def normalize(name: str) -> str:
    return name.lower().strip().replace(".", "").replace("-", " ")

def names_match(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    # точное совпадение
    if na == nb:
        return True
    # одно содержит другое
    if na in nb or nb in na:
        return True
    # первые 5 букв совпадают
    if len(na) >= 5 and len(nb) >= 5 and na[:5] == nb[:5]:
        return True
    return False

# ── Основная логика Playwright ────────────────────────────────────────────────

def login_and_save_session():
    """Открывает браузер для ручного входа, сохраняет сессию."""
    from playwright.sync_api import sync_playwright
    print("Открываю браузер для входа в Pinco...")
    print("Войди вручную, затем нажми Enter в терминале.")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=100)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        page.goto(PINCO_URL)
        input("\n>>> Войди в аккаунт Pinco в браузере, затем нажми Enter здесь <<<\n")
        # Сохраняем cookies и storage
        storage = ctx.storage_state()
        SESSION_FILE.write_text(json.dumps(storage, ensure_ascii=False, indent=2))
        print(f"Сессия сохранена в {SESSION_FILE}")
        browser.close()


def place_bets_headless(bets: list) -> None:
    """Размещает список ставок в headless-режиме."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    if not SESSION_FILE.exists():
        print("Нет файла сессии. Сначала запусти: python3 playwright_bettor.py --login")
        sys.exit(1)

    storage = json.loads(SESSION_FILE.read_text())

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = browser.new_context(
            storage_state=storage,
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="ru-RU",
        )
        # Скрываем webdriver флаг
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        page = ctx.new_page()

        for bet in bets:
            try:
                _place_single_bet(page, bet)
            except PWTimeout as e:
                print(f"  [TIMEOUT] {bet['home_team']} vs {bet['away_team']}: {e}")
            except Exception as e:
                print(f"  [ERROR] {bet['home_team']} vs {bet['away_team']}: {e}")

        # Обновляем файл сессии (cookies могли обновиться)
        SESSION_FILE.write_text(json.dumps(ctx.storage_state(), indent=2))
        browser.close()


def _place_single_bet(page, bet: dict) -> None:
    """Размещает одну ставку на Pinco."""
    home      = bet["home_team"]
    away      = bet["away_team"]
    side      = bet["bet_team"]   # "home" или "away"
    target    = home if side == "home" else away
    stake     = float(bet["stake_usd"])
    start_ts  = int(bet.get("start_time") or 0)
    start_dt  = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%d.%m %H:%M")

    print(f"\n→ Ставка: {home} vs {away}  ({start_dt} UTC)")
    print(f"  На: {target}  Сумма: ${stake:.0f}")

    # 1. Ищем через поиск на сайте
    page.goto(f"{PINCO_URL}/ru/sports/esports/dota-2", wait_until="domcontentloaded")
    human_delay(1.5, 3.0)

    # 2. Ищем матч в списке событий
    # Пробуем найти по названию команды
    found = False
    try:
        # Ищем все события на странице
        events = page.locator("[class*='event'], [class*='match'], [class*='game']").all()
        for ev in events:
            text = ev.inner_text()
            if names_match(home, text) and names_match(away, text):
                ev.click()
                found = True
                human_delay()
                break
    except Exception:
        pass

    if not found:
        # Попробуем через поиск
        try:
            search_btn = page.locator("[class*='search'], [aria-label*='поиск'], [aria-label*='search']").first
            search_btn.click()
            human_delay()
            page.keyboard.type(home[:8], delay=120)
            human_delay(1.0, 2.0)
            # Ищем результат
            results = page.locator("[class*='search-result'], [class*='suggestion']").all()
            for r in results:
                if names_match(home, r.inner_text()) or names_match(away, r.inner_text()):
                    r.click()
                    found = True
                    human_delay()
                    break
        except Exception as e:
            print(f"  [search failed] {e}")

    if not found:
        print(f"  [SKIP] Матч не найден на сайте: {home} vs {away}")
        return

    # 3. Находим нужный коэфф (на нужную команду, рынок moneyline/победитель)
    human_delay()
    try:
        # Ищем блок с командами и кнопками коэффов
        outcome_found = False
        buttons = page.locator("button, [class*='odd'], [class*='outcome'], [class*='coefficient']").all()
        for btn in buttons:
            txt = btn.inner_text().strip()
            # Кнопка должна содержать имя команды ИЛИ быть рядом с ним
            parent_text = ""
            try:
                parent_text = btn.locator("..").inner_text()
            except Exception:
                pass
            if names_match(target, txt) or names_match(target, parent_text):
                btn.click()
                outcome_found = True
                human_delay()
                break

        if not outcome_found:
            print(f"  [SKIP] Кнопка ставки на '{target}' не найдена")
            return
    except Exception as e:
        print(f"  [SKIP] Ошибка выбора исхода: {e}")
        return

    # 4. Вводим сумму в купон
    human_delay(0.5, 1.5)
    try:
        stake_input = page.locator(
            "input[class*='stake'], input[class*='amount'], input[placeholder*='сумма'], "
            "input[placeholder*='ставка'], input[type='number']"
        ).first
        stake_input.click()
        stake_input.triple_click()
        human_delay(0.3, 0.7)
        page.keyboard.type(str(int(stake)), delay=100)
        human_delay(0.5, 1.2)
    except Exception as e:
        print(f"  [SKIP] Поле суммы не найдено: {e}")
        return

    # 5. Подтверждаем ставку
    try:
        confirm_btn = page.locator(
            "button[class*='confirm'], button[class*='place'], button[class*='submit'], "
            "button:has-text('Поставить'), button:has-text('Сделать ставку'), "
            "button:has-text('Подтвердить')"
        ).first
        human_delay(0.5, 1.0)
        confirm_btn.click()
        human_delay(1.5, 3.0)
    except Exception as e:
        print(f"  [SKIP] Кнопка подтверждения не найдена: {e}")
        return

    # 6. Проверяем успех (ищем сообщение об успехе)
    success = False
    try:
        page.wait_for_selector(
            "[class*='success'], [class*='placed'], :has-text('принята'), :has-text('размещена')",
            timeout=5000,
        )
        success = True
    except Exception:
        # Иногда нет явного сообщения — считаем что ок если нет ошибки
        error_el = page.locator("[class*='error'], [class*='danger'], :has-text('ошибка')").all()
        success = len(error_el) == 0

    if success:
        print(f"  ✓ Ставка размещена: {target}  ${stake:.0f}")
        sb_patch("elo_paper_bets", bet["id"], {
            "placed_on_site": True,
            "real_bookmaker": "Pinco",
        })
    else:
        print(f"  ✗ Не удалось подтвердить размещение ставки")


# ── Точка входа ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true",
                        help="Открыть браузер для ручного входа и сохранить сессию")
    args = parser.parse_args()

    if args.login:
        login_and_save_session()
        return

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Нет SUPABASE_URL / SUPABASE_KEY в .env")
        sys.exit(1)

    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] Проверяем pending ставки...")
    bets = get_pending_bets()

    if not bets:
        print("Нет новых ставок для размещения.")
        return

    print(f"Найдено {len(bets)} ставок для размещения на Pinco:")
    for b in bets:
        side = b['home_team'] if b['bet_team'] == 'home' else b['away_team']
        print(f"  • {b['home_team']} vs {b['away_team']} → {side}  ${b['stake_usd']:.0f}")

    place_bets_headless(bets)
    print("\nГотово.")


if __name__ == "__main__":
    main()
