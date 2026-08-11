# Dota Trader MVP v2

## Робот по золоту и нефти

Отдельный модуль `commodities/` — сигналы BUY/SELL по золоту (GC=F) и нефти (CL=F)
с уровнями входа, стопа и цели, бэктестом и уведомлениями в Telegram:

```bash
python run_commodity_bot.py signals    # текущие сигналы
python run_commodity_bot.py backtest   # проверка стратегии на истории
python run_commodity_bot.py live       # цикл с уведомлениями
```

Подробности: [docs/COMMODITY_BOT.md](docs/COMMODITY_BOT.md)

Локальный paper-trading движок под Mac. Реальные ставки не делает.

## Команды

```bash
python3 main.py doctor
python3 main.py demo
python3 main.py report
python3 main.py reset
python3 main.py pandascore_upcoming
python3 main.py stake_test
```

## Что делает
- виртуальный банк $100;
- value-фильтр;
- риск-менеджмент;
- отчеты по PnL/ROI/winrate;
- заготовки под PandaScore и Stake odds;
- сохраняет сырые ответы API в SQLite.
