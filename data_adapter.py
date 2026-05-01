"""
data_adapter.py — CSV ingestion, schema normalisation, CAI computation.
Supports upload mode (bytes) and live-ish mode (file path).
"""

from __future__ import annotations
import io
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

# ══════════════════════════════════════════════════════════════════
# LOCKED THRESHOLDS  (single source of truth for the whole app)
# ══════════════════════════════════════════════════════════════════
PM_GATE      = 35
PM_HIGH      = 75
CO2_HIGH     = 1000
CO2_URGENT   = 1500
CO2_RECOVER  = 900
CAI_EXCELLENT = 80
CAI_GOOD      = 60
CAI_CAUTION   = 40

# ══════════════════════════════════════════════════════════════════
# COLUMN ALIASES — maps raw CSV column names → canonical names
# ══════════════════════════════════════════════════════════════════
_ALIASES: dict[str, list[str]] = {
    "ts":         ["ts", "timestamp", "time", "datetime", "Timestamp"],
    "pm25":       ["pm25", "PM25", "pm2_5", "pm2.5", "PM2.5", "PM", "pm"],
    "co2":        ["co2", "CO2", "co2_ppm", "CO2_ppm"],
    "temp_c":     ["temp_c", "temp", "T", "temperature", "temp_celsius"],
    "rh":         ["rh", "RH", "humidity", "rh_pct", "RH_pct"],
    "cai":        ["cai", "CAI"],
    "level":      ["level", "Level", "air_level", "AirLevel"],
    "fan_hepa":   ["fan_hepa", "HEPA", "hepa", "Fan_HEPA"],
    "fan_exh":    ["fan_exh", "EXH", "exh", "exhaust", "Fan_EXH"],
    "pm_status":  ["pm_status", "pm_stat", "PMStatus"],
    "scd_status": ["scd_status", "scd_stat", "SCDStatus"],
}

# Canonical levels (Arduino firmware uses OK; we normalise to EXCELLENT)
_LEVEL_REMAP = {"OK": "EXCELLENT"}

# ══════════════════════════════════════════════════════════════════
# SCORING FUNCTIONS  (vectorised — operate on numpy arrays)
# ══════════════════════════════════════════════════════════════════

def _pm_score(pm: np.ndarray) -> np.ndarray:
    return np.where(pm <= 15, 100,
           np.where(pm <= 35,  80,
           np.where(pm <= 75,  50, 20))).astype(float)


def _co2_score(co2: np.ndarray) -> np.ndarray:
    return np.where(co2 <= 800,  100,
           np.where(co2 <= 1000,  85,
           np.where(co2 <= 1500,  60, 30))).astype(float)


def _temp_score(t: np.ndarray) -> np.ndarray:
    return np.where((t >= 24) & (t <= 28), 100,
           np.where((t > 28)  & (t <= 31),  85,
           np.where((t > 31)  & (t <= 34),  70, 50))).astype(float)


def _rh_score(rh: np.ndarray) -> np.ndarray:
    return np.where((rh >= 40) & (rh <= 60), 100,
           np.where(((rh > 60) & (rh <= 70)) | ((rh >= 30) & (rh < 40)), 85,
           np.where(((rh > 70) & (rh <= 80)) | ((rh >= 20) & (rh < 30)), 70, 50))
           ).astype(float)


def _compute_cai_array(pm: np.ndarray, co2: np.ndarray,
                        temp: np.ndarray, rh: np.ndarray) -> np.ndarray:
    """Compute CAI for arrays; NaN propagates where any input is NaN."""
    ps = _pm_score(pm);   ps = np.where(np.isnan(pm),  np.nan, ps)
    cs = _co2_score(co2); cs = np.where(np.isnan(co2), np.nan, cs)
    ts = _temp_score(temp); ts = np.where(np.isnan(temp), np.nan, ts)
    rs = _rh_score(rh);    rs = np.where(np.isnan(rh),  np.nan, rs)
    comfort = (ts + rs) / 2.0
    raw = 0.65 * ps + 0.25 * cs + 0.10 * comfort
    return np.clip(np.round(raw), 0, 100)


# ══════════════════════════════════════════════════════════════════
# REASON TAG  (vectorised)
# ══════════════════════════════════════════════════════════════════

