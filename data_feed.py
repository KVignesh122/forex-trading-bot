"""Data feed module - fetches price data, news, and sentiment."""
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import feedparser
import numpy as np
import pandas as pd
import yfinance as yf

import config
import db

logger = logging.getLogger("forex.data")

# Cache for price data to avoid excessive API calls
_price_cache: dict[str, pd.DataFrame] = {}
_price_cache_time: dict[str, float] = {}
CACHE_TTL = 30  # seconds


def fetch_price_data(pair: str, period: str = "5d", interval: str = "15m") -> Optional[pd.DataFrame]:
    """Fetch OHLCV data for a forex pair from yfinance.

    Returns DataFrame with columns: Open, High, Low, Close, Volume
    """
    cache_key = f"{pair}_{period}_{interval}"
    now = time.time()

    if cache_key in _price_cache and (now - _price_cache_time.get(cache_key, 0)) < CACHE_TTL:
        return _price_cache[cache_key]

    try:
        ticker = yf.Ticker(pair)
        df = ticker.history(period=period, interval=interval)
        if df.empty:
            logger.warning(f"No data returned for {pair}")
            return _price_cache.get(cache_key)

        # Clean up
        df = df.dropna()
        if len(df) < 30:
            logger.warning(f"Insufficient data for {pair}: {len(df)} rows")
            return _price_cache.get(cache_key)

        _price_cache[cache_key] = df
        _price_cache_time[cache_key] = now
        return df

    except Exception as e:
        logger.error(f"Error fetching {pair}: {e}")
        return _price_cache.get(cache_key)


def fetch_multi_timeframe(pair: str) -> dict[str, Optional[pd.DataFrame]]:
    """Fetch multiple timeframes for a pair."""
    return {
        "15m": fetch_price_data(pair, period="5d", interval="15m"),
        "1h": fetch_price_data(pair, period="1mo", interval="1h"),
        "1d": fetch_price_data(pair, period="6mo", interval="1d"),
    }


def get_latest_price(pair: str) -> Optional[float]:
    """Get the most recent price for a pair."""
    df = fetch_price_data(pair, period="1d", interval="1m")
    if df is not None and not df.empty:
        return float(df["Close"].iloc[-1])
    # Fallback to 15m data
    df = fetch_price_data(pair, period="5d", interval="15m")
    if df is not None and not df.empty:
        return float(df["Close"].iloc[-1])
    return None


def get_all_latest_prices() -> dict[str, float]:
    """Get latest prices for all configured pairs."""
    prices = {}
    for pair in config.FOREX_PAIRS:
        price = get_latest_price(pair)
        if price is not None:
            prices[pair] = price
    return prices


# --- News & Sentiment ---

_news_cache_time: float = 0
NEWS_CACHE_TTL = 300  # 5 minutes


def fetch_news() -> list[dict]:
    """Fetch and parse news from RSS feeds, returning headline data."""
    global _news_cache_time
    now = time.time()

    if now - _news_cache_time < NEWS_CACHE_TTL:
        return db.get_recent_news(hours=2)

    articles = []
    for feed_url in config.NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:15]:
                title = entry.get("title", "")
                published = entry.get("published", "")
                source = feed.feed.get("title", feed_url)

                sentiment = _analyze_headline_sentiment(title)
                currencies = _extract_currencies(title)

                article = {
                    "title": title,
                    "source": source,
                    "published": published,
                    "sentiment": sentiment,
                    "currencies": currencies,
                }
                articles.append(article)

                # Cache to DB
                try:
                    db.cache_news(title, source, published, sentiment, currencies)
                except Exception:
                    pass  # Duplicate or DB error, skip

        except Exception as e:
            logger.warning(f"Error fetching news from {feed_url}: {e}")
            continue

    _news_cache_time = now
    return articles


