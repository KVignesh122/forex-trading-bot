"""Trading strategy - combines multiple signals into trade decisions."""
import logging
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

import config
import data_feed
import db

logger = logging.getLogger("forex.strategy")

# Default signal weights (updated by learner)
DEFAULT_WEIGHTS = {
    "ema_crossover": 1.0,
    "rsi": 1.0,
    "macd": 1.2,
    "bollinger": 0.8,
    "multi_tf_trend": 1.3,
    "sentiment": 0.7,
    "volatility": 0.6,
    "dxy_bias": 0.5,
    "adx_trend": 0.9,
    "session_quality": 0.5,
}


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all technical indicators on a price DataFrame."""
    df = df.copy()

    # EMA
    df["ema_fast"] = df["Close"].ewm(span=config.EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["Close"].ewm(span=config.EMA_SLOW, adjust=False).mean()
    # EMA 200 for major trend
    df["ema_200"] = df["Close"].ewm(span=200, min_periods=50, adjust=False).mean()

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

    # ADX (trend strength)
    plus_dm = df["High"].diff()
    minus_dm = -df["Low"].diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    atr_smooth = true_range.rolling(14).mean()
    plus_di = 100 * (plus_dm.rolling(14).mean() / atr_smooth.replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(14).mean() / atr_smooth.replace(0, np.nan))
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    df["adx"] = dx.rolling(14).mean()
    df["plus_di"] = plus_di
    df["minus_di"] = minus_di

    # Rate of Change (momentum confirmation)
    df["roc_10"] = df["Close"].pct_change(periods=10) * 100

    return df


def signal_ema_crossover(df: pd.DataFrame) -> float:
    """EMA crossover signal with EMA200 trend filter."""
    if len(df) < 3:
        return 0.0
    fast = df["ema_fast"].iloc[-1]
    slow = df["ema_slow"].iloc[-1]
    fast_prev = df["ema_fast"].iloc[-2]
    slow_prev = df["ema_slow"].iloc[-2]
    ema200 = df["ema_200"].iloc[-1]
    price = df["Close"].iloc[-1]

    # Base crossover signal
    signal = 0.0
    if fast > slow and fast_prev <= slow_prev:
        signal = 1.0  # Fresh bullish cross
    elif fast < slow and fast_prev >= slow_prev:
        signal = -1.0  # Fresh bearish cross
    elif fast > slow:
        spread = (fast - slow) / slow
        signal = min(spread * 100, 0.5)
    else:
        spread = (slow - fast) / fast
        signal = max(-spread * 100, -0.5)

    # Boost if aligned with EMA200 (major trend)
    if not pd.isna(ema200):
        if signal > 0 and price > ema200:
            signal *= 1.3  # Trading with the major trend
        elif signal < 0 and price < ema200:
            signal *= 1.3
        elif signal > 0 and price < ema200:
            signal *= 0.5  # Against major trend, weaken
        elif signal < 0 and price > ema200:
            signal *= 0.5

    return float(np.clip(signal, -1.0, 1.0))


def signal_rsi(df: pd.DataFrame) -> float:
    """RSI signal with divergence detection."""
    rsi = df["rsi"].iloc[-1]
    if pd.isna(rsi):
        return 0.0

    signal = 0.0
    if rsi < config.RSI_OVERSOLD:
        signal = (config.RSI_OVERSOLD - rsi) / config.RSI_OVERSOLD
    elif rsi > config.RSI_OVERBOUGHT:
        signal = -(rsi - config.RSI_OVERBOUGHT) / (100 - config.RSI_OVERBOUGHT)

    # RSI divergence: price makes new low but RSI doesn't (bullish divergence)
    if len(df) >= 20:
        price_low = df["Close"].iloc[-20:].min()
        rsi_at_price_low = df["rsi"].iloc[-20:].min()
        current_price = df["Close"].iloc[-1]

        if current_price <= price_low * 1.005 and rsi > rsi_at_price_low + 5:
            signal = max(signal, 0.6)  # Bullish divergence

        price_high = df["Close"].iloc[-20:].max()
        rsi_at_price_high = df["rsi"].iloc[-20:].max()
        if current_price >= price_high * 0.995 and rsi < rsi_at_price_high - 5:
            signal = min(signal, -0.6)  # Bearish divergence

    return float(np.clip(signal, -1.0, 1.0))


def signal_macd(df: pd.DataFrame) -> float:
    """MACD histogram signal with momentum confirmation."""
    hist = df["macd_hist"].iloc[-1]
    hist_prev = df["macd_hist"].iloc[-2] if len(df) > 1 else 0

    if pd.isna(hist):
        return 0.0

    atr = df["atr"].iloc[-1]
    if pd.isna(atr) or atr == 0:
        return 0.0

    normalized = hist / atr

    # Fresh histogram crossover (momentum shift) — strongest signal
    if hist > 0 and hist_prev <= 0:
        normalized += 0.4
    elif hist < 0 and hist_prev >= 0:
        normalized -= 0.4

    # Histogram accelerating (growing bars) — trend strengthening
    if len(df) >= 3:
        hist_prev2 = df["macd_hist"].iloc[-3]
        if not pd.isna(hist_prev2):
            if hist > hist_prev > hist_prev2 > 0:
                normalized += 0.2  # Accelerating bullish
            elif hist < hist_prev < hist_prev2 < 0:
                normalized -= 0.2  # Accelerating bearish

    return float(np.clip(normalized, -1.0, 1.0))


def signal_bollinger(df: pd.DataFrame) -> float:
    """Bollinger Band signal — mean reversion + breakout detection."""
    bb_pct = df["bb_pct"].iloc[-1]
    if pd.isna(bb_pct):
        return 0.0

    adx = df["adx"].iloc[-1] if not pd.isna(df["adx"].iloc[-1]) else 20

    # In trending market (high ADX): Bollinger breakout
    if adx > 30:
        if bb_pct > 1.0:
            return 0.5  # Breakout above = trend continuation (bullish)
        elif bb_pct < 0.0:
            return -0.5  # Breakout below = trend continuation (bearish)

    # In ranging market (low ADX): mean reversion
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
    """Multi-timeframe trend confirmation — strongest when all agree."""
    data = data_feed.fetch_multi_timeframe(pair)

    trends = {}
    for tf_name, df in data.items():
        if df is None or len(df) < 30:
            continue
        df = compute_indicators(df)
        price = df["Close"].iloc[-1]
        ema_slow = df["ema_slow"].iloc[-1]
        ema_200 = df["ema_200"].iloc[-1]
        if pd.isna(ema_slow):
            continue

        # Score based on EMA alignment
        score = 0.0
        if price > ema_slow:
            score += 0.5
        else:
            score -= 0.5

        if not pd.isna(ema_200):
            if price > ema_200:
                score += 0.5
            else:
                score -= 0.5

        trends[tf_name] = np.clip(score, -1.0, 1.0)

    if not trends:
        return 0.0

    values = list(trends.values())
    avg = np.mean(values)

    # All timeframes agree = very strong signal
    all_same_direction = all(v > 0 for v in values) or all(v < 0 for v in values)
    if all_same_direction:
        return float(np.clip(avg * 1.2, -1.0, 1.0))

    return float(np.clip(avg * 0.5, -1.0, 1.0))


def signal_sentiment(pair: str) -> float:
    """News sentiment signal for a pair."""
    try:
        return data_feed.get_pair_sentiment(pair) * 0.7
    except Exception as e:
        logger.debug(f"Sentiment error for {pair}: {e}")
        return 0.0


def signal_volatility() -> float:
    """VIX-based risk filter. High VIX = reduce exposure."""
    vix = data_feed.get_vix()
    if vix is None:
        return 0.0
    if vix > 30:
        return -0.5
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

    try:
        dxy_ticker = yf.Ticker("DX-Y.NYB")
        hist = dxy_ticker.history(period="5d", interval="1h")
        if hist.empty:
            return 0.0
        avg = hist["Close"].mean()
        bias = (dxy - avg) / avg * 10

        is_usd_base = clean.startswith("USD")
        if is_usd_base:
            return float(np.clip(bias, -0.5, 0.5))
        else:
            return float(np.clip(-bias, -0.5, 0.5))
    except Exception:
        return 0.0


def signal_adx_trend(df: pd.DataFrame) -> float:
    """ADX trend strength — directional signal using +DI/-DI."""
    adx = df["adx"].iloc[-1]
    plus_di = df["plus_di"].iloc[-1]
    minus_di = df["minus_di"].iloc[-1]

    if pd.isna(adx) or pd.isna(plus_di) or pd.isna(minus_di):
        return 0.0

    # ADX < 20 = no trend, don't trade
    if adx < config.MIN_ADX:
        return 0.0

    # Direction from DI lines, strength from ADX
    strength = min((adx - 20) / 30, 1.0)  # Normalize 20-50 range to 0-1

    if plus_di > minus_di:
        return strength * 0.7   # Bullish trend
    else:
        return -strength * 0.7  # Bearish trend


def signal_session_quality() -> float:
    """Session quality — boost during high-liquidity hours, dampen during low."""
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour

    sessions = config.TRADING_SESSIONS
    overlap_start = sessions["overlap_start"]
    overlap_end = sessions["overlap_end"]
    best_start = sessions["best_start"]
    best_end = sessions["best_end"]

    # London-NY overlap = best (boost signals)
    if overlap_start <= hour < overlap_end:
        return 0.3
    # London or NY session = good
    elif best_start <= hour < best_end:
        return 0.1
    # Asian session / off-hours = reduce
    else:
        return -0.3


def is_good_session() -> bool:
    """Check if current time is within tradeable session hours."""
    if not config.ONLY_TRADE_SESSIONS:
        return True
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    return config.TRADING_SESSIONS["best_start"] <= hour < config.TRADING_SESSIONS["best_end"]


def generate_signals(pair: str, df: pd.DataFrame) -> dict[str, float]:
    """Generate all signals for a pair."""
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
        "adx_trend": signal_adx_trend(df),
        "session_quality": signal_session_quality(),
    }

    return signals


def count_agreeing_signals(signals: dict[str, float], direction: str) -> int:
    """Count how many signals agree with the proposed direction."""
    count = 0
    for name, score in signals.items():
        if name in ("volatility", "session_quality"):
            continue  # These are filters, not directional
        if direction == "long" and score > 0.05:
            count += 1
        elif direction == "short" and score < -0.05:
            count += 1
    return count


def get_weighted_signal(signals: dict[str, float]) -> float:
    """Combine signals using learned weights. Returns -1 to +1."""
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


def get_trade_parameters(pair: str, df: pd.DataFrame, direction: str,
                         current_balance: float) -> dict:
    """Calculate stop-loss, take-profit, and position size.

    Uses current balance (not initial) for dynamic position sizing.
    """
    df = compute_indicators(df)
    atr = df["atr"].iloc[-1]
    price = df["Close"].iloc[-1]

    if pd.isna(atr) or atr <= 0:
        atr = price * 0.005

    if direction == "long":
        stop_loss = price - (atr * config.STOP_LOSS_ATR_MULT)
        take_profit = price + (atr * config.TAKE_PROFIT_ATR_MULT)
    else:
        stop_loss = price + (atr * config.STOP_LOSS_ATR_MULT)
        take_profit = price - (atr * config.TAKE_PROFIT_ATR_MULT)

    # Dynamic position sizing based on CURRENT balance
    risk_amount = current_balance * config.RISK_PER_TRADE
    risk_per_unit = abs(price - stop_loss)
    if risk_per_unit > 0:
        position_size = risk_amount / risk_per_unit
    else:
        position_size = 0

    max_size = current_balance * 0.2 / price
    position_size = min(position_size, max_size)

    return {
        "stop_loss": round(stop_loss, 5),
        "take_profit": round(take_profit, 5),
        "position_size": round(position_size, 2),
        "atr": round(atr, 5),
        "entry_price": round(price, 5),
    }
