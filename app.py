"""
app.py — SmartLungBox Teacher Dashboard v1
Run: streamlit run app.py
"""

from __future__ import annotations
import io
import os
import time
from datetime import date, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from alerts import detect_events, get_active_events
from data_adapter import (
    CAI_CAUTION, CAI_EXCELLENT, CAI_GOOD,
    CO2_HIGH, CO2_RECOVER, CO2_URGENT,
    PM_GATE, PM_HIGH,
    downsample, filter_today, get_last_valid_row,
    load_csv_cached, load_csv_path_full, load_from_sheets_json,
)
from metrics import (
    cai_zone_distribution, compute_all_kpis, top_worst_periods,
)

# ══════════════════════════════════════════════════════════════════
# PAGE CONFIG
# ══════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="SmartLungBox — ห้องเรียน",
    page_icon="🫁",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ────────────────────────────────────────────────────
st.markdown("""
<style>
  /* Dark card style */
  .kpi-card {
    background: #0D1B2A; border: 1px solid #1A5E8A;
    border-radius: 10px; padding: 14px 18px; text-align: center;
  }
  .kpi-val  { font-size: 2rem; font-weight: 700; color: #00D4C2; }
  .kpi-lbl  { font-size: 0.78rem; color: #7A9BB5; margin-top: 2px; }
  .kpi-unit { font-size: 0.75rem; color: #5A7A96; }
  /* Status badges */
  .badge-on  { background:#1B4F2A; color:#4CAF50; padding:4px 10px;
               border-radius:8px; font-weight:700; }
  .badge-off { background:#1A2A3A; color:#5A7A96; padding:4px 10px;
               border-radius:8px; font-weight:700; }
  .badge-safe { background:#3A1A1A; color:#E63946; padding:6px 14px;
                border-radius:8px; font-weight:700; font-size:1.05rem; }
  /* Reason tag */
  .reason-tag { background:#122030; border-left:4px solid #00B4A6;
                padding:8px 14px; border-radius:0 8px 8px 0;
                color:#A8D8D0; font-size:0.85rem; }
</style>
""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════
# SESSION STATE INIT
# ══════════════════════════════════════════════════════════════════
_DEFAULTS = {
    "mode":              "upload",
    "live_path":         "",
    "live_df":           pd.DataFrame(),
    "live_mtime":        0.0,
    "upload_df":         pd.DataFrame(),
    "auto_refresh":      False,
    "last_refresh":      0.0,
    "sheets_url":        "",
    "sheets_df":         pd.DataFrame(),
    "sheets_last_fetch": 0.0,
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v
ss = st.session_state

# ══════════════════════════════════════════════════════════════════
# COLOUR MAPS
# ══════════════════════════════════════════════════════════════════
LEVEL_COLOR = {
    "EXCELLENT": "#4CAF50",
    "GOOD":      "#8BC34A",
    "CAUTION":   "#FF9800",
    "RISK":      "#F44336",
    "SAFE":      "#607D8B",
}
LEVEL_THAI = {
    "EXCELLENT": "ดีเยี่ยม",
    "GOOD":      "ดี",
    "CAUTION":   "ระวัง",
    "RISK":      "อันตราย",
    "SAFE":      "SAFE MODE",
}

# ══════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════
with st.sidebar:
    st.title("🫁 SmartLungBox")
    st.caption("ระบบติดตามคุณภาพอากาศ v1")
    st.divider()

    mode_label = st.radio(
        "โหมดข้อมูล",
        ["📂 อัปโหลดไฟล์ (Offline)", "📡 ไฟล์สด (Live)", "☁️ Google Sheets (Auto)"],
        index={"upload": 0, "live": 1, "sheets": 2}.get(ss.mode, 0),
    )
    ss.mode = "upload" if "📂" in mode_label else ("sheets" if "☁️" in mode_label else "live")

    # ── Upload mode ───────────────────────────────────────────────
    if ss.mode == "upload":
        uploaded = st.file_uploader(
            "เลือกไฟล์ CSV จาก SmartLungBox",
            type=["csv"],
            key="uploader",
        )
        if uploaded is not None:
            with st.spinner("กำลังโหลดข้อมูล…"):
                ss.upload_df = load_csv_cached(
                    uploaded.getvalue(), uploaded.name)
            if not ss.upload_df.empty:
                st.success(f"โหลดแล้ว {len(ss.upload_df):,} แถว ✓")
            else:
                st.error("ไม่สามารถอ่านไฟล์ได้ — ตรวจสอบรูปแบบ CSV")
        df_all = ss.upload_df

    # ── Live mode ─────────────────────────────────────────────────
    elif ss.mode == "live":
        path_input = st.text_input(
            "เส้นทางไฟล์ CSV",
            value=ss.live_path,
            placeholder="C:\\airlog.csv  หรือ  /home/pi/airlog.csv",
        )
        ss.live_path = path_input.strip()

        if ss.live_path and os.path.exists(ss.live_path):
            try:
                mtime = os.path.getmtime(ss.live_path)
                if mtime != ss.live_mtime or ss.live_df.empty:
                    with st.spinner("โหลด…"):
                        ss.live_df = load_csv_path_full(ss.live_path)
                    ss.live_mtime = mtime
                st.success(
                    f"📄 {os.path.basename(ss.live_path)}  "
                    f"| {len(ss.live_df):,} แถว"
                )
            except Exception as exc:
                st.error(f"อ่านไฟล์ไม่ได้: {exc}")
        elif ss.live_path:
            st.error("❌ ไม่พบไฟล์ — ตรวจสอบเส้นทาง")

        ss.auto_refresh = st.toggle(
            "🔄 รีเฟรชอัตโนมัติ (5 วินาที)",
            value=ss.auto_refresh,
        )
        df_all = ss.live_df

    # ── Google Sheets mode ────────────────────────────────────────
    elif ss.mode == "sheets":
        import urllib.request as _urlreq
        import json as _json

        url_input = st.text_input(
            "Google Apps Script URL",
            value=ss.sheets_url,
            placeholder="https://script.google.com/macros/s/.../exec",
        )
        ss.sheets_url = url_input.strip()

        FETCH_INTERVAL = 300  # 5 นาที

        col_btn, col_status = st.columns([1, 2])
        force_refresh = col_btn.button("🔄 ดึงข้อมูลเดี๋ยวนี้")

        if ss.sheets_url:
            now = time.time()
            need_fetch = (
                force_refresh
                or ss.sheets_df.empty
                or (now - ss.sheets_last_fetch) > FETCH_INTERVAL
            )
            if need_fetch:
                with st.spinner("กำลังดึงข้อมูลจาก Google Sheets…"):
                    try:
                        with _urlreq.urlopen(ss.sheets_url, timeout=20) as resp:
                            data = _json.loads(resp.read().decode())
                        ss.sheets_df = load_from_sheets_json(data)
                        ss.sheets_last_fetch = time.time()
                    except Exception as exc:
                        st.error(f"ดึงข้อมูลไม่ได้: {exc}")

            if not ss.sheets_df.empty:
                remaining = int(FETCH_INTERVAL - (time.time() - ss.sheets_last_fetch))
                col_status.success(f"{len(ss.sheets_df):,} แถว | รีเฟรชอีก {remaining//60}:{remaining%60:02d} นาที")

        df_all = ss.sheets_df

    # ── Footer info ───────────────────────────────────────────────
    st.divider()
    if not df_all.empty:
        st.caption(f"ข้อมูลล่าสุด: {df_all['ts'].max().strftime('%d/%m %H:%M:%S')}")
        st.caption(f"แถวทั้งหมด: {len(df_all):,}")
    else:
        st.caption("⏳ รอข้อมูล…")

# ══════════════════════════════════════════════════════════════════
# EARLY EXIT — no data
# ══════════════════════════════════════════════════════════════════
if df_all.empty:
    st.info("📂 กรุณาอัปโหลดไฟล์ CSV หรือระบุเส้นทางไฟล์ในแถบด้านซ้าย")
    st.stop()

df_today = filter_today(df_all)
last_row = get_last_valid_row(df_today) if not df_today.empty else get_last_valid_row(df_all)

# ══════════════════════════════════════════════════════════════════
# SHARED COMPUTATIONS (computed once, used across tabs)
# ══════════════════════════════════════════════════════════════════
kpis       = compute_all_kpis(df_today)
zones      = cai_zone_distribution(df_today)
worst3     = top_worst_periods(df_today)
events_all = detect_events(df_all)
events_today = (
    events_all[events_all["start"].dt.date == date.today()].reset_index(drop=True)
    if not events_all.empty else events_all
)
active_events = get_active_events(events_all)

# ══════════════════════════════════════════════════════════════════
# HELPER WIDGETS
# ══════════════════════════════════════════════════════════════════

def _kpi_card(col, label: str, value: str, unit: str = "", color: str = "#00D4C2") -> None:
    col.markdown(
        f'<div class="kpi-card">'
        f'<div class="kpi-val" style="color:{color}">{value}</div>'
        f'<div class="kpi-unit">{unit}</div>'
        f'<div class="kpi-lbl">{label}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _fan_badge(on: bool, label: str) -> str:
    cls = "badge-on" if on else "badge-off"
    state = "เปิด ✓" if on else "ปิด"
    return f'<span class="{cls}">{label}: {state}</span>'


def _level_color(row: dict | None) -> str:
    if row is None:
        return LEVEL_COLOR["SAFE"]
    return LEVEL_COLOR.get(str(row.get("level", "SAFE")), LEVEL_COLOR["SAFE"])


# ══════════════════════════════════════════════════════════════════
# CHART BUILDERS
# ══════════════════════════════════════════════════════════════════

def _cai_gauge(cai_val: int, level: str) -> go.Figure:
    color = LEVEL_COLOR.get(level, "#607D8B")
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=cai_val,
        number={"font": {"size": 52, "color": color, "family": "Arial"}},
        title={"text": "ดัชนีอากาศ (CAI)",
               "font": {"size": 15, "color": "#A8C8D8"}},
        gauge={
            "axis": {"range": [0, 100], "tickcolor": "#5A7A96",
                     "tickfont": {"color": "#5A7A96"}},
            "bar":  {"color": color, "thickness": 0.28},
            "bgcolor": "#0D1B2A",
            "bordercolor": "#0D1B2A",
            "steps": [
                {"range": [0,  40], "color": "#2D0B0B"},
                {"range": [40, 60], "color": "#2D1B0B"},
                {"range": [60, 80], "color": "#1B2D0B"},
                {"range": [80, 100],"color": "#0B2D1B"},
            ],
        },
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=260,
        margin={"l": 30, "r": 30, "t": 50, "b": 10},
    )
    return fig


def _chart_60min(df: pd.DataFrame) -> go.Figure:
    """Time-series chart: PM2.5, CO₂, CAI over last 60 minutes."""
    if df.empty:
        return go.Figure()

    cutoff  = df["ts"].max() - timedelta(hours=1)
    recent  = downsample(df[df["ts"] >= cutoff].copy(), max_points=720)

    pm_v  = recent[recent["pm_status"]  == "VALID"]
    co2_v = recent[recent["scd_status"] == "VALID"]

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.55, 0.45],
        vertical_spacing=0.06,
    )

    # PM2.5 + threshold
    fig.add_trace(go.Scatter(
        x=pm_v["ts"], y=pm_v["pm25"],
        name="PM2.5 (μg/m³)", line={"color": "#E63946", "width": 1.8},
        fill="tozeroy", fillcolor="rgba(230,57,70,0.08)",
    ), row=1, col=1)
    fig.add_hline(y=PM_GATE, line_dash="dash", line_color="#E63946",
                  line_width=1, opacity=0.6, row=1, col=1,
                  annotation_text="PM35", annotation_font_color="#E63946")
    fig.add_hline(y=PM_HIGH, line_dash="dot", line_color="#FF0000",
                  line_width=1, opacity=0.5, row=1, col=1,
                  annotation_text="PM75", annotation_font_color="#FF0000")

    # CO₂ + thresholds
    fig.add_trace(go.Scatter(
        x=co2_v["ts"], y=co2_v["co2"],
        name="CO₂ (ppm)", line={"color": "#F4A261", "width": 1.8},
        fill="tozeroy", fillcolor="rgba(244,162,97,0.06)",
    ), row=2, col=1)
    fig.add_hline(y=CO2_HIGH, line_dash="dash", line_color="#F4A261",
                  line_width=1, opacity=0.6, row=2, col=1,
                  annotation_text="1000", annotation_font_color="#F4A261")
    fig.add_hline(y=CO2_URGENT, line_dash="dot", line_color="#FF6B6B",
                  line_width=1, opacity=0.5, row=2, col=1,
                  annotation_text="1500", annotation_font_color="#FF6B6B")

    # CAI overlay on CO2 row (secondary y)
    fig.add_trace(go.Scatter(
        x=recent["ts"], y=recent["cai"].astype(float),
        name="CAI (0–100)", line={"color": "#00D4C2", "width": 1.5,
                                   "dash": "dot"},
        yaxis="y3",
    ), row=2, col=1)

    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(13,27,42,0.7)",
        font={"color": "#A8C8D8"},
        height=340,
        legend={
            "orientation": "h", "y": 1.04, "x": 0,
            "bgcolor": "rgba(0,0,0,0)", "font": {"size": 11},
        },
        margin={"l": 50, "r": 60, "t": 30, "b": 10},
        xaxis2={"title": "เวลา", "gridcolor": "#1A2A3A"},
        yaxis={"title": "PM2.5", "gridcolor": "#1A2A3A",
               "titlefont": {"color": "#E63946"}},
        yaxis2={"title": "CO₂ (ppm)", "gridcolor": "#1A2A3A",
                "titlefont": {"color": "#F4A261"}},
        yaxis3={"title": "CAI", "overlaying": "y2", "side": "right",
                "range": [0, 100], "showgrid": False,
                "titlefont": {"color": "#00D4C2"}},
    )
    return fig


def _cai_zone_bar(zones: dict) -> go.Figure:
    labels = list(zones.keys())
    values = [round(v, 1) for v in zones.values()]
    colors = [LEVEL_COLOR.get(l, "#607D8B") for l in labels]
    thai   = [LEVEL_THAI.get(l, l) for l in labels]

    fig = go.Figure(go.Bar(
        x=thai, y=values,
        marker_color=colors,
        text=[f"{v:.1f}%" for v in values],
        textposition="outside",
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(13,27,42,0.5)",
        font={"color": "#A8C8D8"},
        height=240,
        margin={"l": 20, "r": 20, "t": 20, "b": 40},
        yaxis={"title": "% เวลา", "gridcolor": "#1A2A3A", "range": [0, 105]},
        xaxis={"gridcolor": "#1A2A3A"},
        showlegend=False,
    )
    return fig


def _mode_timeline(df_today: pd.DataFrame) -> go.Figure:
    """Colour strip showing CAI level over the day."""
    if df_today.empty:
        return go.Figure()

    # Run-length encode to get segments
    df_s = df_today.sort_values("ts").reset_index(drop=True)
    df_s["_grp"] = (df_s["level"] != df_s["level"].shift()).cumsum()
    segs = df_s.groupby("_grp", sort=False).agg(
        level=("level", "first"),
        start=("ts", "first"),
        end=("ts", "last"),
    ).reset_index(drop=True)

    fig = go.Figure()
    # Dummy traces for legend
    for lvl, col in LEVEL_COLOR.items():
        fig.add_trace(go.Scatter(
            x=[None], y=[None], mode="markers",
            marker={"size": 10, "color": col},
            name=LEVEL_THAI.get(lvl, lvl),
        ))
    # Shapes for each segment
    for _, seg in segs.iterrows():
        fig.add_shape(
            type="rect",
            x0=seg["start"], x1=seg["end"] + timedelta(seconds=5),
            y0=0, y1=1,
            fillcolor=LEVEL_COLOR.get(seg["level"], "#607D8B"),
            line_width=0,
        )

    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(13,27,42,0.5)",
        height=80,
        margin={"l": 10, "r": 10, "t": 5, "b": 30},
        xaxis={"title": "เวลา (วันนี้)", "gridcolor": "#1A2A3A"},
        yaxis={"visible": False, "range": [0, 1]},
        legend={"orientation": "h", "y": -0.8, "x": 0,
                "bgcolor": "rgba(0,0,0,0)", "font": {"size": 10}},
        font={"color": "#A8C8D8"},
    )
    return fig


# ══════════════════════════════════════════════════════════════════
# EXPORT — TODAY REPORT PNG
# ══════════════════════════════════════════════════════════════════

def _generate_report_png(df_today: pd.DataFrame,
                         events_today: pd.DataFrame,
                         kpi: dict) -> bytes:
    """Generate a 1-page summary PNG using matplotlib."""
    # Try to support Thai; fall back silently if font unavailable
    plt.rcParams["font.family"] = [
        "TH Sarabun New", "Noto Sans Thai", "Arial Unicode MS",
        "DejaVu Sans", "sans-serif",
    ]

    BG, CARD, ACCENT, TEXT = "#0A1E30", "#0D1B2A", "#00D4C2", "#A8C8D8"
    co2_val = df_today[df_today["scd_status"] == "VALID"]["co2"] \
              if not df_today.empty else pd.Series(dtype=float)

    fig = plt.figure(figsize=(14, 9))
    fig.patch.set_facecolor(BG)

    gs = gridspec.GridSpec(
        3, 5, figure=fig,
        hspace=0.50, wspace=0.35,
        left=0.04, right=0.97, top=0.91, bottom=0.06,
    )

    # ── Title ─────────────────────────────────────────────────────
    fig.suptitle(
        f"SmartLungBox — รายงานประจำวัน | {date.today().strftime('%d %B %Y')}",
        color="white", fontsize=16, fontweight="bold", y=0.97,
    )

    # ── KPI cards (row 0) ─────────────────────────────────────────
    kpi_items = [
        ("% CO₂>1000",        f"{kpi['pct_co2_high']:.1f}%",    "#F4A261"),
        ("CO₂ สูงสุด",        f"{kpi['max_co2']:.0f} ppm",       "#F4A261"),
        ("PM2.5 เฉลี่ย",      f"{kpi['avg_pm25']:.1f} μg/m³",   "#E63946"),
        ("% CAI<40",          f"{kpi['pct_cai_risk']:.1f}%",     "#E63946"),
        ("ความครอบคลุมข้อมูล", f"{kpi['data_coverage']:.0f}%",   ACCENT),
    ]
    for col_i, (lbl, val, col) in enumerate(kpi_items):
        ax = fig.add_subplot(gs[0, col_i])
        ax.set_facecolor(CARD)
        ax.set_xticks([]); ax.set_yticks([])
        for sp in ax.spines.values():
            sp.set_color("#1A5E8A")
        ax.text(0.5, 0.58, val, ha="center", va="center",
                color=col, fontsize=17, fontweight="bold",
                transform=ax.transAxes)
        ax.text(0.5, 0.18, lbl, ha="center", va="center",
                color=TEXT, fontsize=8, transform=ax.transAxes)

    # ── CO₂ chart (row 1) ─────────────────────────────────────────
    ax_co2 = fig.add_subplot(gs[1, :])
    ax_co2.set_facecolor(CARD)
    for sp in ax_co2.spines.values():
        sp.set_color("#1A5E8A")
    ax_co2.tick_params(colors=TEXT, labelsize=7)
    ax_co2.set_ylabel("CO₂ (ppm)", color="#F4A261", fontsize=8)
    ax_co2.set_title("ระดับ CO₂ ตลอดวัน", color="white", fontsize=9, loc="left")

    if not co2_val.empty and not df_today.empty:
        valid_co2 = df_today[df_today["scd_status"] == "VALID"]
        ax_co2.plot(valid_co2["ts"], valid_co2["co2"],
                    color="#F4A261", linewidth=1.2, label="CO₂")
        ax_co2.axhline(CO2_HIGH,   color="#FF9800", linestyle="--",
                       linewidth=0.9, alpha=0.7, label="1000 ppm")
        ax_co2.axhline(CO2_URGENT, color="#E63946", linestyle="--",
                       linewidth=0.9, alpha=0.7, label="1500 ppm")
        ax_co2.legend(fontsize=7, facecolor=CARD, labelcolor=TEXT)
    else:
        ax_co2.text(0.5, 0.5, "ไม่มีข้อมูล CO₂", ha="center", va="center",
                    transform=ax_co2.transAxes, color=TEXT, fontsize=10)

    ax_co2.set_facecolor(CARD)

    # ── Events list (row 2) ───────────────────────────────────────
    ax_ev = fig.add_subplot(gs[2, :])
    ax_ev.set_facecolor(CARD)
    ax_ev.axis("off")
    ax_ev.set_title("เหตุการณ์วันนี้", color="white", fontsize=9, loc="left")
    for sp in ax_ev.spines.values():
        sp.set_color("#1A5E8A")

    if not events_today.empty:
        y_pos = 0.85
        for _, ev in events_today.head(6).iterrows():
            end_s = (ev["end"].strftime("%H:%M")
                     if pd.notna(ev.get("end")) and ev["end"] is not None
                     else "ยังคงเกิดขึ้น")
            line = (f"[{ev['type']}]  "
                    f"{ev['start'].strftime('%H:%M')} – {end_s}  |  "
                    f"สูงสุด {ev['peak_value']}  |  {ev['reason_tag']}")
            ax_ev.text(0.01, y_pos, line, transform=ax_ev.transAxes,
                       color="#F4A261", fontsize=7.5, va="top")
            y_pos -= 0.16
    else:
        ax_ev.text(0.5, 0.5, "ไม่มีเหตุการณ์วันนี้",
                   transform=ax_ev.transAxes,
                   color=TEXT, fontsize=10, ha="center", va="center")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════
# ACTIVE ALERT BANNER
# ══════════════════════════════════════════════════════════════════
if not active_events.empty:
    for _, ae in active_events.iterrows():
        st.markdown(
            f'<div class="badge-safe">🚨 กำลังเกิด: {ae["type"]} '
            f'(เริ่ม {ae["start"].strftime("%H:%M")}) — {ae["reason_tag"]}</div>',
            unsafe_allow_html=True,
        )
    st.write("")

# ══════════════════════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════════════════════
tab_live, tab_today, tab_alerts = st.tabs([
    "📡 สด (Live)", "📊 วันนี้", "🔔 การแจ้งเตือน",
])

# ──────────────────────────────────────────────────────────────────
# TAB 1 — LIVE
# ──────────────────────────────────────────────────────────────────
with tab_live:
    lr = last_row or {}
    cai_now   = int(lr.get("cai",   0))
    level_now = str(lr.get("level", "SAFE"))
    pm_now    = float(lr.get("pm25", 0))
    co2_now   = float(lr.get("co2",  0))
    temp_now  = float(lr.get("temp_c", 0))
    rh_now    = float(lr.get("rh",   0))
    hepa_on   = bool(int(lr.get("fan_hepa", 0)))
    exh_on    = bool(int(lr.get("fan_exh",  0)))
    reason    = str(lr.get("reason_tag", "STABLE: monitoring"))

    left_col, mid_col = st.columns([1.1, 2], gap="large")

    with left_col:
        # CAI gauge
        st.plotly_chart(
            _cai_gauge(cai_now, level_now),
            use_container_width=True,
            config={"displayModeBar": False},
        )
        level_col = LEVEL_COLOR.get(level_now, "#607D8B")
        level_th  = LEVEL_THAI.get(level_now, level_now)
        st.markdown(
            f"<h3 style='text-align:center;color:{level_col};margin:0'>"
            f"{level_th}</h3>",
            unsafe_allow_html=True,
        )

        st.write("")
        # Fan status
        st.markdown(
            f'{_fan_badge(hepa_on, "HEPA")} &nbsp;&nbsp;'
            f'{_fan_badge(exh_on, "พัดลมระบาย")}',
            unsafe_allow_html=True,
        )
        st.write("")
        # Reason tag
        st.markdown(
            f'<div class="reason-tag">ℹ️ {reason}</div>',
            unsafe_allow_html=True,
        )

    with mid_col:
        # Current value cards
        c1, c2, c3, c4 = st.columns(4)
        _kpi_card(c1, "ฝุ่น PM2.5",  f"{pm_now:.1f}",  "μg/m³",  "#E63946")
        _kpi_card(c2, "CO₂",          f"{co2_now:.0f}", "ppm",     "#F4A261")
        _kpi_card(c3, "อุณหภูมิ",    f"{temp_now:.1f}","°C",      "#4FC3F7")
        _kpi_card(c4, "ความชื้น",    f"{rh_now:.0f}",  "%",       "#80DEEA")

        st.write("")
        # 60-min chart
        st.markdown("**ย้อนหลัง 60 นาที**")
        st.plotly_chart(
            _chart_60min(df_all),
            use_container_width=True,
            config={"displayModeBar": False},
        )


# ──────────────────────────────────────────────────────────────────
# TAB 2 — TODAY
# ──────────────────────────────────────────────────────────────────
with tab_today:
    if df_today.empty:
        st.info("ไม่มีข้อมูลของวันนี้ในไฟล์")
    else:
        st.subheader("สรุป KPI วันนี้")

        # KPI row
        k1, k2, k3, k4, k5 = st.columns(5)
        _kpi_card(k1, "% เวลา CO₂>1000",
                  f"{kpis['pct_co2_high']:.1f}", "%", "#F4A261")
        _kpi_card(k2, "CO₂ สูงสุด",
                  f"{kpis['max_co2']:.0f}", "ppm", "#F4A261")
        _kpi_card(k3, "PM2.5 เฉลี่ย",
                  f"{kpis['avg_pm25']:.1f}", "μg/m³", "#E63946")
        _kpi_card(k4, "% เวลา CAI<40",
                  f"{kpis['pct_cai_risk']:.1f}", "%", "#E63946")
        _kpi_card(k5, "ความครอบคลุมข้อมูล",
                  f"{kpis['data_coverage']:.0f}", "%", "#00D4C2")

        st.write("")
        st.divider()

        col_a, col_b = st.columns([1, 1.5], gap="large")

        with col_a:
            st.subheader("สัดส่วนระดับอากาศ (%)")
            st.plotly_chart(
                _cai_zone_bar(zones),
                use_container_width=True,
                config={"displayModeBar": False},
            )

        with col_b:
            st.subheader("ช่วงเวลาที่แย่ที่สุด (TOP 3 — CO₂ เฉลี่ยสูงสุด)")
            if worst3.empty:
                st.info("ข้อมูลไม่เพียงพอ (ต้องการอย่างน้อย 10 นาที)")
            else:
                for rank, row in worst3.iterrows():
                    st.markdown(
                        f"**#{rank+1}**  "
                        f"{row['start'].strftime('%H:%M')} – "
                        f"{row['end'].strftime('%H:%M')}  |  "
                        f"CO₂ เฉลี่ย **{row['avg_co2']:.0f} ppm**  |  "
                        f"CO₂ สูงสุด **{row['peak_co2']} ppm**"
                    )

        st.write("")
        st.subheader("เส้นเวลาระดับอากาศ")
        st.plotly_chart(
            _mode_timeline(df_today),
            use_container_width=True,
            config={"displayModeBar": False},
        )

        # Export report
        st.write("")
        st.divider()
        col_dl, _ = st.columns([1, 3])
        with col_dl:
            if st.button("📥 ส่งออกรายงานวันนี้ (PNG)"):
                with st.spinner("กำลังสร้างรายงาน…"):
                    png_bytes = _generate_report_png(df_today, events_today, kpis)
                filename = f"SmartLungBox_TodayReport_{date.today().isoformat()}.png"
                st.download_button(
                    label="💾 ดาวน์โหลด PNG",
                    data=png_bytes,
                    file_name=filename,
                    mime="image/png",
                )


# ──────────────────────────────────────────────────────────────────
# TAB 3 — ALERTS
# ──────────────────────────────────────────────────────────────────
with tab_alerts:
    st.subheader("บันทึกเหตุการณ์ทั้งหมด")

    if events_all.empty:
        st.success("✅ ไม่พบเหตุการณ์ผิดปกติในข้อมูล")
    else:
        # Display table
        display_ev = events_all.copy()
        display_ev["end"] = display_ev["end"].apply(
            lambda x: x.strftime("%Y-%m-%d %H:%M") if pd.notna(x) and x is not None
            else "ยังคงเกิดขึ้น"
        )
        display_ev["start"] = display_ev["start"].dt.strftime("%Y-%m-%d %H:%M")
        display_ev = display_ev.rename(columns={
            "start":        "เริ่ม",
            "end":          "สิ้นสุด",
            "type":         "ประเภท",
            "peak_value":   "ค่าสูงสุด",
            "duration_min": "นาที",
            "reason_tag":   "สาเหตุ",
            "fan_hepa":     "HEPA",
            "fan_exh":      "พัดลมระบาย",
        })
        st.dataframe(
            display_ev,
            use_container_width=True,
            hide_index=True,
            height=min(400, 55 + len(display_ev) * 35),
        )

        # Export events CSV
        st.write("")
        csv_bytes = events_all.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            label="📥 ดาวน์โหลดบันทึกเหตุการณ์ (CSV)",
            data=csv_bytes,
            file_name=f"SmartLungBox_Events_{date.today().isoformat()}.csv",
            mime="text/csv",
        )

        # Stats
        st.write("")
        st.caption(
            f"เหตุการณ์ทั้งหมด: {len(events_all)} | "
            f"วันนี้: {len(events_today)} | "
            f"กำลังเกิด: {len(active_events)}"
        )


# ══════════════════════════════════════════════════════════════════
# AUTO-REFRESH  (live mode — runs after all UI is rendered)
# ══════════════════════════════════════════════════════════════════
if ss.mode == "live" and ss.auto_refresh and ss.live_path:
    time.sleep(5)
    # Reload if file has changed
    if os.path.exists(ss.live_path):
        mtime = os.path.getmtime(ss.live_path)
        if mtime != ss.live_mtime:
            ss.live_df    = load_csv_path_full(ss.live_path)
            ss.live_mtime = mtime
    st.rerun()
