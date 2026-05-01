from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    result = 100 - (100 / (1 + rs))
    return result.fillna(50.0)


def macd(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    macd_line = ema(series, 12) - ema(series, 26)
    signal_line = ema(macd_line, 9)
    return macd_line, signal_line


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def weekly_vwap(df: pd.DataFrame) -> pd.Series:
    out = pd.Series(index=df.index, dtype=float)
    if getattr(df.index, "tz", None) is not None:
        week_keys = df.index.tz_convert(None).to_period("W")
    else:
        week_keys = df.index.to_period("W")
    for week in week_keys.unique():
        mask = week_keys == week
        sub = df.loc[mask]
        typical = (sub["high"] + sub["low"] + sub["close"]) / 3
        cum_pv = (typical * sub["volume"]).cumsum()
        cum_vol = sub["volume"].replace(0, pd.NA).cumsum()
        out.loc[sub.index] = (cum_pv / cum_vol).ffill()
    return out


def anchored_vwap(df: pd.DataFrame, anchor_date: str | None = None) -> pd.Series:
    if anchor_date:
        anchor_ts = pd.Timestamp(anchor_date)
    else:
        anchor_ts = pd.Timestamp(year=df.index[-1].year, month=1, day=1)
    if getattr(df.index, "tz", None) is not None and anchor_ts.tzinfo is None:
        anchor_ts = anchor_ts.tz_localize(df.index.tz)
    anchored = df[df.index >= anchor_ts]
    result = pd.Series(index=df.index, dtype=float)
    if anchored.empty:
        return result
    typical = (anchored["high"] + anchored["low"] + anchored["close"]) / 3
    cum_pv = (typical * anchored["volume"]).cumsum()
    cum_vol = anchored["volume"].replace(0, pd.NA).cumsum()
    result.loc[anchored.index] = (cum_pv / cum_vol).ffill()
    return result


@dataclass(frozen=True)
class IndicatorSnapshot:
    daily_close: float
    daily_ema20: float
    daily_ema50: float
    daily_rsi14: float
    daily_macd: float
    daily_macd_signal: float
    daily_atr14: float
    daily_weekly_vwap: float
    daily_anchored_vwap: float
    daily_volume: float
    daily_vol_sma20: float
    h4_close: float
    h4_ema20: float
    h4_ema50: float


def build_snapshot(daily: pd.DataFrame, four_hour: pd.DataFrame, anchor_date: str | None = None) -> IndicatorSnapshot:
    d = daily.copy()
    h4 = four_hour.copy()

    d["ema20"] = ema(d["close"], 20)
    d["ema50"] = ema(d["close"], 50)
    d["rsi14"] = rsi(d["close"], 14)
    d["macd"], d["macd_signal"] = macd(d["close"])
    d["atr14"] = atr(d, 14)
    d["weekly_vwap"] = weekly_vwap(d)
    d["anchored_vwap"] = anchored_vwap(d, anchor_date)
    d["vol_sma20"] = d["volume"].rolling(20).mean()

    h4["ema20"] = ema(h4["close"], 20)
    h4["ema50"] = ema(h4["close"], 50)

    dl = d.iloc[-1]
    hl = h4.iloc[-1]
    return IndicatorSnapshot(
        daily_close=float(dl["close"]),
        daily_ema20=float(dl["ema20"]),
        daily_ema50=float(dl["ema50"]),
        daily_rsi14=float(dl["rsi14"]),
        daily_macd=float(dl["macd"]),
        daily_macd_signal=float(dl["macd_signal"]),
        daily_atr14=float(dl["atr14"]),
        daily_weekly_vwap=float(dl["weekly_vwap"]) if pd.notna(dl["weekly_vwap"]) else float(dl["close"]),
        daily_anchored_vwap=float(dl["anchored_vwap"]) if pd.notna(dl["anchored_vwap"]) else float(dl["close"]),
        daily_volume=float(dl["volume"]),
        daily_vol_sma20=float(dl["vol_sma20"]) if pd.notna(dl["vol_sma20"]) else float(dl["volume"]),
        h4_close=float(hl["close"]),
        h4_ema20=float(hl["ema20"]),
        h4_ema50=float(hl["ema50"]),
    )
