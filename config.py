"""Configuration for the Forex Trading Bot."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "trades.db"

# Major forex pairs (yfinance format)
FOREX_PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X",
    "AUDUSD=X", "NZDUSD=X", "USDCAD=X", "EURGBP=X",
    "EURJPY=X", "GBPJPY=X",
]

# Human-readable names
PAIR_NAMES = {
    "EURUSD=X": "EUR/USD", "GBPUSD=X": "GBP/USD", "USDJPY=X": "USD/JPY",
    "USDCHF=X": "USD/CHF", "AUDUSD=X": "AUD/USD", "NZDUSD=X": "NZD/USD",
    "USDCAD=X": "USD/CAD", "EURGBP=X": "EUR/GBP", "EURJPY=X": "EUR/JPY",
    "GBPJPY=X": "GBP/JPY",
}

# Correlation groups — pairs that move together (don't stack same-direction bets)
CORRELATION_GROUPS = {
    "EUR_LONG": ["EURUSD=X", "EURGBP=X", "EURJPY=X"],   # All EUR-base pairs
    "GBP_LONG": ["GBPUSD=X", "GBPJPY=X"],                # All GBP-base pairs
    "JPY_SHORT": ["USDJPY=X", "EURJPY=X", "GBPJPY=X"],   # All JPY-quote pairs
    "USD_SHORT": ["EURUSD=X", "GBPUSD=X", "AUDUSD=X", "NZDUSD=X"],  # USD-quote
    "USD_LONG": ["USDJPY=X", "USDCHF=X", "USDCAD=X"],    # USD-base
}

# Trading parameters
INITIAL_BALANCE = 100_000.0      # Starting paper balance
RISK_PER_TRADE = 0.015           # 1.5% risk per trade (slightly conservative)
MAX_OPEN_POSITIONS = 4           # Reduced to avoid over-exposure
MAX_CORRELATED_POSITIONS = 2     # Max positions in same correlation group
STOP_LOSS_ATR_MULT = 1.5         # Stop-loss = 1.5x ATR
TAKE_PROFIT_ATR_MULT = 3.0       # Take-profit = 3x ATR (2:1 R:R)
TRAILING_ACTIVATION_R = 1.0      # Activate trailing stop after 1R profit
TRAILING_STOP_ATR_MULT = 1.0     # Trail stop at 1x ATR behind price
MIN_SIGNAL_STRENGTH = 0.25       # Minimum combined signal to enter trade
MIN_AGREEING_SIGNALS = 3         # Minimum number of signals in same direction
MIN_ADX = 20                     # Minimum ADX for trend trades (skip choppy markets)

# Session-aware trading (UTC hours)
# London: 07-16, New York: 12-21, Overlap: 12-16 (best volume)
TRADING_SESSIONS = {
    "london_open": 7,
    "london_close": 16,
    "ny_open": 12,
    "ny_close": 21,
    "best_start": 7,        # Trade from London open
    "best_end": 21,          # Through NY close
    "overlap_start": 12,     # London-NY overlap (highest liquidity)
    "overlap_end": 16,
}
ONLY_TRADE_SESSIONS = True       # If True, skip trading outside best hours

# Polling intervals (seconds)
PRICE_POLL_INTERVAL = 60         # Fetch new candles every 60s
STRATEGY_INTERVAL = 60           # Run strategy every 60s
LEARNING_INTERVAL = 300          # Update learning weights every 5 min

# Technical indicator parameters
EMA_FAST = 12
EMA_SLOW = 26
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BB_PERIOD = 20
BB_STD = 2
ATR_PERIOD = 14

# News RSS feeds for sentiment
NEWS_FEEDS = [
    "https://www.forexlive.com/feed",
    "https://www.fxstreet.com/rss",
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=EURUSD=X&region=US&lang=en-US",
    "https://www.investing.com/rss/news_14.rss",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
]

# Sentiment keywords (currency -> positive/negative words)
CURRENCY_KEYWORDS = {
    "USD": {
        "positive": ["fed hike", "strong dollar", "us growth", "nonfarm beat", "hawkish fed", "us jobs"],
        "negative": ["fed cut", "weak dollar", "us recession", "dovish fed", "us slowdown", "deficit"],
    },
    "EUR": {
        "positive": ["ecb hike", "eurozone growth", "strong euro", "hawkish ecb"],
        "negative": ["ecb cut", "eurozone recession", "weak euro", "dovish ecb", "eu crisis"],
    },
    "GBP": {
        "positive": ["boe hike", "uk growth", "strong pound", "hawkish boe"],
        "negative": ["boe cut", "uk recession", "weak pound", "brexit", "dovish boe"],
    },
    "JPY": {
        "positive": ["boj hike", "japan growth", "strong yen", "boj tighten"],
        "negative": ["boj easing", "japan recession", "weak yen", "boj dovish"],
    },
    "AUD": {
        "positive": ["rba hike", "australia growth", "strong aussie", "china growth"],
        "negative": ["rba cut", "australia recession", "weak aussie", "china slowdown"],
    },
    "NZD": {
        "positive": ["rbnz hike", "nz growth", "strong kiwi"],
        "negative": ["rbnz cut", "nz recession", "weak kiwi"],
    },
    "CHF": {
        "positive": ["snb hike", "swiss growth", "strong franc", "safe haven"],
        "negative": ["snb cut", "swiss recession", "weak franc", "snb intervene"],
    },
    "CAD": {
        "positive": ["boc hike", "canada growth", "oil rally", "strong loonie"],
        "negative": ["boc cut", "canada recession", "oil crash", "weak loonie"],
    },
}

# Self-ping keep-alive for Render free tier
SELF_PING_INTERVAL = 600  # Ping every 10 minutes
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

# Web dashboard
WEB_HOST = "0.0.0.0"
WEB_PORT = int(os.environ.get("PORT", 8080))
