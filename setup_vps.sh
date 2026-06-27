#!/bin/bash
# ══════════════════════════════════════════════════════════
#  Dota Trader v2 — полная установка на Ubuntu VPS
#  Запуск: bash setup_vps.sh
#
#  Что будет работать 24/7:
#    - elo_auto_settle.py   каждые 10 мин (завершает ставки)
#    - elo_auto_bet.py      каждые 10 мин (принимает новые ставки)
#    - playwright_bettor.py каждые 5 мин  (ставки на Pinco)
#    - git pull             каждые 30 мин (обновляет скрипты)
# ══════════════════════════════════════════════════════════

set -e

REPO_DIR="$HOME/dota-trader"
REPO_URL="https://github.com/rivaldirector/dota-trader-cloud.git"
LOG_DIR="$HOME/logs"

echo ""
echo "════════════════════════════════════════════════════"
echo "   Dota Trader v2 — установка на VPS"
echo "════════════════════════════════════════════════════"
echo ""

# ── 1. Системные зависимости ──────────────────────────────
echo "[1/8] Устанавливаем системные пакеты..."
sudo apt-get update -q
sudo apt-get install -y python3 python3-pip git curl \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 xvfb 2>/dev/null || \
sudo apt-get install -y python3 python3-pip git curl

# ── 2. Python пакеты ──────────────────────────────────────
echo "[2/8] Устанавливаем Python пакеты..."
pip3 install playwright python-dotenv requests fastapi uvicorn -q

# ── 3. Playwright Chromium ────────────────────────────────
echo "[3/8] Устанавливаем Chromium..."
playwright install chromium 2>/dev/null || python3 -m playwright install chromium

# ── 4. Репозиторий ────────────────────────────────────────
echo "[4/8] Настраиваем репозиторий..."
if [ -d "$REPO_DIR/.git" ]; then
    echo "  Репозиторий существует, обновляем..."
    cd "$REPO_DIR" && git pull --ff-only origin main 2>/dev/null || true
else
    git clone "$REPO_URL" "$REPO_DIR"
fi
mkdir -p "$LOG_DIR"
echo "  Репозиторий: $REPO_DIR"

# ── 5. .env файл ─────────────────────────────────────────
echo "[5/8] Проверяем .env..."
ENV_FILE="$REPO_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << 'EOF'
SUPABASE_URL=https://xplqjpftwvtbxmpsddor.supabase.co
SUPABASE_ANON_KEY=ВСТАВЬ_КЛЮЧ_ЗДЕСЬ
BETSAPI_TOKEN=ВСТАВЬ_ТОКЕН_ЗДЕСЬ
START_BANK=1000
CURRENCY=USD
TRADING_MODE=paper
EOF
    echo "  ⚠ Создан шаблон .env — нужно заполнить ключи!"
else
    echo "  .env найден"
fi

# ── 6. Pipeline runner ────────────────────────────────────
echo "[6/8] Создаём скрипты запуска..."
cat > "$REPO_DIR/run_pipeline.sh" << 'SCRIPT'
#!/bin/bash
# Запускается каждые 10 минут
REPO="$HOME/dota-trader"
LOG="$HOME/logs/pipeline.log"
TS=$(date -u '+%Y-%m-%d %H:%M:%S')

cd "$REPO"
export $(grep -v '^#' .env | xargs) 2>/dev/null
export SUPABASE_SERVICE_KEY="${SUPABASE_SERVICE_KEY:-$SUPABASE_ANON_KEY}"

{ echo ""
  echo "═══ $TS UTC ═══"
  echo "[settle] запуск..."
  timeout 120 python3 scripts/elo_auto_settle.py && echo "[settle] OK" || echo "[settle] ERROR"
  echo "[bet] запуск..."
  timeout 120 python3 scripts/elo_auto_bet.py && echo "[bet] OK" || echo "[bet] ERROR"
} >> "$LOG" 2>&1

