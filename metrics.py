"""
metrics.py — KPI calculations and Top-3 worst period detection.

All KPI denominators exclude invalid/stale rows per the spec:
  - CO2-based  → scd_status == VALID
  - PM-based   → pm_status  == VALID
  - CAI-based  → both VALID
  - SAFE rows are always excluded from sensor-dependent denominators.
"""

from __future__ import annotations
from datetime import date

import numpy as np
import pandas as pd

from data_adapter import CO2_HIGH, CAI_CAUTION, CAI_GOOD, CAI_EXCELLENT


# ══════════════════════════════════════════════════════════════════
# VALIDITY MASKS
# ══════════════════════════════════════════════════════════════════

def _co2_valid(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["scd_status"] == "VALID"]

def _pm_valid(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["pm_status"] == "VALID"]

def _both_valid(df: pd.DataFrame) -> pd.DataFrame:
    return df[(df["pm_status"] == "VALID") & (df["scd_status"] == "VALID")]


# ══════════════════════════════════════════════════════════════════
# KPI CALCULATIONS
# ══════════════════════════════════════════════════════════════════

def pct_co2_high(df: pd.DataFrame) -> float:
    """% of valid CO2 samples where CO2 > 1000 ppm."""
    valid = _co2_valid(df)
    if valid.empty:
        return 0.0
    return float((valid["co2"] > CO2_HIGH).mean() * 100)


def max_co2_today(df: pd.DataFrame) -> float:
    """Maximum CO2 reading from valid samples."""
    valid = _co2_valid(df)
    if valid.empty:
        return 0.0
    return float(valid["co2"].max())


def avg_pm25_today(df: pd.DataFrame) -> float:
    """Average PM2.5 from valid PM samples."""
    valid = _pm_valid(df)
    if valid.empty:
        return 0.0
    return float(valid["pm25"].mean())


def pct_cai_risk(df: pd.DataFrame) -> float:
    """% of samples (both sensors valid) where CAI < 40 (RISK level)."""
    valid = _both_valid(df)
    if valid.empty:
        return 0.0
    return float((valid["cai"] < CAI_CAUTION).mean() * 100)


def data_coverage_pct(df: pd.DataFrame) -> float:
    """
    % of expected 5-second samples today that are valid for analysis.
    Expected count = elapsed time from first to last sample / 5 s.
    """
    if df.empty:
        return 0.0
    today_df = df[df["ts"].dt.date == date.today()]
    if today_df.empty:
        return 0.0
    t_min = today_df["ts"].min()
    t_max = today_df["ts"].max()
    elapsed_s = (t_max - t_min).total_seconds()
    expected = max(1, int(elapsed_s / 5) + 1)
    valid_count = ((today_df["pm_status"] == "VALID") &
                   (today_df["scd_status"] == "VALID")).sum()
    return float(min(100.0, valid_count / expected * 100))


# ══════════════════════════════════════════════════════════════════
# CAI ZONE DISTRIBUTION
# ══════════════════════════════════════════════════════════════════

def cai_zone_distribution(df: pd.DataFrame) -> dict[str, float]:
    """
    Returns dict of CAI zone → % time.
    SAFE is computed from all rows; other zones from both-valid rows.
    """
    zones = {z: 0.0 for z in ["EXCELLENT", "GOOD", "CAUTION", "RISK", "SAFE"]}
    if df.empty:
        return zones

    total_all = max(len(df), 1)
    zones["SAFE"] = float((df["level"] == "SAFE").sum() / total_all * 100)

    valid = _both_valid(df)
    if valid.empty:
        return zones

    total_v = max(len(valid), 1)
    cai = valid["cai"].fillna(0)
    zones["EXCELLENT"] = float((cai >= CAI_EXCELLENT).sum()                            / total_v * 100)
    zones["GOOD"]      = float(((cai >= CAI_GOOD) & (cai < CAI_EXCELLENT)).sum()
                                / total_v * 100)
    zones["CAUTION"]   = float(((cai >= CAI_CAUTION) & (cai < CAI_GOOD)).sum()
                                / total_v * 100)
    zones["RISK"]      = float((cai < CAI_CAUTION).sum() / total_v * 100)
    return zones


# ══════════════════════════════════════════════════════════════════
# TOP 3 WORST PERIODS
# ══════════════════════════════════════════════════════════════════

def top_worst_periods(df: pd.DataFrame,
                      n: int = 3,
                      window_min: int = 10) -> pd.DataFrame:
    """
    Find top N non-overlapping 10-minute windows ranked by average CO2.
    Only considers rows where scd_status == VALID.
    Returns DataFrame: start, end, avg_co2, peak_co2.
    """
    empty = pd.DataFrame(columns=["start", "end", "avg_co2", "peak_co2"])
    valid = _co2_valid(df).copy()
    if len(valid) < 2:
        return empty

    # Resample to 5 s grid to make rolling uniform
    valid = valid.set_index("ts").sort_index()
    co2_5s = valid["co2"].resample("5s").mean()

    win_samples = window_min * 60 // 5          # 10 min = 120 samples
    if len(co2_5s) < win_samples:
        return empty

    roll_avg = co2_5s.rolling(window=win_samples, min_periods=win_samples // 2).mean()
    roll_max = co2_5s.rolling(window=win_samples, min_periods=win_samples // 2).max()

    # Rank by highest rolling avg; pick non-overlapping windows
    sorted_ends = roll_avg.dropna().sort_values(ascending=False)
    min_gap = pd.Timedelta(minutes=window_min)

    results = []
    selected_ends: list[pd.Timestamp] = []

    for end_ts, avg_val in sorted_ends.items():
        # Skip if this window overlaps an already-selected one
        too_close = any(
            abs((end_ts - s).total_seconds()) < min_gap.total_seconds()
            for s in selected_ends
        )
        if too_close:
            continue

        start_ts = end_ts - pd.Timedelta(minutes=window_min)
        peak_val = roll_max.loc[end_ts]
        results.append({
            "start":    start_ts,
            "end":      end_ts,
            "avg_co2":  round(float(avg_val), 1),
            "peak_co2": int(peak_val) if not np.isnan(peak_val) else 0,
        })
        selected_ends.append(end_ts)
        if len(results) >= n:
            break

    return pd.DataFrame(results) if results else empty


# ══════════════════════════════════════════════════════════════════
# BUNDLE ALL KPIS
# ══════════════════════════════════════════════════════════════════

def compute_all_kpis(df_today: pd.DataFrame) -> dict:
    return {
        "pct_co2_high":  pct_co2_high(df_today),
        "max_co2":       max_co2_today(df_today),
        "avg_pm25":      avg_pm25_today(df_today),
        "pct_cai_risk":  pct_cai_risk(df_today),
        "data_coverage": data_coverage_pct(df_today),
    }
