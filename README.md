# SmartLungBox Teacher Dashboard v1

A Streamlit-based air quality monitoring dashboard for classroom environments.
Supports both **offline CSV upload** and **live file-watch** mode (auto-refresh every 5 s).

---

## Installation

```bash
# 1. Clone / copy the dashboard/ folder to your machine

# 2. Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Running the Dashboard

```bash
cd dashboard/
streamlit run app.py
```

The app opens at **http://localhost:8501** by default.

---

## Modes of Operation

### Upload Mode (offline)
1. Open the sidebar → **"อัปโหลด CSV"** section.
2. Upload any `.csv` file exported from the SmartLungBox (or compatible format — see below).
3. The dashboard processes the entire file and displays all data.

### Live Mode (real-time)
1. Connect the SmartLungBox to your computer via USB and confirm it is logging to a `.csv` file on disk.
2. In the sidebar → **"Live File Path"**, enter the full path to the CSV (e.g. `/dev/serial/…` or `C:\logs\smartlung.csv`).
3. Click **"เริ่ม Live Mode"**. The dashboard polls for file changes and auto-refreshes every 5 seconds.

---

## Supported CSV Formats

The dashboard accepts flexible column names via an alias map. The following column names are all recognised automatically:

| Canonical name | Accepted aliases |
|---|---|
| `ts` | `timestamp`, `time`, `datetime`, `Timestamp` |
| `pm25` | `PM25`, `pm2_5`, `pm2.5`, `PM2.5`, `PM`, `pm` |
| `co2` | `CO2`, `co2_ppm`, `CO2_ppm` |
| `temp_c` | `temp`, `T`, `temperature`, `temp_celsius` |
| `rh` | `RH`, `humidity`, `rh_pct`, `RH_pct` |
| `fan_hepa` | `HEPA`, `hepa`, `Fan_HEPA` |
| `fan_exh` | `EXH`, `exh`, `exhaust`, `Fan_EXH` |
| `pm_status` | `pm_stat`, `PMStatus` |
| `scd_status` | `scd_stat`, `SCDStatus` |

**Timestamp formats supported:**
- Numeric elapsed seconds from midnight (most common from SmartLungBox firmware)
- ISO 8601 datetime strings (`YYYY-MM-DD HH:MM:SS`)
- Any format parseable by `pandas.to_datetime()`
- Fallback: row index × 5 s from today's midnight

**Minimum required columns:** `pm25` (or alias) and `co2` (or alias). All other columns are optional and will be synthesised if absent.

**Native firmware CSV header:**
```
ts,PM,CO2,T,RH,CAI,Level,HEPA,EXH,pm_status,scd_status
```

---

## Assumptions & Thresholds

All thresholds are locked constants in `data_adapter.py` and mirror the firmware exactly:

| Constant | Value | Meaning |
|---|---|---|
| `PM_GATE` | 35 µg/m³ | PM2.5 level at which exhaust is suppressed |
| `PM_HIGH` | 75 µg/m³ | PM2.5 critical threshold |
| `CO2_HIGH` | 1000 ppm | CO₂ warning threshold |
| `CO2_URGENT` | 1500 ppm | CO₂ critical threshold |
| `CO2_RECOVER` | 900 ppm | CO₂ recovery (event close) threshold |
| `CAI_EXCELLENT` | 80 | CAI excellent zone |
| `CAI_GOOD` | 60 | CAI good zone |
| `CAI_CAUTION` | 40 | CAI caution zone (below = RISK) |

**CAI formula:**
```
CAI = 0.65 × PM_score + 0.25 × CO2_score + 0.10 × Comfort_score
```
where Comfort = (Temp_score + RH_score) / 2. CAI is clamped to [0, 100].

**Data sampling interval assumed:** 5 seconds (used for coverage % and rolling window calculations).

---

## Architecture Overview

```
dashboard/
├── app.py            — Streamlit UI: tabs, charts, export, auto-refresh
├── data_adapter.py   — CSV loading, schema normalisation, CAI computation
├── metrics.py        — KPI calculations (with per-sensor validity filtering)
├── alerts.py         — Event detection (4-state machine, 5 event types, cooldown)
├── requirements.txt  — Python dependencies
└── README.md         — This file
```

### Module responsibilities

**`data_adapter.py`**
Handles all data ingestion. Normalises column names via alias map, parses timestamps (numeric elapsed-seconds, datetime strings, or fallback), coerces sensor values, fills `pm_status`/`scd_status` if absent, and computes CAI + Level + reason_tag. Exports `load_csv_cached()` (Streamlit-cached, for upload mode) and `load_csv_path_full()` (for live mode).

**`metrics.py`**
Computes KPIs from a processed DataFrame. Each KPI uses only rows where the relevant sensor status is `VALID` — CO₂ KPIs filter on `scd_status == 'VALID'`; PM KPIs on `pm_status == 'VALID'`; CAI KPIs on both. Also computes CAI zone distribution and top-3 worst 10-minute windows by average CO₂.

**`alerts.py`**
Detects alert *events* (not per-sample warnings) using a 4-state machine: `IDLE → TRIGGERING → ACTIVE → ENDING → IDLE`. Detects five event types: `CO2_WARNING`, `CO2_CRITICAL`, `PM_WARNING`, `PM_CRITICAL`, and `SAFE_MODE`. Each event records start time, end time (or `None` if still open), peak value, duration, and fan states at peak. A 10-minute cooldown prevents duplicate events.

**`app.py`**
Main Streamlit application. Manages session state for mode switching. Renders: a live CAI gauge (Plotly Indicator), KPI metric cards, 60-minute time-series subplots (PM2.5 + CO₂ + CAI), CAI zone bar chart, HEPA/Exhaust mode timeline, top-3 worst periods table, and alert event log. Provides PNG report download (Matplotlib) and CSV alert export.

---

## Exporting Reports

- **PNG Report:** Click **"ดาวน์โหลด PNG Report"** in the Today tab to download a summary image containing KPI cards, CO₂ trend chart, and alert list.
- **Alert CSV:** Click **"ดาวน์โหลด Alert Log"** to download all detected events as a CSV file.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "ไม่มีข้อมูล" on all tabs | CSV has no rows, or all rows are INVALID | Check CSV format; ensure sensor columns are present |
| CAI always 0 | Both sensors INVALID in all rows | Check `pm_status`/`scd_status` columns or sensor wiring |
| Live mode not refreshing | File path is wrong or file not updating | Verify path; check that SmartLungBox is writing to that file |
| Thai text garbled in PNG report | Thai font not installed on the system | Install `fonts-thai-tlwg` (Linux) or `Noto Sans Thai` (any OS) |
| `st.cache_data` warning | Streamlit version < 1.18 | Upgrade: `pip install --upgrade streamlit` |