# Ротируем если лог > 5 MB
[ $(stat -c%s "$LOG" 2>/dev/null || echo 0) -gt 5242880 ] && \
    mv "$LOG" "${LOG}.$(date +%Y%m%d)" && gzip "${LOG}.$(date +%Y%m%d)" || true
SCRIPT
chmod +x "$REPO_DIR/run_pipeline.sh"

cat > "$REPO_DIR/run_bettor.sh" << 'SCRIPT'
#!/bin/bash
# Запускается каждые 5 минут — ставки на Pinco
REPO="$HOME/dota-trader"
LOG="$HOME/logs/bettor.log"
SESSION="$REPO/.pinco_session.json"

[ ! -f "$SESSION" ] && exit 0  # нет сессии — пропускаем

cd "$REPO"
export $(grep -v '^#' .env | xargs) 2>/dev/null

{ echo "── $(date -u '+%Y-%m-%d %H:%M:%S') UTC ──"
  timeout 120 python3 scripts/playwright_bettor.py && echo "OK" || echo "ERROR"
} >> "$LOG" 2>&1
SCRIPT
chmod +x "$REPO_DIR/run_bettor.sh"

cat > "$REPO_DIR/run_git_pull.sh" << 'SCRIPT'
#!/bin/bash
# Подтягивает обновления скриптов каждые 30 мин
cd "$HOME/dota-trader"
echo "$(date -u '+%Y-%m-%d %H:%M') git pull" >> "$HOME/logs/git_pull.log"
git pull --ff-only origin main >> "$HOME/logs/git_pull.log" 2>&1 || true
SCRIPT
chmod +x "$REPO_DIR/run_git_pull.sh"

# ── 7. Cron ───────────────────────────────────────────────
echo "[7/8] Настраиваем cron..."
(crontab -l 2>/dev/null | grep -v "dota-trader\|run_pipeline\|run_bettor\|run_git_pull"; \
 echo "*/10 * * * * $REPO_DIR/run_pipeline.sh"; \
 echo "*/5  * * * * $REPO_DIR/run_bettor.sh"; \
 echo "*/30 * * * * $REPO_DIR/run_git_pull.sh") | crontab -
echo "  3 cron-задачи установлены"

# ── 8. Тест ───────────────────────────────────────────────
echo "[8/8] Тестовый прогон..."
if grep -q "ВСТАВЬ" "$ENV_FILE" 2>/dev/null; then
    echo "  ⚠ Пропускаем — .env не заполнен"
else
    bash "$REPO_DIR/run_pipeline.sh" && echo "  Пайплайн OK" || echo "  Ошибка — см. $LOG_DIR/pipeline.log"
fi

# ── Итог ──────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════"
echo "  ✓ Установка завершена!"
echo "════════════════════════════════════════════════════"
echo ""
echo "  Репозиторий:  $REPO_DIR"
echo "  Логи:         $LOG_DIR/"
echo ""
echo "  Расписание:"
echo "    каждые 5 мин  → Pinco-ставки (playwright)"
echo "    каждые 10 мин → settle + bet (Supabase)"
echo "    каждые 30 мин → git pull (обновление скриптов)"
echo ""

if grep -q "ВСТАВЬ" "$ENV_FILE" 2>/dev/null; then
    echo "  ⚠  СЛЕДУЮЩИЙ ШАГ — заполни ключи:"
    echo "     nano $ENV_FILE"
    echo ""
    echo "     SUPABASE_ANON_KEY — из Supabase Dashboard → Project Settings → API"
    echo "     BETSAPI_TOKEN     — 257106-TJjBvfrWh9NSuF"
    echo ""
fi

echo "  Для Pinco (один раз, на Mac):"
echo "    python3 scripts/playwright_bettor.py --login"
echo "    scp .pinco_session.json root@VPS_IP:$REPO_DIR/"
echo ""
echo "  Логи в реальном времени:"
echo "    tail -f $LOG_DIR/pipeline.log"
echo "    tail -f $LOG_DIR/bettor.log"
echo ""
