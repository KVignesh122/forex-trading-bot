"""Trading strategy - combines multiple signals into trade decisions."""
import logging
from typing import Optional

import numpy as np
import pandas as pd

import config
import data_feed
import db

logger = logging.getLogger("forex.strategy")

# Default signal weights (updated by learner)
DEFAULT_WEIGHTS = {
    "ema_crossover": 1.0,
    "rsi": 1.0,
    "macd": 1.0,
    "bollinger": 1.0,
    "multi_tf_trend": 1.0,
    "sentiment": 0.8,
    "volatility": 0.6,
    "dxy_bias": 0.5,
}


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all technical indicators on a price DataFrame."""
    df = df.copy()

    # EMA
    df["ema_fast"] = df["Close"].ewm(span=config.EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["Close"].ewm(span=config.EMA_SLOW, adjust=False).mean()

    # RSI
    delta = df["Close"].diff()
    gain = delta.where(delta > 0, 0.0).rolling(window=config.RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=config.RSI_PERIOD).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # MACD
    ema_fast = df["Close"].ewm(span=config.MACD_FAST, adjust=False).mean()
    ema_slow = df["Close"].ewm(span=config.MACD_SLOW, adjust=False).mean()
    df["macd"] = ema_fast - ema_slow
    df["macd_signal"] = df["macd"].ewm(span=config.MACD_SIGNAL, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # Bollinger Bands
    df["bb_mid"] = df["Close"].rolling(window=config.BB_PERIOD).mean()
    bb_std = df["Close"].rolling(window=config.BB_PERIOD).std()
    df["bb_upper"] = df["bb_mid"] + config.BB_STD * bb_std
    df["bb_lower"] = df["bb_mid"] - config.BB_STD * bb_std
    df["bb_pct"] = (df["Close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])

    # ATR
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = true_range.rolling(window=config.ATR_PERIOD).mean()

    # ADX (simplified trend strength)
    plus_dm = df["High"].diff()
    minus_dm = -df["Low"].diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    atr_smooth = true_range.rolling(14).mean()
    plus_di = 100 * (plus_dm.rolling(14).mean() / atr_smooth.replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(14).mean() / atr_smooth.replace(0, np.nan))
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    df["adx"] = dx.rolling(14).mean()

    return df


def signal_ema_crossover(df: pd.DataFrame) -> float:
    """EMA crossover signal. +1 = bullish cross, -1 = bearish cross."""
    if len(df) < 3:
        return 0.0
    fast = df["ema_fast"].iloc[-1]
    slow = df["ema_slow"].iloc[-1]
    fast_prev = df["ema_fast"].iloc[-2]
    slow_prev = df["ema_slow"].iloc[-2]

    # Fresh crossover
    if fast > slow and fast_prev <= slow_prev:
        return 1.0
    elif fast < slow and fast_prev >= slow_prev:
        return -1.0

    # Existing trend (weaker signal)
    if fast > slow:
        spread = (fast - slow) / slow
        return min(spread * 100, 0.5)
    else:
        spread = (slow - fast) / fast
        return max(-spread * 100, -0.5)


def signal_rsi(df: pd.DataFrame) -> float:
    """RSI signal. Oversold = bullish, overbought = bearish."""
    rsi = df["rsi"].iloc[-1]
    if pd.isna(rsi):
        return 0.0

    if rsi < config.RSI_OVERSOLD:
        return (config.RSI_OVERSOLD - rsi) / config.RSI_OVERSOLD  # 0 to 1
    elif rsi > config.RSI_OVERBOUGHT:
        return -(rsi - config.RSI_OVERBOUGHT) / (100 - config.RSI_OVERBOUGHT)  # -1 to 0
    return 0.0


def signal_macd(df: pd.DataFrame) -> float:
    """MACD histogram signal."""
    hist = df["macd_hist"].iloc[-1]
    hist_prev = df["macd_hist"].iloc[-2] if len(df) > 1 else 0

    if pd.isna(hist):
        return 0.0

    # Normalize by ATR for comparable scale
    atr = df["atr"].iloc[-1]
    if pd.isna(atr) or atr == 0:
        return 0.0

    normalized = hist / atr
    # Bonus for histogram direction change (momentum shift)
    if hist > 0 and hist_prev <= 0:
        normalized += 0.3
    elif hist < 0 and hist_prev >= 0:
        normalized -= 0.3

    return float(np.clip(normalized, -1.0, 1.0))


def signal_bollinger(df: pd.DataFrame) -> float:
    """Bollinger Band mean reversion signal."""
    bb_pct = df["bb_pct"].iloc[-1]
    if pd.isna(bb_pct):
        return 0.0

    # Near lower band = bullish (expect reversion up)
    # Near upper band = bearish (expect reversion down)
    if bb_pct < 0.1:
        return 0.8
    elif bb_pct < 0.2:
        return 0.4
    elif bb_pct > 0.9:
        return -0.8
    elif bb_pct > 0.8:
        return -0.4
    return 0.0


def signal_multi_timeframe(pair: str) -> float:
    """Multi-timeframe trend confirmation."""
    data = data_feed.fetch_multi_timeframe(pair)

    trends = []
    for tf_name, df in data.items():
        if df is None or len(df) < 30:
            continue
        df = compute_indicators(df)
        # Simple trend: is price above or below EMA slow?
        price = df["Close"].iloc[-1]
        ema = df["ema_slow"].iloc[-1]
        if pd.isna(ema):
            continue
        if price > ema:
            trends.append(1.0)
        else:
            trends.append(-1.0)

    if not trends:
        return 0.0

    # Agreement across timeframes
    avg = np.mean(trends)
    # Strong signal if all timeframes agree
    if abs(avg) == 1.0:
        return avg * 0.8
    return avg * 0.4


def signal_sentiment(pair: str) -> float:
    """News sentiment signal for a pair."""
    try:
        return data_feed.get_pair_sentiment(pair) * 0.7
    except Exception as e:
        logger.debug(f"Sentiment error for {pair}: {e}")
        return 0.0


def signal_volatility() -> float:
    """VIX-based risk-off signal. High VIX = reduce exposure."""
    vix = data_feed.get_vix()
    if vix is None:
        return 0.0

    # VIX > 25 = high fear, reduce trading
    # VIX < 15 = low fear, normal trading
    if vix > 30:
        return -0.5  # Strong risk-off
    elif vix > 25:
        return -0.3
    elif vix > 20:
        return -0.1
    return 0.0


def signal_dxy_bias(pair: str) -> float:
    """Dollar index bias for USD pairs."""
    clean = pair.replace("=X", "")
    if "USD" not in clean:
        return 0.0

    dxy = data_feed.get_dxy()
    if dxy is None:
        return 0.0

    # Get DXY trend via simple comparison to recent average
    try:
        dxy_ticker = yf.Ticker("DX-Y.NYB")
        hist = dxy_ticker.history(period="5d", interval="1h")
        if hist.empty:
            return 0.0
        avg = hist["Close"].mean()
        bias = (dxy - avg) / avg * 10  # Normalize

        # If USD is base currency, strong dollar = bullish
        # If USD is quote currency, strong dollar = bearish
        is_usd_base = clean.startswith("USD")
        if is_usd_base:
            return float(np.clip(bias, -0.5, 0.5))
        else:
            return float(np.clip(-bias, -0.5, 0.5))
    except Exception:
        return 0.0


import yfinance as yf


def generate_signals(pair: str, df: pd.DataFrame) -> dict[str, float]:
    """Generate all signals for a pair. Returns dict of signal_name -> score."""
    df = compute_indicators(df)

    signals = {
        "ema_crossover": signal_ema_crossover(df),
        "rsi": signal_rsi(df),
        "macd": signal_macd(df),
        "bollinger": signal_bollinger(df),
        "multi_tf_trend": signal_multi_timeframe(pair),
        "sentiment": signal_sentiment(pair),
        "volatility": signal_volatility(),
        "dxy_bias": signal_dxy_bias(pair),
    }

    return signals


def get_weighted_signal(signals: dict[str, float]) -> float:
    """Combine signals using learned weights. Returns -1 to +1."""
    # Get learned weights from DB, fallback to defaults
    learned = db.get_signal_weights()

    weighted_sum = 0.0
    total_weight = 0.0

    for name, score in signals.items():
        w = DEFAULT_WEIGHTS.get(name, 1.0)
        if name in learned:
            w = learned[name]["weight"]
        weighted_sum += score * w
        total_weight += abs(w)

    if total_weight == 0:
        return 0.0

    return float(np.clip(weighted_sum / total_weight, -1.0, 1.0))


def get_trade_parameters(pair: str, df: pd.DataFrame, direction: str) -> dict:
    """Calculate stop-loss, take-profit, and position size."""
    df = compute_indicators(df)
    atr = df["atr"].iloc[-1]
    price = df["Close"].iloc[-1]

    if pd.isna(atr) or atr <= 0:
        atr = price * 0.005  # Fallback: 0.5% of price

    if direction == "long":
        stop_loss = price - (atr * config.STOP_LOSS_ATR_MULT)
        take_profit = price + (atr * config.TAKE_PROFIT_ATR_MULT)
    else:
        stop_loss = price + (atr * config.STOP_LOSS_ATR_MULT)
        take_profit = price - (atr * config.TAKE_PROFIT_ATR_MULT)

    # Position sizing based on risk
    risk_amount = config.INITIAL_BALANCE * config.RISK_PER_TRADE
    risk_per_unit = abs(price - stop_loss)
    if risk_per_unit > 0:
        position_size = risk_amount / risk_per_unit
    else:
        position_size = 0

    # Cap position size to reasonable amount
    max_size = config.INITIAL_BALANCE * 0.2 / price  # Max 20% of balance per trade
    position_size = min(position_size, max_size)

    return {
        "stop_loss": round(stop_loss, 5),
        "take_profit": round(take_profit, 5),
        "position_size": round(position_size, 2),
        "atr": round(atr, 5),
        "entry_price": round(price, 5),
    }