def compute_reason_tags(df: pd.DataFrame) -> pd.Series:
    """Priority-ordered one-line reason tag for every row."""
    safe_m   = df["level"] == "SAFE"
    pm_m     = df["pm25"].fillna(0) >= PM_GATE
    co2u_m   = df["co2"].fillna(0) >= CO2_URGENT
    co2h_m   = df["co2"].fillna(0) >= CO2_HIGH
    tags = np.where(safe_m,    "SAFE MODE: sensor stale/invalid",
           np.where(pm_m,      "EXH BLOCKED: PM>35, HEPA active",
           np.where(co2u_m,    "EXH BURST: CO2 urgent",
           np.where(co2h_m,    "EXH BURST: CO2 high",
                               "STABLE: monitoring"))))
    return pd.Series(tags, index=df.index, dtype="string")


# ══════════════════════════════════════════════════════════════════
# INTERNAL PIPELINE STEPS
# ══════════════════════════════════════════════════════════════════

def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename aliased raw column names to canonical names."""
    rename = {}
    for canonical, aliases in _ALIASES.items():
        if canonical in df.columns:
            continue
        for alias in aliases:
            if alias in df.columns:
                rename[alias] = canonical
                break
    return df.rename(columns=rename)


def _parse_timestamps(df: pd.DataFrame) -> pd.DataFrame:
    """
    Reconstruct `ts` as proper datetime.
    Priority:
      1. Numeric elapsed-seconds column → today midnight + elapsed
      2. Datetime string column → parse directly
      3. Fallback → today midnight + row_index × 5 s
    """
    midnight = datetime.combine(date.today(), datetime.min.time())

    def _fallback_series() -> pd.Series:
        return pd.Series(
            [midnight + timedelta(seconds=i * 5) for i in range(len(df))],
            index=df.index, dtype="datetime64[ns]")

    if "ts" not in df.columns:
        df["ts"] = _fallback_series()
        return df

    numeric = pd.to_numeric(df["ts"], errors="coerce")
    if numeric.notna().mean() > 0.8:          # majority numeric → elapsed seconds
        df["ts"] = pd.Timestamp(midnight) + pd.to_timedelta(numeric, unit="s")
        df["ts"] = df["ts"].fillna(_fallback_series())
        return df

    parsed = pd.to_datetime(df["ts"], errors="coerce", infer_datetime_format=True)
    if parsed.notna().mean() > 0.8:
        df["ts"] = parsed.fillna(_fallback_series())
        return df

    df["ts"] = _fallback_series()
    return df


def _coerce_numerics(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce sensor columns to float; fan flags to int."""
    for col in ("pm25", "co2", "temp_c", "rh", "cai"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = np.nan
    for col in ("fan_hepa", "fan_exh"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
        else:
            df[col] = 0
    return df


def _fill_statuses(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure pm_status and scd_status columns exist and are upper-cased."""
    for stat_col, sensor_col in (("pm_status", "pm25"), ("scd_status", "co2")):
        if stat_col in df.columns:
            df[stat_col] = df[stat_col].fillna("INVALID").str.strip().str.upper()
        else:
            df[stat_col] = np.where(df[sensor_col].isna(), "INVALID", "VALID")
        # If sensor value is NaN, status is always INVALID regardless of what CSV says
        df.loc[df[sensor_col].isna(), stat_col] = "INVALID"
    return df


def _compute_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Compute CAI (if missing), Level, and reason_tag."""
    # CAI
    need_cai = "cai" not in df.columns or df["cai"].isna().all()
    if need_cai:
        df["cai"] = _compute_cai_array(
            df["pm25"].values, df["co2"].values,
            df["temp_c"].values, df["rh"].values)
    df["cai"] = pd.to_numeric(df["cai"], errors="coerce").clip(0, 100).round()

    # Level
    safe_mask = (
        df["pm_status"].isin(["STALE", "INVALID"]) |
        df["scd_status"].isin(["STALE", "INVALID"])
    )
    cai = df["cai"].fillna(0)
    computed_level = np.where(safe_mask,       "SAFE",
                     np.where(cai >= 80,        "EXCELLENT",
                     np.where(cai >= 60,        "GOOD",
                     np.where(cai >= 40,        "CAUTION", "RISK"))))

    if "level" in df.columns and not df["level"].isna().all():
        # Use CSV level but normalise aliases and only override SAFE from sensor status
        df["level"] = df["level"].str.strip().str.upper().replace(_LEVEL_REMAP)
        # Sensor safety overrides everything
        df.loc[safe_mask, "level"] = "SAFE"
    else:
        df["level"] = computed_level

    # Reason tag (vectorised)
    df["reason_tag"] = compute_reason_tags(df)
    return df


def _process(raw: pd.DataFrame) -> pd.DataFrame:
    """Full processing pipeline on a raw DataFrame."""
    if raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    df = _normalise_columns(df)
    df = _parse_timestamps(df)
    df = _coerce_numerics(df)
    df = _fill_statuses(df)
    df = _compute_derived(df)
    df = df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    return df


# ══════════════════════════════════════════════════════════════════
# PUBLIC LOADERS
# ══════════════════════════════════════════════════════════════════

@st.cache_data(show_spinner=False, max_entries=3)
def load_csv_cached(file_bytes: bytes, _filename: str) -> pd.DataFrame:
    """Load and process CSV from uploaded file bytes (Streamlit cached)."""
    try:
        raw = pd.read_csv(io.BytesIO(file_bytes), on_bad_lines="skip",
                          low_memory=False)
        return _process(raw)
    except Exception:
        return pd.DataFrame()


def load_csv_path_full(path: str) -> pd.DataFrame:
    """Load and process entire CSV from a file path (live mode)."""
    try:
        raw = pd.read_csv(path, on_bad_lines="skip", low_memory=False)
        return _process(raw)
    except Exception:
        return pd.DataFrame()


def load_from_sheets_json(json_data: list) -> pd.DataFrame:
    """Convert Google Apps Script JSON array response to processed DataFrame."""
    if not json_data:
        return pd.DataFrame()
    try:
        rows = []
        for item in json_data:
            pm   = float(item.get("pm25", 0) or 0)
            co2  = float(item.get("co2",  0) or 0)
            rows.append({
                "ts":         item.get("timestamp", ""),
                "pm25":       pm,
                "co2":        co2,
                "temp_c":     float(item.get("temp", 0) or 0),
                "rh":         float(item.get("rh",   0) or 0),
                "cai":        int(item.get("cai",   0) or 0),
                "level":      str(item.get("level", "SAFE")),
                "fan_hepa":   int(item.get("fan",   0) or 0),
                "fan_exh":    0,
                "pm_status":  "VALID"   if pm  >= 0 else "INVALID",
                "scd_status": "VALID"   if co2 > 0  else "INVALID",
            })
        df = pd.DataFrame(rows)
        if df.empty:
            return df

        # Parse timestamps
        # Google Sheets returns: "Fri May 01 2026 13:39:24 GMT+0700 (Indochina Time)"
        # Strip the "(TIMEZONE NAME)" suffix before parsing
        import re
        def _parse_ts(s):
            s = re.sub(r'\s*\([^)]*\)', '', str(s)).strip()  # remove "(Indochina Time)"
            try:
                t = pd.to_datetime(s, utc=False)
                # strip tz offset if present
                if t.tzinfo is not None:
                    t = t.tz_localize(None) if hasattr(t, 'tz_localize') else t.replace(tzinfo=None)
                return t
            except Exception:
                return pd.NaT

        df["ts"] = df["ts"].apply(_parse_ts)
        df = df.dropna(subset=["ts"])
        if df.empty:
            return df
        df = _compute_derived(df)
        return df.sort_values("ts").reset_index(drop=True)
    except Exception as e:
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

def filter_today(df: pd.DataFrame) -> pd.DataFrame:
    """Return only rows whose timestamp is today (local date)."""
    if df.empty:
        return df
    mask = df["ts"].dt.date == date.today()
    return df.loc[mask].reset_index(drop=True)


def get_last_valid_row(df: pd.DataFrame) -> dict | None:
    """Return the most recent row as a dict; prefer VALID sensor rows."""
    if df.empty:
        return None
    # prefer both sensors valid, else accept any last row
    valid = df[(df["pm_status"] == "VALID") & (df["scd_status"] == "VALID")]
    source = valid if not valid.empty else df
    return source.iloc[-1].to_dict()


def downsample(df: pd.DataFrame, max_points: int = 2000) -> pd.DataFrame:
    """Thin a large DataFrame for charting — keeps last `max_points` rows."""
    if len(df) <= max_points:
        return df
    step = len(df) // max_points
    return df.iloc[::step].reset_index(drop=True)
