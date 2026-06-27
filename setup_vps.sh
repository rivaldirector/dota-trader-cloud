#!/bin/bash
# Установка бота на Ubuntu VPS (Ubuntu 22.04+)
# Запуск: bash setup_vps.sh

set -e
echo "=== Установка Dota Trader Playwright Bot ==="

# 1. Системные зависимости
sudo apt-get update -q
sudo apt-get install -y python3 python3-pip git curl

# 2. Python пакеты
pip3 install playwright python-dotenv requests

# 3. Chromium через Playwright
playwright install chromium
playwright install-deps chromium

# 4. Клонируем/обновляем репозиторий
REPO_DIR="$HOME/dota-trader-cloud"
if [ -d "$REPO_DIR" ]; then
    echo "Обновляем репозиторий..."
    cd "$REPO_DIR" && git pull
else
    echo "Клонируем репозиторий..."
    git clone https://github.com/rivaldirector/dota-trader-cloud.git "$REPO_DIR"
    cd "$REPO_DIR"
fi

# 5. Создаём .env файл
ENV_FILE="$REPO_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "Создаём .env..."
    cat > "$ENV_FILE" <<EOF
SUPABASE_URL=https://xplqjpftwvtbxmpsddor.supabase.co
SUPABASE_ANON_KEY=ВСТАВЬ_КЛЮЧ_ЗДЕСЬ
EOF
    echo "⚠️  Заполни $ENV_FILE — вставь SUPABASE_ANON_KEY"
fi

# 6. Настраиваем cron — каждые 5 минут
CRON_CMD="*/5 * * * * cd $REPO_DIR && python3 scripts/playwright_bettor.py >> /tmp/bettor.log 2>&1"
(crontab -l 2>/dev/null | grep -v "playwright_bettor"; echo "$CRON_CMD") | crontab -

echo ""
echo "=== Установка завершена ==="
echo ""
echo "Следующие шаги:"
echo ""
echo "1. Заполни .env файл:"
echo "   nano $REPO_DIR/.env"
echo ""
echo "2. Сохрани сессию Pinco (один раз, нужен GUI или VNC):"
echo "   cd $REPO_DIR && python3 scripts/playwright_bettor.py --login"
echo ""
echo "3. Проверь что бот работает:"
echo "   cd $REPO_DIR && python3 scripts/playwright_bettor.py"
echo ""
echo "4. Логи крона:"
echo "   tail -f /tmp/bettor.log"
echo ""
echo "Бот будет автоматически запускаться каждые 5 минут."
