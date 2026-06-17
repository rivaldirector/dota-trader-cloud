from storage.db import Database
from engine.value_engine import implied_probability, calculate_edge
from engine.risk_engine import calculate_stake

class PaperTrader:
    def __init__(self, db: Database, start_bank: float, currency: str, min_edge: float, max_stake_pct: float):
        self.db = db
        self.start_bank = start_bank
        self.currency = currency
        self.min_edge = min_edge
        self.max_stake_pct = max_stake_pct
        self._ensure_bankroll()

    def _ensure_bankroll(self):
        row = self.db.fetchone("SELECT bank FROM bankroll WHERE id = 1")
        if row is None:
            self.db.execute(
                "INSERT INTO bankroll (id, bank, currency) VALUES (1, ?, ?)",
                (self.start_bank, self.currency),
            )

    @property
    def bank(self) -> float:
        row = self.db.fetchone("SELECT bank FROM bankroll WHERE id = 1")
        return float(row["bank"])

    def reset(self):
        self.db.execute("DELETE FROM bets")
        self.db.execute("DELETE FROM bankroll")
        self._ensure_bankroll()

    def place_if_value(self, match_name: str, market: str, selection: str, odds: float, model_probability: float):
        book_prob = implied_probability(odds)
        edge = calculate_edge(model_probability, odds)
        if edge < self.min_edge:
            return None
        stake = calculate_stake(self.bank, edge, self.max_stake_pct)
        if stake <= 0:
            return None
        cur = self.db.execute(
            """
            INSERT INTO bets (match_name, market, selection, odds, model_probability, book_probability, edge, stake, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
            """,
            (match_name, market, selection, odds, model_probability, book_prob, edge, stake),
        )
        return cur.lastrowid

    def settle_bet(self, bet_id: int, won: bool):
        bet = self.db.fetchone("SELECT * FROM bets WHERE id = ? AND status = 'OPEN'", (bet_id,))
        if bet is None:
            raise ValueError(f"Open bet {bet_id} not found")
        stake = float(bet["stake"])
        odds = float(bet["odds"])
        profit = round(stake * (odds - 1), 2) if won else round(-stake, 2)
        result = "WIN" if won else "LOSS"
        self.db.execute(
            "UPDATE bets SET status = 'CLOSED', result = ?, profit = ? WHERE id = ?",
            (result, profit, bet_id),
        )
        self.db.execute(
            "UPDATE bankroll SET bank = bank + ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
            (profit,),
        )