def _analyze_headline_sentiment(headline: str) -> float:
    """Simple keyword-based sentiment analysis. Returns -1.0 to 1.0."""
    headline_lower = headline.lower()

    positive_words = [
        "surge", "rally", "gain", "rise", "jump", "climb", "strong",
        "bullish", "growth", "boom", "soar", "record high", "beat",
        "optimism", "recovery", "upgrade", "hawkish", "hike",
    ]
    negative_words = [
        "crash", "fall", "drop", "plunge", "decline", "weak", "bearish",
        "recession", "crisis", "slump", "tumble", "record low", "miss",
        "pessimism", "downgrade", "dovish", "cut", "fear", "risk-off",
    ]

    pos_count = sum(1 for w in positive_words if w in headline_lower)
    neg_count = sum(1 for w in negative_words if w in headline_lower)

    total = pos_count + neg_count
    if total == 0:
        return 0.0

    return (pos_count - neg_count) / total


def _extract_currencies(text: str) -> list[str]:
    """Extract currency mentions from text."""
    text_upper = text.upper()
    currencies = []
    for ccy in config.CURRENCY_KEYWORDS:
        if ccy in text_upper:
            currencies.append(ccy)
    # Also check pair mentions
    pair_pattern = r"\b(EUR|USD|GBP|JPY|AUD|NZD|CHF|CAD)[/]?(EUR|USD|GBP|JPY|AUD|NZD|CHF|CAD)\b"
    for match in re.finditer(pair_pattern, text_upper):
        for g in match.groups():
            if g not in currencies:
                currencies.append(g)
    return currencies


def get_currency_sentiment(currency: str) -> float:
    """Get aggregate sentiment for a specific currency from recent news."""
    recent_news = db.get_recent_news(hours=4)
    if not recent_news:
        fetch_news()
        recent_news = db.get_recent_news(hours=4)

    scores = []
    keywords = config.CURRENCY_KEYWORDS.get(currency, {})

    for article in recent_news:
        title_lower = article["title"].lower()
        currencies_mentioned = article.get("currencies_mentioned", "[]")
        if isinstance(currencies_mentioned, str):
            import json
            try:
                currencies_mentioned = json.loads(currencies_mentioned)
            except (json.JSONDecodeError, TypeError):
                currencies_mentioned = []

        if currency in currencies_mentioned:
            # Weighted by general sentiment
            scores.append(article.get("sentiment_score", 0) or 0)

        # Check specific keywords
        for word in keywords.get("positive", []):
            if word in title_lower:
                scores.append(0.5)
        for word in keywords.get("negative", []):
            if word in title_lower:
                scores.append(-0.5)

    if not scores:
        return 0.0

    return float(np.clip(np.mean(scores), -1.0, 1.0))


def get_pair_sentiment(pair: str) -> float:
    """Get net sentiment for a forex pair (base currency - quote currency)."""
    # Extract currencies from pair name (e.g., "EURUSD=X" -> EUR, USD)
    clean = pair.replace("=X", "")
    base = clean[:3]
    quote = clean[3:]

    base_sent = get_currency_sentiment(base)
    quote_sent = get_currency_sentiment(quote)

    # Net sentiment: positive means bullish on pair (base strengthens vs quote)
    return float(np.clip(base_sent - quote_sent, -1.0, 1.0))


# --- Volatility & Correlation ---

def get_vix() -> Optional[float]:
    """Fetch VIX (fear index) as a risk-off indicator."""
    try:
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="1d", interval="1m")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.warning(f"Error fetching VIX: {e}")
    return None


def get_dxy() -> Optional[float]:
    """Fetch US Dollar Index."""
    try:
        dxy = yf.Ticker("DX-Y.NYB")
        hist = dxy.history(period="5d", interval="1h")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.warning(f"Error fetching DXY: {e}")
    return None


def get_correlation_matrix(period="3mo") -> Optional[pd.DataFrame]:
    """Calculate correlation matrix between forex pairs."""
    try:
        closes = {}
        for pair in config.FOREX_PAIRS:
            df = fetch_price_data(pair, period=period, interval="1d")
            if df is not None and not df.empty:
                closes[config.PAIR_NAMES.get(pair, pair)] = df["Close"]

        if len(closes) < 2:
            return None

        df = pd.DataFrame(closes)
        returns = df.pct_change().dropna()
        return returns.corr()
    except Exception as e:
        logger.error(f"Error calculating correlations: {e}")
        return None
