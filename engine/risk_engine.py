def calculate_stake(bank: float, edge: float, max_stake_pct: float = 0.03) -> float:
    if edge < 0.06:
        stake_pct = 0.0
    elif edge < 0.09:
        stake_pct = 0.005
    elif edge < 0.13:
        stake_pct = 0.01
    elif edge < 0.18:
        stake_pct = 0.015
    else:
        stake_pct = 0.02

    stake = bank * stake_pct
    return round(min(stake, bank * max_stake_pct), 2)
