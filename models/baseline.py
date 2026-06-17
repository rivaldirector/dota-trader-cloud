def estimate_probability(market: str, odds: float, context=None) -> float:
    """Baseline-модель v1.
    Пока нет истории, она имитирует модель: берет вероятность букмекера и добавляет/убирает небольшой сдвиг.
    Потом заменим на обученную модель.
    """
    book_prob = 1 / odds
    context = context or {}
    signal_strength = float(context.get("signal_strength", 0.08))
    if market == "winner":
        return min(max(book_prob + signal_strength, 0.01), 0.95)
    if market == "duration":
        return min(max(book_prob + signal_strength * 0.9, 0.01), 0.95)
    if market == "kills":
        return min(max(book_prob + signal_strength * 0.8, 0.01), 0.95)
    return min(max(book_prob, 0.01), 0.95)
