# Dota Trader MVP v2

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
