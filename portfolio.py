"""Portfolio manager - handles paper trading execution and position management."""
import logging
from datetime import datetime

import config
import db
import data_feed
import strategy

logger = logging.getLogger("forex.portfolio")


class Portfolio:
    """Manages paper trading portfolio."""

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
        unrealized = self.get_unrealized_pnl()
        return self.balance + unrealized

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

    def open_trade(self, pair: str, direction: str, signals: dict,
                   entry_price: float, stop_loss: float, take_profit: float,
                   position_size: float) -> int:
        """Open a new paper trade."""
        # Check max positions
        if db.count_open_positions() >= config.MAX_OPEN_POSITIONS:
            logger.info(f"Max positions reached, skipping {pair}")
            return -1

        # Check if already have position in this pair
        existing = db.get_open_trades_for_pair(pair)
        if existing:
            logger.info(f"Already have position in {pair}, skipping")
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
        """Check all open trades for stop-loss or take-profit hits."""
        open_trades = db.get_open_trades()

        for trade in open_trades:
            current_price = data_feed.get_latest_price(trade["pair"])
            if current_price is None:
                continue

            pair_name = config.PAIR_NAMES.get(trade["pair"], trade["pair"])

            if trade["direction"] == "long":
                # Check stop-loss
                if current_price <= trade["stop_loss"]:
                    pnl = db.close_trade(trade["id"], current_price, "closed_sl")
                    self.balance += (pnl or 0)
                    self.save_balance()
                    logger.info(f"STOP-LOSS {pair_name} @ {current_price:.5f} PnL={pnl:.2f}")
                    continue

                # Check take-profit
                if current_price >= trade["take_profit"]:
                    pnl = db.close_trade(trade["id"], current_price, "closed_tp")
                    self.balance += (pnl or 0)
                    self.save_balance()
                    logger.info(f"TAKE-PROFIT {pair_name} @ {current_price:.5f} PnL={pnl:.2f}")
                    continue

            else:  # short
                if current_price >= trade["stop_loss"]:
                    pnl = db.close_trade(trade["id"], current_price, "closed_sl")
                    self.balance += (pnl or 0)
                    self.save_balance()
                    logger.info(f"STOP-LOSS {pair_name} @ {current_price:.5f} PnL={pnl:.2f}")
                    continue

                if current_price <= trade["take_profit"]:
                    pnl = db.close_trade(trade["id"], current_price, "closed_tp")
                    self.balance += (pnl or 0)
                    self.save_balance()
                    logger.info(f"TAKE-PROFIT {pair_name} @ {current_price:.5f} PnL={pnl:.2f}")
                    continue

    def evaluate_and_trade(self):
        """Main trading loop iteration - evaluate all pairs and trade."""
        self.check_exits()

        for pair in config.FOREX_PAIRS:
            try:
                self._evaluate_pair(pair)
            except Exception as e:
                logger.error(f"Error evaluating {pair}: {e}", exc_info=True)

        # Record equity snapshot
        db.record_equity(self.equity, db.count_open_positions())

    def _evaluate_pair(self, pair: str):
        """Evaluate a single pair for potential entry."""
        # Skip if already have position
        if db.get_open_trades_for_pair(pair):
            return

        # Skip if max positions reached
        if db.count_open_positions() >= config.MAX_OPEN_POSITIONS:
            return

        # Fetch data
        df = data_feed.fetch_price_data(pair, period="5d", interval="15m")
        if df is None or len(df) < 30:
            return

        # Generate signals
        signals = strategy.generate_signals(pair, df)
        combined = strategy.get_weighted_signal(signals)

        pair_name = config.PAIR_NAMES.get(pair, pair)
        logger.debug(f"{pair_name} combined signal: {combined:.3f} | signals: {signals}")

        # Check if signal is strong enough
        if abs(combined) < config.MIN_SIGNAL_STRENGTH:
            return

        direction = "long" if combined > 0 else "short"

        # Get trade parameters
        params = strategy.get_trade_parameters(pair, df, direction)
        if params["position_size"] <= 0:
            return

        # Open the trade
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

        # Enrich open trades with current P&L
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
        }
