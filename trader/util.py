def round_tick(px: float, tick: float = 0.05) -> float:
    """Round a price to the exchange tick size."""
    return round(round(px / tick) * tick, 2)
