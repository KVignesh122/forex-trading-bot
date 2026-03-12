"""Main entry point - starts the trading bot and web dashboard."""
import logging
import os
import sys
import threading
import time
import urllib.request

import uvicorn

import config
import db
import learner
from app import app, set_portfolio
from portfolio import Portfolio

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("forex_bot.log", mode="a"),
    ],
)
logger = logging.getLogger("forex.main")


def self_ping():
    """Keep-alive ping to prevent Render free tier from spinning down.

    Pings own /health endpoint every 10 minutes.
    """
    url = config.RENDER_URL
    if not url:
        logger.info("No RENDER_EXTERNAL_URL set, self-ping disabled")
        return

    health_url = url.rstrip("/") + "/health"
    logger.info(f"Self-ping enabled: {health_url} every {config.SELF_PING_INTERVAL}s")

    while True:
        time.sleep(config.SELF_PING_INTERVAL)
        try:
            req = urllib.request.Request(health_url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    logger.debug("Self-ping OK")
                else:
                    logger.warning(f"Self-ping got status {resp.status}")
        except Exception as e:
            logger.warning(f"Self-ping failed: {e}")


def trading_loop(portfolio: Portfolio):
    """Background thread running the trading strategy."""
    logger.info("Trading loop started")
    last_learn_time = 0

    while True:
        try:
            if portfolio.running:
                logger.info("--- Running strategy cycle ---")
                portfolio.evaluate_and_trade()

                # Periodic learning update
                now = time.time()
                if now - last_learn_time > config.LEARNING_INTERVAL:
                    try:
                        learner.update_weights()
                        last_learn_time = now
                    except Exception as e:
                        logger.error(f"Learning update error: {e}", exc_info=True)

                stats = db.get_stats()
                logger.info(
                    f"Balance: ${portfolio.balance:,.2f} | "
                    f"Equity: ${portfolio.equity:,.2f} | "
                    f"Open: {stats['open_trades']} | "
                    f"Total: {stats['total_trades']} | "
                    f"Win rate: {stats['win_rate']:.1f}%"
                )
            else:
                # Still check exits even when paused (honor stop-losses)
                try:
                    portfolio.check_exits()
                except Exception:
                    pass

            time.sleep(config.STRATEGY_INTERVAL)

        except Exception as e:
            logger.error(f"Trading loop error: {e}", exc_info=True)
            time.sleep(10)


def main():
    logger.info("=" * 60)
    logger.info("FOREX TRADING BOT v2 - Starting up")
    logger.info("=" * 60)

    # Initialize database
    db.init_db()
    logger.info("Database initialized")

    # Initialize portfolio
    portfolio = Portfolio()
    set_portfolio(portfolio)

    # Restore running state
    was_running = db.get_state("running", False)
    portfolio.running = was_running
    logger.info(f"Portfolio balance: ${portfolio.balance:,.2f}")
    logger.info(f"Auto-start trading: {was_running}")
    logger.info(f"Monitoring pairs: {', '.join(config.PAIR_NAMES.values())}")

    # Start trading loop in background thread
    trade_thread = threading.Thread(target=trading_loop, args=(portfolio,), daemon=True)
    trade_thread.start()
    logger.info("Trading loop thread started")

    # Start self-ping keep-alive for Render
    ping_thread = threading.Thread(target=self_ping, daemon=True)
    ping_thread.start()

    # Start web dashboard
    logger.info(f"Dashboard: http://localhost:{config.WEB_PORT}")
    if config.RENDER_URL:
        logger.info(f"Public URL: {config.RENDER_URL}")
    logger.info("=" * 60)

    uvicorn.run(
        app,
        host=config.WEB_HOST,
        port=config.WEB_PORT,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
