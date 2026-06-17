def implied_probability(decimal_odds: float) -> float:
    if decimal_odds <= 1:
        raise ValueError("Odds must be greater than 1.0")
    return 1.0 / decimal_odds


def calculate_edge(model_probability: float, decimal_odds: float) -> float:
    return model_probability - implied_probability(decimal_odds)


def is_value_bet(model_probability: float, decimal_odds: float, min_edge: float) -> bool:
    return calculate_edge(model_probability, decimal_odds) >= min_edge
