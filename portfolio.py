"""Portfolio manager - handles paper trading execution and position management."""
import json
import logging
from datetime import datetime

import config
import db
import data_feed
import strategy

logger = logging.getLogger("forex.portfolio")


class Portfolio:
    """Manages paper trading portfolio with trailing stops and correlation filtering."""

    def __init__(self):
        self.balance = self._load_balance()
        self.running = False

    def _load_balance(self) -> float:
        saved = db.get_state("balance")
        if saved is not None:
            return float(saved)
        return config.INITIAL_BALANCE

    def save_balance(self):
        db.set_state("balance", self.balance)

    @property
    def equity(self) -> float:
        """Current equity = balance + unrealized P&L."""
        return self.balance + self.get_unrealized_pnl()

    def get_unrealized_pnl(self) -> float:
        """Calculate total unrealized P&L from open positions."""
        open_trades = db.get_open_trades()
        total_pnl = 0.0
        for trade in open_trades:
            current_price = data_feed.get_latest_price(trade["pair"])
            if current_price is None:
                continue
            if trade["direction"] == "long":
                pnl = (current_price - trade["entry_price"]) * trade["position_size"]
            else:
                pnl = (trade["entry_price"] - current_price) * trade["position_size"]
            total_pnl += pnl
        return total_pnl

    def _check_correlation_limit(self, pair: str, direction: str) -> bool:
        """Check if opening this position would exceed correlation limits.

        Returns True if OK to trade, False if too correlated with existing positions.
        """
        open_trades = db.get_open_trades()
        if not open_trades:
            return True

        for group_name, group_pairs in config.CORRELATION_GROUPS.items():
            if pair not in group_pairs:
                continue

            # Count existing positions in this correlation group with same direction
            same_dir_count = 0
            for trade in open_trades:
                if trade["pair"] in group_pairs and trade["direction"] == direction:
                    same_dir_count += 1

            if same_dir_count >= config.MAX_CORRELATED_POSITIONS:
                logger.info(
                    f"Correlation limit: {config.PAIR_NAMES.get(pair, pair)} "
                    f"blocked by group {group_name} ({same_dir_count} existing)"
                )
                return False

        return True

    def open_trade(self, pair: str, direction: str, signals: dict,
                   entry_price: float, stop_loss: float, take_profit: float,
                   position_size: float) -> int:
        """Open a new paper trade."""
        if db.count_open_positions() >= config.MAX_OPEN_POSITIONS:
            logger.info(f"Max positions reached, skipping {pair}")
            return -1

        if db.get_open_trades_for_pair(pair):
            logger.info(f"Already have position in {pair}, skipping")
            return -1

        if not self._check_correlation_limit(pair, direction):
            return -1

        trade_id = db.insert_trade(
            pair=pair,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_size=position_size,
            signals=signals,
        )

        pair_name = config.PAIR_NAMES.get(pair, pair)
        logger.info(
            f"OPENED {direction.upper()} {pair_name} @ {entry_price:.5f} "
            f"SL={stop_loss:.5f} TP={take_profit:.5f} Size={position_size:.2f}"
        )
        return trade_id

    def check_exits(self):
        """Check all open trades for SL, TP, and manage trailing stops."""
        open_trades = db.get_open_trades()

        for trade in open_trades:
            current_price = data_feed.get_latest_price(trade["pair"])
            if current_price is None:
                continue

            pair_name = config.PAIR_NAMES.get(trade["pair"], trade["pair"])
            entry = trade["entry_price"]
            sl = trade["stop_loss"]
            tp = trade["take_profit"]

            # --- Trailing stop logic ---
            # Calculate how many "R" of profit we're in
            risk_per_unit = abs(entry - sl)
            if risk_per_unit > 0:
                if trade["direction"] == "long":
                    current_r = (current_price - entry) / risk_per_unit
                else:
                    current_r = (entry - current_price) / risk_per_unit

                # Activate trailing stop after TRAILING_ACTIVATION_R profit
                if current_r >= config.TRAILING_ACTIVATION_R:
                    new_sl = self._calculate_trailing_stop(
                        trade, current_price
                    )
                    if new_sl is not None and self._is_better_stop(
                        trade["direction"], new_sl, sl
                    ):
                        db.update_stop_loss(trade["id"], new_sl)
                        sl = new_sl  # Use updated SL for exit check below
                        logger.info(
                            f"TRAIL SL {pair_name} -> {new_sl:.5f} "
                            f"({current_r:.1f}R profit)"
                        )

            # --- Check exits ---
            if trade["direction"] == "long":
                if current_price <= sl:
                    pnl = db.close_trade(trade["id"], current_price, "closed_sl")
                    self.balance += (pnl or 0)
                    self.save_balance()
                    logger.info(f"STOP-LOSS {pair_name} @ {current_price:.5f} PnL={pnl:.2f}")
                    continue
                if current_price >= tp:
                    pnl = db.close_trade(trade["id"], current_price, "closed_tp")
                    self.balance += (pnl or 0)
                    self.save_balance()
                    logger.info(f"TAKE-PROFIT {pair_name} @ {current_price:.5f} PnL={pnl:.2f}")
                    continue
            else:
                if current_price >= sl:
                    pnl = db.close_trade(trade["id"], current_price, "closed_sl")
                    self.balance += (pnl or 0)
                    self.save_balance()
                    logger.info(f"STOP-LOSS {pair_name} @ {current_price:.5f} PnL={pnl:.2f}")
                    continue
                if current_price <= tp:
                    pnl = db.close_trade(trade["id"], current_price, "closed_tp")
                    self.balance += (pnl or 0)
                    self.save_balance()
                    logger.info(f"TAKE-PROFIT {pair_name} @ {current_price:.5f} PnL={pnl:.2f}")
                    continue

    def _calculate_trailing_stop(self, trade: dict, current_price: float) -> float:
        """Calculate new trailing stop level based on ATR."""
        df = data_feed.fetch_price_data(trade["pair"], period="5d", interval="15m")
        if df is None or len(df) < 20:
            return None

        df = strategy.compute_indicators(df)
        atr = df["atr"].iloc[-1]
        if atr is None or atr <= 0:
            return None

        trail_distance = atr * config.TRAILING_STOP_ATR_MULT

        if trade["direction"] == "long":
            return round(current_price - trail_distance, 5)
        else:
            return round(current_price + trail_distance, 5)

    def _is_better_stop(self, direction: str, new_sl: float, old_sl: float) -> bool:
        """Check if new stop-loss is tighter (better) than old one."""
        if direction == "long":
            return new_sl > old_sl  # Higher SL = tighter for longs
        else:
            return new_sl < old_sl  # Lower SL = tighter for shorts

    def evaluate_and_trade(self):
        """Main trading loop iteration."""
        self.check_exits()

        # Session filter — skip new entries outside trading hours
        if not strategy.is_good_session():
            logger.info("Outside trading session, skipping new entries")
            db.record_equity(self.equity, db.count_open_positions())
            return

        for pair in config.FOREX_PAIRS:
            try:
                self._evaluate_pair(pair)
            except Exception as e:
                logger.error(f"Error evaluating {pair}: {e}", exc_info=True)

        db.record_equity(self.equity, db.count_open_positions())

    def _evaluate_pair(self, pair: str):
        """Evaluate a single pair for potential entry."""
        if db.get_open_trades_for_pair(pair):
            return
        if db.count_open_positions() >= config.MAX_OPEN_POSITIONS:
            return

        df = data_feed.fetch_price_data(pair, period="5d", interval="15m")
        if df is None or len(df) < 30:
            return

        # Generate signals
        signals = strategy.generate_signals(pair, df)
        combined = strategy.get_weighted_signal(signals)

        pair_name = config.PAIR_NAMES.get(pair, pair)
        logger.debug(f"{pair_name} combined signal: {combined:.3f}")

        # Gate 1: Minimum combined signal strength
        if abs(combined) < config.MIN_SIGNAL_STRENGTH:
            return

        direction = "long" if combined > 0 else "short"

        # Gate 2: Minimum agreeing signals (avoid one strong signal dominating)
        agreeing = strategy.count_agreeing_signals(signals, direction)
        if agreeing < config.MIN_AGREEING_SIGNALS:
            logger.debug(
                f"{pair_name} only {agreeing} agreeing signals, need {config.MIN_AGREEING_SIGNALS}"
            )
            return

        # Gate 3: Correlation check
        if not self._check_correlation_limit(pair, direction):
            return

        # All gates passed — calculate trade params and enter
        params = strategy.get_trade_parameters(pair, df, direction, self.balance)
        if params["position_size"] <= 0:
            return

        self.open_trade(
            pair=pair,
            direction=direction,
            signals=signals,
            entry_price=params["entry_price"],
            stop_loss=params["stop_loss"],
            take_profit=params["take_profit"],
            position_size=params["position_size"],
        )

    def close_all(self):
        """Emergency close all open positions."""
        open_trades = db.get_open_trades()
        for trade in open_trades:
            current_price = data_feed.get_latest_price(trade["pair"])
            if current_price:
                pnl = db.close_trade(trade["id"], current_price, "closed_manual")
                self.balance += (pnl or 0)
                pair_name = config.PAIR_NAMES.get(trade["pair"], trade["pair"])
                logger.info(f"MANUAL CLOSE {pair_name} @ {current_price:.5f} PnL={pnl:.2f}")
        self.save_balance()

    def get_dashboard_data(self) -> dict:
        """Get all data needed for the dashboard."""
        stats = db.get_stats()
        open_trades = db.get_open_trades()
        prices = data_feed.get_all_latest_prices()

        for trade in open_trades:
            current = prices.get(trade["pair"])
            if current:
                if trade["direction"] == "long":
                    trade["unrealized_pnl"] = (current - trade["entry_price"]) * trade["position_size"]
                else:
                    trade["unrealized_pnl"] = (trade["entry_price"] - current) * trade["position_size"]
                trade["current_price"] = current
            else:
                trade["unrealized_pnl"] = 0
                trade["current_price"] = trade["entry_price"]
            trade["pair_name"] = config.PAIR_NAMES.get(trade["pair"], trade["pair"])

        history = db.get_trade_history(limit=50)
        for trade in history:
            trade["pair_name"] = config.PAIR_NAMES.get(trade["pair"], trade["pair"])

        equity_history = db.get_equity_history()
        signal_weights = db.get_signal_weights()

        return {
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "unrealized_pnl": round(self.get_unrealized_pnl(), 2),
            "stats": stats,
            "open_trades": open_trades,
            "trade_history": history,
            "equity_history": equity_history,
            "signal_weights": signal_weights,
            "prices": {config.PAIR_NAMES.get(k, k): v for k, v in prices.items()},
            "running": self.running,
            "session_active": strategy.is_good_session(),
        }
