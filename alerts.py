"""
alerts.py — Event-based alert detection.

Events are NOT per-sample warnings. Each event has:
  start, end (None if still open), type, peak_value,
  duration_min, reason_tag, fan_hepa, fan_exh

Cooldown: 10 minutes after an event ends before another of the same type can open.
"""

from __future__ import annotations
from datetime import timedelta

import numpy as np
import pandas as pd

from data_adapter import CO2_HIGH, CO2_URGENT, CO2_RECOVER, PM_GATE, PM_HIGH

COOLDOWN_S = 600  # 10 minutes in seconds

# ── Event type labels ─────────────────────────────────────────────
CO2_WARNING  = "CO2_WARNING"
CO2_CRITICAL = "CO2_CRITICAL"
PM_WARNING   = "PM_WARNING"
PM_CRITICAL  = "PM_CRITICAL"
SAFE_MODE    = "SAFE_MODE"


def _event_reason(event_type: str) -> str:
    return {
        CO2_WARNING:  "EXH BURST: CO2 high",
        CO2_CRITICAL: "EXH BURST: CO2 urgent",
        PM_WARNING:   "EXH BLOCKED: PM>35, HEPA active",
        PM_CRITICAL:  "EXH BLOCKED: PM>75, HEPA active",
        SAFE_MODE:    "SAFE MODE: sensor stale/invalid",
    }.get(event_type, "STABLE: monitoring")


# ══════════════════════════════════════════════════════════════════
# CORE STATE MACHINE
# ══════════════════════════════════════════════════════════════════

def _state_machine(
    df: pd.DataFrame,
    trigger_mask: np.ndarray,    # bool: event trigger condition
    end_mask: np.ndarray,        # bool: event end condition
    min_trigger_s: float,        # must be triggered this long to open
    min_end_s: float,            # must be ended this long to close
    event_type: str,
    peak_col: str,
) -> list[dict]:
    """
    4-state machine: IDLE → TRIGGERING → ACTIVE → ENDING → IDLE.
    Returns list of event dicts.
    """
    n = len(df)
    if n == 0:
        return []

    ts_arr    = df["ts"].values          # numpy datetime64
    trigger   = trigger_mask.astype(bool)
    end_cond  = end_mask.astype(bool)
    peak_vals = df[peak_col].values.astype(float) if peak_col in df.columns \
                else np.zeros(n)

    IDLE, TRIGGERING, ACTIVE, ENDING = 0, 1, 2, 3
    state           = IDLE
    trig_start_i    = 0
    event_start_i   = 0
    end_start_i     = 0
    last_end_ts: pd.Timestamp | None = None

    events: list[dict] = []

    def _ts(i: int) -> pd.Timestamp:
        return pd.Timestamp(ts_arr[i])

    def _elapsed(i: int, j: int) -> float:
        return float((_ts(i) - _ts(j)).total_seconds())

    def _close(end_i: int) -> None:
        nonlocal state, last_end_ts
        sl = slice(event_start_i, end_i + 1)
        pv = peak_vals[sl]
        valid_pv = pv[~np.isnan(pv)]
        pk_val   = float(np.nanmax(pv)) if len(valid_pv) > 0 else 0.0
        pk_rel   = int(np.nanargmax(pv)) if len(valid_pv) > 0 else 0
        pk_abs   = event_start_i + pk_rel

        event_end_ts   = _ts(end_i)
        event_start_ts = _ts(event_start_i)
        dur_min = (event_end_ts - event_start_ts).total_seconds() / 60.0

        events.append({
            "start":        event_start_ts,
            "end":          event_end_ts,
            "type":         event_type,
            "peak_value":   round(pk_val, 1),
            "duration_min": round(dur_min, 1),
            "reason_tag":   _event_reason(event_type),
            "fan_hepa":     int(df["fan_hepa"].iat[pk_abs]),
            "fan_exh":      int(df["fan_exh"].iat[pk_abs]),
        })
        last_end_ts = event_end_ts
        state = IDLE

    def _close_open() -> None:
        """Close an event still open at end of data (end=None)."""
        sl = slice(event_start_i, n)
        pv = peak_vals[sl]
        valid_pv = pv[~np.isnan(pv)]
        pk_val   = float(np.nanmax(pv)) if len(valid_pv) > 0 else 0.0
        pk_rel   = int(np.nanargmax(pv)) if len(valid_pv) > 0 else 0
        pk_abs   = event_start_i + pk_rel
        dur_min  = _elapsed(n - 1, event_start_i) / 60.0

        events.append({
            "start":        _ts(event_start_i),
            "end":          None,          # still ongoing
            "type":         event_type,
            "peak_value":   round(pk_val, 1),
            "duration_min": round(dur_min, 1),
            "reason_tag":   _event_reason(event_type),
            "fan_hepa":     int(df["fan_hepa"].iat[pk_abs]),
            "fan_exh":      int(df["fan_exh"].iat[pk_abs]),
        })

    for i in range(n):
        t = _ts(i)

        if state == IDLE:
            in_cooldown = (last_end_ts is not None and
                           (t - last_end_ts).total_seconds() < COOLDOWN_S)
            if not in_cooldown and trigger[i]:
                state       = TRIGGERING
                trig_start_i = i

        elif state == TRIGGERING:
            if not trigger[i]:
                state = IDLE          # trigger broke; reset
            elif _elapsed(i, trig_start_i) >= min_trigger_s:
                state         = ACTIVE
                event_start_i = trig_start_i

        elif state == ACTIVE:
            if end_cond[i]:
                state       = ENDING
                end_start_i = i
            # If trigger breaks but end condition not yet met, stay ACTIVE

        elif state == ENDING:
            if not end_cond[i]:
                state = ACTIVE        # end condition broke; back to active
            elif _elapsed(i, end_start_i) >= min_end_s:
                _close(end_start_i)   # event ends at start of recovery run

    # Handle event still open when data ends
    if state in (ACTIVE, ENDING):
        _close_open()

    return events


