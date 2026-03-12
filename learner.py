"""Adaptive learning module - updates signal weights based on trade outcomes."""
import json
import logging
from collections import defaultdict

import numpy as np

import db
import strategy

logger = logging.getLogger("forex.learner")

# Exponential smoothing factor (higher = more weight on recent trades)
ALPHA = 0.15
MIN_WEIGHT = 0.1
MAX_WEIGHT = 3.0


def update_weights():
    """Analyze closed trades and update signal weights.

    Uses exponential smoothing of signal accuracy:
    - For each closed trade, look at which signals contributed to the entry
    - If the trade was profitable, the contributing signals were 'correct'
    - If unprofitable, they were 'incorrect'
    - Update weights using exponential moving average of accuracy
    """
    closed_trades = db.get_trade_history(limit=200)
    if len(closed_trades) < 5:
        logger.info("Not enough trades to update weights yet")
        return

    # Track performance per signal
    signal_performance = defaultdict(lambda: {"correct": 0, "incorrect": 0, "total_pnl": 0.0})

    for trade in closed_trades:
        signals_json = trade.get("signals_json")
        if not signals_json:
            continue

        try:
            signals = json.loads(signals_json)
        except (json.JSONDecodeError, TypeError):
            continue

        pnl = trade.get("pnl", 0) or 0
        direction = trade.get("direction", "long")
        is_win = pnl > 0

        for signal_name, score in signals.items():
            if abs(score) < 0.05:
                continue  # Signal was neutral, skip

            # Was this signal 'correct'?
            # Signal agrees with direction and trade was profitable
            signal_correct = False
            if direction == "long" and score > 0 and is_win:
                signal_correct = True
            elif direction == "short" and score < 0 and is_win:
                signal_correct = True
            elif direction == "long" and score < 0 and not is_win:
                signal_correct = True  # Signal warned against, and it was right
            elif direction == "short" and score > 0 and not is_win:
                signal_correct = True

            if signal_correct:
                signal_performance[signal_name]["correct"] += 1
            else:
                signal_performance[signal_name]["incorrect"] += 1

            signal_performance[signal_name]["total_pnl"] += pnl * abs(score)

    # Update weights
    current_weights = db.get_signal_weights()

    for signal_name, perf in signal_performance.items():
        total = perf["correct"] + perf["incorrect"]
        if total < 3:
            continue

        accuracy = perf["correct"] / total

        # Current weight
        current = current_weights.get(signal_name, {})
        old_weight = current.get("weight", strategy.DEFAULT_WEIGHTS.get(signal_name, 1.0))

        # New weight based on accuracy (>0.5 = good signal, increase weight)
        target_weight = 0.5 + accuracy * 2.0  # Maps 0-1 accuracy to 0.5-2.5

        # Exponential smoothing
        new_weight = old_weight * (1 - ALPHA) + target_weight * ALPHA
        new_weight = float(np.clip(new_weight, MIN_WEIGHT, MAX_WEIGHT))

        wins = current.get("wins", 0)
        losses_count = current.get("losses", 0)

        # Accumulate from this batch
        wins += perf["correct"]
        losses_count += perf["incorrect"]

        db.upsert_signal_weight(
            signal_name=signal_name,
            weight=round(new_weight, 4),
            wins=wins,
            losses=losses_count,
            total_pnl=round(perf["total_pnl"], 2),
        )

        logger.info(
            f"Weight update: {signal_name} {old_weight:.3f} -> {new_weight:.3f} "
            f"(accuracy={accuracy:.1%}, trades={total})"
        )


def get_learning_summary() -> dict:
    """Get a summary of the learning state."""
    weights = db.get_signal_weights()
    stats = db.get_stats()

    summary = {
        "total_trades_analyzed": stats["total_trades"],
        "current_weights": {},
        "weight_changes": {},
    }

    for name, data in weights.items():
        default = strategy.DEFAULT_WEIGHTS.get(name, 1.0)
        summary["current_weights"][name] = round(data["weight"], 3)
        summary["weight_changes"][name] = {
            "from_default": round(data["weight"] - default, 3),
            "wins": data["wins"],
            "losses": data["losses"],
        }

    return summary
