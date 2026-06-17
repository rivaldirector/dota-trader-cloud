from collections import defaultdict
from rich.console import Console
from rich.table import Table
from storage.db import Database

console = Console()

def print_report(db: Database):
    bank_row = db.fetchone("SELECT bank, currency FROM bankroll WHERE id = 1")
    bank = float(bank_row["bank"]) if bank_row else 0.0
    currency = bank_row["currency"] if bank_row else "USD"
    bets = db.fetchall("SELECT * FROM bets ORDER BY id")
    closed = [b for b in bets if b["status"] == "CLOSED"]
    open_bets = [b for b in bets if b["status"] == "OPEN"]
    total_staked = sum(float(b["stake"]) for b in closed)
    total_profit = sum(float(b["profit"] or 0) for b in closed)
    wins = sum(1 for b in closed if b["result"] == "WIN")
    winrate = (wins / len(closed) * 100) if closed else 0
    roi = (total_profit / total_staked * 100) if total_staked else 0

    console.print(f"\n[bold]Dota Trader Report[/bold]")
    console.print(f"Bank: [bold]{bank:.2f} {currency}[/bold]")
    console.print(f"Closed bets: {len(closed)} | Open bets: {len(open_bets)}")
    console.print(f"Winrate: {winrate:.1f}% | ROI: {roi:.2f}% | PnL: {total_profit:+.2f} {currency}\n")

    by_market = defaultdict(lambda: {"n": 0, "profit": 0.0, "stake": 0.0, "wins": 0})
    for b in closed:
        m = by_market[b["market"]]
        m["n"] += 1
        m["profit"] += float(b["profit"] or 0)
        m["stake"] += float(b["stake"] or 0)
        m["wins"] += 1 if b["result"] == "WIN" else 0
    if by_market:
        mt = Table(title="Performance by Market")
        for col in ["Market", "Bets", "Winrate", "ROI", "PnL"]:
            mt.add_column(col)
        for market, s in by_market.items():
            mt.add_row(market, str(s["n"]), f'{s["wins"] / s["n"] * 100:.1f}%', f'{s["profit"] / s["stake"] * 100:.1f}%' if s["stake"] else "0.0%", f'{s["profit"]:+.2f}')
        console.print(mt)

    table = Table(title="Bets")
    for col in ["ID", "Match", "Market", "Pick", "Odds", "Model", "Edge", "Stake", "Status", "Result", "Profit"]:
        table.add_column(col)
    for b in bets[-30:]:
        table.add_row(
            str(b["id"]), b["match_name"], b["market"], b["selection"], f'{float(b["odds"]):.2f}',
            f'{float(b["model_probability"])*100:.1f}%', f'{float(b["edge"])*100:.1f}%', f'{float(b["stake"]):.2f}',
            b["status"], b["result"] or "-", f'{float(b["profit"] or 0):+.2f}'
        )
    console.print(table)