# ══════════════════════════════════════════════════════════════════
# PUBLIC INTERFACE
# ══════════════════════════════════════════════════════════════════

@st.cache_data(show_spinner=False)
def detect_events(df: pd.DataFrame) -> pd.DataFrame:
    """
    Detect all alert events across all event types.
    Returns a DataFrame sorted by start time.
    """
    import streamlit as st          # import here to avoid circular at module level
    if df.empty:
        return _empty_events()

    df = df.sort_values("ts").reset_index(drop=True)
    all_events: list[dict] = []

    # ── CO2_WARNING: co2>=1000 for 5 min, ends co2<=900 for 3 min ─
    co2_ok = df["scd_status"] == "VALID"
    trig_w  = (df["co2"] >= CO2_HIGH)  & co2_ok
    end_w   = (df["co2"] <= CO2_RECOVER) & co2_ok
    all_events += _state_machine(df, trig_w.values, end_w.values,
                                  300, 180, CO2_WARNING, "co2")

    # ── CO2_CRITICAL: co2>=1500 for 2 min, ends co2<=900 for 3 min ─
    trig_c  = (df["co2"] >= CO2_URGENT) & co2_ok
    all_events += _state_machine(df, trig_c.values, end_w.values,
                                  120, 180, CO2_CRITICAL, "co2")

    # ── PM_WARNING: pm25>=35 for 3 min, ends pm25<35 for 3 min ─────
    pm_ok   = df["pm_status"] == "VALID"
    trig_pw = (df["pm25"] >= PM_GATE) & pm_ok
    end_pm  = (df["pm25"] < PM_GATE)  & pm_ok
    all_events += _state_machine(df, trig_pw.values, end_pm.values,
                                  180, 180, PM_WARNING, "pm25")

    # ── PM_CRITICAL: pm25>=75 for 1 min, ends pm25<35 for 3 min ────
    trig_pc = (df["pm25"] >= PM_HIGH) & pm_ok
    all_events += _state_machine(df, trig_pc.values, end_pm.values,
                                  60, 180, PM_CRITICAL, "pm25")

    # ── SAFE_MODE: any sensor invalid/stale for 60 s ────────────────
    trig_s  = (df["pm_status"].isin(["STALE", "INVALID"]) |
                df["scd_status"].isin(["STALE", "INVALID"]))
    end_s   = ((df["pm_status"] == "VALID") & (df["scd_status"] == "VALID"))
    # use co2 as proxy peak col for safe events (peak_value ~= 0 if all invalid)
    all_events += _state_machine(df, trig_s.values, end_s.values,
                                  60, 60, SAFE_MODE, "co2")

    if not all_events:
        return _empty_events()

    ev_df = pd.DataFrame(all_events).sort_values("start").reset_index(drop=True)
    return ev_df


def get_active_events(events: pd.DataFrame) -> pd.DataFrame:
    """Return events that are currently open (end is None/NaT)."""
    if events.empty:
        return events
    return events[events["end"].isna()].reset_index(drop=True)


def _empty_events() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "start", "end", "type", "peak_value",
        "duration_min", "reason_tag", "fan_hepa", "fan_exh"])


# Guard: st is imported inside detect_events to keep top-level imports clean.
# Expose the cache decorator separately so tests can call without Streamlit.
import streamlit as st   # noqa: E402  (needed for @st.cache_data on detect_events)
