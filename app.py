"""
Pet Rent Analysis — Streamlit Application
Connects to Snowflake, fetches Yardi GetRentroll data, and visualizes
pet/fee collection over time per property and parent company.
"""

import os
import io
import json
import xml.etree.ElementTree as ET
import urllib.parse
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import numpy as np
import requests
import snowflake.connector
from dotenv import load_dotenv

load_dotenv()

# ─── Page config ─────────────────────────────────────────────────────
st.set_page_config(
    page_title="PetScreening · Value Report",
    page_icon="PS",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── PetScreening Brand Assets ───────────────────────────────────────
import base64 as _b64

def _load_logo_b64(fill_color=None):
    """Load logo SVG, optionally recolor, return base64 data URI."""
    try:
        _logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.svg")
        with open(_logo_path, "r") as _f:
            _svg = _f.read()
        if fill_color:
            _svg = _svg.replace('fill="white"', f'fill="{fill_color}"')
        return "data:image/svg+xml;base64," + _b64.b64encode(_svg.encode()).decode()
    except FileNotFoundError:
        return ""

_PS_LOGO_WHITE_URI = _load_logo_b64()           # white for dark backgrounds
_PS_LOGO_DARK_URI  = _load_logo_b64("#1F2257")  # Pack Blue for light backgrounds

# ─── PetScreening Brand CSS ──────────────────────────────────────────
# Force light theme and apply warm brand palette matching the HTML report
st.markdown("""
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&family=Lora:wght@700&display=swap" rel="stylesheet">
<style>
    /* ══════════════════════════════════════════════════════════════
       FORCE LIGHT MODE — override Streamlit dark theme entirely.
       Must match the HTML report warm off-white aesthetic.
       ══════════════════════════════════════════════════════════════ */

    /* ── Force light color-scheme globally ── */
    :root, html, body {
        color-scheme: light !important;
    }

    /* ── Glide Data Grid (Streamlit table renderer) — MUST be on :root ──
       glide-data-grid reads CSS custom properties at init time from
       the closest ancestor.  Placing them on :root guarantees they
       are found before the canvas is painted. */
    :root {
        --gdg-text-dark: #4F5155 !important;
        --gdg-text-medium: #636569 !important;
        --gdg-text-light: #AFB2B3 !important;
        --gdg-text-bubble: #1F2257 !important;
        --gdg-bg-cell: #ffffff !important;
        --gdg-bg-cell-medium: #FAFAF8 !important;
        --gdg-bg-header: #F9F4E6 !important;
        --gdg-bg-header-has: #F0EBD8 !important;
        --gdg-bg-header-hovered: #EDE8D5 !important;
        --gdg-text-header: #1F2257 !important;
        --gdg-text-header-selected: #1F2257 !important;
        --gdg-border-color: #E8E6E0 !important;
        --gdg-accent-color: #B17455 !important;
        --gdg-accent-light: rgba(177,116,85,0.15) !important;
        --gdg-accent-fg: #ffffff !important;
        --gdg-link-color: #B17455 !important;
        --gdg-cell-horizontal-padding: 8px !important;
        --gdg-cell-vertical-padding: 3px !important;
    }

    /* ── ROOT: global background & text color ── */
    .stApp, [data-testid="stAppViewContainer"],
    .main, [data-testid="stMain"],
    [data-testid="stAppViewBlockContainer"],
    [data-testid="stVerticalBlock"],
    [data-testid="stHorizontalBlock"],
    [data-testid="column"] {
        background-color: #FAFAF8 !important;
        color: #4F5155 !important;
    }
    [data-testid="stHeader"] {
        background-color: #FAFAF8 !important;
    }
    [data-testid="stBottomBlockContainer"] {
        background-color: #FAFAF8 !important;
    }

    /* ── Typography — Poppins everywhere, readable dark grey ── */
    html, body, [class*="css"], .stMarkdown, .stText,
    [data-testid="stMetricLabel"], .streamlit-expanderHeader,
    .stRadio label, [data-baseweb="select"],
    p, span:not([class*="material"]), li, td, th, label, div {
        font-family: 'Poppins', Arial, sans-serif !important;
    }
    /* Preserve Material Symbols font for Streamlit's built-in icons */
    span[class*="material"] {
        font-family: 'Material Symbols Rounded', 'Material Symbols Outlined', sans-serif !important;
        font-size: 1.2em !important;
    }
    /* Default text color — NOT on td/th (dataframe handled separately) */
    html, body, p, span:not([class*="material"]), li, label, div,
    .stMarkdown, .stMarkdown *:not([class*="material"]), .stText,
    [class*="css"]:not([class*="material"]) {
        color: #4F5155 !important;
    }
    h1, h1 * {
        font-family: 'Poppins', Arial, sans-serif !important;
        color: #1F2257 !important;
        font-weight: 600 !important;
        letter-spacing: -0.3px !important;
    }
    h2, h3, h2 *, h3 * {
        font-family: 'Poppins', Arial, sans-serif !important;
        color: #1F2257 !important;
    }
    strong, b {
        color: #1F2257 !important;
    }
    /* Captions — use Smokey Gray (not lighter) */
    .stCaption, [data-testid="stCaptionContainer"],
    [data-testid="stCaptionContainer"] * {
        color: #636569 !important;
    }

    /* ── Sidebar — Dog Bone White bg, dark readable text ── */
    section[data-testid="stSidebar"],
    section[data-testid="stSidebar"] > div,
    section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
        background-color: #F9F4E6 !important;
        overflow-x: hidden !important;
    }
    section[data-testid="stSidebar"] *:not([class*="material"]) {
        color: #4F5155 !important;
    }
    section[data-testid="stSidebar"] h1,
    section[data-testid="stSidebar"] h2,
    section[data-testid="stSidebar"] h3 {
        color: #1F2257 !important;
    }
    /* Sidebar collapse/expand button — nuke ALL child text, use CSS chevron */
    button[data-testid="stSidebarCollapseButton"],
    button[data-testid="baseButton-headerNoPadding"] {
        overflow: hidden !important;
        position: relative !important;
    }
    button[data-testid="stSidebarCollapseButton"] *,
    button[data-testid="baseButton-headerNoPadding"] * {
        font-size: 0 !important;
        color: transparent !important;
        visibility: hidden !important;
    }
    button[data-testid="stSidebarCollapseButton"]::after,
    button[data-testid="baseButton-headerNoPadding"]::after {
        content: '' !important;
        visibility: visible !important;
        display: block !important;
        width: 8px !important;
        height: 8px !important;
        border-right: 2px solid #4F5155 !important;
        border-bottom: 2px solid #4F5155 !important;
        transform: rotate(135deg) !important;
        position: absolute !important;
        top: 50% !important;
        left: 50% !important;
        margin-top: -4px !important;
        margin-left: -2px !important;
    }

    /* ══════════════════════════════════════════════════════════════
       INPUT WIDGETS — white backgrounds, warm borders
       ══════════════════════════════════════════════════════════════ */
    [data-baseweb="select"] > div,
    [data-baseweb="input"] > div,
    .stTextInput > div > div,
    .stSelectbox > div > div > div,
    .stMultiSelect > div > div > div,
    .stNumberInput > div > div {
        background-color: #ffffff !important;
        border-color: #D3CEBD !important;
        color: #4F5155 !important;
    }
    /* Widget labels */
    [data-testid="stWidgetLabel"], [data-testid="stWidgetLabel"] *,
    .stSelectbox label, .stMultiSelect label,
    .stTextInput label, .stNumberInput label, .stSlider label {
        color: #4F5155 !important;
    }
    /* Dropdown menus */
    [data-baseweb="menu"], [data-baseweb="popover"],
    [data-baseweb="menu"] *, [data-baseweb="popover"] * {
        background-color: #ffffff !important;
        color: #4F5155 !important;
    }
    [data-baseweb="menu"] li:hover,
    [data-baseweb="popover"] li:hover {
        background-color: #F9F4E6 !important;
    }

    /* ── Multiselect pills (charge codes) — warm brand colors ── */
    [data-baseweb="tag"] {
        background-color: #B17455 !important;
        color: white !important;
        border-radius: 6px !important;
    }
    [data-baseweb="tag"] span {
        color: white !important;
    }
    [data-baseweb="tag"] [role="presentation"] {
        color: white !important;
    }

    /* ══════════════════════════════════════════════════════════════
       DATAFRAMES / TABLES — force white background, dark text
       This is the critical section for Streamlit's Arrow dataframe.
       We blast every wrapper, iframe, and canvas container white.
       ══════════════════════════════════════════════════════════════ */
    [data-testid="stDataFrame"],
    .stDataFrame,
    [data-testid="stDataFrame"] > div,
    [data-testid="stDataFrame"] iframe,
    [data-testid="stDataFrameResizable"],
    [data-testid="stDataFrame"] [class*="glideDataEditor"],
    [data-testid="stDataFrame"] canvas + div,
    [data-testid="stDataFrame"] > div > div > div,
    .dvn-scroller,
    [data-testid="stDataFrame"] [class*="dvn-underlay"] {
        border-color: #E8E6E0 !important;
        background-color: #ffffff !important;
        color-scheme: light !important;
    }

    /* Reinforce --gdg-* on every dataframe element as fallback
       (primary definition is on :root above, this catches iframes) */
    [data-testid="stDataFrame"],
    [data-testid="stDataFrame"] *,
    [data-testid="stDataFrame"] iframe {
        --gdg-text-dark: #4F5155 !important;
        --gdg-text-medium: #636569 !important;
        --gdg-text-light: #AFB2B3 !important;
        --gdg-text-bubble: #1F2257 !important;
        --gdg-bg-cell: #ffffff !important;
        --gdg-bg-cell-medium: #FAFAF8 !important;
        --gdg-bg-header: #F9F4E6 !important;
        --gdg-bg-header-has: #F0EBD8 !important;
        --gdg-bg-header-hovered: #EDE8D5 !important;
        --gdg-text-header: #1F2257 !important;
        --gdg-text-header-selected: #1F2257 !important;
        --gdg-border-color: #E8E6E0 !important;
        --gdg-accent-color: #B17455 !important;
        --gdg-accent-light: rgba(177,116,85,0.15) !important;
        --gdg-accent-fg: #ffffff !important;
        --gdg-link-color: #B17455 !important;
        --gdg-cell-horizontal-padding: 8px !important;
        --gdg-cell-vertical-padding: 3px !important;
    }

    /* ── Metric cards ── */
    [data-testid="stMetricValue"], [data-testid="stMetricValue"] * {
        font-family: 'Poppins', Arial, sans-serif !important;
        color: #1F2257 !important;
    }
    [data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * {
        color: #4F5155 !important;
    }
    [data-testid="stMetricDelta"], [data-testid="stMetricDelta"] * {
        color: #677848 !important;
    }

    /* ── Expanders — white bg ── */
    .streamlit-expanderHeader, [data-testid="stExpander"] summary {
        color: transparent !important;
        background-color: #ffffff !important;
    }
    /* Hide ligature text in ALL spans/divs inside summary
       (overrides the global span color rule) */
    [data-testid="stExpander"] summary span,
    [data-testid="stExpander"] summary div {
        color: transparent !important;
    }
    /* Restore visible color for the actual title text (always in <p>) */
    [data-testid="stExpander"] summary p,
    [data-testid="stExpander"] summary strong {
        color: #4F5155 !important;
        font-family: 'Poppins', Arial, sans-serif !important;
    }
    /* Right-pointing CSS triangle on summary itself (collapsed) */
    [data-testid="stExpander"] summary::before {
        content: '' !important;
        display: inline-block !important;
        flex-shrink: 0 !important;
        width: 0 !important;
        height: 0 !important;
        border-top: 5px solid transparent !important;
        border-bottom: 5px solid transparent !important;
        border-left: 7px solid #4F5155 !important;
        border-right: none !important;
        margin-right: 8px !important;
    }
    /* Down-pointing triangle (expanded) */
    [data-testid="stExpander"] details[open] > summary::before,
    details[open] > summary::before {
        border-top: 7px solid #4F5155 !important;
        border-bottom: none !important;
        border-left: 5px solid transparent !important;
        border-right: 5px solid transparent !important;
    }
    [data-testid="stExpander"],
    [data-testid="stExpander"] > div {
        border-color: #E8E6E0 !important;
        background-color: #ffffff !important;
    }
    [data-testid="stExpanderDetails"] {
        background-color: #ffffff !important;
    }
    /* Ensure dataframe text is visible inside expanders */
    [data-testid="stExpanderDetails"] [data-testid="stDataFrame"],
    [data-testid="stExpanderDetails"] [data-testid="stDataFrame"] * {
        --gdg-text-dark: #4F5155 !important;
        --gdg-text-header: #1F2257 !important;
        --gdg-bg-cell: #ffffff !important;
        --gdg-bg-header: #F9F4E6 !important;
    }
    [data-testid="stExpanderDetails"] table,
    [data-testid="stTable"] table {
        color: #4F5155 !important;
        width: 100% !important;
    }
    [data-testid="stExpanderDetails"] table td,
    [data-testid="stExpanderDetails"] table th,
    [data-testid="stTable"] table td,
    [data-testid="stTable"] table th {
        color: #4F5155 !important;
        padding: 8px 12px !important;
    }
    [data-testid="stExpanderDetails"] table th,
    [data-testid="stTable"] table th {
        background-color: #F9F4E6 !important;
        color: #1F2257 !important;
        font-weight: 600 !important;
    }

    /* ── Info / Warning / Error banners ── */
    .stAlert, [data-testid="stAlert"] {
        background-color: #DAEBF5 !important;
        color: #1F2257 !important;
        border-color: #7D9BC1 !important;
    }
    [data-testid="stAlert"] * {
        color: #1F2257 !important;
    }

    /* ══════════════════════════════════════════════════════════════
       BUTTONS
       ══════════════════════════════════════════════════════════════ */
    .stButton > button[kind="primary"],
    button[data-testid="stBaseButton-primary"] {
        background-color: #B17455 !important;
        border-color: #B17455 !important;
        color: white !important;
        font-weight: 500 !important;
        border-radius: 8px !important;
        transition: all 0.15s ease !important;
    }
    .stButton > button[kind="primary"]:hover,
    button[data-testid="stBaseButton-primary"]:hover {
        background-color: #9A6349 !important;
        border-color: #9A6349 !important;
    }
    .stButton > button:not([kind="primary"]),
    button[data-testid="stBaseButton-secondary"] {
        background-color: #ffffff !important;
        border-color: #D3CEBD !important;
        color: #4F5155 !important;
        border-radius: 8px !important;
    }
    .stButton > button:not([kind="primary"]):hover,
    button[data-testid="stBaseButton-secondary"]:hover {
        background-color: #F9F4E6 !important;
        border-color: #B17455 !important;
    }
    /* Download button — warm orange, matches brand */
    .stDownloadButton > button {
        background-color: #B17455 !important;
        border-color: #B17455 !important;
        color: white !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
        transition: all 0.15s ease !important;
    }
    .stDownloadButton > button:hover {
        background-color: #9A6349 !important;
        border-color: #9A6349 !important;
    }

    /* ── Tabs ── */
    .stTabs [data-baseweb="tab-list"] {
        background-color: transparent !important;
    }
    .stTabs [data-baseweb="tab-list"] button {
        color: #636569 !important;
        background-color: transparent !important;
    }
    .stTabs [data-baseweb="tab-list"] button[aria-selected="true"] {
        color: #B17455 !important;
        border-bottom-color: #B17455 !important;
    }
    .stTabs [data-baseweb="tab-panel"] {
        background-color: transparent !important;
    }

    /* ══════════════════════════════════════════════════════════════
       TOGGLE — make it visible with a warm highlight
       ══════════════════════════════════════════════════════════════ */
    .stToggle, [data-testid="stToggle"] {
        background-color: #F9F4E6 !important;
        border: 1px solid #D3CEBD !important;
        border-radius: 8px !important;
        padding: 8px 12px !important;
    }
    .stToggle label span, .stCheckbox label span,
    [data-testid="stToggle"] label span {
        color: #1F2257 !important;
        font-weight: 500 !important;
    }

    /* ── Radio buttons ── */
    .stRadio > div > label, .stRadio label {
        color: #4F5155 !important;
    }

    /* ── Slider ── */
    .stSlider label, .stSlider [data-testid="stTickBarMin"],
    .stSlider [data-testid="stTickBarMax"],
    .stSlider [data-testid="stThumbValue"] {
        color: #4F5155 !important;
    }

    /* ── Spinner ── */
    .stSpinner > div, .stSpinner > div * {
        color: #4F5155 !important;
    }

    /* ── Plotly chart backgrounds — transparent so page bg shows ── */
    .js-plotly-plot .plotly .main-svg {
        background: transparent !important;
    }

    /* ── Dividers ── */
    hr { border-color: #D3CEBD !important; }

    /* ── Scrollbar — subtle warm ── */
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: #F9F4E6; }
    ::-webkit-scrollbar-thumb { background: #D3CEBD; border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: #AFB2B3; }

    /* ── Container borders (scrollable chart block) ── */
    [data-testid="stVerticalBlockBorderWrapper"] {
        border-color: #E8E6E0 !important;
        background-color: #FAFAF8 !important;
    }

    /* ── Toast messages ── */
    [data-testid="stToast"], [data-testid="stToast"] * {
        background-color: #ffffff !important;
        color: #4F5155 !important;
    }

    /* ══════════════════════════════════════════════════════════════
       HTML TABLES — replaces st.dataframe() for guaranteed visibility.
       White background, dark text, visible grid lines, branded headers.
       ══════════════════════════════════════════════════════════════ */
    .ps-table-wrap {
        width: 100%;
        border: 1px solid #E8E6E0;
        border-radius: 8px;
        overflow: auto;
        background: #ffffff;
        margin-bottom: 0.5rem;
    }
    .ps-table {
        width: 100%;
        border-collapse: collapse;
        font-family: 'Poppins', Arial, sans-serif;
        font-size: 13px;
        color: #4F5155;
        background: #ffffff;
    }
    .ps-table thead th {
        background: #F9F4E6 !important;
        color: #1F2257 !important;
        font-weight: 600;
        font-size: 12px;
        padding: 10px 12px;
        border-bottom: 2px solid #D3CEBD;
        text-align: left;
        position: sticky;
        top: 0;
        z-index: 1;
        white-space: nowrap;
    }
    .ps-table tbody td {
        padding: 8px 12px;
        border-bottom: 1px solid #F0EDE5;
        color: #4F5155 !important;
        background: #ffffff;
        font-size: 13px;
    }
    .ps-table tbody tr:nth-child(even) td {
        background: #FEFDFB;
    }
    .ps-table tbody tr:hover td {
        background: #F9F4E6;
    }

    /* ── Nuclear: force light on Streamlit's internal theme attribute ── */
    [data-testid="stAppViewContainer"][data-theme="dark"],
    .stApp[data-theme="dark"] {
        background-color: #FAFAF8 !important;
        color: #4F5155 !important;
    }

    /* ── Force prefers-color-scheme: light within iframes ── */
    iframe {
        color-scheme: light !important;
    }
</style>
<script>
    // Force Streamlit to use light theme by setting localStorage
    // and removing any dark-theme data attributes
    (function() {
        try {
            localStorage.setItem('stActiveTheme-/-v1',
                JSON.stringify({name:"Light", themeInput:{
                    primaryColor:"#B17455",
                    backgroundColor:"#FAFAF8",
                    secondaryBackgroundColor:"#F9F4E6",
                    textColor:"#4F5155",
                    base:"light",
                    font:"sans serif"
                }}));
            // Remove dark theme attributes if present
            document.querySelectorAll('[data-theme="dark"]').forEach(
                el => el.setAttribute('data-theme', 'light'));
            // Also set on documentElement
            document.documentElement.setAttribute('data-theme', 'light');
            document.documentElement.style.colorScheme = 'light';
        } catch(e) {}
    })();
</script>
""", unsafe_allow_html=True)

# ─── Styled HTML table helper (replaces st.dataframe for visibility) ─
def _render_table(df, height=None, hide_index=True, max_rows=1000):
    """Render a DataFrame as a styled HTML table with guaranteed white bg + dark text.

    Large DataFrames are automatically truncated to *max_rows* for display.
    The full dataset is untouched — only the browser rendering is capped.
    """
    truncated = False
    display_df = df
    if max_rows and len(df) > max_rows:
        display_df = df.head(max_rows)
        truncated = True

    html = display_df.to_html(
        index=not hide_index,
        classes="ps-table",
        border=0,
        escape=False,
        na_rep="—",
    )
    wrapper_style = ""
    if height:
        wrapper_style = f' style="max-height:{height}px;overflow-y:auto;"'
    st.markdown(
        f'<div class="ps-table-wrap"{wrapper_style}>{html}</div>',
        unsafe_allow_html=True,
    )
    if truncated:
        st.caption(f"Showing first {max_rows:,} of {len(df):,} rows. Download CSV/Excel for full data.")


# ─── Property Funnel — consistent cascade across all tabs ────────────
def _render_property_funnel(
    n_total=None,
    n_api=None,
    n_with_charges=None,
    n_with_adoption=None,
    n_comparable=None,
    n_with_launch=None,
):
    """Render a one-line property cascade/funnel showing the filtering steps.

    Shows the chain:  Total → API access → Charge data → [Adoption data] → [Comparable]
    Each step only appears if its count is provided.
    """
    steps = []
    if n_total is not None:
        steps.append(f"<strong>{n_total}</strong> total")
    if n_api is not None:
        steps.append(f"<strong>{n_api}</strong> with API")
    if n_with_charges is not None:
        steps.append(f"<strong>{n_with_charges}</strong> with charge data")
    if n_with_launch is not None:
        steps.append(f"<strong>{n_with_launch}</strong> with launch date")
    if n_with_adoption is not None:
        steps.append(f"<strong>{n_with_adoption}</strong> with adoption data")
    if n_comparable is not None:
        steps.append(f"<strong>{n_comparable}</strong> comparable")

    if not steps:
        return

    chain = ' <span style="color:#B17455;font-weight:600">→</span> '.join(steps)
    st.markdown(
        f'<div style="background:#F9F4E6;border:1px solid #E8E4DA;border-radius:8px;'
        f'padding:10px 16px;margin-bottom:16px;font-family:Poppins,Arial,sans-serif;'
        f'font-size:13px;color:#4F5155">'
        f'<span style="color:#636569;font-size:11px;text-transform:uppercase;'
        f'letter-spacing:0.5px;font-weight:600">Properties: </span>'
        f'{chain}'
        f'</div>',
        unsafe_allow_html=True,
    )


# ─── Snowflake connection (cached, with auto-reconnect) ─────────────
@st.cache_resource
def _create_snowflake_connection():
    return snowflake.connector.connect(
        account=os.getenv("SNOWFLAKE_ACCOUNT"),
        user=os.getenv("SNOWFLAKE_USER"),
        password=os.getenv("SNOWFLAKE_PASSWORD"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        role=os.getenv("SNOWFLAKE_ROLE"),
    )


def get_snowflake_connection():
    """Return a live Snowflake connection, reconnecting if the token expired."""
    conn = _create_snowflake_connection()
    try:
        conn.cursor().execute("SELECT 1")
    except Exception:
        # Token expired or connection lost — clear cache and reconnect
        _create_snowflake_connection.clear()
        conn = _create_snowflake_connection()
    return conn


YARDI_LICENSE_TOKEN = os.getenv("YARDI_LICENSE_TOKEN", "")
ENTRATA_API_KEY = os.getenv("ENTRATA_API_KEY", "")
ENTRATA_BASE_URL = "https://apis.entrata.com/ext/orgs"

# ─── SOAP / API helpers ─────────────────────────────────────────────
SOAP_ACTION = "http://tempuri.org/YSI.Interfaces.WebServices/ItfResidentData/GetRentroll"
SOAP_HEADERS = {
    "Content-Type": "text/xml; charset=utf-8",
    "SOAPAction": SOAP_ACTION,
}


def build_soap_payload(row: dict, license_token: str,
                       move_date: str, charge_from: str, charge_to: str) -> str:
    """Build SOAP XML for GetRentroll.

    Parameters
    ----------
    move_date : str   – earliest MoveIn/MoveOut date (tenant filter)
    charge_from : str – earliest lease-charge FromDate to return
    charge_to : str   – latest date for lease-charge range (usually today)
    """
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema"
               xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <GetRentroll xmlns="http://tempuri.org/YSI.Interfaces.WebServices/ItfResidentData">
        <UserName>{row['USER_NAME']}</UserName>
        <Password>{row['PASSWORD']}</Password>
        <ServerName>{row['SERVER_NAME']}</ServerName>
        <Database>{row['DATABASE_NAME']}</Database>
        <Platform>Yardi</Platform>
        <InterfaceEntity>PetScreening</InterfaceEntity>
        <InterfaceLicense>{license_token}</InterfaceLicense>
        <YardiPropertyId>{row['PROPERTY_CODE']}</YardiPropertyId>
        <MoveIn>{move_date}</MoveIn>
        <MoveOut>{move_date}</MoveOut>
        <LeaseChgFrom>{charge_from}</LeaseChgFrom>
        <LeaseChgTo>{charge_to}</LeaseChgTo>
    </GetRentroll>
  </soap:Body>
</soap:Envelope>"""


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _etree_to_obj(elem):
    children = list(elem)
    if not children:
        return (elem.text or "").strip()
    obj = {}
    for child in children:
        tag = _strip_ns(child.tag)
        val = _etree_to_obj(child)
        if tag in obj:
            if not isinstance(obj[tag], list):
                obj[tag] = [obj[tag]]
            obj[tag].append(val)
        else:
            obj[tag] = val
    return obj


def xml_to_dict(xml_text: str):
    root = ET.fromstring(xml_text)
    return {_strip_ns(root.tag): _etree_to_obj(root)}


def extract_property(parsed_payload):
    try:
        docs = (
            parsed_payload
            .get("Envelope", {})
            .get("Body", {})
            .get("GetRentrollResponse", {})
            .get("GetRentrollResult", {})
            .get("XmlDocument", [])
        )
        if not isinstance(docs, list):
            docs = [docs]
        for doc in docs:
            props = doc.get("Properties")
            if isinstance(props, dict):
                prop = props.get("Property")
                if prop:
                    return prop
        return None
    except Exception:
        return None


def extract_charges_from_property(prop_data, property_row):
    """Extract ALL lease charges (flat rows) from a property's rent roll."""
    rows = []
    if not isinstance(prop_data, dict):
        return rows

    units = prop_data.get("Units", {})
    if not isinstance(units, dict):
        return rows
    unit_list = units.get("Unit", [])
    if not isinstance(unit_list, list):
        unit_list = [unit_list]

    for unit in unit_list:
        if not isinstance(unit, dict):
            continue
        unit_code = unit.get("UnitCode", "")
        unit_type = unit.get("UnitType", "")
        market_rent = unit.get("MarketRent", "")

        tenants_raw = unit.get("Tenants", {})
        if not isinstance(tenants_raw, dict):
            continue
        tenant_list = tenants_raw.get("Tenant", [])
        if not isinstance(tenant_list, list):
            tenant_list = [tenant_list]

        for tenant in tenant_list:
            if not isinstance(tenant, dict):
                continue
            tenant_code = tenant.get("TenantCode", "")
            first_name = tenant.get("FirstName", "")
            last_name = tenant.get("LastName", "")
            tenant_status = tenant.get("TenantStatus", "")
            lease_from = tenant.get("LeaseFrom", "")
            lease_to = tenant.get("LeaseTo", "")
            move_in = tenant.get("MoveIn", "")
            move_out = tenant.get("MoveOut", "")
            email = tenant.get("Email", "")

            charges = tenant.get("LeaseCharges", "")
            if not charges or not isinstance(charges, dict):
                continue
            charge_list = charges.get("LeaseCharge", [])
            if not isinstance(charge_list, list):
                charge_list = [charge_list]

            for charge in charge_list:
                if not isinstance(charge, dict):
                    continue
                rows.append({
                    "parent_company": property_row["PARENT_COMPANY_NAME"],
                    "property_id": property_row["PROPERTY_ID"],
                    "property_name": property_row["PROPERTY_NAME"],
                    "property_code": property_row["PROPERTY_CODE"],
                    "launch_date": property_row.get("PROPERTY_LAUNCH_DATE"),
                    "unit_code": unit_code,
                    "unit_type": unit_type,
                    "market_rent": market_rent,
                    "tenant_code": tenant_code,
                    "first_name": first_name,
                    "last_name": last_name,
                    "tenant_status": tenant_status,
                    "lease_from": lease_from,
                    "lease_to": lease_to,
                    "move_in": move_in,
                    "move_out": move_out,
                    "email": email,
                    "charge_code": charge.get("ChargeCode", ""),
                    "charge_type": charge.get("ChargeType", ""),
                    "charge_amount": charge.get("ChargeAmount", ""),
                    "charge_from_date": charge.get("FromDate", ""),
                    "charge_to_date": charge.get("ToDate", ""),
                })
    return rows


# ─── Snowflake queries ───────────────────────────────────────────────
@st.cache_data(ttl=300)
def load_parent_companies():
    """Count ALL properties per parent company from d_properties (total + yardi-integrated)."""
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)
    cur.execute("""
        SELECT
            p.parent_company_name,
            MAX(p.parent_company_ancestry_id)    AS ancestry_id,
            MAX(p.parent_company_ancestry_name)   AS ancestry_name,
            COUNT(DISTINCT p.property_id)         AS total_props,
            COUNT(DISTINCT CASE
                WHEN i.integration_id IS NOT NULL AND i.system = 'yardi'
                THEN p.property_id
            END) AS api_props
        FROM PROD.COMMON.D_PROPERTIES p
        LEFT JOIN PROD.STAGING.STG_PETSCREENING__INTEGRATIONS i
            ON p.integration_id = i.integration_id
           AND i.system = 'yardi'
        WHERE p.parent_company_name IS NOT NULL
        GROUP BY 1
        HAVING api_props > 0
        ORDER BY 1
    """)
    return cur.fetchall()


@st.cache_data(ttl=300)
def load_all_properties():
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)
    cur.execute("""
        SELECT DISTINCT
            p.property_id,
            p.property_name,
            p.parent_company_name,
            p.parent_company_ancestry_id
        FROM PROD.COMMON.D_PROPERTIES p
        WHERE p.property_source_name = 'yardi'
        ORDER BY p.property_name
    """)
    return cur.fetchall()


def load_properties_for_selection(parent_company_name=None, property_id=None, ancestry_id=None):
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)

    where_clause = """
        WHERE i.system = 'yardi'
          AND p.property_source_name = 'yardi'
    """
    if parent_company_name:
        where_clause += f" AND p.parent_company_name = '{parent_company_name}'"
    if property_id:
        where_clause += f" AND p.property_id = {property_id}"
    if ancestry_id:
        where_clause += f" AND p.parent_company_ancestry_id = '{ancestry_id}'"

    cur.execute(f"""
        SELECT DISTINCT
            i.integration_id,
            PARSE_JSON(i.settings):"resident_data_url"::STRING AS resident_data_url,
            PARSE_JSON(i.settings):"user_name"::STRING         AS user_name,
            PARSE_JSON(i.settings):"password"::STRING           AS password,
            PARSE_JSON(i.settings):"server_name"::STRING        AS server_name,
            PARSE_JSON(i.settings):"database_name"::STRING      AS database_name,
            p.property_id,
            p.property_name,
            p.parent_company_name,
            COALESCE(
                PARSE_JSON(u.SOURCE_EXTERNAL_ID):"property_code"::STRING,
                PARSE_JSON(p.PROPERTY_SOURCE_ID):"code"::STRING
            ) AS property_code,
            kf.property_launch_date
        FROM PROD.COMMON.D_PROPERTIES p
        JOIN PROD.STAGING.STG_PETSCREENING__INTEGRATIONS i
            ON p.integration_id = i.integration_id
        LEFT JOIN PROD.STAGING.STG_PETSCREENING__UNITS u
            ON PARSE_JSON(u.SOURCE_EXTERNAL_ID):"property_code"::STRING =
               PARSE_JSON(p.property_source_id):"code"::STRING
        LEFT JOIN PROD.PETSCREENING.PETSCREENING__PROPERTY_KEY_FACTS kf
            ON p.property_id = kf.property_id
        {where_clause}
        ORDER BY p.property_name
    """)
    return cur.fetchall()


# ─── Entrata Snowflake queries ────────────────────────────────────────
@st.cache_data(ttl=300)
def load_entrata_parent_companies():
    """Count ALL properties per parent company that have Entrata integrations.

    total_props = every property under the parent company in d_properties
    api_props   = only those with active Entrata API integrations
    """
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)
    cur.execute("""
        SELECT
            p.parent_company_name,
            MAX(p.parent_company_ancestry_id)    AS ancestry_id,
            MAX(p.parent_company_ancestry_name)   AS ancestry_name,
            COUNT(DISTINCT p.property_id)         AS total_props,
            COUNT(DISTINCT CASE
                WHEN i.integration_id IS NOT NULL AND i.system = 'entrata'
                     AND i.state = 'enabled'
                     AND p.integration_status = 'enabled'
                     AND p.property_status = 'active'
                THEN p.property_id
            END) AS api_props
        FROM PROD.COMMON.D_PROPERTIES p
        LEFT JOIN PROD.STAGING.STG_PETSCREENING__INTEGRATIONS i
            ON p.integration_id = i.integration_id
           AND i.system = 'entrata'
        WHERE p.parent_company_name IS NOT NULL
        GROUP BY 1
        HAVING api_props > 0
        ORDER BY 1
    """)
    return cur.fetchall()


@st.cache_data(ttl=300)
def load_entrata_all_properties():
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)
    cur.execute("""
        SELECT DISTINCT
            p.property_id,
            p.property_name,
            p.parent_company_name,
            p.parent_company_ancestry_id
        FROM PROD.COMMON.D_PROPERTIES p
        WHERE p.property_source_name = 'entrata'
        ORDER BY p.property_name
    """)
    return cur.fetchall()


def load_entrata_properties_for_selection(parent_company_name=None, property_id=None, ancestry_id=None):
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)

    where_clause = """
        WHERE i.system = 'entrata'
          AND p.property_source_name = 'entrata'
          AND i.state = 'enabled'
          AND p.integration_status = 'enabled'
          AND p.property_status = 'active'
          AND PARSE_JSON(p.property_source_id):"property_id"::STRING IS NOT NULL
          AND PARSE_JSON(i.settings):"corp_id"::STRING IS NOT NULL
    """
    if parent_company_name:
        where_clause += f" AND p.parent_company_name = '{parent_company_name}'"
    if property_id:
        where_clause += f" AND p.property_id = {property_id}"
    if ancestry_id:
        where_clause += f" AND p.parent_company_ancestry_id = '{ancestry_id}'"

    cur.execute(f"""
        SELECT DISTINCT
            i.integration_id,
            PARSE_JSON(i.settings):"corp_id"::STRING AS corp_id,
            PARSE_JSON(p.property_source_id):"property_id"::STRING AS entrata_property_id,
            p.property_id          AS property_id,
            p.property_name        AS property_name,
            p.parent_company_name  AS parent_company_name,
            PARSE_JSON(p.property_source_id):"property_id"::STRING AS property_code,
            kf.property_launch_date AS property_launch_date
        FROM PROD.COMMON.D_PROPERTIES p
        JOIN PROD.STAGING.STG_PETSCREENING__INTEGRATIONS i
            ON p.integration_id = i.integration_id
        LEFT JOIN PROD.PETSCREENING.PETSCREENING__PROPERTY_KEY_FACTS kf
            ON p.property_id = kf.property_id
        {where_clause}
        ORDER BY p.property_name
    """)
    return cur.fetchall()


# ─── Entrata API fetch ────────────────────────────────────────────────
def _extract_charges_from_entrata_lease(lease_obj, property_row):
    """Flatten an Entrata lease JSON into rows matching the Yardi all_charges_df shape.

    Key Entrata-specific handling vs Yardi:

    1. **Interval filtering** — Entrata leases can contain multiple intervals
       (original, renewals, cancelled attempts). We only include charges from
       intervals with status Current / Past / Notice.  Cancelled / Applicant /
       Future intervals are dropped so we don't count revenue from leases that
       never went live.

    2. **Charge→interval linkage** — Each scheduledCharge carries a
       leaseIntervalId that ties it to a specific interval.  We use this to
       skip charges attached to cancelled renewal attempts.

    3. **Deduplication key** — Charges belong to the *lease*, not an individual
       customer.  Every customer row for the same lease has identical charges.
       We still emit one row per customer×charge (so all customers appear in
       the paying-set for missing-pet-rent matching), but stamp each row with
       ``_entrata_charge_dedup_key`` so downstream revenue aggregation can
       ``drop_duplicates`` before summing.

    4. **Frequency-based end-date logic** — Entrata provides a ``frequency``
       field on each charge.  One-Time charges get charge_to = charge_from
       (same day) so they don't get spread across months.  Monthly charges
       with no endDate fall back to the interval's lease_to.

    5. **Status field choice** — We filter on ``leaseIntervalStatus`` (did the
       lease go live?) NOT ``leaseCustomerStatus`` (is this person still on
       the lease?).  A guarantor with customerStatus="Cancelled" on a
       "Notice" lease should not cause us to drop the lease's charges.
    """
    rows = []

    # ── Parse customers ────────────────────────────────────────────────
    customers_data = lease_obj.get("customers", {})
    customers = customers_data.get("customer", [])
    if isinstance(customers, dict):
        customers = [customers]
    if not customers:
        return rows

    # ── Parse lease intervals — find valid ones ────────────────────────
    VALID_INTERVAL_STATUSES = {"current", "past", "notice"}
    INVALID_INTERVAL_STATUSES = {"cancelled", "applicant", "denied", "future"}

    intervals_data = lease_obj.get("leaseIntervals", {})
    intervals = intervals_data.get("leaseInterval", [])
    if isinstance(intervals, dict):
        intervals = [intervals]

    valid_interval_ids = set()
    interval_info = {}  # interval_id → {status, startDate, endDate}
    for iv in intervals:
        iv_id = str(iv.get("id", ""))
        iv_status = str(iv.get("status", "")).strip().lower()
        if iv_status not in INVALID_INTERVAL_STATUSES:
            valid_interval_ids.add(iv_id)
            interval_info[iv_id] = {
                "status": iv_status,
                "start": iv.get("startDate", ""),
                "end": iv.get("endDate", ""),
            }

    # Fallback: if no detailed intervals, check top-level lease fields
    top_level_status = str(lease_obj.get("leaseIntervalStatus", "")).strip().lower()
    top_level_interval_id = str(lease_obj.get("leaseIntervalId", ""))
    if not valid_interval_ids and top_level_status not in INVALID_INTERVAL_STATUSES:
        valid_interval_ids.add(top_level_interval_id)
        interval_info[top_level_interval_id] = {
            "status": top_level_status,
            "start": "",
            "end": "",
        }

    if not valid_interval_ids:
        return rows

    # ── Parse lease activities for date fallbacks ──────────────────────
    activities_data = lease_obj.get("leaseActivities", {})
    activities = activities_data.get("leasesActivity", [])
    if isinstance(activities, dict):
        activities = [activities]
    act_dates = {}
    for act in activities:
        et = act.get("eventType", "")
        dt = act.get("date", "")
        if et and dt:
            act_dates[et] = dt

    lease_from_fallback = act_dates.get("Lease From", "")
    lease_to_fallback = act_dates.get("Lease To", "")

    lease_id = str(lease_obj.get("id", ""))
    unit_number = lease_obj.get("unitNumberSpace", "")
    unit_id = lease_obj.get("unitId", "")

    # ── Parse scheduled charges — keep only valid-interval charges ─────
    charges_data = lease_obj.get("scheduledCharges", {})
    charges = charges_data.get("scheduledCharge", [])
    if isinstance(charges, dict):
        charges = [charges]

    valid_charges = []
    for ch in charges:
        ch_interval_id = str(ch.get("leaseIntervalId", ""))

        # If the charge has an interval ID, skip unless that interval is valid
        if ch_interval_id and ch_interval_id not in valid_interval_ids:
            continue

        iv = interval_info.get(ch_interval_id, {})
        iv_lease_from = iv.get("start", "") or lease_from_fallback
        iv_lease_to = iv.get("end", "") or lease_to_fallback

        frequency = ch.get("frequency", "")
        charge_from = ch.get("startDate", "")
        charge_to = ch.get("endDate", "")

        # ── Frequency-based end-date logic ──
        freq_lower = (frequency or "").strip().lower()
        if freq_lower == "one-time":
            # One-time charges: same day, do NOT spread across months
            charge_to = charge_from
        elif not charge_to or str(charge_to).strip() == "":
            # Monthly charges missing endDate → fall back to interval lease_to
            charge_to = iv_lease_to

        valid_charges.append({
            "charge_code": ch.get("chargeCode", ""),
            "charge_type": ch.get("chargeType", ""),
            "charge_amount": ch.get("amount", ""),
            "charge_from_date": charge_from,
            "charge_to_date": charge_to,
            "frequency": frequency,
            "lease_interval_id": ch_interval_id,
            "iv_lease_from": iv_lease_from,
            "iv_lease_to": iv_lease_to,
        })

    # ── Emit rows: every customer × every valid charge ─────────────────
    #
    # WHY emit for all customers (not just primary)?
    # Charges belong to the lease, but downstream _build_paying_sets needs
    # every customer's email/tenant_code in the paying set so roommates
    # aren't flagged as "missing pet rent".  Revenue dedup happens later
    # via _entrata_charge_dedup_key.
    for cust in customers:
        first_name = cust.get("firstName", "")
        last_name = cust.get("lastName", "")
        customer_id = cust.get("id", "")
        customer_type = cust.get("customerType", "")
        customer_status = cust.get("leaseCustomerStatus", "")
        move_in = cust.get("moveInDate", "") or act_dates.get("Actual Move In", "")
        move_out = cust.get("moveOutDate", "") or act_dates.get("Actual Move Out", "")

        # Email: try direct field, then addresses.address
        email = cust.get("email", "")
        if not email:
            addrs = cust.get("addresses", {})
            if isinstance(addrs, dict):
                addr = addrs.get("address", {})
                if isinstance(addr, dict):
                    email = addr.get("email", "") or addr.get("additionalEmail", "")

        base_row = {
            "parent_company": property_row["PARENT_COMPANY_NAME"],
            "property_id": property_row["PROPERTY_ID"],
            "property_name": property_row["PROPERTY_NAME"],
            "property_code": property_row["PROPERTY_CODE"],
            "launch_date": property_row.get("PROPERTY_LAUNCH_DATE"),
            "unit_code": unit_number or unit_id,
            "unit_type": "",
            "market_rent": "",
            "lease_id": lease_id,
            "tenant_code": customer_id,
            "first_name": first_name,
            "last_name": last_name,
            "tenant_status": customer_status,
            "move_in": move_in,
            "move_out": move_out,
            "email": email,
        }

        if not valid_charges:
            # Tenant-only row (for reports, even when no charges)
            rows.append({
                **base_row,
                "lease_from": lease_from_fallback,
                "lease_to": lease_to_fallback,
                "charge_code": "",
                "charge_type": "",
                "charge_amount": "",
                "charge_from_date": "",
                "charge_to_date": "",
                "frequency": "",
                "lease_interval_id": "",
                "_entrata_charge_dedup_key": "",
            })
            continue

        for ch in valid_charges:
            dedup_key = (
                f"{lease_id}|{ch['lease_interval_id']}|"
                f"{ch['charge_code']}|{ch['charge_from_date']}|{ch['charge_amount']}"
            )
            rows.append({
                **base_row,
                "lease_from": ch["iv_lease_from"],
                "lease_to": ch["iv_lease_to"],
                "charge_code": ch["charge_code"],
                "charge_type": ch["charge_type"],
                "charge_amount": ch["charge_amount"],
                "charge_from_date": ch["charge_from_date"],
                "charge_to_date": ch["charge_to_date"],
                "frequency": ch["frequency"],
                "lease_interval_id": ch["lease_interval_id"],
                "_entrata_charge_dedup_key": dedup_key,
            })

    return rows


def _extract_ar_charges_from_entrata_lease(lease_obj, property_row):
    """Extract AR (actual posted) transactions from an Entrata lease.

    Returns a list of dicts with actual posted charges including amount paid.
    Only pet-related charge codes are kept (matched case-insensitively against
    common pet charge code names).
    """
    PET_KEYWORDS = {
        'pet rent', 'pet fee', 'pet deposit', 'pet damage',
        'animal rent', 'animal fee', 'animal deposit',
        'pet', 'pet charge', 'monthly pet', 'pet premium',
        'companion animal', 'esa fee', 'esa rent',
    }
    rows = []
    ar_data = lease_obj.get("arTransactions", {})
    if not ar_data:
        return rows
    transactions = ar_data.get("arTransaction", [])
    if isinstance(transactions, dict):
        transactions = [transactions]

    lease_id = str(lease_obj.get("id", ""))
    unit_number = lease_obj.get("unitNumberSpace", "")

    PARTIAL_KEYWORDS = {'pet', 'animal', 'companion'}
    for txn in transactions:
        code_name = str(txn.get("chargeCodeName", "")).strip()
        if not code_name:
            continue
        code_lower = code_name.lower()
        # Match exact keywords OR any partial substring match
        if code_lower not in PET_KEYWORDS and not any(kw in code_lower for kw in PARTIAL_KEYWORDS):
            continue
        rows.append({
            "property_id": property_row["PROPERTY_ID"],
            "property_name": property_row["PROPERTY_NAME"],
            "launch_date": property_row.get("PROPERTY_LAUNCH_DATE"),
            "lease_id": lease_id,
            "unit": unit_number,
            "charge_code_name": code_name,
            "charge_code_id": txn.get("chargeCodeId", ""),
            "amount": txn.get("amount", 0),
            "amount_paid": txn.get("amountPaid", 0),
            "balance_due": txn.get("balanceDue", 0),
            "post_date": txn.get("postDate", ""),
            "post_month": txn.get("postMonth", ""),
            "transaction_date": txn.get("transactionDate", ""),
            "description": txn.get("description", ""),
        })
    return rows


def _fetch_entrata_leases_for_property(prop, api_key):
    """Call Entrata getLeases for a single property with pagination. Returns (leases_list, error_msg)."""
    corp_id = prop.get("CORP_ID")
    entrata_pid = prop.get("ENTRATA_PROPERTY_ID") or prop.get("PROPERTY_CODE")

    if not corp_id or not entrata_pid:
        return [], "Missing corp_id or entrata_property_id"

    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
    }

    all_leases = []
    page_no = 1
    per_page = 500
    max_pages = 200

    while page_no <= max_pages:
        api_url = f"{ENTRATA_BASE_URL}/{corp_id}/v1/leases?page_no={page_no}&per_page={per_page}"

        request_body = {
            "auth": {"type": "apikey"},
            "requestId": page_no,
            "method": {
                "name": "getLeases",
                "version": "r1",
                "params": {
                    "propertyId": entrata_pid,
                    "includeArTransactions": 1,
                    "includeLeaseHistory": 1,
                    "includeScheduledCharges": 1,  # Explicitly request scheduled charges
                    "leaseStatusTypeIds": "1,2,3,4,5,6",  # All statuses for full history
                },
            },
        }

        try:
            resp = requests.post(api_url, json=request_body, headers=headers, timeout=120)
            if resp.status_code == 401:
                return [], "401 Unauthorized — check API key"
            if resp.status_code == 403:
                return [], f"403 Forbidden — corp_id={corp_id}"
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            return all_leases, f"Timeout on page {page_no}" if all_leases else "Timeout"
        except requests.exceptions.HTTPError as e:
            return all_leases, str(e)[:80]
        except Exception as e:
            return all_leases, str(e)[:80]

        data = resp.json()
        response_obj = data.get("response", {})
        if response_obj.get("code") != 200:
            err = response_obj.get("message", f"API code {response_obj.get('code')}")
            return all_leases, err if not all_leases else None

        result = response_obj.get("result", {})
        leases_data = result.get("leases", {})
        leases = leases_data.get("lease", [])
        if isinstance(leases, dict):
            leases = [leases]

        all_leases.extend(leases)

        if len(leases) < per_page:
            break
        meta = result.get("meta", {})
        last_page = meta.get("lastPage", 0)
        if last_page > 0 and meta.get("currentPage", 0) >= last_page:
            break
        page_no += 1

    return all_leases, None


def fetch_entrata_for_properties(properties, progress_bar, status_text, lookback_months=24):
    """Call Entrata getLeases API for each property, return normalized charge rows.

    Returns (all_charges, results_log, all_ar_charges) tuple.  The first two
    match the Yardi equivalent so downstream code works unchanged.
    all_ar_charges contains actual posted AR transactions for comparison.
    """
    all_charges = []
    all_ar_charges = []
    all_raw_lease_arrays = []
    results_log = []

    for i, prop in enumerate(properties):
        prop_name = prop['PROPERTY_NAME']
        prop_code = prop.get('ENTRATA_PROPERTY_ID') or prop.get('PROPERTY_CODE', '')
        progress = (i + 1) / len(properties)
        progress_bar.progress(progress)
        status_text.text(f"[{i+1}/{len(properties)}] Fetching {prop_name} (Entrata {prop_code})...")

        leases, error_msg = _fetch_entrata_leases_for_property(prop, ENTRATA_API_KEY)
        status_text.text(f"[{i+1}/{len(properties)}] {prop_name}: {len(leases)} leases, extracting charges...")

        if error_msg and not leases:
            results_log.append({
                "property": prop_name, "code": prop_code,
                "status": f"Error: {error_msg}", "charges": 0,
            })
            continue

        charges = []
        ar_charges_prop = []
        for lease in leases:
            charges.extend(_extract_charges_from_entrata_lease(lease, prop))
            ar_charges_prop.extend(_extract_ar_charges_from_entrata_lease(lease, prop))

            # Capture raw arrays for export
            lease_id = str(lease.get("id", ""))
            unit = lease.get("unitNumberSpace", "")
            # Get primary customer name
            _custs = lease.get("customers", {}).get("customer", [])
            if isinstance(_custs, dict):
                _custs = [_custs]
            _primary = _custs[0] if _custs else {}
            _tenant_name = f"{_primary.get('firstName', '')} {_primary.get('lastName', '')}".strip()
            _tenant_status = _primary.get("leaseCustomerStatus", "")

            # Raw scheduled charges array
            _sched_raw = lease.get("scheduledCharges", {}).get("scheduledCharge", [])
            if isinstance(_sched_raw, dict):
                _sched_raw = [_sched_raw]
            # Raw AR transactions array
            _ar_raw = lease.get("arTransactions", {}).get("arTransaction", [])
            if isinstance(_ar_raw, dict):
                _ar_raw = [_ar_raw]

            all_raw_lease_arrays.append({
                "property_name": prop_name,
                "property_code": prop_code,
                "lease_id": lease_id,
                "unit": unit,
                "tenant": _tenant_name,
                "tenant_status": _tenant_status,
                "n_scheduled_charges": len(_sched_raw),
                "n_ar_transactions": len(_ar_raw),
                "scheduled_charges_json": json.dumps(_sched_raw, default=str),
                "ar_transactions_json": json.dumps(_ar_raw, default=str),
            })

        if charges:
            all_charges.extend(charges)
            all_ar_charges.extend(ar_charges_prop)
            status = f"Success ({len(leases)} leases)"
            if error_msg:
                status += f" (partial: {error_msg})"
            results_log.append({
                "property": prop_name, "code": prop_code,
                "status": status, "charges": len(charges),
            })
        else:
            all_ar_charges.extend(ar_charges_prop)
            results_log.append({
                "property": prop_name, "code": prop_code,
                "status": "Warning: No charges found", "charges": 0,
            })

    progress_bar.progress(1.0)
    status_text.text("Done!")
    return all_charges, results_log, all_ar_charges, all_raw_lease_arrays


# ─── Yardi API fetch ─────────────────────────────────────────────────
def fetch_rentroll_for_properties(properties, progress_bar, status_text, lookback_months=24):
    """Call GetRentroll API for each property, return all charge rows.

    IMPORTANT: The API date parameters are ALWAYS set to the widest useful
    range (10 years back) regardless of the display lookback slider.
    This guarantees that the same charge rows are returned no matter what
    display window the user picks, so numbers stay consistent.
    The ``lookback_months`` argument is kept for interface compat but is
    NOT used for the API call dates.
    """
    today = datetime.now(timezone.utc).date()

    # Always fetch the widest useful window from Yardi so the data never
    # changes when the user adjusts the display slider.
    move_date    = (today - timedelta(days=10 * 365)).strftime("%Y-%m-%d")   # 10 years
    charge_from  = "2000-01-01"                                               # very early
    charge_to    = today.strftime("%Y-%m-%d")                                 # today

    all_charges = []
    results_log = []

    for i, prop in enumerate(properties):
        prop_name = prop['PROPERTY_NAME']
        prop_code = prop['PROPERTY_CODE']
        progress = (i + 1) / len(properties)
        progress_bar.progress(progress)
        status_text.text(f"[{i+1}/{len(properties)}] Fetching {prop_name} ({prop_code})...")

        try:
            payload = build_soap_payload(prop, YARDI_LICENSE_TOKEN, move_date, charge_from, charge_to)
            resp = requests.post(
                prop['RESIDENT_DATA_URL'],
                data=payload,
                headers=SOAP_HEADERS,
                timeout=120,
            )

            if resp.status_code != 200:
                results_log.append({"property": prop_name, "code": prop_code, "status": f"Error: HTTP {resp.status_code}", "charges": 0})
                continue

            parsed = xml_to_dict(resp.text)
            extracted = extract_property(parsed)

            if not extracted:
                error_msg = "No data"
                for m in ET.fromstring(resp.text).iter():
                    if "Message" in m.tag and m.text:
                        error_msg = m.text.strip()[:60]
                        break
                results_log.append({"property": prop_name, "code": prop_code, "status": f"Warning: {error_msg}", "charges": 0})
                continue

            charges = extract_charges_from_property(extracted, prop)
            all_charges.extend(charges)
            results_log.append({"property": prop_name, "code": prop_code, "status": "Success", "charges": len(charges)})

        except requests.exceptions.Timeout:
            results_log.append({"property": prop_name, "code": prop_code, "status": "Error: Timeout", "charges": 0})
        except Exception as exc:
            results_log.append({"property": prop_name, "code": prop_code, "status": f"Error: {str(exc)[:50]}", "charges": 0})

    progress_bar.progress(1.0)
    status_text.text("Done!")
    return all_charges, results_log


# ─── Date parsing helper ─────────────────────────────────────────────
def parse_date(d):
    if pd.isna(d) or not d or str(d).strip() == "":
        return None
    for fmt in ["%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"]:
        try:
            return datetime.strptime(str(d).strip(), fmt)
        except ValueError:
            continue
    return None


# ─── Launch analysis ─────────────────────────────────────────────────
def compute_launch_analysis(monthly_by_prop, months, launch_dates):
    """
    For each property with a launch date, compute:
      - pre_avg             = average monthly revenue in up to 6 months BEFORE launch
      - post_recent_avg     = average monthly revenue across all completed post-launch months
      - diff_monthly        = cumulative impact ÷ completed post months  (average monthly uplift since launch)
      - diff_total          = actual observed cumulative lift: total_post_revenue − (pre_avg × n_post_months)
      - post_monthly_avg    = average of ALL post months (kept for backward compat / charts)

    Methodology (updated 2026-07-14):
      Pre-baseline:  up to 6 months before launch (uses whatever is available)
      Post-current:  all completed post-launch months (excludes current partial month)
      Monthly lift:  cumulative impact ÷ completed post months
      Total lift:    sum(all post revenue) − (pre_avg × total post months)  [actual observed]

    Returns dict keyed by property name.
    """
    today = datetime.now()
    current_month = datetime(today.year, today.month, 1)

    analysis = {}
    for prop, prop_data in monthly_by_prop.items():
        launch = launch_dates.get(prop)
        if launch is None:
            continue
        # Skip NaT / NaN values
        try:
            if pd.isna(launch):
                continue
        except (TypeError, ValueError):
            pass
        if isinstance(launch, str):
            try:
                launch = datetime.strptime(launch[:10], "%Y-%m-%d")
            except:
                continue
        # Convert pandas Timestamp / numpy datetime to pure Python datetime
        if hasattr(launch, 'to_pydatetime'):
            launch = launch.to_pydatetime()
        # Final guard: make sure year/month are valid numbers
        try:
            yr, mo = int(launch.year), int(launch.month)
        except (ValueError, TypeError, OverflowError):
            continue

        # Snap launch to first-of-month; the launch month itself is "post"
        # (PetScreening was active for at least part of this month)
        launch_month = datetime(yr, mo, 1)
        first_post_month = launch_month  # launch month = first post-launch month

        pre_months = [m for m in months if m < first_post_month]
        post_months = [m for m in months if m >= first_post_month]

        if not post_months:
            continue

        n_post = len(post_months)

        post_total = sum(prop_data.get(m, 0) for m in post_months)

        # Pre-launch avg: use up to 6 months before launch (whatever is available)
        # This gives a robust "business as usual" baseline that smooths seasonal noise
        pre_baseline_months = pre_months[-6:] if len(pre_months) >= 1 else pre_months
        pre_values = [prop_data.get(m, 0) for m in pre_baseline_months]
        pre_avg = sum(pre_values) / len(pre_values) if pre_values else 0

        # Post-launch: all-time average (kept for charts and backward compat)
        post_monthly_avg = post_total / n_post if n_post > 0 else 0

        # Completed post months (excludes current partial month so we don't undercount)
        completed_post = [m for m in post_months if m < current_month]
        n_completed_post = len(completed_post)
        completed_post_total = sum(prop_data.get(m, 0) for m in completed_post)

        # Post-launch avg across all completed months (used for display & baseline check)
        post_recent_avg = (
            completed_post_total / n_completed_post
            if n_completed_post > 0 else post_monthly_avg
        )

        # Total lift = actual observed cumulative difference
        # (what they actually collected minus what they would have at the old rate)
        diff_total = post_total - (pre_avg * n_post)

        # Monthly lift = cumulative impact ÷ completed post months
        # (average monthly uplift since launch — mathematically consistent: monthly × months ≈ cumulative)
        diff_monthly = (completed_post_total - (pre_avg * n_completed_post)) / n_completed_post if n_completed_post > 0 else 0

        # Baseline is "meaningful" if pre_avg is >= 2% of post_recent_avg.
        # Properties with near-zero baselines (e.g. $8 vs $5,600 post) weren't
        # really charging pet rent before PS — their "lift" is misleading.
        _baseline_meaningful = (
            post_recent_avg <= 0  # no post data — keep whatever we have
            or pre_avg >= post_recent_avg * 0.02
        )

        analysis[prop] = {
            "n_post": n_post,
            "n_pre": len(pre_baseline_months),
            "n_recent_post": n_completed_post,
            "all_pre_months": len(pre_months),
            "baseline_reliable": len(pre_months) >= 3,
            "baseline_meaningful": _baseline_meaningful,
            "pre_avg": pre_avg,
            "post_total": post_total,
            "post_monthly_avg": post_monthly_avg,
            "post_recent_avg": post_recent_avg,
            "diff_monthly": diff_monthly,
            "diff_total": diff_total,
            "launch_month": launch_month,
        }
    return analysis


# ─── Compliance / adoption data from QBR table ──────────────────────
@st.cache_data(ttl=600)
def fetch_compliance_data(property_ids_tuple):
    """
    Fetch per-property, per-month adoption rates from
    PROD.REPORTING.R_QUARTERLY_BUSINESS_REVIEW_REPORTING.

    Returns a dict:
      {
        property_id: {
          datetime(month): {
            "unit_adoption": float 0-1,
            "resident_adoption": float 0-1,
            "active_units": int, "total_units": int,
            "active_users": int, "total_users": int,
          }, ...
        }, ...
      }
    """
    property_ids = list(property_ids_tuple)
    if not property_ids:
        return {}
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)
    ids_str = ", ".join(str(int(pid)) for pid in property_ids)
    cur.execute(f"""
        SELECT
            PROPERTY_ID,
            PROPERTY_NAME,
            PERIOD_MONTH,
            ACTIVE_UNITS,
            TOTAL_UNITS,
            ACTIVE_USERS,
            TOTAL_USERS
        FROM PROD.REPORTING.R_QUARTERLY_BUSINESS_REVIEW_REPORTING
        WHERE PROPERTY_ID IN ({ids_str})
          AND AFTER_PROPERTY_LAUNCH_FLAG = TRUE
          AND (TOTAL_UNITS > 0 OR TOTAL_USERS > 0)
        ORDER BY PROPERTY_ID, PERIOD_MONTH
    """)
    rows = cur.fetchall()
    cur.close()
    result = {}
    for r in rows:
        pid = int(r["PROPERTY_ID"])  # normalize to Python int
        pm = r["PERIOD_MONTH"]
        if hasattr(pm, 'to_pydatetime'):
            pm = pm.to_pydatetime()
        elif isinstance(pm, str):
            try:
                pm = datetime.strptime(pm[:10], "%Y-%m-%d")
            except:
                continue
        month_key = datetime(pm.year, pm.month, 1)
        au = r["ACTIVE_UNITS"] or 0
        tu = r["TOTAL_UNITS"] or 0
        ar = r["ACTIVE_USERS"] or 0
        tr = r["TOTAL_USERS"] or 0

        if pid not in result:
            result[pid] = {}
        result[pid][month_key] = {
            "unit_adoption": au / tu if tu > 0 else None,
            "resident_adoption": ar / tr if tr > 0 else None,
            "active_units": au,
            "total_units": tu,
            "active_users": ar,
            "total_users": tr,
        }
    return result


def _build_property_id_lookup(parsed_charges):
    """Build a property_name → property_id mapping from parsed charges."""
    lookup = {}
    for rec in parsed_charges:
        pname = rec.get("property_name")
        pid = rec.get("property_id")
        if pname and pid and pname not in lookup:
            try:
                lookup[pname] = int(pid)
            except (ValueError, TypeError):
                lookup[pname] = pid
    return lookup


# ─── Chart builders ──────────────────────────────────────────────────
def build_portfolio_chart(monthly_data, monthly_counts, months, title_prefix, cumulative=False, launch_analysis=None):
    """Build the aggregated portfolio bar + line chart."""
    portfolio_values = [monthly_data.get(m, 0) for m in months]
    portfolio_counts = [monthly_counts.get(m, 0) for m in months]

    if cumulative:
        import itertools
        portfolio_values = list(itertools.accumulate(portfolio_values))
        portfolio_counts = list(itertools.accumulate(portfolio_counts))
        mode_label = "Cumulative"
    else:
        mode_label = "Monthly"

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        subplot_titles=(
            f"{title_prefix}: {mode_label} Selected Fee Revenue (Total)",
            f"{mode_label} Charge Count"
        ),
        row_heights=[0.65, 0.35],
    )

    fig.add_trace(
        go.Bar(
            x=months, y=portfolio_values,
            marker_color='#B17455',
            text=[f"${v:,.0f}" for v in portfolio_values],
            textposition='outside',
            textfont_size=9,
            name="Revenue",
        ),
        row=1, col=1,
    )

    fig.add_trace(
        go.Scatter(
            x=months, y=portfolio_counts,
            mode='lines+markers',
            line=dict(color='#B17455', width=2.5),
            marker=dict(size=5),
            fill='tozeroy',
            fillcolor='rgba(177,116,85,0.1)',
            name="# Charges",
        ),
        row=2, col=1,
    )

    fig.update_layout(
        height=600,
        showlegend=False,
        template="plotly_white",
        plot_bgcolor="white",
        paper_bgcolor="#FAFAF8",
        font=dict(family="Poppins, Arial, sans-serif", size=10, color="#4F5155"),
    )
    fig.update_yaxes(title_text=f"{mode_label} Revenue ($)", row=1, col=1, tickfont=dict(color="#4F5155"))
    fig.update_yaxes(title_text=f"{'Total' if cumulative else '#'} Active Charges", row=2, col=1, tickfont=dict(color="#4F5155"))

    return fig


def build_stacked_area_chart(monthly_by_prop, months, title_prefix, cumulative=False):
    """Build stacked area chart by property."""
    import itertools
    prop_totals = {p: sum(monthly_by_prop[p].values()) for p in monthly_by_prop}
    sorted_props = sorted(prop_totals.keys(), key=lambda p: prop_totals[p], reverse=True)

    mode_label = "Cumulative" if cumulative else "Monthly"

    fig = go.Figure()
    colors = px.colors.qualitative.Plotly + px.colors.qualitative.Set3 + px.colors.qualitative.Pastel

    for i, prop in enumerate(sorted_props):
        values = [monthly_by_prop[prop].get(m, 0) for m in months]
        if cumulative:
            values = list(itertools.accumulate(values))
        short_name = prop.split(" - ", 1)[-1] if " - " in prop else prop
        fig.add_trace(go.Scatter(
            x=months, y=values,
            mode='lines',
            stackgroup='one',
            name=short_name,
            line=dict(width=0.5),
            fillcolor=colors[i % len(colors)],
        ))

    fig.update_layout(
        title=f"{title_prefix}: {mode_label} Fee Revenue by Property (Stacked)",
        height=550,
        template="plotly_white",
        yaxis_title=f"{mode_label} Revenue ($)",
        xaxis_title="Month",
        legend=dict(font=dict(size=9, color="#4F5155")),
        plot_bgcolor="white",
        paper_bgcolor="#FAFAF8",
        font=dict(family="Poppins, Arial, sans-serif", size=10, color="#4F5155"),
    )
    return fig


def _resolve_launch_dt(launch):
    """Parse a launch date value into a datetime or None."""
    if not launch:
        return None
    if isinstance(launch, str):
        try:
            return datetime.strptime(launch[:10], "%Y-%m-%d")
        except:
            return None
    if hasattr(launch, 'year'):
        return launch
    return None


def build_individual_property_charts(
    monthly_by_prop, months, launch_dates, title_prefix,
    launch_analysis=None,
    overlay_mode=None, compliance_data=None, prop_id_lookup=None,
    missing_rent_data=None, show_missing_rent=False,
    suspected_data=None, show_suspected=False,
):
    """Build a grid of individual property charts — 2 per row for clarity.

    Parameters
    ----------
    overlay_mode       : str or None – 'unit' or 'resident' to overlay adoption line on secondary y-axis
    compliance_data    : dict – {property_id: {month: {unit_adoption, resident_adoption, ...}}}
    prop_id_lookup     : dict – {property_name: property_id}
    missing_rent_data  : dict – {property_name: {missing_count, avg_fee, estimated_missing_rev, ...}}
    show_missing_rent  : bool – whether to show the confirmed missing rent bars
    suspected_data     : dict – {property_name: {missing_count, monthly_missing, ...}}
    show_suspected     : bool – whether to show the suspected undisclosed bars
    """
    prop_totals = {p: sum(monthly_by_prop[p].values()) for p in monthly_by_prop}
    sorted_props = sorted(prop_totals.keys(), key=lambda p: prop_totals[p], reverse=True)

    n_props = len(sorted_props)
    if n_props == 0:
        return None

    la = launch_analysis or {}
    mrd = missing_rent_data or {}
    srd = suspected_data or {}
    m0 = months[0]
    mN = months[-1]

    cols = 2
    rows = (n_props + cols - 1) // cols

    # Dynamic spacing — more generous with 2 columns
    max_v = 1.0 / max(rows - 1, 1)
    v_spacing = min(0.08, max_v * 0.75)
    h_spacing = 0.08

    has_overlay = (
        overlay_mode in ("unit", "resident")
        and compliance_data
        and prop_id_lookup
    )

    # Secondary y-axis needed for adoption overlay
    specs = None
    if has_overlay:
        specs = [[{"secondary_y": True} for _ in range(cols)] for _ in range(rows)]

    def _fmt_dollar(val):
        if abs(val) >= 1_000_000:
            return f"${val/1_000_000:,.1f}M"
        elif abs(val) >= 1_000:
            return f"${val/1_000:,.1f}K"
        else:
            return f"${val:,.0f}"

    subtitles = []
    for p in sorted_props:
        short = p.split(" - ", 1)[-1] if " - " in p else p
        launch_dt = _resolve_launch_dt(launch_dates.get(p))
        a = la.get(p)
        if launch_dt:
            launch_month = datetime(launch_dt.year, launch_dt.month, 1)
            if launch_month < m0:
                short += f" Live since {launch_dt.strftime('%b %Y')}"
            elif a and a["n_pre"] > 0 and a.get("baseline_reliable", True):
                # Only show lift annotation if baseline is meaningful relative to post
                # A near-zero baseline (e.g. $8/mo vs $5,600 post) means the property
                # wasn't really charging pet rent before PS, so the "lift" is misleading.
                # Threshold: pre_avg must be >= 2% of post_recent_avg to be meaningful.
                _post_ref = a.get("post_recent_avg", a.get("post_monthly_avg", 0))
                _baseline_meaningful = (
                    _post_ref <= 0  # no post data — show whatever we have
                    or a["pre_avg"] >= _post_ref * 0.02  # baseline is ≥ 2% of post
                )
                if _baseline_meaningful:
                    sign = "+" if a["diff_monthly"] >= 0 else ""
                    color = "#677848" if a["diff_monthly"] >= 0 else "#CF5A3F"
                    arrow = "↑" if a["diff_monthly"] >= 0 else "↓"
                    short += (
                        f'  <b><span style="color:{color}">'
                        f'{arrow} {sign}{_fmt_dollar(a["diff_monthly"])}/mo'
                        f'</span></b>'
                    )
                else:
                    short += f'  <span style="color:#999;font-size:0.85em">no meaningful pre-PS baseline</span>'
            elif a and a["n_pre"] > 0 and not a.get("baseline_reliable", True):
                short += f'  <span style="color:#999;font-size:0.85em">insufficient baseline</span>'
            else:
                short += f' Launched {launch_dt.strftime("%b %Y")}'
        else:
            short += " No launch date"

        # Add missing pet rent count as badge
        mr = mrd.get(p)
        if mr and mr["missing_count"] > 0:
            short += (
                f'  <span style="color:#DD7B45;font-size:0.85em">'
                f'{mr["missing_count"]} unpaid'
                f'</span>'
            )
        # Add suspected undisclosed count as badge
        sr = srd.get(p)
        if sr and sr["missing_count"] > 0:
            short += (
                f'  <span style="color:#CF5A3F;font-size:0.85em">'
                f'{sr["missing_count"]} suspected'
                f'</span>'
            )
        subtitles.append(short)

    fig = make_subplots(
        rows=rows, cols=cols,
        subplot_titles=subtitles,
        vertical_spacing=v_spacing,
        horizontal_spacing=h_spacing,
        specs=specs,
    )

    adoption_key = None
    if has_overlay:
        adoption_key = "unit_adoption" if overlay_mode == "unit" else "resident_adoption"

    for idx, prop in enumerate(sorted_props):
        r = idx // cols + 1
        c = idx % cols + 1
        values = [monthly_by_prop[prop].get(m, 0) for m in months]

        launch_dt = _resolve_launch_dt(launch_dates.get(prop))
        a = la.get(prop)
        launch_info = f"Launch: {launch_dt.strftime('%b %d, %Y')}" if launch_dt else "No launch date"

        if launch_dt:
            launch_month = datetime(launch_dt.year, launch_dt.month, 1)
            # Launch month itself = post-launch (green)
            if launch_month <= m0:
                bar_colors = '#677848'
            else:
                bar_colors = ['#677848' if m >= launch_month else '#7D9BC1' for m in months]
        else:
            bar_colors = '#AFB2B3'

        # Custom hover text with month name and launch date
        hover_texts = [
            f"<b>{m.strftime('%B %Y')}</b><br>"
            f"Revenue: ${v:,.0f}<br>"
            f"{launch_info}"
            for m, v in zip(months, values)
        ]

        fig.add_trace(
            go.Bar(
                x=months, y=values, marker_color=bar_colors, showlegend=False,
                hovertemplate="%{customdata}<extra></extra>",
                customdata=hover_texts,
                name="Collected",
            ),
            row=r, col=c,
            secondary_y=False if has_overlay else None,
        )

        # ── Missing rent bars (stacked on top of collected) ──────
        if show_missing_rent:
            mr = mrd.get(prop)
            if mr and mr.get("monthly_missing"):
                mm = mr["monthly_missing"]
                n_miss = mr["missing_count"]
                cl = mr.get("charge_type_label", "")
                missing_vals = [mm.get(m, 0) for m in months]

                # Only add trace if there's any missing revenue in the window
                if any(v > 0 for v in missing_vals):
                    missing_hovers = [
                        f"<b>{m.strftime('%B %Y')}</b><br>"
                        f"<b>Uncollected: ${mm.get(m, 0):,.0f}</b><br>"
                        f"{n_miss} tenants not paying<br>"
                        f"Type: {cl}<br>"
                        f"{launch_info}"
                        for m in months
                    ]
                    fig.add_trace(
                        go.Bar(
                            x=months, y=missing_vals,
                            marker_color="rgba(221, 123, 69, 0.65)",
                            showlegend=False,
                            hovertemplate="%{customdata}<extra></extra>",
                            customdata=missing_hovers,
                            name="Uncollected",
                        ),
                        row=r, col=c,
                        secondary_y=False if has_overlay else None,
                    )

        # ── Suspected undisclosed bars (stacked on top) ──────────
        if show_suspected:
            sr = srd.get(prop)
            if sr and sr.get("monthly_missing"):
                sm = sr["monthly_missing"]
                n_susp = sr["missing_count"]
                suspected_vals = [sm.get(m, 0) for m in months]

                if any(v > 0 for v in suspected_vals):
                    suspected_hovers = [
                        f"<b>{m.strftime('%B %Y')}</b><br>"
                        f"<b>Suspected: ${sm.get(m, 0):,.0f}</b><br>"
                        f"{n_susp} suspected undisclosed<br>"
                        f"{launch_info}"
                        for m in months
                    ]
                    fig.add_trace(
                        go.Bar(
                            x=months, y=suspected_vals,
                            marker_color="rgba(207, 90, 63, 0.45)",
                            showlegend=False,
                            hovertemplate="%{customdata}<extra></extra>",
                            customdata=suspected_hovers,
                            name="Suspected",
                        ),
                        row=r, col=c,
                        secondary_y=False if has_overlay else None,
                    )

        # ── Adoption line overlay (secondary y-axis) ─────────────
        if has_overlay:
            pid = prop_id_lookup.get(prop)
            if pid:
                prop_comp = compliance_data.get(pid, {})
                adoption_vals = []
                adoption_hovers = []
                for m in months:
                    entry = prop_comp.get(m)
                    if entry and entry.get(adoption_key) is not None:
                        val = round(entry[adoption_key] * 100, 1)
                        adoption_vals.append(val)
                        adoption_hovers.append(
                            f"<b>{m.strftime('%B %Y')}</b><br>"
                            f"Adoption: {val:.1f}%<br>"
                            f"{launch_info}"
                        )
                    else:
                        adoption_vals.append(None)
                        adoption_hovers.append("")

                fig.add_trace(
                    go.Scatter(
                        x=months, y=adoption_vals,
                        mode="lines+markers",
                        line=dict(color="rgba(156, 39, 176, 0.9)", width=3),
                        marker=dict(size=6, color="rgba(156, 39, 176, 0.9)"),
                        showlegend=False,
                        connectgaps=True,
                        hovertemplate="%{customdata}<extra></extra>",
                        customdata=adoption_hovers,
                    ),
                    row=r, col=c,
                    secondary_y=True,
                )

                # Fix secondary y-axis range 0–110%
                fig.update_yaxes(
                    range=[0, 110],
                    showgrid=False,
                    ticksuffix="%",
                    tickfont=dict(size=8, color="rgba(156, 39, 176, 0.7)"),
                    row=r, col=c, secondary_y=True,
                )

        # ── Baseline (pre-launch avg) — use row/col so axis refs are correct ──
        # Only show baseline line if it's meaningful relative to post-launch revenue
        _post_ref_bl = a.get("post_recent_avg", a.get("post_monthly_avg", 0)) if a else 0
        _bl_meaningful = a and a["pre_avg"] > 0 and a["n_pre"] > 0 and (
            _post_ref_bl <= 0 or a["pre_avg"] >= _post_ref_bl * 0.02
        )
        if _bl_meaningful:
            fig.add_hline(
                y=a["pre_avg"],
                row=r, col=c,
                line=dict(color="#E2AB58", width=1.5, dash="dot"),
            )
            _baseline_label = f"Pre-PS baseline ${a['pre_avg']:,.0f}/mo ({a['n_pre']}mo avg)"
            if not a.get("baseline_reliable", True):
                _baseline_label += " -- insufficient data"
            fig.add_annotation(
                x=months[-1], y=a["pre_avg"],
                row=r, col=c,
                text=_baseline_label,
                showarrow=False,
                font=dict(size=8, color="#B17455"),
                xanchor="right", yanchor="bottom",
            )

        # ── Red launch line — use row/col so it targets the correct subplot ──
        if launch_dt:
            launch_month_start = datetime(launch_dt.year, launch_dt.month, 1)
            line_pos = launch_month_start - timedelta(days=1)
            if m0 - timedelta(days=31) <= line_pos <= mN + timedelta(days=31):
                fig.add_vline(
                    x=line_pos,
                    row=r, col=c,
                    line=dict(color="#CF5A3F", width=2, dash="dash"),
                )

    # ── Legend line for overlay & missing rent ──
    overlay_legend = ""
    if has_overlay:
        overlay_label = "Unit" if overlay_mode == "unit" else "Resident"
        overlay_legend = (
            f"  ·  <span style='color:rgba(156,39,176,0.85)'>━●━</span> "
            f"{overlay_label} Adoption % (right axis)"
        )
    missing_legend = ""
    if show_missing_rent:
        missing_legend = (
            "  ·  <span style='color:#DD7B45'>■</span> Confirmed uncollected (est.)"
        )
    suspected_legend = ""
    if show_suspected:
        suspected_legend = (
            "  ·  <span style='color:#CF5A3F'>■</span> Suspected undisclosed (est.)"
        )

    # Stack bars when showing missing rent or suspected
    if show_missing_rent or show_suspected:
        fig.update_layout(barmode="stack")

    row_height = 300 if rows <= 15 else (250 if rows <= 30 else 200)
    fig.update_layout(
        height=max(450, rows * row_height),
        autosize=True,
        template="plotly_white",
        title=dict(
            text=(
                f"{title_prefix}: Individual Property Fee Trends ({n_props} properties)<br>"
                f"<sub style='font-size:11px;color:#636569'>"
                f"<span style='color:#7D9BC1'>■</span> Before PetScreening  ·  "
                f"<span style='color:#677848'>■</span> After PetScreening  ·  "
                f"<span style='color:#AFB2B3'>■</span> No launch date  ·  "
                f"<span style='color:#E2AB58'>---</span> Pre-launch avg  ·  "
                f"<span style='color:#CF5A3F'>|</span> Launch date  ·  "
                f"Already live before window"
                f"{overlay_legend}{missing_legend}{suspected_legend}</sub>"
            ),
            font=dict(size=14, color="#1F2257"),
        ),
        showlegend=False,
        plot_bgcolor="white",
        paper_bgcolor="#FAFAF8",
        font=dict(family="Poppins, Arial, sans-serif", size=10, color="#4F5155"),
    )
    # Make subplot title annotations (property names) dark, readable, and properly sized
    for ann in fig.layout.annotations:
        if ann.text and not ann.text.startswith("<"):
            ann.font = dict(size=12, color="#1F2257", family="Poppins, Arial, sans-serif")
    return fig



def generate_html_report(
    label, fig_individual, fig_snapshot, launch_analysis, monthly_by_prop, months,
    launch_dates, projected_100=None, overlay_mode_label=None,
    missing_rent_data=None, show_missing_rent=False, total_properties_fetched=0,
):
    """Generate a self-contained interactive HTML report for client sharing.

    Returns an HTML string with embedded Plotly charts (fully interactive —
    hover, zoom, pan all work), KPI summary, impact table, uncollected pet
    rent summary, and PetScreening branding.
    """
    import plotly.io as pio

    today_str = datetime.now().strftime("%B %d, %Y")

    # ── KPI summary ───────────────────────────────────────────────────
    comparable = {}
    if launch_analysis:
        comparable = {p: a for p, a in launch_analysis.items()
                      if a["n_pre"] > 0 and a.get("baseline_reliable", True) and a.get("baseline_meaningful", True)}
    agg_diff_mo = sum(a["diff_monthly"] for a in comparable.values()) if comparable else 0
    agg_diff = sum(a["diff_total"] for a in comparable.values()) if comparable else 0
    _launch_in_data = {p: d for p, d in launch_dates.items() if p in monthly_by_prop}
    n_with_launch = len(_launch_in_data)
    n_props_total = total_properties_fetched if total_properties_fetched else len(monthly_by_prop)
    n_comparable = len(comparable)

    sign_mo = "+" if agg_diff_mo >= 0 else ""
    sign_t = "+" if agg_diff >= 0 else ""

    # ── Impact table rows ─────────────────────────────────────────────
    impact_rows_html = ""
    if launch_analysis:
        sorted_la = sorted(launch_analysis.items(), key=lambda x: -x[1].get("diff_monthly", 0))
        for prop, a in sorted_la:
            short = prop.split(" - ", 1)[-1] if " - " in prop else prop
            if a["n_pre"] > 0 and a.get("baseline_reliable", True) and a.get("baseline_meaningful", True):
                s_m = "+" if a["diff_monthly"] >= 0 else ""
                s_t = "+" if a["diff_total"] >= 0 else ""
                color = "#677848" if a["diff_monthly"] >= 0 else "#CF5A3F"
                impact_rows_html += f"""
                <tr>
                    <td>{short}</td>
                    <td>{a["launch_month"].strftime("%b %Y")}</td>
                    <td>${a["pre_avg"]:,.0f}</td>
                    <td>${a["post_recent_avg"]:,.0f}</td>
                    <td style="color:{color};font-weight:bold">{s_m}${a["diff_monthly"]:,.0f}/mo</td>
                    <td style="color:{color};font-weight:bold">{s_t}${a["diff_total"]:,.0f}</td>
                    <td>{a["n_pre"]}mo pre · {a.get("n_recent_post", 0)}mo completed post · {a["n_post"]}mo total</td>
                </tr>"""
            elif a["n_pre"] > 0 and not a.get("baseline_reliable", True):
                impact_rows_html += f"""
                <tr>
                    <td>{short}</td>
                    <td>{a["launch_month"].strftime("%b %Y")}</td>
                    <td>${a["pre_avg"]:,.0f}</td>
                    <td>${a.get("post_recent_avg", a["post_monthly_avg"]):,.0f}</td>
                    <td colspan="2" style="text-align:center;color:#999">Insufficient baseline ({a["n_pre"]}mo)</td>
                    <td>{a["n_pre"]}mo pre · {a["n_post"]}mo total <span style="color:#CF5A3F;font-size:0.8em">(low data)</span></td>
                </tr>"""
            else:
                impact_rows_html += f"""
                <tr>
                    <td>{short}</td>
                    <td>{a["launch_month"].strftime("%b %Y")}</td>
                    <td colspan="4" style="text-align:center;color:#888">Live before lookback window</td>
                    <td>{a["n_post"]}mo after</td>
                </tr>"""

    # ── Projected revenue table (if overlay active) ───────────────────
    projected_section_html = ""
    if projected_100 and overlay_mode_label:
        total_current = sum(p["current_rev"] for p in projected_100.values())
        total_projected = sum(p["projected_rev_100"] for p in projected_100.values())
        total_additional = total_projected - total_current
        avg_adoption = sum(p["current_adoption"] for p in projected_100.values()) / len(projected_100)
        proj_rows_html = ""
        for prop, p in sorted(projected_100.items(), key=lambda x: -x[1]["additional_rev"]):
            short = prop.split(" - ", 1)[-1] if " - " in prop else prop
            proj_rows_html += f"""
            <tr>
                <td>{short}</td>
                <td>{p['current_adoption']:.1f}%</td>
                <td>${p['current_rev']:,.0f}</td>
                <td>${p['projected_rev_100']:,.0f}</td>
                <td style="color:#677848;font-weight:bold">+${p['additional_rev']:,.0f}/mo</td>
            </tr>"""
        projected_section_html = f"""
        <div class="section">
            <h2>Revenue Opportunity at 100% {overlay_mode_label} Adoption</h2>
            <div class="kpi-row">
                <div class="kpi-card">
                    <div class="kpi-label">Current Monthly Pet-Related Revenue</div>
                    <div class="kpi-value">${total_current:,.0f}</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">Projected at 100% {overlay_mode_label} Adoption</div>
                    <div class="kpi-value">${total_projected:,.0f}</div>
                    <div class="kpi-delta">+${total_additional:,.0f}/mo</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">Avg {overlay_mode_label} Adoption Now</div>
                    <div class="kpi-value">{avg_adoption:.1f}%</div>
                </div>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Property</th>
                        <th>Current {overlay_mode_label} Adoption</th>
                        <th>Current Revenue</th>
                        <th>Projected (100%)</th>
                        <th>Additional Rev</th>
                    </tr>
                </thead>
                <tbody>{proj_rows_html}</tbody>
            </table>
        </div>"""

    # ── Adoption methodology (when overlay active) ──────────────────────
    adoption_methodology_html = ""
    if projected_100 and overlay_mode_label:
        _is_unit = overlay_mode_label.lower() == "unit"
        _entity = "units" if _is_unit else "residents"
        _metric_desc = "Active units ÷ Total units" if _is_unit else "Active users ÷ Total users"
        adoption_methodology_html = f"""
        <div class="methodology" style="margin-top:24px">
            <h3>How We Calculate Revenue Opportunity at 100% {overlay_mode_label} Compliance</h3>
            <p><b>What is {overlay_mode_label} Adoption?</b><br>
            {"<b>Unit Adoption</b> = the percentage of units at a property that have at least one active PetScreening profile." if _is_unit else "<b>Resident Adoption</b> = the percentage of residents at a property that have created a PetScreening profile."}
            This data comes from the <b>Quarterly Business Review (QBR) reporting table</b>.</p>

            <p><b>How we calculate the projection</b><br>
            For each property, we use two inputs from the <b>latest month</b>:</p>
            <table style="margin:12px 0;font-size:12px;max-width:600px">
              <thead><tr><th>Input</th><th>Source</th><th>Example</th></tr></thead>
              <tbody>
                <tr><td>Current Monthly Pet-Related Revenue</td><td>Selected pet fee charges (Yardi)</td><td>$5,000/mo</td></tr>
                <tr><td>Current {overlay_mode_label} Adoption</td><td>{_metric_desc} (QBR)</td><td>65%</td></tr>
              </tbody>
            </table>
            <p style="font-family:monospace;font-size:12px;background:#F9F4E6;padding:12px 16px;border-radius:6px;line-height:1.8">
            Projected Revenue at 100% = Current Revenue ÷ (Current Adoption / 100)<br>
            &nbsp;&nbsp;= $5,000 ÷ 0.65 = <b>$7,692/mo</b><br><br>
            Additional Revenue = Projected − Current<br>
            &nbsp;&nbsp;= $7,692 − $5,000 = <b>+$2,692/mo</b>
            </p>
            <p><b>Why this works:</b> If a property earns $5,000/mo when 65% of {_entity} have completed screening,
            the avg revenue per compliant {"unit" if _is_unit else "resident"} is $5,000 ÷ 65% ≈ $76.92.
            At 100% adoption, that same per-{"unit" if _is_unit else "resident"} rate → ~$7,692/mo.</p>
            <p style="font-size:12px;color:var(--text-muted)"><b>Note:</b> This is a linear extrapolation.
            The last {_entity} to comply may have fewer or no pets, so actual revenue may be lower.
            Only properties with both fee revenue and adoption data are included.</p>
        </div>"""

    # ── Uncollected Pet Rent section (when toggle was on) ──────────────
    uncollected_section_html = ""
    if missing_rent_data and show_missing_rent:
        _mr_total = sum(v["missing_count"] for v in missing_rent_data.values())
        _mr_latest = months[-1]
        _mr_current_mo = sum(v["monthly_missing"].get(_mr_latest, 0) for v in missing_rent_data.values())
        _mr_total_window = sum(v.get("total_missing_in_window", 0) for v in missing_rent_data.values())
        _mr_n_props = sum(1 for v in missing_rent_data.values() if v["missing_count"] > 0)
        _mr_n_total = total_properties_fetched if total_properties_fetched else len(monthly_by_prop)

        _mr_rows_html = ""
        for pname, minfo in sorted(missing_rent_data.items(), key=lambda x: -x[1].get("total_missing_in_window", 0)):
            if minfo["missing_count"] == 0:
                continue
            _short = pname.split(" - ", 1)[-1] if " - " in pname else pname
            _mr_rows_html += f"""
            <tr>
                <td>{_short}</td>
                <td>{minfo["missing_count"]}</td>
                <td>{minfo.get("charge_type_label", "—")}</td>
                <td>${minfo["monthly_missing"].get(_mr_latest, 0):,.0f}</td>
                <td style="color:#DD7B45;font-weight:bold">${minfo.get("total_missing_in_window", 0):,.0f}</td>
            </tr>"""

        uncollected_section_html = f"""
        <div class="section">
            <h2>Uncollected Pet Rent</h2>
            <div class="kpi-row">
                <div class="kpi-card">
                    <div class="kpi-label">Tenants Not Paying</div>
                    <div class="kpi-value">{_mr_total:,}</div>
                    <div class="kpi-caption">Tenants with active household pet screening who are not being charged pet rent</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">Uncollected ({_mr_latest.strftime('%b %Y')})</div>
                    <div class="kpi-value" style="color:#DD7B45">${_mr_current_mo:,.0f}</div>
                    <div class="kpi-caption">Estimated uncollected revenue for the current month</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">Total Uncollected ({len(months)}mo window)</div>
                    <div class="kpi-value" style="color:#DD7B45">${_mr_total_window:,.0f}</div>
                    <div class="kpi-caption">Total estimated uncollected revenue across the lookback window</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">Properties Affected</div>
                    <div class="kpi-value">{_mr_n_props} of {_mr_n_total}</div>
                    <div class="kpi-caption">Properties with at least one unpaid tenant</div>
                </div>
            </div>
            <div class="methodology" style="border-left-color:#DD7B45">
                <h3>Why This Matters</h3>
                <p>When adoption goes up but revenue stays flat, these are the tenants causing the gap — they've
                completed their PetScreening screening but aren't being charged pet rent. The orange bars on the
                charts show this uncollected revenue based on each tenant's <b>actual lease dates</b>
                (only for months they were at the property, and only after PetScreening launched).</p>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Property</th>
                        <th>Unpaid Tenants</th>
                        <th>Charge Type</th>
                        <th>Current Mo ({_mr_latest.strftime('%b %Y')})</th>
                        <th>Total Uncollected ({len(months)}mo)</th>
                    </tr>
                </thead>
                <tbody>{_mr_rows_html}</tbody>
            </table>
        </div>"""

    # ── Convert Plotly figures to HTML divs ────────────────────────────
    _plotly_config = {"responsive": True, "displayModeBar": True, "scrollZoom": False}
    individual_html = pio.to_html(
        fig_individual, full_html=False, include_plotlyjs=False,
        config=_plotly_config, default_width="100%",
    ) if fig_individual else ""
    snapshot_html = pio.to_html(
        fig_snapshot, full_html=False, include_plotlyjs=False,
        config=_plotly_config, default_width="100%",
    ) if fig_snapshot else ""

    # ── Logo data URIs for embedding ──────────────────────────────────
    _logo_white = _PS_LOGO_WHITE_URI
    _logo_dark = _PS_LOGO_DARK_URI

    # ── Assemble the full HTML ────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PetScreening Fee Collection Report — {label}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&family=Lora:wght@700&display=swap" rel="stylesheet">
<style>
  :root {{
    --pack-blue: #1F2257;
    --retriever-rust: #B17455;
    --tabby-yellow: #E2AB58;
    --sky-blue: #DAEBF5;
    --succulent-green: #8DAEA7;
    --catnip-green: #677848;
    --dog-bone-white: #F9F4E6;
    --whisker-beige: #D3CEBD;
    --smokey-gray: #636569;
    --great-dane-gray: #4F5155;
    --cornflower-blue: #7D9BC1;
    --chew-toy-orange: #DD7B45;
    --fire-hydrant-red: #CF5A3F;
    --bg: #FAFAF8;
    --card-bg: #ffffff;
    --text: #4F5155;
    --text-heading: #1F2257;
    --text-muted: #636569;
    --border: #E8E6E0;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'Poppins', Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.7;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }}

  /* ── Header ── */
  .header {{
    background: var(--pack-blue);
    color: white;
    padding: 28px 48px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }}
  .header-left {{
    display: flex;
    align-items: center;
    gap: 20px;
  }}
  .header-logo {{
    height: 26px;
    opacity: 0.95;
  }}
  .header-divider {{
    width: 1px;
    height: 32px;
    background: rgba(255,255,255,0.2);
  }}
  .header h1 {{
    font-family: 'Poppins', Arial, sans-serif;
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.3px;
    margin: 0;
  }}
  .header .subtitle {{
    font-size: 13px;
    color: rgba(255,255,255,0.6);
    margin-top: 2px;
  }}
  .header-right {{
    text-align: right;
    font-size: 12px;
    color: rgba(255,255,255,0.55);
    line-height: 1.6;
  }}

  /* ── Container ── */
  .container {{
    max-width: 1400px;
    margin: 0 auto;
    padding: 32px 48px 64px;
  }}

  /* ── Sections ── */
  .section {{
    margin-bottom: 36px;
  }}
  .section h2 {{
    font-family: 'Poppins', Arial, sans-serif;
    font-size: 16px;
    font-weight: 600;
    margin-bottom: 16px;
    color: var(--text-heading);
    border-bottom: 2px solid var(--retriever-rust);
    padding-bottom: 8px;
    display: inline-block;
    letter-spacing: -0.2px;
  }}

  /* ── KPI Cards ── */
  .kpi-row {{
    display: flex;
    gap: 16px;
    margin-bottom: 24px;
  }}
  .kpi-card {{
    flex: 1;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 22px 26px;
    box-shadow: 0 1px 3px rgba(31,34,87,0.04);
    transition: box-shadow 0.15s ease;
  }}
  .kpi-card:hover {{
    box-shadow: 0 4px 12px rgba(31,34,87,0.08);
  }}
  .kpi-label {{
    font-size: 11px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 6px;
    font-weight: 500;
  }}
  .kpi-value {{
    font-size: 28px;
    font-weight: 700;
    color: var(--text-heading);
    letter-spacing: -0.5px;
  }}
  .kpi-delta {{
    font-size: 14px;
    color: var(--catnip-green);
    font-weight: 600;
    margin-top: 4px;
  }}
  .kpi-caption {{
    font-size: 11px;
    color: var(--text-muted);
    margin-top: 8px;
    line-height: 1.5;
  }}

  /* ── Tables ── */
  table {{
    width: 100%;
    border-collapse: collapse;
    background: var(--card-bg);
    border-radius: 10px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(31,34,87,0.04);
    font-size: 13px;
  }}
  thead {{
    background: var(--dog-bone-white);
  }}
  th {{
    padding: 11px 16px;
    text-align: left;
    font-weight: 600;
    color: var(--text-heading);
    border-bottom: 2px solid var(--border);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  td {{
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    color: var(--text);
  }}
  tr:last-child td {{
    border-bottom: none;
  }}
  tr:hover td {{
    background: #FAFAF5;
  }}

  /* ── Chart section ── */
  .chart-section {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 24px;
    box-shadow: 0 1px 3px rgba(31,34,87,0.04);
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }}
  .chart-section .plotly-graph-div {{
    width: 100% !important;
  }}

  /* ── Methodology ── */
  .methodology {{
    background: #F7F5EE;
    border: 1px solid var(--whisker-beige);
    border-left: 3px solid var(--retriever-rust);
    border-radius: 0 8px 8px 0;
    padding: 18px 24px;
    font-size: 13px;
    margin-bottom: 28px;
    line-height: 1.8;
  }}
  .methodology h3 {{
    font-family: 'Poppins', Arial, sans-serif;
    font-size: 14px;
    font-weight: 600;
    margin-bottom: 8px;
    color: var(--text-heading);
  }}
  .methodology ul {{
    margin-left: 20px;
    margin-top: 6px;
  }}
  .methodology li {{
    margin-bottom: 3px;
  }}

  /* ── Legend ── */
  .legend {{
    display: flex;
    gap: 20px;
    flex-wrap: wrap;
    font-size: 12px;
    margin-bottom: 20px;
    padding: 12px 16px;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
  }}
  .legend-item {{
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--text-muted);
  }}
  .legend-dot {{
    width: 14px;
    height: 14px;
    border-radius: 3px;
    display: inline-block;
  }}

  /* ── Footer ── */
  .footer {{
    text-align: center;
    padding: 28px 24px;
    font-size: 11px;
    color: var(--text-muted);
    border-top: 1px solid var(--border);
    margin-top: 56px;
    letter-spacing: 0.2px;
  }}
  .footer-logo {{
    height: 22px;
    margin-bottom: 10px;
    opacity: 0.7;
  }}

  /* ── Mobile notice (hidden on desktop) ── */
  .mobile-chart-notice {{
    display: none;
    background: var(--sky-blue);
    border: 1px solid var(--cornflower-blue);
    border-left: 4px solid var(--pack-blue);
    border-radius: 0 10px 10px 0;
    padding: 20px 24px;
    margin-bottom: 20px;
    text-align: center;
  }}
  .mobile-chart-notice .notice-icon {{
    font-size: 32px;
    margin-bottom: 8px;
  }}
  .mobile-chart-notice .notice-title {{
    font-family: 'Poppins', Arial, sans-serif;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-heading);
    margin-bottom: 6px;
  }}
  .mobile-chart-notice .notice-body {{
    font-size: 13px;
    color: var(--text-muted);
    line-height: 1.6;
  }}

  /* ── Mobile / Phone ── */
  @media (max-width: 768px) {{
    .header {{
      padding: 20px 16px;
      flex-direction: column;
      align-items: flex-start;
      gap: 8px;
    }}
    .header-right {{ text-align: left; }}
    .header h1 {{ font-size: 16px; }}
    .container {{ padding: 16px; }}
    .kpi-row {{
      flex-direction: column;
      gap: 10px;
    }}
    .kpi-card {{
      padding: 16px;
    }}
    .kpi-value {{ font-size: 22px; }}
    .kpi-label {{ font-size: 10px; }}
    .section h2 {{ font-size: 14px; }}
    table {{ font-size: 11px; }}
    th, td {{ padding: 8px 10px; }}
    .chart-section {{ display: none; }}
    .mobile-chart-notice {{ display: block; }}
    .methodology {{ padding: 14px 16px; font-size: 12px; }}
    .legend {{ display: none; }}
  }}
  @media (max-width: 480px) {{
    .header h1 {{ font-size: 14px; }}
    .kpi-value {{ font-size: 18px; }}
    table {{ font-size: 10px; display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
    .methodology {{ font-size: 11px; }}
  }}

  @media print {{
    body {{ background: white; }}
    .header {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
    .kpi-card {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; break-inside: avoid; }}
    .chart-section {{ break-inside: avoid; }}
  }}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <img src="{_logo_white}" alt="PetScreening" class="header-logo"
         onerror="this.style.display='none'">
    <div class="header-divider"></div>
    <div>
      <h1>Fee Collection Analysis</h1>
      <div class="subtitle">{label}</div>
    </div>
  </div>
  <div class="header-right">
    Report generated {today_str}<br>
    {months[0].strftime("%b %Y")} – {months[-1].strftime("%b %Y")} · {len(months)} months
  </div>
</div>

<div class="container">

  <!-- KPI Summary -->
  <div class="section">
    <h2>PetScreening Revenue Impact</h2>
    <div class="kpi-row">
      <div class="kpi-card">
        <div class="kpi-label">Cumulative Pet Revenue Impact</div>
        <div class="kpi-value">{sign_t}${agg_diff:,.0f}</div>
        <div class="kpi-caption">Cumulative pet fee impact since launch</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Monthly Pet Revenue Change</div>
        <div class="kpi-value">{sign_mo}${agg_diff_mo:,.0f}/mo</div>
        <div class="kpi-caption">Average monthly pet fee uplift since launch across {n_comparable} properties</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Properties with Launch Date</div>
        <div class="kpi-value">{n_with_launch} of {n_props_total}</div>
        <div class="kpi-caption">{n_comparable} with pre &amp; post data for comparison</div>
      </div>
    </div>
  </div>

  <!-- Methodology -->
  <div class="methodology">
    <h3>How We Calculate PetScreening Impact</h3>
    <p>For each property, we compare average monthly pet fee revenue <b>before</b> vs <b>after</b> PetScreening launch:</p>
    <ul>
      <li><b>Before PetScreening (avg/mo)</b> — Average of up to 6 months before launch (uses whatever pre-launch data is available)</li>
      <li><b>After PetScreening (avg/mo)</b> — Average of all completed post-launch months (excludes current partial month)</li>
      <li><b>Monthly Change</b> = Cumulative impact ÷ completed post months (average monthly uplift since launch)</li>
      <li><b>Total Change</b> = Total post-launch revenue − (Pre avg × post months) — actual observed cumulative impact</li>
    </ul>
  </div>

  <!-- Legend -->
  <div class="legend">
    <div class="legend-item"><span class="legend-dot" style="background:#7D9BC1"></span> Before PetScreening</div>
    <div class="legend-item"><span class="legend-dot" style="background:#677848"></span> After PetScreening</div>
    <div class="legend-item"><span class="legend-dot" style="background:#AFB2B3"></span> No launch date</div>
    <div class="legend-item"><span class="legend-dot" style="background:#E2AB58;height:3px;border-radius:0"></span> Pre-launch avg</div>
    <div class="legend-item"><span class="legend-dot" style="background:#CF5A3F;width:3px;height:14px;border-radius:0"></span> Launch date</div>
    {"<div class='legend-item'><span class='legend-dot' style='background:#DD7B45'></span> Uncollected pet rent (est.)</div>" if show_missing_rent else ""}
  </div>

  <!-- Mobile-only notice (hidden on desktop) -->
  <div class="mobile-chart-notice">
    <div class="notice-icon"></div>
    <div class="notice-title">Interactive Charts — Best Viewed on Desktop</div>
    <div class="notice-body">
      This report includes interactive charts with hover details, zoom, and pan.
      These features require a desktop or laptop browser to render properly.<br>
      <span style="margin-top:8px;display:inline-block;font-size:12px;color:var(--retriever-rust);font-weight:500">
        All KPIs, tables, and data above &amp; below are fully readable on mobile.
      </span>
    </div>
  </div>

  <!-- Individual Property Charts -->
  <div class="section">
    <h2>Individual Property Fee Trends</h2>
    <div class="chart-section">{individual_html}</div>
  </div>

  {projected_section_html}

  {adoption_methodology_html}

  {uncollected_section_html}

  <!-- Current Snapshot -->
  {"<div class='section'><h2>Current Monthly Fee Revenue by Property</h2><div class='chart-section'>" + snapshot_html + "</div></div>" if snapshot_html else ""}

  <!-- Impact Breakdown Table -->
  {"<div class='section'><h2>PetScreening Impact by Property</h2><table><thead><tr><th>Property</th><th>Launch</th><th>Pre-PS Avg ($/mo)</th><th>Current Avg ($/mo)</th><th>Monthly Lift</th><th>Cumulative Impact</th><th>Window</th></tr></thead><tbody>" + impact_rows_html + "</tbody></table></div>" if impact_rows_html else ""}

  <div class="footer">
    <img src="{_logo_dark}" alt="PetScreening" class="footer-logo"
         onerror="this.style.display='none'"><br>
    Powered by PetScreening · Report generated {today_str} · Charts are interactive on desktop — hover, zoom, and pan to explore
  </div>

</div>

<script>
// Resize Plotly charts when window size changes (e.g. tablet rotation).
// On phones (<768px) charts are hidden via CSS, so this only fires for larger screens.
function resizePlotlyCharts() {{
    var plots = document.querySelectorAll('.plotly-graph-div');
    plots.forEach(function(plot) {{
        if (plot && plot.offsetParent !== null && typeof Plotly !== 'undefined') {{
            Plotly.Plots.resize(plot);
        }}
    }});
}}
window.addEventListener('load', function() {{ setTimeout(resizePlotlyCharts, 400); }});
window.addEventListener('resize', resizePlotlyCharts);
</script>

</body>
</html>"""
    return html


def generate_tranche_pdf(
    label, today_str, pre_baseline, comparable_count,
    t1_mo, t1_total, t1_pct, t1_months,
    t2_tenants, t2_mo, t2_props, t1_t2_combined, t1_t2_pct,
    t3_adoption, t3_additional, t3_total_impact, t3_pct,
    adopt_type_label, current_monthly_rev,
    n_props_total, n_with_launch, n_props_with_data,
    include_pm=False, pm_rows=None,
    su_total_profiles=0, su_current_mo=0, total_projected=0,
    missing_rent_data=None, total_units=0,
):
    """Generate a branded PDF with card-based KPIs + narrative storytelling.

    Matches the Summary tab visual style: big numbers in cards, short
    narrative sentences, and the key metrics table Eduardo likes.

    Returns PDF bytes ready for st.download_button.
    """
    from fpdf import FPDF

    class PDF(FPDF):
        def header(self):
            self.set_fill_color(31, 34, 87)  # #1F2257
            self.rect(0, 0, 210, 38, 'F')
            self.set_font('Helvetica', 'B', 18)
            self.set_text_color(255, 255, 255)
            self.set_xy(15, 6)
            self.cell(0, 8, label, ln=True)
            self.set_font('Helvetica', '', 11)
            self.set_text_color(226, 171, 88)  # #E2AB58
            self.set_xy(15, 15)
            self.cell(0, 6, 'PetScreening Value Report', ln=True)
            self.set_font('Helvetica', '', 9)
            self.set_text_color(218, 235, 245)  # #DAEBF5
            self.set_xy(15, 23)
            self.cell(0, 6, today_str, ln=True)
            self.ln(12)

        def footer(self):
            self.set_y(-15)
            self.set_font('Helvetica', 'I', 8)
            self.set_text_color(150, 150, 150)
            self.cell(0, 10, f'PetScreening Value Report  |  {label}  |  Page {self.page_no()}', align='C')

    pdf = PDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # ── Brand colors ──
    dark_blue = (31, 34, 87)
    green = (103, 120, 72)
    orange = (221, 123, 69)
    warm = (177, 116, 85)
    body_gray = (79, 81, 85)
    light_gray = (99, 101, 105)
    card_border = (232, 228, 218)
    card_fill = (250, 250, 248)

    # ── Layout constants ──
    PAGE_L = 15          # left margin
    PAGE_R = 195         # right edge
    USABLE_W = PAGE_R - PAGE_L  # 180mm
    CARD_GAP = 4
    CARD_W = (USABLE_W - 2 * CARD_GAP) / 3  # ~57.3mm
    CARD_H = 30          # compact cards to fit 3 sections on page 1

    # ── Helper: draw a row of 3 KPI cards ──
    def draw_card_row(cards):
        """Draw 3 cards side-by-side. Each card is a dict:
        {value: str, label: str, sub: str|None, color: tuple}
        """
        start_y = pdf.get_y()
        # Check if we need a page break (card row + narrative below ~50mm)
        if start_y + CARD_H + 25 > pdf.h - pdf.b_margin:
            pdf.add_page()
            start_y = pdf.get_y()

        for i, card in enumerate(cards):
            x = PAGE_L + i * (CARD_W + CARD_GAP)
            # Card background
            pdf.set_fill_color(*card_fill)
            pdf.set_draw_color(*card_border)
            pdf.rect(x, start_y, CARD_W, CARD_H, 'DF')
            # Big number
            pdf.set_font('Helvetica', 'B', 20)
            pdf.set_text_color(*card.get("color", dark_blue))
            pdf.set_xy(x + 2, start_y + 3)
            pdf.cell(CARD_W - 4, 9, card["value"], align='C')
            # Label
            pdf.set_font('Helvetica', '', 7.5)
            pdf.set_text_color(*light_gray)
            pdf.set_xy(x + 2, start_y + 14)
            pdf.cell(CARD_W - 4, 4, card["label"].upper(), align='C')
            # Sub-label (optional)
            if card.get("sub"):
                pdf.set_font('Helvetica', '', 6.5)
                pdf.set_text_color(150, 150, 150)
                pdf.set_xy(x + 2, start_y + 19)
                pdf.cell(CARD_W - 4, 4, card["sub"], align='C')

        pdf.set_y(start_y + CARD_H + 3)

    # ── Helper: highlighted callout box (for cap rate) ──
    def callout_box(text, color=orange):
        """Draw a prominent highlighted callout with colored left border."""
        start_y = pdf.get_y()
        box_x = PAGE_L + 4
        box_w = USABLE_W - 8
        # Measure text height
        pdf.set_font('Helvetica', 'B', 10)
        # Draw background
        pdf.set_fill_color(255, 248, 240)  # warm cream
        pdf.set_draw_color(*color)
        box_h = 12
        pdf.rect(box_x, start_y, box_w, box_h, 'F')
        # Left accent bar
        pdf.set_fill_color(*color)
        pdf.rect(box_x, start_y, 2.5, box_h, 'F')
        # Text
        pdf.set_text_color(*color)
        pdf.set_xy(box_x + 6, start_y + 2)
        pdf.cell(box_w - 10, 8, text, align='C')
        pdf.set_y(start_y + box_h + 3)

    # ── Helper: section heading ──
    def section_heading(title, color=dark_blue):
        # Check for page break
        if pdf.get_y() + 45 > pdf.h - pdf.b_margin:
            pdf.add_page()
        pdf.set_font('Helvetica', 'B', 12)
        pdf.set_text_color(*color)
        pdf.cell(0, 7, title, ln=True)
        # Colored underline
        y = pdf.get_y()
        pdf.set_draw_color(*color)
        pdf.set_line_width(0.6)
        pdf.line(PAGE_L, y, PAGE_L + 50, y)
        pdf.set_line_width(0.2)
        pdf.ln(3)

    # ── Helper: narrative text ──
    def narrative(text):
        pdf.set_font('Helvetica', '', 9)
        pdf.set_text_color(*body_gray)
        pdf.multi_cell(0, 4.5, text)
        pdf.ln(2)

    # ── Helper: divider ──
    def divider():
        pdf.set_draw_color(*card_border)
        y = pdf.get_y()
        pdf.line(PAGE_L, y, PAGE_R, y)
        pdf.ln(5)

    # ── Pre-compute recurring vs one-time (used by both sections) ──
    _opp_recurring_mo = 0
    _opp_onetime_total = 0
    if missing_rent_data:
        for _v in missing_rent_data.values():
            _cnt = _v.get("missing_count", 0)
            if _cnt == 0:
                continue
            _opp_recurring_mo += _cnt * _v.get("avg_recurring", 0)
            _opp_onetime_total += _cnt * _v.get("avg_onetime", 0)
    else:
        _opp_recurring_mo = t2_mo

    _opp_annual_recurring = _opp_recurring_mo * 12
    _opp_annual_impact = _opp_annual_recurring + _opp_onetime_total
    _opp_cap_rate = 0.05
    _opp_value_impact = _opp_annual_impact / _opp_cap_rate if _opp_annual_impact > 0 else 0

    # ═══════════════════════════════════════════════════════════
    #  SECTION 1: VALUE CREATED
    # ═══════════════════════════════════════════════════════════
    section_heading("Value Created", green)

    _sign_t1 = "+" if t1_mo > 0 else ""
    _t1_color = green if t1_mo >= 0 else orange
    _cum_str = f"${t1_total:,.0f}" if comparable_count > 0 and t1_total != 0 else "--"

    draw_card_row([
        {
            "value": f"${current_monthly_rev:,.0f}",
            "label": "Current Monthly Pet-Related Revenue",
            "sub": "Total pet fee revenue",
            "color": dark_blue,
        },
        {
            "value": f"{_sign_t1}${t1_mo:,.0f}" if comparable_count > 0 and t1_mo != 0 else "--",
            "label": "Pet Revenue Change Since PS",
            "sub": f"{t1_pct:+.1f}% vs baseline" if comparable_count > 0 and t1_pct != 0 else None,
            "color": _t1_color,
        },
        {
            "value": _cum_str,
            "label": "Cumulative Pet Revenue Impact",
            "sub": f"Over {t1_months} months" if comparable_count > 0 and t1_months > 0 else None,
            "color": green if t1_total > 0 else dark_blue,
        },
    ])

    if comparable_count > 0 and t1_mo != 0:
        if t1_mo > 0:
            pct_note = f", a {t1_pct:.1f}% increase" if t1_pct > 0 else ""
            _simple_diff = current_monthly_rev - pre_baseline
            narrative(
                f"Before PetScreening, this portfolio collected ${pre_baseline:,.0f}/mo in pet fees. "
                f"Today it collects ${current_monthly_rev:,.0f}/mo - a ${_simple_diff:,.0f}/mo increase. "
                f"After adjusting for property-by-property pre/post averages across {comparable_count} "
                f"comparable properties, the net revenue change is ${t1_mo:,.0f}/mo{pct_note}. "
                f"Over {t1_months} months, this has resulted in ${t1_total:,.0f} in cumulative "
                f"incremental revenue."
            )
        else:
            # Negative lift — acknowledge and pivot to opportunity
            _neg_mo = abs(t1_mo)
            _neg_total = abs(t1_total)
            _pivot_parts = [
                f"Pet fee revenue is currently ${_neg_mo:,.0f}/mo below the pre-launch baseline "
                f"of ${pre_baseline:,.0f}/mo (${_neg_total:,.0f} cumulative over {t1_months} months). "
            ]
            # Pivot to opportunity
            if t2_tenants > 0 and _opp_recurring_mo > 0:
                _pivot_parts.append(
                    f"However, ${_opp_recurring_mo:,.0f}/mo in pet rent is going uncollected from "
                    f"tenants who have already completed screening -- a billing correction that would "
                    f"more than close this gap. "
                )
            if t3_adoption is not None and t3_adoption < 100 and total_projected and total_projected > 0:
                _additional_at_100 = total_projected - (current_monthly_rev or 0)
                if _additional_at_100 > 0:
                    _pivot_parts.append(
                        f"Combined with closing the adoption gap from {t3_adoption:.1f}% to 100% "
                        f"(+${_additional_at_100:,.0f}/mo), the revenue picture changes significantly."
                    )
            narrative("".join(_pivot_parts))
    else:
        narrative(
            "Revenue lift data is not yet available. Once properties have sufficient "
            "pre-launch and post-launch charge data, this section will show the incremental "
            "value PetScreening has created."
        )

    divider()

    # ═══════════════════════════════════════════════════════════
    #  SECTION 2: REVENUE OPPORTUNITY
    # ═══════════════════════════════════════════════════════════
    section_heading("Revenue Opportunity", orange)

    # ── Single row: 3 cards ──
    draw_card_row([
        {
            "value": f"{t2_tenants:,}" if t2_tenants > 0 else "0",
            "label": "Tenants Not Paying",
            "sub": f"Across {t2_props} properties" if t2_tenants > 0 else "All tenants compliant",
            "color": orange if t2_tenants > 0 else green,
        },
        {
            "value": f"${_opp_recurring_mo:,.0f}" if _opp_recurring_mo > 0 else "$0",
            "label": "Missing Monthly Pet Rent",
            "sub": f"{t2_tenants:,} tenants x ${_opp_recurring_mo / t2_tenants:,.0f}/mo avg fee" if t2_tenants > 0 and _opp_recurring_mo > 0 else "Fully collected",
            "color": orange if _opp_recurring_mo > 0 else green,
        },
        {
            "value": f"${_opp_annual_impact:,.0f}/yr" if _opp_annual_impact > 0 else "$0",
            "label": "Annual Revenue Impact",
            "sub": f"${_opp_recurring_mo:,.0f}/mo x 12" + (f" + ${_opp_onetime_total:,.0f} one-time" if _opp_onetime_total > 0 else "") if _opp_annual_impact > 0 else "No revenue gap identified",
            "color": orange if _opp_annual_impact > 0 else green,
        },
    ])

    if t2_tenants > 0:
        _avg_fee_per_tenant = _opp_recurring_mo / t2_tenants if t2_tenants > 0 else 0
        narrative(
            f"{t2_tenants:,} tenants have completed PetScreening profiles but are not being "
            f"charged pet rent. At an average pet fee of ${_avg_fee_per_tenant:,.0f}/mo per tenant "
            f"({t2_tenants:,} x ${_avg_fee_per_tenant:,.0f} = ${_opp_recurring_mo:,.0f}/mo)"
            + (f" plus ${_opp_onetime_total:,.0f} in uncollected one-time fees" if _opp_onetime_total > 0 else "")
            + f" across {t2_props} properties. "
            f"This is a billing correction, not a sales effort."
        )
        if _opp_annual_impact > 0:
            narrative(
                f"On an annual basis, that is ${_opp_annual_impact:,.0f} in revenue not being captured."
            )
            callout_box(
                f"Unrealized Property Value: ${_opp_value_impact:,.0f}  (${_opp_annual_impact:,.0f}/yr at 5% cap rate)"
            )
        if su_total_profiles and su_total_profiles > 0:
            narrative(
                f"Additionally, {su_total_profiles:,} tenants show signals of undisclosed pets "
                f"(abandoned screening, unresolved requests), representing an estimated "
                f"${su_current_mo:,.0f}/mo in potential additional revenue."
            )
    elif su_total_profiles and su_total_profiles > 0:
        narrative(
            f"No confirmed tenants are missing pet rent charges. However, "
            f"{su_total_profiles:,} tenants show signals of undisclosed pets, representing "
            f"an estimated ${su_current_mo:,.0f}/mo in potential additional revenue."
        )
    else:
        _opportunity_note = (
            "All screened tenants are currently being charged pet rent -- no billing gaps identified."
        )
        if t3_adoption is not None and t3_adoption < 100 and total_projected and total_projected > 0:
            _additional_at_100 = total_projected - current_monthly_rev if current_monthly_rev else 0
            if _additional_at_100 > 0:
                _opportunity_note += (
                    f" However, your portfolio is currently at {t3_adoption:.1f}% {adopt_type_label.lower()} adoption. "
                    f"Closing that gap to 100% -- through consistent screening enforcement at move-in "
                    f"and renewal -- would unlock an estimated ${_additional_at_100:,.0f}/mo in additional "
                    f"pet fee revenue (${total_projected:,.0f}/mo projected at full adoption)."
                )
        narrative(_opportunity_note)

    divider()

    # ═══════════════════════════════════════════════════════════
    #  SECTION 3: PORTFOLIO HEALTH
    # ═══════════════════════════════════════════════════════════
    section_heading("Portfolio Health", dark_blue)

    _adopt_str = f"{t3_adoption:.1f}%" if t3_adoption is not None else "--"
    _su_str = f"{su_total_profiles:,}" if su_total_profiles and su_total_profiles > 0 else "n/a"

    draw_card_row([
        {
            "value": _adopt_str,
            "label": f"Avg {adopt_type_label} Adoption",
            "sub": f"Across {n_props_with_data} properties",
            "color": green if t3_adoption is not None and t3_adoption >= 50 else orange,
        },
        {
            "value": f"{n_with_launch}",
            "label": "Properties with Launch Date",
            "sub": f"of {n_props_total} total",
            "color": dark_blue,
        },
        {
            "value": _su_str,
            "label": "Suspected Undisclosed",
            "sub": f"~${su_current_mo:,.0f}/mo" if su_total_profiles and su_total_profiles > 0 else None,
            "color": orange if su_total_profiles and su_total_profiles > 0 else dark_blue,
        },
    ])

    if t3_adoption is not None:
        _ph_narrative = (
            f"Your portfolio is running at {t3_adoption:.1f}% {adopt_type_label.lower()} adoption. "
            f"{n_with_launch} of {n_props_total} properties have an established PetScreening launch date, "
            f"and {n_props_with_data} have sufficient charge data for analysis."
        )
        if total_projected and total_projected > 0:
            _additional_at_100 = total_projected - (current_monthly_rev or 0)
            if _additional_at_100 > 0:
                _ph_narrative += (
                    f" At 100% {adopt_type_label.lower()} adoption, projected pet fee revenue would be "
                    f"${total_projected:,.0f}/mo -- an additional ${_additional_at_100:,.0f}/mo opportunity."
                )
        narrative(_ph_narrative)
    else:
        narrative(
            f"{n_with_launch} of {n_props_total} properties have an established PetScreening "
            f"launch date. Adoption data will populate once screening activity is available."
        )

    divider()

    # ═══════════════════════════════════════════════════════════
    #  KEY METRICS TABLE (always starts on page 2)
    # ═══════════════════════════════════════════════════════════
    pdf.add_page()

    pdf.set_font('Helvetica', 'B', 12)
    pdf.set_text_color(*dark_blue)
    pdf.cell(0, 8, 'Key Metrics', ln=True)
    pdf.ln(2)

    # Table header row
    pdf.set_fill_color(249, 244, 230)  # dog-bone-white
    pdf.set_draw_color(*card_border)
    pdf.set_font('Helvetica', 'B', 8)
    pdf.set_text_color(*dark_blue)
    pdf.cell(100, 7, '  METRIC', border=1, fill=True)
    pdf.cell(80, 7, '  VALUE', border=1, fill=True, ln=True)

    metrics = []
    metrics.append(("Pre-PS Baseline", f"${pre_baseline:,.0f}/mo"))
    metrics.append(("Current Monthly Pet-Related Revenue", f"${current_monthly_rev:,.0f}/mo"))
    if comparable_count > 0 and t1_mo != 0:
        metrics.append(("Monthly Revenue Lift", f"+${t1_mo:,.0f}/mo"))
        metrics.append(("Cumulative Revenue Impact", f"${t1_total:,.0f}"))
    if t2_tenants > 0:
        metrics.append(("Tenants Not Paying Pet Rent", f"{t2_tenants:,}"))
        metrics.append(("Uncollected Revenue", f"${t2_mo:,.0f}/mo"))
    if su_total_profiles and su_total_profiles > 0:
        metrics.append(("Suspected Undisclosed Pets", f"{su_total_profiles:,}"))
        metrics.append(("Suspected Undisclosed Revenue", f"~${su_current_mo:,.0f}/mo"))
    if t3_adoption is not None:
        metrics.append((f"{adopt_type_label} Adoption", f"{t3_adoption:.1f}%"))
    if t3_additional > 0:
        metrics.append(("Additional Opportunity at 100%", f"+${t3_additional:,.0f}/mo"))
    if total_projected and total_projected > 0:
        metrics.append(("Projected Revenue at 100% Adoption", f"${total_projected:,.0f}/mo"))
    if total_units and total_units > 0:
        metrics.append(("Total Units", f"{total_units:,}"))
        if current_monthly_rev and total_units > 0:
            _rev_per_unit = current_monthly_rev / total_units
            metrics.append(("Pet Revenue per Unit", f"${_rev_per_unit:,.2f}/mo"))
    metrics.append(("Properties with Launch Date", f"{n_with_launch} of {n_props_total}"))
    metrics.append(("Properties with Charge Data", f"{n_props_with_data} of {n_props_total}"))

    pdf.set_font('Helvetica', '', 9)
    for i, (label_m, value_m) in enumerate(metrics):
        # Alternate row shading
        if i % 2 == 0:
            pdf.set_fill_color(255, 255, 255)
        else:
            pdf.set_fill_color(250, 250, 248)
        pdf.set_text_color(*body_gray)
        pdf.cell(100, 6, f'  {label_m}', border='LR', fill=True)
        pdf.set_text_color(*dark_blue)
        pdf.set_font('Helvetica', 'B', 9)
        pdf.cell(80, 6, f'  {value_m}', border='LR', fill=True, ln=True)
        pdf.set_font('Helvetica', '', 9)
    # Close table bottom
    pdf.cell(180, 0, '', border='T', ln=True)

    # ── Property Managers (optional) ──
    if include_pm and pm_rows and len(pm_rows) > 0:
        pdf.ln(6)
        divider()
        pdf.set_font('Helvetica', 'B', 11)
        pdf.set_text_color(*dark_blue)
        pdf.cell(0, 8, 'Property Managers', ln=True)
        pdf.ln(2)

        _pm_by_prop = defaultdict(set)
        for r in pm_rows:
            pname = r.get('PROPERTY_NAME', 'Unknown')
            email = r.get('PM_EMAIL', '')
            if email and email.strip():
                short = pname.split(" - ", 1)[-1] if " - " in pname else pname
                _pm_by_prop[short].add(email)

        pdf.set_font('Helvetica', 'B', 8)
        pdf.set_fill_color(240, 238, 232)
        pdf.cell(90, 6, 'Property', border=1, fill=True)
        pdf.cell(90, 6, 'Property Manager(s)', border=1, fill=True, ln=True)

        pdf.set_font('Helvetica', '', 8)
        pdf.set_text_color(*body_gray)
        for pname in sorted(_pm_by_prop.keys()):
            emails = sorted(_pm_by_prop[pname])
            pdf.cell(90, 5, pname[:45], border='LR')
            pdf.cell(90, 5, ", ".join(emails)[:80], border='LR', ln=True)
        # Close table bottom
        pdf.cell(180, 0, '', border='T', ln=True)

    # ═══════════════════════════════════════════════════════════
    #  METHODOLOGY
    # ═══════════════════════════════════════════════════════════
    pdf.add_page()
    section_heading("Methodology", dark_blue)

    narrative(
        "Current Monthly Pet-Related Revenue: The total monthly revenue from the "
        "selected pet-related charge codes (e.g. pet rent, pet deposits) as reported "
        "in the property management system. This is the sum of active charges in the "
        "most recent month of data."
    )

    narrative(
        "Pet Revenue Change Since PetScreening: For each property with a known launch "
        "date, we compare the average monthly pet fee revenue in the post-launch period "
        "to the pre-launch baseline using a 6-and-6 methodology: up to 6 months of "
        "pre-launch data as the baseline, compared against completed post-launch months. "
        "The portfolio-level change is the sum of each comparable property's monthly lift."
    )

    narrative(
        "Cumulative Pet Revenue Impact: The total observed post-launch pet fee revenue "
        "minus the projected baseline (pre-launch average extended across the same number "
        "of months). This represents the total incremental revenue attributable to the "
        "PetScreening program across all comparable properties."
    )

    narrative(
        "Revenue Opportunity - Missing Monthly Pet Rent: PetScreening profiles with "
        "active household pets are matched against charge data. Tenants who have a "
        "compliant profile with at least one active household pet but no matching pet "
        "charge code are counted as 'missing.' The missing revenue estimate equals the "
        "count of missing tenants multiplied by the median pet rent charge at their "
        "respective property. Each property's actual average fee from paying tenants "
        "is used, not a flat portfolio-wide number."
    )

    narrative(
        "Property Scoping: Only properties that have at least one charge matching "
        "the selected pet-related charge codes are included in the missing rent "
        "analysis. Properties with PetScreening profiles but no pet charges in "
        "the data are excluded to avoid false positives."
    )

    # Return bytes
    buf = io.BytesIO()
    pdf.output(buf)
    return buf.getvalue()


def generate_exec_summary_html(
    label, rev_change_mo, rev_change_total, avg_adoption, adopt_type_label,
    total_projected, total_additional, n_proj_props,
    mr_total_profiles, mr_current_mo, comparable_count,
    current_monthly_rev, n_props_total, n_with_launch,
    quick_rows, pm_rows=None, email_subject="", email_body="",
    su_total_profiles=0, su_current_mo=0,
):
    """Generate a self-contained HTML executive summary for VP-level sharing.

    Includes KPIs, narrative, detailed metrics table, and an optional
    'Email All Property Managers' button (mailto link).
    """
    today_str = datetime.now().strftime("%B %d, %Y")
    _logo_white = _PS_LOGO_WHITE_URI
    _logo_dark = _PS_LOGO_DARK_URI

    sign = "+" if rev_change_mo >= 0 else ""
    color_rev = "#677848" if rev_change_mo >= 0 else "#CF5A3F"
    adopt_str = f"{avg_adoption:.1f}%" if avg_adoption is not None else "—"
    proj_str = f"${total_projected:,.0f}/mo" if total_projected > 0 else "—"
    addl_str = f"+${total_additional:,.0f}/mo" if total_additional > 0 else ""
    _combined_mr = mr_total_profiles + su_total_profiles
    _combined_mr_mo = mr_current_mo + su_current_mo
    mr_str = f"{_combined_mr:,}" if _combined_mr > 0 else "—"
    mr_rev_str = f"${_combined_mr_mo:,.0f}/mo" if _combined_mr_mo > 0 else ""
    _mr_detail = ""
    if mr_total_profiles > 0 and su_total_profiles > 0:
        _mr_detail = f" ({mr_total_profiles} confirmed + {su_total_profiles} suspected)"
    elif su_total_profiles > 0:
        _mr_detail = f" ({su_total_profiles} suspected)"

    # ── Quick stats table rows ─────────────────────────────────
    stats_html = ""
    for row in quick_rows:
        stats_html += f"""
        <tr>
            <td style="font-weight:500">{row['Metric']}</td>
            <td style="font-weight:700;color:#1F2257">{row['Value']}</td>
            <td style="color:#636569">{row.get('Period', '')}</td>
        </tr>"""

    # ── Story paragraphs ──────────────────────────────────────
    story_parts = []
    if comparable_count > 0:
        story_parts.append(
            f"Since launching PetScreening across <b>{comparable_count}</b> comparable properties, "
            f"pet fee revenue {'increased' if rev_change_mo >= 0 else 'decreased'} by "
            f"<b>{sign}${rev_change_mo:,.0f}/mo</b>."
        )
    if mr_total_profiles > 0 and mr_current_mo > 0:
        story_parts.append(
            f"<b>{mr_total_profiles}</b> tenants have completed their PetScreening screening but "
            f"aren't being charged pet rent — that's an estimated <b>${mr_current_mo:,.0f}/mo</b> "
            f"in uncollected revenue."
        )
    elif mr_total_profiles > 0:
        story_parts.append(
            f"<b>{mr_total_profiles}</b> tenants have completed their PetScreening screening but "
            f"aren't being charged pet rent."
        )
    if su_total_profiles > 0 and su_current_mo > 0:
        story_parts.append(
            f"Additionally, <b>{su_total_profiles}</b> tenants show signals of having undisclosed pets "
            f"(abandoned screening, unresolved requests) — an estimated <b>${su_current_mo:,.0f}/mo</b> "
            f"in potential additional revenue."
        )
    elif su_total_profiles > 0:
        story_parts.append(
            f"Additionally, <b>{su_total_profiles}</b> tenants show signals of having undisclosed pets."
        )
    if total_projected > 0 and avg_adoption is not None:
        story_parts.append(
            f"Currently collecting <b>${current_monthly_rev:,.0f}/mo</b> in pet fees. "
            f"At <b>100% {adopt_type_label.lower()} adoption</b> (currently {avg_adoption:.1f}%), "
            f"projected total pet fee revenue could reach <b>${total_projected:,.0f}/mo</b> — "
            f"an additional <b>${total_additional:,.0f}/mo</b> across <b>{n_proj_props}</b> properties with data."
        )
    story_html = "<br><br>".join(story_parts) if story_parts else f"Analyzing {n_props_total} properties for {label}."

    # ── Email section ──────────────────────────────────────────
    email_section_html = ""
    if pm_rows and len(pm_rows) > 0:
        unique_emails = sorted(set(
            r['PM_EMAIL'] for r in pm_rows
            if r.get('PM_EMAIL') and r['PM_EMAIL'].strip()
        ))
        n_pm_props = len(set(r.get('PROPERTY_ID') for r in pm_rows if r.get('PROPERTY_ID')))

        if unique_emails:
            bcc_str = ",".join(unique_emails)
            mailto_url = (
                f"mailto:?bcc={urllib.parse.quote(bcc_str)}"
                f"&subject={urllib.parse.quote(email_subject)}"
                f"&body={urllib.parse.quote(email_body)}"
            )

            # PM table
            pm_by_prop = defaultdict(set)
            for r in pm_rows:
                pname = r.get('PROPERTY_NAME', 'Unknown')
                email = r.get('PM_EMAIL', '')
                if email and email.strip():
                    short = pname.split(" - ", 1)[-1] if " - " in pname else pname
                    pm_by_prop[short].add(email)

            pm_rows_html = ""
            for pname in sorted(pm_by_prop.keys()):
                emails = sorted(pm_by_prop[pname])
                pm_rows_html += f"""
                <tr>
                    <td>{pname}</td>
                    <td>{", ".join(emails)}</td>
                    <td>{len(emails)}</td>
                </tr>"""

            email_section_html = f"""
            <div class="section" style="margin-top:36px">
                <h2>Recommended Next Step</h2>
                <p style="font-size:14px;margin-bottom:16px;color:var(--text-muted)">
                    Remind property managers that all new leases and renewals require a completed PetScreening profile.
                </p>
                <div style="text-align:center;margin:24px 0">
                    <a href="{mailto_url}" style="
                        display:inline-block;background:var(--pack-blue);color:#FFFFFF;
                        font-family:'Poppins',Arial,sans-serif;font-size:16px;font-weight:600;
                        padding:16px 36px;border-radius:10px;text-decoration:none;
                        letter-spacing:0.3px;box-shadow:0 2px 8px rgba(31,34,87,0.15)
                    ">Email All Property Managers ({len(unique_emails)})</a>
                    <p style="font-size:12px;color:var(--text-muted);margin:10px 0 0 0">
                        Opens your email client with all {len(unique_emails)} PMs in BCC across {n_pm_props} properties. You still have to click Send.
                    </p>
                </div>
                <details style="margin-top:20px;cursor:pointer">
                    <summary style="font-size:13px;font-weight:600;color:var(--text-heading);padding:8px 0">
                        View all {len(unique_emails)} property manager emails
                    </summary>
                    <table style="margin-top:12px;font-size:12px">
                        <thead>
                            <tr>
                                <th>Property</th>
                                <th>Property Managers</th>
                                <th>#</th>
                            </tr>
                        </thead>
                        <tbody>{pm_rows_html}</tbody>
                    </table>
                </details>
            </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PetScreening Impact Summary — {label}</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&family=Lora:wght@700&display=swap" rel="stylesheet">
<style>
  :root {{
    --pack-blue: #1F2257;
    --retriever-rust: #B17455;
    --tabby-yellow: #E2AB58;
    --sky-blue: #DAEBF5;
    --succulent-green: #8DAEA7;
    --catnip-green: #677848;
    --dog-bone-white: #F9F4E6;
    --whisker-beige: #D3CEBD;
    --smokey-gray: #636569;
    --great-dane-gray: #4F5155;
    --chew-toy-orange: #DD7B45;
    --fire-hydrant-red: #CF5A3F;
    --bg: #FAFAF8;
    --card-bg: #ffffff;
    --text: #4F5155;
    --text-heading: #1F2257;
    --text-muted: #636569;
    --border: #E8E6E0;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'Poppins', Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.7;
    -webkit-font-smoothing: antialiased;
    max-width: 900px;
    margin: 0 auto;
  }}
  .header {{
    background: linear-gradient(135deg, #1F2257 0%, #2a2d6e 100%);
    color: white;
    padding: 36px 48px 28px;
    border-radius: 0 0 16px 16px;
  }}
  .header-logo {{ height: 26px; opacity: 0.95; margin-bottom: 12px; }}
  .header h1 {{
    font-family: 'Lora', Georgia, serif;
    font-size: 26px;
    font-weight: 700;
    color: #E2AB58;
    letter-spacing: -0.5px;
    margin-bottom: 4px;
  }}
  .header .subtitle {{
    font-size: 14px;
    color: rgba(255,255,255,0.7);
  }}
  .container {{ padding: 32px 48px 48px; }}
  .section {{ margin-bottom: 32px; }}
  .section h2 {{
    font-family: 'Poppins', Arial, sans-serif;
    font-size: 16px;
    font-weight: 600;
    margin-bottom: 16px;
    color: var(--text-heading);
    border-bottom: 2px solid var(--retriever-rust);
    padding-bottom: 8px;
    display: inline-block;
  }}
  .kpi-grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    margin-bottom: 24px;
  }}
  .kpi-card {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 22px 20px;
    text-align: center;
    box-shadow: 0 1px 3px rgba(31,34,87,0.04);
  }}
  .kpi-label {{
    font-size: 11px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 6px;
    font-weight: 500;
  }}
  .kpi-value {{
    font-size: 28px;
    font-weight: 700;
    color: var(--text-heading);
    letter-spacing: -0.5px;
  }}
  .kpi-caption {{
    font-size: 11px;
    color: var(--text-muted);
    margin-top: 6px;
  }}
  .story {{
    background: var(--dog-bone-white);
    border-left: 4px solid var(--tabby-yellow);
    border-radius: 0 10px 10px 0;
    padding: 20px 24px;
    margin: 0 0 28px 0;
    font-size: 14px;
    line-height: 1.8;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    background: var(--card-bg);
    border-radius: 10px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(31,34,87,0.04);
    font-size: 13px;
  }}
  thead {{ background: var(--dog-bone-white); }}
  th {{
    padding: 10px 14px;
    text-align: left;
    font-weight: 600;
    color: var(--text-heading);
    border-bottom: 2px solid var(--border);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  td {{
    padding: 9px 14px;
    border-bottom: 1px solid var(--border);
    color: var(--text);
  }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #FAFAF5; }}
  details summary {{
    list-style: none;
  }}
  details summary::-webkit-details-marker {{
    display: none;
  }}
  details summary::before {{
    content: "▸ ";
    transition: transform 0.2s;
  }}
  details[open] summary::before {{
    content: "▾ ";
  }}
  .footer {{
    text-align: center;
    padding: 24px;
    font-size: 11px;
    color: var(--text-muted);
    border-top: 1px solid var(--border);
    margin-top: 48px;
  }}
  .footer-logo {{ height: 20px; margin-bottom: 8px; opacity: 0.7; }}
  /* ── Mobile / Phone ── */
  @media (max-width: 700px) {{
    .kpi-grid {{ grid-template-columns: repeat(2, 1fr) !important; }}
    .container {{ padding: 16px; }}
    .header {{ padding: 24px 16px; }}
    .header h1 {{ font-size: 20px; }}
    .kpi-value {{ font-size: 22px; }}
    .kpi-label {{ font-size: 10px; }}
    .story {{ padding: 16px; font-size: 13px; }}
    table {{ font-size: 11px; display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
    th, td {{ padding: 8px 10px; }}
  }}
  @media (max-width: 480px) {{
    .kpi-grid {{ grid-template-columns: 1fr !important; }}
    .header h1 {{ font-size: 18px; }}
    .kpi-value {{ font-size: 20px; }}
    .story {{ font-size: 12px; padding: 14px; }}
    table {{ font-size: 10px; }}
  }}
  @media print {{
    body {{ background: white; }}
    .header {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
    .kpi-card {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; break-inside: avoid; }}
  }}
</style>
</head>
<body>

<div class="header">
  <img src="{_logo_white}" alt="PetScreening" class="header-logo"
       onerror="this.style.display='none'">
  <h1>Impact Summary</h1>
  <div class="subtitle">{label} · {today_str}</div>
</div>

<div class="container">

  <!-- Value Created -->
  <h2 style="font-size:13px;font-weight:600;color:var(--catnip-green);text-transform:uppercase;letter-spacing:1px;margin-bottom:12px">Value Created</h2>
  <div class="kpi-grid" style="grid-template-columns:repeat(3,1fr)">
    <div class="kpi-card">
      <div class="kpi-label">Current Monthly Pet-Related Revenue</div>
      <div class="kpi-value">${current_monthly_rev:,.0f}<span style="font-size:16px">/mo</span></div>
      <div class="kpi-caption">Total pet fee revenue (latest month)</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Pet Revenue Change Since PS</div>
      <div class="kpi-value" style="color:{color_rev}">{sign}${rev_change_mo:,.0f}<span style="font-size:16px">/mo</span></div>
      <div class="kpi-caption">vs pre-launch baseline</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Projected at 100% Adoption</div>
      <div class="kpi-value" style="color:var(--retriever-rust)">{proj_str}</div>
      {f'<div class="kpi-caption">{addl_str} additional</div>' if addl_str else ''}
    </div>
  </div>

  <!-- Revenue Opportunity -->
  <h2 style="font-size:13px;font-weight:600;color:var(--chew-toy-orange);text-transform:uppercase;letter-spacing:1px;margin:8px 0 12px 0">Revenue Opportunity</h2>
  <div class="kpi-grid" style="grid-template-columns:repeat(3,1fr)">
    <div class="kpi-card">
      <div class="kpi-label">Not Paying Pet Rent</div>
      <div class="kpi-value" style="color:var(--chew-toy-orange)">{mr_str}</div>
      {f'<div class="kpi-caption">~{mr_rev_str} uncollected{_mr_detail}</div>' if mr_rev_str else f'<div class="kpi-caption">{_mr_detail}</div>' if _mr_detail else ''}
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Avg {adopt_type_label} Adoption</div>
      <div class="kpi-value">{adopt_str}</div>
      <div class="kpi-caption">Across {n_proj_props} properties with data</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Suspected Undisclosed</div>
      <div class="kpi-value" style="color:var(--chew-toy-orange)">{f'{su_total_profiles:,}' if su_total_profiles > 0 else 'n/a'}</div>
      {f'<div class="kpi-caption">~${su_current_mo:,.0f}/mo potential revenue</div>' if su_total_profiles > 0 and su_current_mo > 0 else ''}
    </div>
  </div>

  <!-- Story -->
  <div class="story">
    {story_html}
  </div>

  <!-- Detailed Metrics -->
  <div class="section">
    <details>
      <summary style="font-size:14px;font-weight:600;color:var(--text-heading);padding:8px 0;cursor:pointer">
        Detailed metrics breakdown
      </summary>
      <table style="margin-top:12px">
        <thead>
          <tr><th>Metric</th><th>Value</th><th>Period</th></tr>
        </thead>
        <tbody>{stats_html}</tbody>
      </table>
    </details>
  </div>

  {email_section_html}

  <div class="footer">
    <img src="{_logo_dark}" alt="PetScreening" class="footer-logo"
         onerror="this.style.display='none'"><br>
    Powered by PetScreening · Generated {today_str}
  </div>

</div>

</body>
</html>"""
    return html


def build_current_snapshot_chart(monthly_by_prop, months, title_prefix):
    """Horizontal bar chart of current month's revenue by property."""
    latest_month = months[-1]
    current = {}
    for prop, month_data in monthly_by_prop.items():
        val = month_data.get(latest_month, 0)
        if val > 0:
            short = prop.split(" - ", 1)[-1] if " - " in prop else prop
            current[short] = val

    sorted_items = sorted(current.items(), key=lambda x: x[1], reverse=True)
    if not sorted_items:
        return None

    props = [x[0] for x in sorted_items]
    vals = [x[1] for x in sorted_items]

    fig = go.Figure(go.Bar(
        x=vals,
        y=props,
        orientation='h',
        marker_color='#677848',
        text=[f"${v:,.0f}" for v in vals],
        textposition='outside',
        textfont=dict(size=12, color="#1F2257", family="Poppins, Arial, sans-serif"),
    ))
    # Dynamic left margin — enough for labels but not too wide for mobile
    max_label_len = max((len(p) for p in props), default=10)
    left_margin = min(200, max(80, max_label_len * 7))

    fig.update_layout(
        title=dict(
            text=f"{title_prefix}: Current Monthly Fee Revenue by Property ({latest_month.strftime('%b %Y')})",
            font=dict(size=14, color="#1F2257"),
        ),
        height=max(400, len(props) * 28),
        autosize=True,
        template="plotly_white",
        xaxis_title="Monthly Revenue ($)",
        yaxis=dict(
            autorange="reversed",
            tickfont=dict(size=12, color="#4F5155"),
        ),
        xaxis=dict(tickfont=dict(color="#4F5155")),
        margin=dict(l=left_margin),
        plot_bgcolor="white",
        paper_bgcolor="#FAFAF8",
        font=dict(family="Poppins, Arial, sans-serif", size=11, color="#4F5155"),
    )
    return fig


# ─── Missing Pet Rent Report ─────────────────────────────────────────
JUNK_EMAILS = (
    "'none@none.com','noemail@noemail.com','none@nowhere.com','none@gmail.com',"
    "'noemail@gmail.com','na@na.com','no@email.com','na@gmail.com',"
    "'noemail@greystar.com','no@no.com','n/a@gmail.com','none@aol.com',"
    "'none@embreydc.com','noemail@comcapp.com'"
)


def fetch_property_manager_emails(property_ids, ancestry_id=None, parent_company_name=None):
    """
    Fetch property manager emails from Snowflake.

    Uses parent_company_ancestry_id or parent_company_name on user_enriched
    (more reliable than pm.entity_id which may not match d_properties.property_id).
    Falls back to pm.entity_id IN (property_ids) if neither is available.

    Returns a list of dicts: [{PM_EMAIL, PROPERTY_ID, PROPERTY_NAME}]
    """
    if not property_ids and not ancestry_id and not parent_company_name:
        return []
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)

    # Resolve ancestry_id if not provided — much more reliable than pm.entity_id
    if not ancestry_id and not parent_company_name and property_ids:
        try:
            _cur = conn.cursor()
            _cur.execute(f"""
                SELECT DISTINCT parent_company_ancestry_id
                FROM PROD.common.d_properties
                WHERE property_id IN ({", ".join(str(int(pid)) for pid in property_ids)})
                  AND parent_company_ancestry_id IS NOT NULL
            """)
            _aids = [str(r[0]) for r in _cur.fetchall() if r[0]]
            _cur.close()
            if len(_aids) == 1:
                ancestry_id = _aids[0]
        except Exception:
            pass

    # Build the WHERE filter — prefer ancestry_id on user_enriched
    if ancestry_id:
        scope_filter = f"u.parent_company_ancestry_id = '{ancestry_id}'"
    elif parent_company_name:
        safe_name = parent_company_name.replace("'", "''")
        scope_filter = f"u.parent_company_name ILIKE '%{safe_name}%'"
    else:
        props_str = ", ".join(str(int(pid)) for pid in property_ids)
        scope_filter = f"pm.entity_id IN ({props_str})"

    sql = f"""
    SELECT DISTINCT
        CASE
            WHEN u.user_email = 'parkatvenetopm@stylresidential.com'
            THEN 'parkatvenetoteam@stylresidential.com'
            WHEN u.user_email = 'admin@endeavourhsv.com'
            THEN 'contracts@endeavourhsv.com'
            ELSE u.user_email
        END AS PM_EMAIL,
        pm.entity_id AS PROPERTY_ID,
        p.property_name AS PROPERTY_NAME
    FROM PROD.staging.stg_petscreening__property_manager_permissions_only pm
    LEFT JOIN PROD.petscreening.petscreening__user_enriched u
        ON u.user_id = pm.user_id
    LEFT JOIN PROD.common.d_properties p
        ON pm.entity_id = p.property_id
    WHERE u.user_status = 'active'
      AND pm.name IN (
          'manager_admin_permission',
          'manager_view_hipaa_permission',
          'manager_reviewer_permission',
          'manager_view_only_permission'
      )
      AND u.compliance_status != 'n/a|manual|individual_level'
      AND u.user_role = 'property_manager'
      AND {scope_filter}
      AND u.user_email IS NOT NULL
      AND TRIM(u.user_email) <> ''
    ORDER BY PROPERTY_NAME, PM_EMAIL
    """
    try:
        cur.execute(sql)
        rows = cur.fetchall()
        cur.close()
        return rows
    except Exception as e:
        cur.close()
        st.error(f"Error fetching PM emails: {e}")
        return []


# ─── Robust tenant matching helpers ──────────────────────────────────
def _build_paying_sets(charges_df, selected_codes):
    """Build normalized paying-tenant sets for robust matching.

    Unit/lease-level expansion: if *any* tenant on a unit has a matching pet
    charge, **all** tenants on that unit are considered paying.  This handles
    shared leases where the charge is tied to one customer but covers the
    whole unit (especially important for Entrata, also relevant for Yardi).

    Returns
    -------
    paying_tc_set : set of (property_id_str, tenant_code_upper_stripped)
        Primary match by tenant code (case-insensitive, trimmed).
    paying_email_set : set of (property_id_str, email_lower_stripped)
        Fallback match by email when tenant code doesn't align
        (e.g. lease renewal changed the code).
    """
    pet_rows = charges_df[charges_df['charge_code'].isin(selected_codes)]

    # Primary: (property_id, tenant_code) — uppercased + stripped
    tc_pairs = pet_rows[['property_id', 'tenant_code']].drop_duplicates()
    paying_tc_set = set(
        zip(
            tc_pairs['property_id'].astype(str).str.strip(),
            tc_pairs['tenant_code'].astype(str).str.strip().str.upper(),
        )
    )

    # Fallback: (property_id, email) — lowercased + stripped
    paying_email_set = set()
    if 'email' in pet_rows.columns:
        email_rows = pet_rows[['property_id', 'email']].drop_duplicates()
        email_rows = email_rows[
            email_rows['email'].astype(str).str.strip().ne('')
            & email_rows['email'].astype(str).str.strip().str.lower().ne('nan')
            & email_rows['email'].astype(str).str.strip().str.lower().ne('none')
        ]
        if not email_rows.empty:
            paying_email_set = set(
                zip(
                    email_rows['property_id'].astype(str).str.strip(),
                    email_rows['email'].astype(str).str.strip().str.lower(),
                )
            )

    # ── Unit-level expansion ───────────────────────────────────────────
    # If ANY tenant on a (property_id, unit_code) has a pet charge, ALL
    # tenants on that same unit are paying.  This prevents roommates from
    # being flagged as "missing pet rent" when the lease already has it.
    if 'unit_code' in charges_df.columns and 'property_id' in charges_df.columns:
        # Identify units that have at least one paying tenant
        pet_unit_keys = set(
            zip(
                pet_rows['property_id'].astype(str).str.strip(),
                pet_rows['unit_code'].astype(str).str.strip().str.upper(),
            )
        )
        # Discard empty unit codes
        pet_unit_keys = {(p, u) for p, u in pet_unit_keys if u and u not in ('', 'NAN', 'NONE')}

        if pet_unit_keys:
            # Find all tenants on those units (from the full charges_df)
            _all = charges_df.copy()
            _all['_pid'] = _all['property_id'].astype(str).str.strip()
            _all['_uc'] = _all['unit_code'].astype(str).str.strip().str.upper()
            _all['_unit_key'] = list(zip(_all['_pid'], _all['_uc']))
            unit_matches = _all[_all['_unit_key'].isin(pet_unit_keys)]

            # Expand paying sets with all tenants on those units
            _tc_extra = unit_matches[['_pid', 'tenant_code']].drop_duplicates()
            paying_tc_set |= set(
                zip(
                    _tc_extra['_pid'],
                    _tc_extra['tenant_code'].astype(str).str.strip().str.upper(),
                )
            )
            if 'email' in unit_matches.columns:
                _em_extra = unit_matches[['_pid', 'email']].drop_duplicates()
                _em_extra = _em_extra[
                    _em_extra['email'].astype(str).str.strip().ne('')
                    & _em_extra['email'].astype(str).str.strip().str.lower().ne('nan')
                    & _em_extra['email'].astype(str).str.strip().str.lower().ne('none')
                ]
                if not _em_extra.empty:
                    paying_email_set |= set(
                        zip(
                            _em_extra['_pid'],
                            _em_extra['email'].astype(str).str.strip().str.lower(),
                        )
                    )

    # ── Lease-level expansion (Entrata) ──────────────────────────────
    # If ANY customer on the same lease_id has a pet charge, ALL customers
    # on that lease are paying.  This handles co-tenants whose charge is
    # recorded under one customer_id but covers the whole lease.
    if 'lease_id' in charges_df.columns:
        pet_lease_keys = set(
            zip(
                pet_rows['property_id'].astype(str).str.strip(),
                pet_rows['lease_id'].astype(str).str.strip(),
            )
        )
        pet_lease_keys = {(p, lid) for p, lid in pet_lease_keys if lid and lid not in ('', 'nan', 'None')}

        if pet_lease_keys:
            _all_l = charges_df.copy()
            _all_l['_pid'] = _all_l['property_id'].astype(str).str.strip()
            _all_l['_lid'] = _all_l['lease_id'].astype(str).str.strip()
            _all_l['_lease_key'] = list(zip(_all_l['_pid'], _all_l['_lid']))
            lease_matches = _all_l[_all_l['_lease_key'].isin(pet_lease_keys)]

            _tc_extra2 = lease_matches[['_pid', 'tenant_code']].drop_duplicates()
            paying_tc_set |= set(
                zip(
                    _tc_extra2['_pid'],
                    _tc_extra2['tenant_code'].astype(str).str.strip().str.upper(),
                )
            )
            if 'email' in lease_matches.columns:
                _em_extra2 = lease_matches[['_pid', 'email']].drop_duplicates()
                _em_extra2 = _em_extra2[
                    _em_extra2['email'].astype(str).str.strip().ne('')
                    & _em_extra2['email'].astype(str).str.strip().str.lower().ne('nan')
                    & _em_extra2['email'].astype(str).str.strip().str.lower().ne('none')
                ]
                if not _em_extra2.empty:
                    paying_email_set |= set(
                        zip(
                            _em_extra2['_pid'],
                            _em_extra2['email'].astype(str).str.strip().str.lower(),
                        )
                    )

    return paying_tc_set, paying_email_set


def _is_paying(row, paying_tc_set, paying_email_set):
    """Check if a profile row matches a paying tenant.

    Matches on (property_id, tenant_code) first (case-insensitive),
    then falls back to (property_id, user_email) if available.
    """
    pid = str(row.get('PROPERTY_ID', '')).strip()
    tc = str(row.get('TENANT_CODE', '')).strip().upper()
    if (pid, tc) in paying_tc_set:
        return 1
    # Email fallback
    email = str(row.get('USER_EMAIL', '')).strip().lower()
    if email and email not in ('', 'nan', 'none') and (pid, email) in paying_email_set:
        return 1
    return 0


def _apply_paying_flag(profiles_df, paying_tc_set, paying_email_set):
    """Apply `_is_paying` per row, then propagate across all rows for the
    same user at the same property.

    A user may have multiple f_leases rows — some with a matching
    tenant_code and some with NULL / mismatched codes.  If *any* lease
    row for the same (PROPERTY_ID, USER_EMAIL) pair is paying, we mark
    *all* of that user's rows as paying so they are correctly excluded
    from the "missing pet rent" list.

    Returns the DataFrame with a `pet_rent_paid` column (1 = paying).
    """
    profiles_df = profiles_df.copy()
    profiles_df['pet_rent_paid'] = profiles_df.apply(
        lambda r: _is_paying(r, paying_tc_set, paying_email_set),
        axis=1,
    )

    # Propagate: if ANY row for (property_id, email) is paying → all are
    if 'USER_EMAIL' in profiles_df.columns:
        user_paying = (
            profiles_df
            .groupby(['PROPERTY_ID', 'USER_EMAIL'])['pet_rent_paid']
            .transform('max')
        )
        profiles_df['pet_rent_paid'] = user_paying

    return profiles_df


def generate_missing_pet_rent_report(all_charges_df, selected_codes, property_ids):
    """
    Generate the 'Missing Pet Rent' report using LIVE API rent roll data.

    Uses the **same** profile identification as fetch_missing_pet_rent_by_property
    so that counts match across all tabs (Charts, Summary, Report).

    Flow:
      1. Live API data → identify tenants paying selected charge codes
      2. Snowflake → PetScreening profiles with household pets (+ tenant_code)
         ** Same filters as Charts tab: compliant, household, active **
      3. Python join → profile with pet + no selected charge = Profile_No_Rent
      4. Snowflake → R_MONTHLY_EXECUTIVE_SUMMARY for detailed pet/profile columns
      5. Python join → final report (per-pet granularity for download)
    """
    pmc_system = st.session_state.get("pmc_system", "yardi")
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)

    # ── Scope to properties that actually have PET-RELATED charge data ──
    # Only include properties with at least 1 charge matching selected codes.
    # Without this, properties with other charges but no pet charges would
    # have ALL their PetScreening profiles flagged as "missing," inflating
    # the count with unknowns.
    _pet_charges = all_charges_df[all_charges_df['charge_code'].isin(selected_codes)]
    _charge_pids = set(_pet_charges['property_id'].astype(str).str.strip().unique())
    property_ids = [pid for pid in property_ids if str(pid).strip() in _charge_pids]
    if not property_ids:
        return pd.DataFrame()

    props_str = ", ".join(str(int(pid)) for pid in property_ids)

    # ── Step 1: From LIVE API data, build set of tenants paying selected charges ──
    paying_tc_set, paying_email_set = _build_paying_sets(all_charges_df, selected_codes)

    # ── Step 2: Query Snowflake for PetScreening household profiles ──
    # ** MUST match fetch_missing_pet_rent_by_property exactly: compliant + household + active **
    sql_profiles = f"""
    SELECT DISTINCT
        du.property_id,
        COALESCE(
            l.lease_source_external_id:tenant_code::STRING,
            l.lease_source_external_id:"customerId"::STRING,
            l.lease_source_external_id:"customer_id"::STRING
        ) AS tenant_code,
        ue.user_email,
        ue.user_first_name,
        ue.user_last_name,
        ue.compliance_status,
        ue.user_pet_type,
        ue.user_pet_status,
        ue.user_profile_url,
        p.property_name
    FROM PROD.common.d_units du
    JOIN PROD.common.d_properties p
        ON du.property_id = p.property_id
    JOIN PROD.petscreening.petscreening__user_enriched ue
        ON ue.unit_id = du.unit_id
    JOIN PROD.common.f_leases l
        ON du.unit_key = l.unit_key AND l.user_key = ue.user_key
    WHERE du.unit_source = '{pmc_system}'
      AND du.property_id IN ({props_str})
      AND ue.compliance_status = 'compliant'
      AND ue.user_pet_type = 'household'
      AND ue.user_pet_status = 'active'
      AND ue.user_email IS NOT NULL
      AND TRIM(ue.user_email) <> ''
      AND LOWER(TRIM(ue.user_email)) NOT IN ({JUNK_EMAILS})
    """
    cur.execute(sql_profiles)
    profiles_df = pd.DataFrame(cur.fetchall())

    if profiles_df.empty:
        return pd.DataFrame()

    # ── Step 3: Match live charges to profiles ──
    profiles_df = _apply_paying_flag(profiles_df, paying_tc_set, paying_email_set)

    # ── Step 3b: Entrata freshness filter — exclude profiles not in API data ──
    if pmc_system == 'entrata' and 'email' in all_charges_df.columns:
        api_emails_by_prop = (
            all_charges_df
            .assign(
                _pid=lambda d: d['property_id'].astype(str).str.strip(),
                _em=lambda d: d['email'].astype(str).str.strip().str.lower(),
            )
            .loc[lambda d: d['_em'].ne('') & d['_em'].ne('nan') & d['_em'].ne('none')]
            .groupby('_pid')['_em']
            .apply(set)
            .to_dict()
        )
        def _in_api(row):
            pid = str(row.get('PROPERTY_ID', '')).strip()
            em = str(row.get('USER_EMAIL', '')).strip().lower()
            return em in api_emails_by_prop.get(pid, set())
        profiles_df['_in_api'] = profiles_df.apply(_in_api, axis=1)
        _n_before = len(profiles_df)
        profiles_df = profiles_df[profiles_df['_in_api'] | (profiles_df['pet_rent_paid'] == 1)]
        _n_excluded = _n_before - len(profiles_df)
        if _n_excluded > 0:
            st.info(f"Entrata freshness filter: excluded {_n_excluded:,} profiles not found in current API data")
        profiles_df = profiles_df.drop(columns=['_in_api'], errors='ignore')

    # Tenants with household pets who are NOT paying any selected charge
    missing_profiles = profiles_df[profiles_df['pet_rent_paid'] == 0].copy()

    if missing_profiles.empty:
        return pd.DataFrame()

    # Build lookup set: (email_lower, property_id_str) for these missing tenants
    missing_lookup = set(
        zip(
            missing_profiles['USER_EMAIL'].str.lower().str.strip(),
            missing_profiles['PROPERTY_ID'].astype(str),
        )
    )

    # ── Step 4: Query R_MONTHLY_EXECUTIVE_SUMMARY for detailed pet-level output ──
    # No extra restrictive filters — the profile identification in Step 2–3 is
    # already canonical.  We only need pet_profile_type/status to get the right
    # pet records from the exec summary.
    sql_exec = f"""
    SELECT DISTINCT
        m.pet_profile_url,
        m.pet_id,
        m.pet_name,
        m.breed,
        m.species,
        m.user_first_name,
        m.user_last_name,
        m.user_email,
        m.unit_id,
        m.lease_start_date,
        m.lease_end_date,
        m.resident_type,
        m.unit_address_1,
        m.unit_address_2,
        CONCAT_WS(' ', m.unit_address_1, m.unit_address_2) AS full_unit_address,
        m.property_name,
        m.property_id,
        m.pet_level_compliance_status,
        m.user_level_compliance_status,
        m.pet_profile_type,
        m.pet_profile_status,
        m.property_source_name
    FROM PROD.REPORTING.R_MONTHLY_EXECUTIVE_SUMMARY AS m
    WHERE m.property_id IN ({props_str})
      AND m.pet_profile_type = 'household'
      AND m.pet_profile_status = 'active'
      AND m.property_source_name = '{pmc_system}'
    """
    cur.execute(sql_exec)
    exec_df = pd.DataFrame(cur.fetchall())

    if exec_df.empty:
        return pd.DataFrame()

    # Deduplicate: a tenant can have multiple exec summary rows (current + past lease).
    # Keep 'current' rows preferentially; if a tenant only has non-current rows, keep them
    # (profile matching in Step 2-3 already validated they're active).
    if 'RESIDENT_TYPE' in exec_df.columns:
        _rt_lower = exec_df['RESIDENT_TYPE'].astype(str).str.strip().str.lower()
        exec_df['_is_current'] = _rt_lower.str.contains('current', na=False)
        # Sort so current rows come first, then drop duplicates per tenant+property+pet
        exec_df = exec_df.sort_values('_is_current', ascending=False)
        _dedup_cols = ['USER_EMAIL', 'PROPERTY_ID', 'PET_ID']
        _dedup_cols = [c for c in _dedup_cols if c in exec_df.columns]
        if _dedup_cols:
            exec_df = exec_df.drop_duplicates(subset=_dedup_cols, keep='first')
        exec_df = exec_df.drop(columns=['_is_current'], errors='ignore')

    # ── Step 5: Keep only exec summary records whose (email, property) is missing ──
    report_df = exec_df[
        exec_df.apply(
            lambda r: (
                str(r['USER_EMAIL']).lower().strip(),
                str(r['PROPERTY_ID']),
            ) in missing_lookup,
            axis=1,
        )
    ].copy()

    # Add the status columns to match the original query output
    report_df['has_petscreening_pets'] = 1
    report_df['pet_rent_paid'] = 0
    report_df['overall_status'] = 'Profile_No_Rent'

    return report_df


def fetch_missing_pet_rent_by_property(
    all_charges_df, selected_codes, property_ids, launch_dates, months,
):
    """
    Per-property, per-month estimated uncollected pet rent, accounting for:
      • Each missing tenant's actual lease dates (move_in/lease_from → move_out/lease_to)
      • Property launch date (only count months on or after launch)
      • Recurring rent vs one-time deposit classification

    Returns
    -------
    dict  – {property_name: {
        "missing_count": int,               # total missing tenants
        "monthly_missing": {month: float},   # estimated missing $ per month
        "avg_recurring": float,              # avg recurring pet fee/mo
        "avg_onetime": float,                # avg one-time deposit
        "charge_type_label": str,            # "rent", "deposit", or "rent + deposit"
        "missing_tenants": [list of dicts],
    }}
    """
    pmc_system = st.session_state.get("pmc_system", "yardi")
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)

    # ── Scope to properties that actually have PET-RELATED charge data ──
    # Only include properties with at least 1 charge matching selected codes.
    # Without this, properties with other charges but no pet charges would
    # have ALL their PetScreening profiles flagged as "missing," inflating
    # the count with unknowns.
    pet_charges = all_charges_df[all_charges_df['charge_code'].isin(selected_codes)].copy()
    _charge_pids = set(pet_charges['property_id'].astype(str).str.strip().unique())
    property_ids = [pid for pid in property_ids if str(pid).strip() in _charge_pids]
    if not property_ids:
        return {}

    props_str = ", ".join(str(int(pid)) for pid in property_ids)

    # ── Step 1: From LIVE API data, classify charge codes and compute avg fees ──
    pet_charges['_amt'] = pd.to_numeric(pet_charges['charge_amount'], errors='coerce').fillna(0)
    pet_charges['_from'] = pet_charges['charge_from_date'].apply(parse_date)
    pet_charges['_to'] = pet_charges['charge_to_date'].apply(parse_date)

    paying_tc_set, paying_email_set = _build_paying_sets(all_charges_df, selected_codes)
    properties_with_payers = set(pet_charges['property_id'].astype(str).unique())

    # Classify each charge code at each property as recurring or one-time.
    # Entrata: use the explicit `frequency` field when available.
    # Yardi: infer from median date span (> 60 days → recurring).
    _has_frequency = 'frequency' in pet_charges.columns
    code_class = {}   # {(property_name, charge_code): "recurring" | "onetime"}
    for (pname, code), grp in pet_charges.groupby(['property_name', 'charge_code']):
        if _has_frequency:
            # Entrata: use the frequency field directly
            freqs = grp['frequency'].dropna().str.strip().str.lower()
            onetime_count = (freqs == 'one-time').sum()
            monthly_count = (freqs.isin(['monthly', 'recurring'])).sum()
            if onetime_count > monthly_count:
                code_class[(pname, code)] = "onetime"
            else:
                code_class[(pname, code)] = "recurring"
        else:
            # Yardi: infer from median date span
            spans = []
            for _, row in grp.iterrows():
                f, t = row['_from'], row['_to']
                if f and t and not pd.isna(f) and not pd.isna(t):
                    spans.append((t - f).days)
            # No valid date spans → assume recurring (missing dates ≠ one-time)
            median_span = float(np.median(spans)) if spans else None
            if median_span is None:
                code_class[(pname, code)] = "recurring"
            else:
                code_class[(pname, code)] = "recurring" if median_span > 60 else "onetime"

    # Apply user overrides (from the charge type classification UI)
    try:
        _user_overrides = st.session_state.get("charge_type_overrides", {})
        if _user_overrides:
            for (pname, code) in list(code_class.keys()):
                if code in _user_overrides:
                    code_class[(pname, code)] = _user_overrides[code]
    except Exception:
        pass

    # Avg fee per property, split by recurring vs one-time
    avg_recurring_by_prop = {}   # avg monthly recurring fee per tenant
    avg_onetime_by_prop = {}     # avg one-time deposit per tenant
    for pname, pgrp in pet_charges.groupby('property_name'):
        rec_amts, ot_amts = [], []
        for tc, tgrp in pgrp.groupby('tenant_code'):
            for _, row in tgrp.iterrows():
                cls = code_class.get((pname, row['charge_code']), 'recurring')
                if cls == 'recurring':
                    rec_amts.append(row['_amt'])
                else:
                    ot_amts.append(row['_amt'])
        # De-dup per tenant (avg per tenant, then avg across tenants)
        if rec_amts:
            avg_recurring_by_prop[pname] = float(np.mean(rec_amts))
        if ot_amts:
            avg_onetime_by_prop[pname] = float(np.mean(ot_amts))

    # Build tenant_info lookup from ALL API data (every tenant, all charge codes)
    # so we can get lease dates for any tenant, even those without pet charges.
    tenant_info = {}  # {(property_name, tenant_code): {move_in, move_out, lease_from, lease_to, status}}
    for _, row in all_charges_df.iterrows():
        key = (str(row['property_name']), str(row['tenant_code']))
        if key in tenant_info:
            continue
        tenant_info[key] = {
            'move_in': parse_date(row.get('move_in', '')),
            'move_out': parse_date(row.get('move_out', '')),
            'lease_from': parse_date(row.get('lease_from', '')),
            'lease_to': parse_date(row.get('lease_to', '')),
            'status': str(row.get('tenant_status', '')).strip().lower(),
        }

    # ── Step 2: Snowflake → PetScreening household profiles with tenant_code ──
    sql_profiles = f"""
    SELECT DISTINCT
        du.property_id,
        p.property_name,
        COALESCE(
            l.lease_source_external_id:tenant_code::STRING,
            l.lease_source_external_id:"customerId"::STRING,
            l.lease_source_external_id:"customer_id"::STRING
        ) AS tenant_code,
        ue.user_first_name,
        ue.user_last_name,
        ue.user_email,
        ue.user_profile_url
    FROM PROD.common.d_units du
    JOIN PROD.common.d_properties p
        ON du.property_id = p.property_id
    JOIN PROD.petscreening.petscreening__user_enriched ue
        ON ue.unit_id = du.unit_id
    JOIN PROD.common.f_leases l
        ON du.unit_key = l.unit_key AND l.user_key = ue.user_key
    WHERE du.unit_source = '{pmc_system}'
      AND du.property_id IN ({props_str})
      AND ue.compliance_status = 'compliant'
      AND ue.user_pet_type = 'household'
      AND ue.user_pet_status = 'active'
      AND ue.user_email IS NOT NULL
      AND TRIM(ue.user_email) <> ''
      AND LOWER(TRIM(ue.user_email)) NOT IN ({JUNK_EMAILS})
    """
    cur.execute(sql_profiles)
    profiles_df = pd.DataFrame(cur.fetchall())
    cur.close()

    if profiles_df.empty:
        return {}

    # ── Step 3: Match to paying set (case-insensitive tc + email fallback) ──
    # If a user has multiple leases, paying on ANY lease counts for all rows.
    profiles_df = _apply_paying_flag(profiles_df, paying_tc_set, paying_email_set)

    # ── Step 3b: Entrata freshness filter ──
    if pmc_system == 'entrata' and 'email' in all_charges_df.columns:
        api_emails_by_prop = (
            all_charges_df
            .assign(
                _pid=lambda d: d['property_id'].astype(str).str.strip(),
                _em=lambda d: d['email'].astype(str).str.strip().str.lower(),
            )
            .loc[lambda d: d['_em'].ne('') & d['_em'].ne('nan') & d['_em'].ne('none')]
            .groupby('_pid')['_em']
            .apply(set)
            .to_dict()
        )
        def _in_api_check(row):
            pid = str(row.get('PROPERTY_ID', '')).strip()
            em = str(row.get('USER_EMAIL', '')).strip().lower()
            return em in api_emails_by_prop.get(pid, set())
        profiles_df['_in_api'] = profiles_df.apply(_in_api_check, axis=1)
        profiles_df = profiles_df[profiles_df['_in_api'] | (profiles_df['pet_rent_paid'] == 1)]
        profiles_df = profiles_df.drop(columns=['_in_api'], errors='ignore')

    # Compute a portfolio-wide average fee as fallback for properties with no payers
    _all_recurring = [v for v in avg_recurring_by_prop.values() if v > 0]
    _all_onetime = [v for v in avg_onetime_by_prop.values() if v > 0]
    portfolio_avg_rec = float(np.mean(_all_recurring)) if _all_recurring else 0
    portfolio_avg_ot = float(np.mean(_all_onetime)) if _all_onetime else 0

    # ── Step 4: Per-property, per-month missing revenue ──
    m0, mN = months[0], months[-1]
    result = {}
    for pname, grp in profiles_df.groupby('PROPERTY_NAME'):
        missing = grp[grp['pet_rent_paid'] == 0]
        if missing.empty:
            continue

        # Property launch date — only count missing rent on or after launch
        launch_dt = _resolve_launch_dt(launch_dates.get(pname))
        launch_month = datetime(launch_dt.year, launch_dt.month, 1) if launch_dt else None

        # Use property-specific avg if available, otherwise fall back to portfolio avg
        avg_rec = avg_recurring_by_prop.get(pname, portfolio_avg_rec)
        avg_ot = avg_onetime_by_prop.get(pname, portfolio_avg_ot)

        # Determine what charge types are active at this property
        has_recurring = avg_rec > 0
        has_onetime = avg_ot > 0
        _is_portfolio_avg = (pname not in avg_recurring_by_prop and pname not in avg_onetime_by_prop)
        _suffix = " (portfolio avg)" if _is_portfolio_avg else ""
        if has_recurring and has_onetime:
            charge_label = f"rent (${avg_rec:,.0f}/mo) + deposit (${avg_ot:,.0f}){_suffix}"
        elif has_recurring:
            charge_label = f"rent (${avg_rec:,.0f}/mo){_suffix}"
        elif has_onetime:
            charge_label = f"deposit (${avg_ot:,.0f} one-time){_suffix}"
        else:
            charge_label = "no fee data available"

        monthly_missing = {m: 0.0 for m in months}
        missing_tenants = []

        for _, row in missing.iterrows():
            tc = str(row['TENANT_CODE'])
            tinfo = tenant_info.get((pname, tc))

            # Determine tenant's active period
            if tinfo:
                t_start = tinfo['move_in'] or tinfo['lease_from']
                t_end = tinfo['move_out'] or tinfo['lease_to']
                t_status = tinfo['status']
            else:
                t_start = None
                t_end = None
                t_status = 'current'

            # Default start: if we have no date, assume they were active at launch
            if t_start is None:
                t_start = launch_dt if launch_dt else m0
            active_from = datetime(t_start.year, t_start.month, 1)

            # Default end: if current tenant or no end date, they're still active
            if t_end is None or t_status == 'current':
                active_to = mN
            else:
                active_to = datetime(t_end.year, t_end.month, 1)

            # Only count months on or after property launch
            if launch_month:
                active_from = max(active_from, launch_month)

            # Clamp to display window
            active_from = max(active_from, m0)
            active_to = min(active_to, mN)

            first_month_done = False
            for m in months:
                if active_from <= m <= active_to:
                    # Recurring rent: add every active month
                    if has_recurring:
                        monthly_missing[m] += avg_rec
                    # One-time deposit: add only in tenant's first active month
                    if has_onetime and not first_month_done:
                        monthly_missing[m] += avg_ot
                        first_month_done = True

            missing_tenants.append({
                "name": f"{row.get('USER_FIRST_NAME', '')} {row.get('USER_LAST_NAME', '')}".strip(),
                "email": row.get('USER_EMAIL', ''),
                "profile_url": row.get('USER_PROFILE_URL', ''),
                "active_from": active_from.strftime('%b %Y'),
                "active_to": active_to.strftime('%b %Y') if active_to < mN else "Current",
            })

        # Compute summary stats
        total_missing_in_window = sum(monthly_missing.values())
        months_with_missing = sum(1 for v in monthly_missing.values() if v > 0)
        est_per_month = total_missing_in_window / months_with_missing if months_with_missing > 0 else 0

        # Count unique tenants by email (not raw rows which may have duplicates
        # from multiple lease rows for the same person)
        _unique_missing = missing['USER_EMAIL'].nunique() if 'USER_EMAIL' in missing.columns else len(missing)

        result[pname] = {
            "missing_count": _unique_missing,
            "monthly_missing": monthly_missing,
            "avg_recurring": avg_rec,
            "avg_onetime": avg_ot,
            "charge_type_label": charge_label,
            "estimated_missing_per_month": est_per_month,
            "total_missing_in_window": total_missing_in_window,
            "missing_tenants": missing_tenants,
        }
    return result


# ─── Suspected Undisclosed Pets ──────────────────────────────────────

def fetch_suspected_undisclosed_by_property(
    all_charges_df, selected_codes, property_ids, launch_dates, months,
):
    """
    Per-property, per-month estimated uncollected pet rent from *suspected*
    undisclosed pets — residents whose PetScreening profile signals a likely
    pet they haven't disclosed or completed screening for.

    Uses the same revenue estimation methodology as confirmed missing rent:
    property-specific average fee from paying tenants, with portfolio-wide
    fallback.

    Returns
    -------
    dict  – same shape as fetch_missing_pet_rent_by_property:
    {property_name: {
        "missing_count": int,
        "monthly_missing": {month: float},
        "avg_recurring": float,
        "avg_onetime": float,
        "charge_type_label": str,
        "estimated_missing_per_month": float,
        "total_missing_in_window": float,
        "missing_tenants": [list of dicts],  # includes suspected_reason
    }}
    """
    pmc_system = st.session_state.get("pmc_system", "yardi")
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)

    # ── Scope to properties that actually have PET-RELATED charge data ──
    _pet_charges_filter = all_charges_df[all_charges_df['charge_code'].isin(selected_codes)]
    _charge_pids = set(_pet_charges_filter['property_id'].astype(str).str.strip().unique())
    property_ids = [pid for pid in property_ids if str(pid).strip() in _charge_pids]
    if not property_ids:
        return {}

    props_str = ", ".join(str(int(pid)) for pid in property_ids)

    # ── Step 1: Reuse the same charge classification & avg fee logic ──
    pet_charges = all_charges_df[all_charges_df['charge_code'].isin(selected_codes)].copy()
    pet_charges['_amt'] = pd.to_numeric(pet_charges['charge_amount'], errors='coerce').fillna(0)
    pet_charges['_from'] = pet_charges['charge_from_date'].apply(parse_date)
    pet_charges['_to'] = pet_charges['charge_to_date'].apply(parse_date)

    paying_tc_set, paying_email_set = _build_paying_sets(all_charges_df, selected_codes)

    _has_frequency = 'frequency' in pet_charges.columns
    code_class = {}
    for (pname, code), grp in pet_charges.groupby(['property_name', 'charge_code']):
        if _has_frequency:
            freqs = grp['frequency'].dropna().str.strip().str.lower()
            onetime_count = (freqs == 'one-time').sum()
            monthly_count = (freqs.isin(['monthly', 'recurring'])).sum()
            code_class[(pname, code)] = "onetime" if onetime_count > monthly_count else "recurring"
        else:
            spans = []
            for _, row in grp.iterrows():
                f, t = row['_from'], row['_to']
                if f and t and not pd.isna(f) and not pd.isna(t):
                    spans.append((t - f).days)
            # No valid date spans → assume recurring (missing dates ≠ one-time)
            median_span = float(np.median(spans)) if spans else None
            if median_span is None:
                code_class[(pname, code)] = "recurring"
            else:
                code_class[(pname, code)] = "recurring" if median_span > 60 else "onetime"

    # Apply user overrides (from the charge type classification UI)
    try:
        _user_overrides = st.session_state.get("charge_type_overrides", {})
        if _user_overrides:
            for (pname, code) in list(code_class.keys()):
                if code in _user_overrides:
                    code_class[(pname, code)] = _user_overrides[code]
    except Exception:
        pass

    avg_recurring_by_prop = {}
    avg_onetime_by_prop = {}
    for pname, pgrp in pet_charges.groupby('property_name'):
        rec_amts, ot_amts = [], []
        for tc, tgrp in pgrp.groupby('tenant_code'):
            for _, row in tgrp.iterrows():
                cls = code_class.get((pname, row['charge_code']), 'recurring')
                if cls == 'recurring':
                    rec_amts.append(row['_amt'])
                else:
                    ot_amts.append(row['_amt'])
        if rec_amts:
            avg_recurring_by_prop[pname] = float(np.mean(rec_amts))
        if ot_amts:
            avg_onetime_by_prop[pname] = float(np.mean(ot_amts))

    # Tenant info for lease dates
    tenant_info = {}
    for _, row in all_charges_df.iterrows():
        key = (str(row['property_name']), str(row['tenant_code']))
        if key in tenant_info:
            continue
        tenant_info[key] = {
            'move_in': parse_date(row.get('move_in', '')),
            'move_out': parse_date(row.get('move_out', '')),
            'lease_from': parse_date(row.get('lease_from', '')),
            'lease_to': parse_date(row.get('lease_to', '')),
            'status': str(row.get('tenant_status', '')).strip().lower(),
        }

    # Portfolio-wide fallback averages
    _all_recurring = [v for v in avg_recurring_by_prop.values() if v > 0]
    _all_onetime = [v for v in avg_onetime_by_prop.values() if v > 0]
    portfolio_avg_rec = float(np.mean(_all_recurring)) if _all_recurring else 0
    portfolio_avg_ot = float(np.mean(_all_onetime)) if _all_onetime else 0

    # ── Step 2: Query Snowflake for suspected undisclosed pets ──
    sql_suspected = f"""
    SELECT DISTINCT
        du.property_id,
        p.property_name,
        COALESCE(
            l.lease_source_external_id:tenant_code::STRING,
            l.lease_source_external_id:"customerId"::STRING,
            l.lease_source_external_id:"customer_id"::STRING
        ) AS tenant_code,
        ue.user_first_name,
        ue.user_last_name,
        ue.user_email,
        ue.user_profile_url,
        ue.user_pet_type,
        ue.user_pet_status,
        ue.compliance_status,
        ue.household_profile_started_alltime,
        ue.assistance_profile_started_alltime,
        CASE
            WHEN ue.household_profile_started_alltime > 0
                 AND NOT (ue.user_pet_type = 'household' AND ue.user_pet_status = 'active')
            THEN 'Abandoned household profile'
            WHEN ue.assistance_profile_started_alltime > 0
                 AND ue.user_pet_type = 'assistance'
                 AND ue.user_pet_status IN ('draft','non_responsive','declined','not_recommended','returned')
            THEN 'Unresolved assistance request'
            WHEN ue.assistance_profile_started_alltime > 0
                 AND ue.user_pet_type = 'not_pet'
            THEN 'No-pet after assistance started'
            ELSE 'Other suspected'
        END AS suspected_reason
    FROM PROD.common.d_units du
    JOIN PROD.common.d_properties p
        ON du.property_id = p.property_id
    JOIN PROD.petscreening.petscreening__user_enriched ue
        ON ue.unit_id = du.unit_id
    JOIN PROD.common.f_leases l
        ON du.unit_key = l.unit_key AND l.user_key = ue.user_key
    LEFT JOIN PROD.common.f_user_pets up
        ON up.user_key = ue.user_key
    LEFT JOIN PROD.common.d_pet_profiles pp
        ON pp.pet_key = up.pet_key
    WHERE du.unit_source = '{pmc_system}'
      AND du.property_id IN ({props_str})
      AND ue.compliance_status IN ('compliant', 'non_compliant')
      /* Exclude recommended AND expired assistance profiles — both at pet_profile and user_enriched level */
      AND NOT (
          COALESCE(pp.pet_profile_kind, '') = 'assistance'
          AND COALESCE(pp.pet_profile_status, '') IN ('recommended', 'expired')
      )
      AND NOT (
          ue.user_pet_type = 'assistance'
          AND ue.user_pet_status IN ('recommended', 'expired')
      )
      AND (
            /* 1) Household profile started but NOT currently active household */
            (
              ue.household_profile_started_alltime > 0
              AND NOT (
                ue.user_pet_type = 'household'
                AND ue.user_pet_status = 'active'
              )
            )
            OR
            /* 2) Assistance profile started + unresolved / denied / no-pet */
            (
              ue.assistance_profile_started_alltime > 0
              AND (
                    (
                      ue.user_pet_type = 'assistance'
                      AND ue.user_pet_status IN (
                        'draft', 'non_responsive', 'declined',
                        'not_recommended', 'returned'
                      )
                    )
                    OR (
                      ue.user_pet_type = 'not_pet'
                    )
                  )
            )
          )
      AND COALESCE(pp.pet_profile_archive_reason, '') = ''
      AND ue.user_email IS NOT NULL
      AND TRIM(ue.user_email) <> ''
      AND LOWER(TRIM(ue.user_email)) NOT IN ({JUNK_EMAILS})
    """
    cur.execute(sql_suspected)
    profiles_df = pd.DataFrame(cur.fetchall())
    cur.close()

    if profiles_df.empty:
        return {}

    # ── Step 3: Exclude anyone already paying selected charges ──
    # Uses case-insensitive tc + email fallback; propagates across multi-lease users
    profiles_df = _apply_paying_flag(profiles_df, paying_tc_set, paying_email_set)

    # ── Step 3b: Entrata freshness filter ──
    if pmc_system == 'entrata' and 'email' in all_charges_df.columns:
        api_emails_by_prop = (
            all_charges_df
            .assign(
                _pid=lambda d: d['property_id'].astype(str).str.strip(),
                _em=lambda d: d['email'].astype(str).str.strip().str.lower(),
            )
            .loc[lambda d: d['_em'].ne('') & d['_em'].ne('nan') & d['_em'].ne('none')]
            .groupby('_pid')['_em']
            .apply(set)
            .to_dict()
        )
        def _in_api_susp(row):
            pid = str(row.get('PROPERTY_ID', '')).strip()
            em = str(row.get('USER_EMAIL', '')).strip().lower()
            return em in api_emails_by_prop.get(pid, set())
        profiles_df['_in_api'] = profiles_df.apply(_in_api_susp, axis=1)
        profiles_df = profiles_df[profiles_df['_in_api'] | (profiles_df['pet_rent_paid'] == 1)]
        profiles_df = profiles_df.drop(columns=['_in_api'], errors='ignore')

    profiles_df = profiles_df[profiles_df['pet_rent_paid'] == 0].copy()

    if profiles_df.empty:
        return {}

    # ── Step 4: Per-property, per-month missing revenue ──
    m0, mN = months[0], months[-1]
    result = {}
    for pname, grp in profiles_df.groupby('PROPERTY_NAME'):
        avg_rec = avg_recurring_by_prop.get(pname, portfolio_avg_rec)
        avg_ot = avg_onetime_by_prop.get(pname, portfolio_avg_ot)

        has_recurring = avg_rec > 0
        has_onetime = avg_ot > 0
        _is_portfolio_avg = (pname not in avg_recurring_by_prop and pname not in avg_onetime_by_prop)
        _suffix = " (portfolio avg)" if _is_portfolio_avg else ""
        if has_recurring and has_onetime:
            charge_label = f"rent (${avg_rec:,.0f}/mo) + deposit (${avg_ot:,.0f}){_suffix}"
        elif has_recurring:
            charge_label = f"rent (${avg_rec:,.0f}/mo){_suffix}"
        elif has_onetime:
            charge_label = f"deposit (${avg_ot:,.0f} one-time){_suffix}"
        else:
            charge_label = "no fee data available"

        launch_dt = _resolve_launch_dt(launch_dates.get(pname))
        launch_month = datetime(launch_dt.year, launch_dt.month, 1) if launch_dt else None

        monthly_missing = {m: 0.0 for m in months}
        missing_tenants = []

        for _, row in grp.iterrows():
            tc = str(row['TENANT_CODE'])
            tinfo = tenant_info.get((pname, tc))

            if tinfo:
                t_start = tinfo['move_in'] or tinfo['lease_from']
                t_end = tinfo['move_out'] or tinfo['lease_to']
                t_status = tinfo['status']
            else:
                t_start = None
                t_end = None
                t_status = 'current'

            if t_start is None:
                t_start = launch_dt if launch_dt else m0
            active_from = datetime(t_start.year, t_start.month, 1)

            if t_end is None or t_status == 'current':
                active_to = mN
            else:
                active_to = datetime(t_end.year, t_end.month, 1)

            if launch_month:
                active_from = max(active_from, launch_month)
            active_from = max(active_from, m0)
            active_to = min(active_to, mN)

            first_month_done = False
            for m in months:
                if active_from <= m <= active_to:
                    if has_recurring:
                        monthly_missing[m] += avg_rec
                    if has_onetime and not first_month_done:
                        monthly_missing[m] += avg_ot
                        first_month_done = True

            missing_tenants.append({
                "name": f"{row.get('USER_FIRST_NAME', '')} {row.get('USER_LAST_NAME', '')}".strip(),
                "email": row.get('USER_EMAIL', ''),
                "profile_url": row.get('USER_PROFILE_URL', ''),
                "suspected_reason": row.get('SUSPECTED_REASON', 'Unknown'),
                "user_pet_type": row.get('USER_PET_TYPE', ''),
                "user_pet_status": row.get('USER_PET_STATUS', ''),
                "active_from": active_from.strftime('%b %Y'),
                "active_to": active_to.strftime('%b %Y') if active_to < mN else "Current",
            })

        total_missing_in_window = sum(monthly_missing.values())
        months_with_missing = sum(1 for v in monthly_missing.values() if v > 0)
        est_per_month = total_missing_in_window / months_with_missing if months_with_missing > 0 else 0

        # Count unique tenants by email (not raw rows which may have duplicates)
        _unique_suspected = grp['USER_EMAIL'].nunique() if 'USER_EMAIL' in grp.columns else len(grp)

        result[pname] = {
            "missing_count": _unique_suspected,
            "monthly_missing": monthly_missing,
            "avg_recurring": avg_rec,
            "avg_onetime": avg_ot,
            "charge_type_label": charge_label,
            "estimated_missing_per_month": est_per_month,
            "total_missing_in_window": total_missing_in_window,
            "missing_tenants": missing_tenants,
        }
    return result


def generate_suspected_undisclosed_report(all_charges_df, selected_codes, property_ids):
    """
    Generate a downloadable report of suspected undisclosed pets.
    Similar to generate_missing_pet_rent_report but uses the suspected
    undisclosed query criteria and adds suspected_reason column.
    """
    pmc_system = st.session_state.get("pmc_system", "yardi")
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)
    props_str = ", ".join(str(int(pid)) for pid in property_ids)

    # Step 1: From live API data, build paying tenant set (normalized)
    paying_tc_set, paying_email_set = _build_paying_sets(all_charges_df, selected_codes)

    # Step 2: Query Snowflake for suspected undisclosed profiles
    sql = f"""
    SELECT DISTINCT
        du.property_id,
        p.property_name,
        COALESCE(
            l.lease_source_external_id:tenant_code::STRING,
            l.lease_source_external_id:"customerId"::STRING,
            l.lease_source_external_id:"customer_id"::STRING
        ) AS tenant_code,
        ue.user_first_name,
        ue.user_last_name,
        ue.user_email,
        ue.user_profile_url,
        ue.user_pet_type,
        ue.user_pet_status,
        ue.compliance_status,
        ue.household_profile_started_alltime,
        ue.assistance_profile_started_alltime,
        du.unit_address_1,
        du.unit_address_2,
        CONCAT_WS(' ', du.unit_address_1, du.unit_address_2) AS full_unit_address,
        l.lease_start_date,
        l.lease_end_date,
        CASE
            WHEN ue.household_profile_started_alltime > 0
                 AND NOT (ue.user_pet_type = 'household' AND ue.user_pet_status = 'active')
            THEN 'Abandoned household profile'
            WHEN ue.assistance_profile_started_alltime > 0
                 AND ue.user_pet_type = 'assistance'
                 AND ue.user_pet_status IN ('draft','non_responsive','declined','not_recommended','returned')
            THEN 'Unresolved assistance request'
            WHEN ue.assistance_profile_started_alltime > 0
                 AND ue.user_pet_type = 'not_pet'
            THEN 'No-pet after assistance started'
            ELSE 'Other suspected'
        END AS suspected_reason
    FROM PROD.common.d_units du
    JOIN PROD.common.d_properties p
        ON du.property_id = p.property_id
    JOIN PROD.petscreening.petscreening__user_enriched ue
        ON ue.unit_id = du.unit_id
    JOIN PROD.common.f_leases l
        ON du.unit_key = l.unit_key AND l.user_key = ue.user_key
    LEFT JOIN PROD.common.f_user_pets up
        ON up.user_key = ue.user_key
    LEFT JOIN PROD.common.d_pet_profiles pp
        ON pp.pet_key = up.pet_key
    WHERE du.unit_source = '{pmc_system}'
      AND du.property_id IN ({props_str})
      AND ue.compliance_status IN ('compliant', 'non_compliant')
      /* Exclude recommended AND expired assistance profiles — both at pet_profile and user_enriched level */
      AND NOT (
          COALESCE(pp.pet_profile_kind, '') = 'assistance'
          AND COALESCE(pp.pet_profile_status, '') IN ('recommended', 'expired')
      )
      AND NOT (
          ue.user_pet_type = 'assistance'
          AND ue.user_pet_status IN ('recommended', 'expired')
      )
      AND (
            (
              ue.household_profile_started_alltime > 0
              AND NOT (
                ue.user_pet_type = 'household'
                AND ue.user_pet_status = 'active'
              )
            )
            OR
            (
              ue.assistance_profile_started_alltime > 0
              AND (
                    (
                      ue.user_pet_type = 'assistance'
                      AND ue.user_pet_status IN (
                        'draft', 'non_responsive', 'declined',
                        'not_recommended', 'returned'
                      )
                    )
                    OR (
                      ue.user_pet_type = 'not_pet'
                    )
                  )
            )
          )
      AND COALESCE(pp.pet_profile_archive_reason, '') = ''
      AND ue.user_email IS NOT NULL
      AND TRIM(ue.user_email) <> ''
      AND LOWER(TRIM(ue.user_email)) NOT IN ({JUNK_EMAILS})
    """
    cur.execute(sql)
    profiles_df = pd.DataFrame(cur.fetchall())
    cur.close()

    if profiles_df.empty:
        return pd.DataFrame()

    # Step 3: Exclude anyone already paying (case-insensitive tc + email fallback)
    # Propagates across multi-lease users — if any lease row is paying, all are
    profiles_df = _apply_paying_flag(profiles_df, paying_tc_set, paying_email_set)

    # Step 3b: Entrata freshness filter
    if pmc_system == 'entrata' and 'email' in all_charges_df.columns:
        api_emails_by_prop = (
            all_charges_df
            .assign(
                _pid=lambda d: d['property_id'].astype(str).str.strip(),
                _em=lambda d: d['email'].astype(str).str.strip().str.lower(),
            )
            .loc[lambda d: d['_em'].ne('') & d['_em'].ne('nan') & d['_em'].ne('none')]
            .groupby('_pid')['_em']
            .apply(set)
            .to_dict()
        )
        def _in_api_susp_rpt(row):
            pid = str(row.get('PROPERTY_ID', '')).strip()
            em = str(row.get('USER_EMAIL', '')).strip().lower()
            return em in api_emails_by_prop.get(pid, set())
        profiles_df['_in_api'] = profiles_df.apply(_in_api_susp_rpt, axis=1)
        profiles_df = profiles_df[profiles_df['_in_api'] | (profiles_df['pet_rent_paid'] == 1)]
        profiles_df = profiles_df.drop(columns=['_in_api'], errors='ignore')

    report_df = profiles_df[profiles_df['pet_rent_paid'] == 0].copy()

    if report_df.empty:
        return pd.DataFrame()

    report_df['overall_status'] = 'Suspected_Undisclosed'
    return report_df


# ═════════════════════════════════════════════════════════════════════
# STREAMLIT UI
# ═════════════════════════════════════════════════════════════════════

# ─── Branded header ──────────────────────────────────────────────────
if _PS_LOGO_DARK_URI:
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:14px;padding:4px 0 8px 0">'
        f'<img src="{_PS_LOGO_DARK_URI}" height="28" alt="PetScreening">'
        f'<div style="height:28px;width:1px;background:#D3CEBD"></div>'
        f'<span style="font-family:Poppins,Arial,sans-serif;font-size:22px;font-weight:600;'
        f'color:#1F2257;letter-spacing:-0.3px">Value Report</span>'
        f'</div>'
        f'<p style="color:#636569;font-size:13px;margin:0 0 8px 0;font-family:Poppins,Arial,sans-serif">'
        f'Fetch rent roll data · Select charge codes · Visualize collection trends</p>',
        unsafe_allow_html=True,
    )
else:
    st.title("Value Report")
    st.markdown("Fetch rent roll data, select fee charge codes, and visualize collection trends over time.")

# ─── Persistent selection banner ────────────────────────────────────
# Shows the currently selected parent company / property at the top
# so users always know what data they're viewing.
_sel_label = st.session_state.get("selection_label", "")
_sel_system = st.session_state.get("pmc_system", "yardi")
if _sel_label:
    _sys_badge = "Yardi" if _sel_system == "yardi" else "Entrata"
    _badge_color = "#7D9BC1" if _sel_system == "yardi" else "#677848"
    st.markdown(
        f'<div style="background:#F9F4E6;border-left:4px solid {_badge_color};padding:8px 16px;'
        f'border-radius:0 6px 6px 0;margin:0 0 12px 0;display:flex;align-items:center;gap:10px">'
        f'<span style="background:{_badge_color};color:white;font-size:11px;font-weight:600;'
        f'padding:2px 8px;border-radius:4px;font-family:Poppins,Arial,sans-serif">{_sys_badge}</span>'
        f'<span style="font-family:Poppins,Arial,sans-serif;font-size:15px;font-weight:600;'
        f'color:#1F2257">{_sel_label}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

# ─── Early session state (needed before sidebar renders) ─────────────
if "pmc_system" not in st.session_state:
    st.session_state.pmc_system = "yardi"

# ─── Sidebar: Selection ──────────────────────────────────────────────
with st.sidebar:
    if _PS_LOGO_WHITE_URI:
        st.markdown(
            f'<div style="background:#1F2257;padding:14px 18px;border-radius:8px;'
            f'text-align:center;margin-bottom:16px">'
            f'<img src="{_PS_LOGO_WHITE_URI}" height="20" alt="PetScreening">'
            f'</div>',
            unsafe_allow_html=True,
        )

    # ── PMC System selector ──────────────────────────────────────────
    st.header("PMC System")
    _pmc_choice = st.radio(
        "Data Source:",
        ["Yardi", "Entrata"],
        index=0 if st.session_state.pmc_system == "yardi" else 1,
        help="Select the property management system to fetch charge data from.",
        horizontal=True,
    )
    _pmc_system = _pmc_choice.lower()
    # If user switches PMC system, clear stale data
    if _pmc_system != st.session_state.pmc_system:
        st.session_state.pmc_system = _pmc_system
        st.session_state.all_charges_df = None
        st.session_state.ar_charges_df = None
        st.session_state.raw_lease_arrays_df = None
        st.session_state.fetch_log = None
        st.session_state.chart_data = None
        st.session_state.selection_label = ""
        st.session_state.property_ids = []
        for _sk in list(st.session_state.keys()):
            if (_sk.startswith("missing_rent_") and isinstance(st.session_state[_sk], dict)) or _sk in ("export_html", "exec_html"):
                del st.session_state[_sk]

    _is_entrata = _pmc_system == "entrata"
    _system_label = "Entrata" if _is_entrata else "Yardi"

    st.divider()
    st.header("Select Properties")

    search_by = st.radio(
        "Search by:",
        ["Parent Company Name", "Parent Company Ancestry ID", "Property Name / ID"],
        index=0,
    )

    selected_parent = None
    selected_property_id = None
    selected_ancestry_id = None

    if search_by == "Parent Company Name":
        parents = load_entrata_parent_companies() if _is_entrata else load_parent_companies()
        parent_options = {
            f"{r['PARENT_COMPANY_NAME']} ({r['TOTAL_PROPS']} props)": r
            for r in parents
        }
        choice = st.selectbox("Parent Company:", [""] + list(parent_options.keys()))
        if choice:
            row = parent_options[choice]
            selected_parent = row['PARENT_COMPANY_NAME']
            st.caption(f"Ancestry ID: `{row['ANCESTRY_ID']}`  ·  "
                        f"{row['API_PROPS']} of {row['TOTAL_PROPS']} have {_system_label} API access")

    elif search_by == "Parent Company Ancestry ID":
        parents = load_entrata_parent_companies() if _is_entrata else load_parent_companies()
        ancestry_options = {}
        for r in parents:
            aid = r.get('ANCESTRY_ID') or ''
            if aid:
                label = f"{aid} — {r['PARENT_COMPANY_NAME']} ({r['TOTAL_PROPS']} props)"
                ancestry_options[label] = r
        choice = st.selectbox(
            "Ancestry ID:",
            [""] + sorted(ancestry_options.keys()),
            help="parent_company_ancestry_id from PROD.COMMON.D_PROPERTIES",
        )
        if choice:
            row = ancestry_options[choice]
            selected_ancestry_id = str(row['ANCESTRY_ID'])
            st.caption(f"Parent Company: `{row['PARENT_COMPANY_NAME']}`  ·  "
                        f"{row['API_PROPS']} of {row['TOTAL_PROPS']} have {_system_label} API access")

    else:
        all_props = load_entrata_all_properties() if _is_entrata else load_all_properties()
        prop_options = {
            f"{r['PROPERTY_NAME']} (ID: {r['PROPERTY_ID']})": r['PROPERTY_ID']
            for r in all_props
        }
        choice = st.selectbox("Property:", [""] + list(prop_options.keys()))
        if choice:
            selected_property_id = prop_options[choice]

    # Display window is now derived from the data (no slider).
    # Set a large default; the actual window will be clamped to the
    # earliest charge date once data is loaded.
    lookback_months = 120  # 10 years — effectively "all data"

    st.divider()

    # ── Check for saved exports ──
    _auto_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_exports")
    _saved_files = []
    if os.path.isdir(_auto_dir):
        _saved_files = sorted(
            [f for f in os.listdir(_auto_dir) if f.startswith("charges_") and f.endswith(".csv")],
            reverse=True,
        )

    fetch_btn = st.button(f"Fetch {_system_label} Data", type="primary", use_container_width=True)

    if _saved_files:
        with st.expander("📂 Or load from saved export"):
            _selected_export = st.selectbox(
                "Saved files",
                options=_saved_files,
                key="load_export_select",
            )
            if st.button("Load selected file", key="load_export_btn", use_container_width=True):
                _load_path = os.path.join(_auto_dir, _selected_export)
                _loaded_df = pd.read_csv(_load_path, low_memory=False)

                # ── Fix CSV type coercion ──
                # Pandas reads integer columns with NaN as float (e.g. 12345 → 12345.0).
                # Downstream matching compares string IDs ("12345" vs "12345.0") and fails.
                # Normalize ID-like columns back to clean strings.
                for _id_col in ['property_id', 'tenant_code', 'lease_id', 'unit_code']:
                    if _id_col in _loaded_df.columns:
                        _loaded_df[_id_col] = (
                            _loaded_df[_id_col]
                            .astype(str)
                            .str.replace(r'\.0$', '', regex=True)
                            .replace({'nan': '', 'None': '', 'none': ''})
                        )

                st.session_state.all_charges_df = _loaded_df
                # Try to load matching fetch log file
                _log_name = _selected_export.replace("charges_", "fetch_log_", 1)
                _log_path = os.path.join(_auto_dir, _log_name)
                if os.path.isfile(_log_path):
                    st.session_state.fetch_log = pd.read_csv(_log_path, low_memory=False)
                else:
                    st.session_state.fetch_log = None
                # Try to load matching AR file
                _ar_name = _selected_export.replace("charges_", "ar_charges_", 1)
                _ar_path = os.path.join(_auto_dir, _ar_name)
                if os.path.isfile(_ar_path):
                    _ar_df = pd.read_csv(_ar_path, low_memory=False)
                    for _id_col in ['property_id', 'tenant_code', 'lease_id', 'unit_code']:
                        if _id_col in _ar_df.columns:
                            _ar_df[_id_col] = (
                                _ar_df[_id_col]
                                .astype(str)
                                .str.replace(r'\.0$', '', regex=True)
                                .replace({'nan': '', 'None': '', 'none': ''})
                            )
                    st.session_state.ar_charges_df = _ar_df
                else:
                    st.session_state.ar_charges_df = None
                st.session_state.raw_lease_arrays_df = None
                # Recover property_ids from the loaded data
                if 'property_id' in _loaded_df.columns:
                    st.session_state.property_ids = _loaded_df['property_id'].dropna().unique().tolist()
                # Recover selection_label from parent_company column if not already set
                if not st.session_state.get("selection_label") and 'parent_company' in _loaded_df.columns:
                    _pc_vals = _loaded_df['parent_company'].dropna().unique()
                    if len(_pc_vals) > 0:
                        st.session_state.selection_label = str(_pc_vals[0]).strip()
                st.success(f"Loaded **{len(_loaded_df):,}** charges from `{_selected_export}`")
                st.rerun()

# ─── Session state ───────────────────────────────────────────────────
if "all_charges_df" not in st.session_state:
    st.session_state.all_charges_df = None
if "ar_charges_df" not in st.session_state:
    st.session_state.ar_charges_df = None
if "raw_lease_arrays_df" not in st.session_state:
    st.session_state.raw_lease_arrays_df = None
if "fetch_log" not in st.session_state:
    st.session_state.fetch_log = None
if "selection_label" not in st.session_state:
    st.session_state.selection_label = ""
if "property_ids" not in st.session_state:
    st.session_state.property_ids = []
if "total_parent_props" not in st.session_state:
    st.session_state.total_parent_props = 0
if "api_props_count" not in st.session_state:
    st.session_state.api_props_count = 0
if "chart_data" not in st.session_state:
    st.session_state.chart_data = None

# ─── Fetch data ──────────────────────────────────────────────────────
if fetch_btn:
    if not selected_parent and not selected_property_id and not selected_ancestry_id:
        st.warning("Please select a parent company, ancestry ID, or property first.")
    else:
        # Route to correct property loader based on PMC system
        if _is_entrata:
            properties = load_entrata_properties_for_selection(
                parent_company_name=selected_parent,
                property_id=selected_property_id,
                ancestry_id=selected_ancestry_id,
            )
        else:
            properties = load_properties_for_selection(
                parent_company_name=selected_parent,
                property_id=selected_property_id,
                ancestry_id=selected_ancestry_id,
            )

        if not properties:
            st.error(f"No {_system_label} properties with active integrations found for that selection.")
        else:
            label = selected_parent or (f"Ancestry {selected_ancestry_id}" if selected_ancestry_id else f"Property {selected_property_id}")
            st.session_state.selection_label = label
            st.session_state.property_ids = [p['PROPERTY_ID'] for p in properties]

            # Store selection context for downstream queries (e.g., PM email lookup)
            # Always try to resolve the ancestry_id, even when searching by name
            _resolved_ancestry_id = selected_ancestry_id
            if not _resolved_ancestry_id and (selected_parent or selected_ancestry_id):
                try:
                    _pc_list = load_entrata_parent_companies() if _is_entrata else load_parent_companies()
                    for r in _pc_list:
                        if (selected_parent and r['PARENT_COMPANY_NAME'] == selected_parent) or \
                           (selected_ancestry_id and str(r.get('ANCESTRY_ID', '')) == selected_ancestry_id):
                            _resolved_ancestry_id = str(r.get('ANCESTRY_ID', ''))
                            break
                except Exception:
                    pass
            st.session_state.selected_ancestry_id = _resolved_ancestry_id
            st.session_state.selected_parent_company = selected_parent

            # Get total property count for context
            total_count = None
            if selected_parent or selected_ancestry_id:
                _pc_list2 = load_entrata_parent_companies() if _is_entrata else load_parent_companies()
                for r in _pc_list2:
                    if (selected_parent and r['PARENT_COMPANY_NAME'] == selected_parent) or \
                       (selected_ancestry_id and str(r.get('ANCESTRY_ID', '')) == selected_ancestry_id):
                        total_count = r['TOTAL_PROPS']
                        break

            api_count = len(properties)
            st.session_state.total_parent_props = total_count if total_count else api_count
            st.session_state.api_props_count = api_count
            if total_count and total_count > api_count:
                st.info(
                    f"**{label}** has **{total_count}** total properties in `d_properties`.  \n"
                    f"**{api_count}** have active {_system_label} API integrations (credentials to fetch).  \n"
                    f"**{total_count - api_count}** properties are missing integration records and will be skipped."
                )
            else:
                st.info(f"Found **{api_count}** {_system_label}-integrated properties for **{label}**.")

            # Clear ALL stale data from previous selection
            st.session_state.chart_data = None
            st.session_state.pm_emails_cache = None
            st.session_state.all_charges_df = None
            st.session_state.ar_charges_df = None
            st.session_state.raw_lease_arrays_df = None
            st.session_state.fetch_log = None
            # Clear cached missing rent, suspected, and charge type data
            for _stale_key in list(st.session_state.keys()):
                if (_stale_key.startswith("missing_rent_") or _stale_key.startswith("suspected_") or
                    _stale_key in ("export_html", "exec_html", "charge_type_overrides")):
                    del st.session_state[_stale_key]
            st.markdown(f"Fetching {_system_label} data (full history — display window: **{lookback_months} months**)...")
            progress_bar = st.progress(0)
            status_text = st.empty()

            # Route to correct API fetch
            if _is_entrata:
                all_charges, fetch_log, ar_charges, raw_lease_arrays = fetch_entrata_for_properties(properties, progress_bar, status_text, lookback_months)
            else:
                all_charges, fetch_log = fetch_rentroll_for_properties(properties, progress_bar, status_text, lookback_months)
                ar_charges = []
                raw_lease_arrays = []

            success_count = sum(1 for l in fetch_log if l['status'].startswith('Success'))
            error_count = sum(1 for l in fetch_log if l['status'].startswith('Error'))
            warn_count = sum(1 for l in fetch_log if l['status'].startswith('Warning'))

            if all_charges:
                st.session_state.all_charges_df = pd.DataFrame(all_charges)
                st.session_state.fetch_log = pd.DataFrame(fetch_log)
                if ar_charges:
                    st.session_state.ar_charges_df = pd.DataFrame(ar_charges)
                else:
                    st.session_state.ar_charges_df = None
                if raw_lease_arrays:
                    st.session_state.raw_lease_arrays_df = pd.DataFrame(raw_lease_arrays)
                else:
                    st.session_state.raw_lease_arrays_df = None

                # ── Auto-export to disk (survives crashes / memory kills) ──
                _auto_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_exports")
                os.makedirs(_auto_dir, exist_ok=True)
                _ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                _label_safe = st.session_state.get("selection_label", "export").replace(" ", "_")[:60]
                _auto_path = os.path.join(_auto_dir, f"charges_{_label_safe}_{_ts}.csv")
                st.session_state.all_charges_df.to_csv(_auto_path, index=False)
                _auto_msg = f"Auto-saved {len(all_charges):,} charges → `{os.path.basename(_auto_path)}`"
                if ar_charges and st.session_state.ar_charges_df is not None:
                    _ar_path = os.path.join(_auto_dir, f"ar_charges_{_label_safe}_{_ts}.csv")
                    st.session_state.ar_charges_df.to_csv(_ar_path, index=False)
                    _auto_msg += f" + AR transactions"
                st.info(f"💾 {_auto_msg} (in `auto_exports/` folder)")

                # Build summary message
                summary_parts = [f"Fetched **{len(all_charges):,}** charges across **{success_count}** properties"]
                if error_count:
                    summary_parts.append(f"**{error_count}** failed (timeout/HTTP error)")
                if warn_count:
                    summary_parts.append(f"**{warn_count}** had no data or access issues")
                st.success("  ·  ".join(summary_parts))
            else:
                st.session_state.all_charges_df = None
                st.session_state.ar_charges_df = None
                st.session_state.raw_lease_arrays_df = None
                st.session_state.fetch_log = pd.DataFrame(fetch_log)
                st.warning("No charge data returned from any property.")

# ─── Show fetch results ──────────────────────────────────────────────
if st.session_state.fetch_log is not None:
    with st.expander("Fetch Results (click to expand)", expanded=False):
        _render_table(st.session_state.fetch_log, height=300)

# ─── Charge code selection ───────────────────────────────────────────
if st.session_state.all_charges_df is not None:
    df = st.session_state.all_charges_df

    st.divider()
    st.header("Select Fee Charge Codes")
    st.markdown("Below are all unique charge codes found. **Select the ones you believe are pet-related fees / pet rent.**")

    # Show charge code summary
    code_counts = df['charge_code'].value_counts().reset_index()
    code_counts.columns = ['Charge Code', 'Count']

    # Pre-select codes that look pet-related
    pet_keywords = ['pet', 'animal', 'petnr', 'concpet']
    default_selected = [
        code for code in code_counts['Charge Code'].tolist()
        if any(kw in code.lower() for kw in pet_keywords)
    ]

    col1, col2 = st.columns([1, 2])

    with col1:
        st.markdown("**Charge Code Summary:**")
        _render_table(code_counts, height=400)

    with col2:
        selected_codes = st.multiselect(
            "Select charge codes to analyze:",
            options=code_counts['Charge Code'].tolist(),
            default=default_selected,
            help="Pick the charge codes that represent pet fees, pet rent, etc."
        )

        if selected_codes:
            filtered = df[df['charge_code'].isin(selected_codes)]
            st.metric("Matching Charge Lines", f"{len(filtered):,}",
                      help="Unique charge line items from the API matching your selected codes. Each line has a from/to date range — it contributes to every month it was active in the charts below.")

            # Summary of selected codes: count + total amount per code
            filtered_with_amt = filtered.copy()
            filtered_with_amt['amount'] = pd.to_numeric(filtered_with_amt['charge_amount'], errors='coerce').fillna(0)
            code_summary = (
                filtered_with_amt.groupby('charge_code')
                .agg(count=('charge_code', 'size'), total_amount=('amount', 'sum'))
                .sort_values('count', ascending=False)
                .reset_index()
            )
            code_summary.columns = ['Charge Code', 'Count', 'Total Amount']
            code_summary['Total Amount'] = code_summary['Total Amount'].apply(lambda x: f"${x:,.2f}")
            _render_table(code_summary)

            # Sample rows: take up to 3 rows from each selected code so all codes are represented
            sample_cols = ['property_name', 'tenant_code', 'charge_code', 'charge_amount',
                           'charge_from_date', 'charge_to_date', 'tenant_status']
            sample_rows = filtered.groupby('charge_code').head(3).sort_values('charge_code')
            with st.expander(f"Preview rows ({len(sample_rows)} shown, up to 3 per code)"):
                _render_table(sample_rows[sample_cols])

    if not selected_codes:
        st.info("Select at least one charge code above to continue.")
    else:
        # ─── Charge Type Classification (Recurring vs One-Time) ──────
        with st.expander("⚙️ Charge type classification — recurring vs one-time", expanded=False):
            st.markdown(
                "Each charge code is auto-classified as **recurring** (monthly pet rent) or "
                "**one-time** (pet deposit/fee). One-time charges only count in their start month "
                "on the revenue charts — they don't spread across months.\n\n"
                "**Override** any classification below if the auto-detection is wrong."
            )

            # Auto-detect classification for selected codes
            _has_freq = 'frequency' in df.columns
            _auto_class = {}  # {charge_code: "recurring" | "onetime"}
            _class_reason = {}  # {charge_code: explanation}

            _pet_df = df[df['charge_code'].isin(selected_codes)].copy()
            _pet_df['_amt'] = pd.to_numeric(_pet_df['charge_amount'], errors='coerce').fillna(0)

            for code in selected_codes:
                code_rows = _pet_df[_pet_df['charge_code'] == code]
                if _has_freq:
                    freqs = code_rows['frequency'].dropna().str.strip().str.lower()
                    ot = (freqs == 'one-time').sum()
                    mo = freqs.isin(['monthly', 'recurring']).sum()
                    if ot > mo:
                        _auto_class[code] = "onetime"
                        _class_reason[code] = f"Entrata frequency field: {ot} one-time vs {mo} recurring"
                    else:
                        _auto_class[code] = "recurring"
                        _class_reason[code] = f"Entrata frequency field: {mo} recurring vs {ot} one-time"
                else:
                    spans = []
                    for _, row in code_rows.iterrows():
                        fd = parse_date(row.get('charge_from_date'))
                        td = parse_date(row.get('charge_to_date'))
                        if fd and td:
                            try:
                                spans.append((td - fd).days)
                            except (TypeError, AttributeError):
                                pass
                    med = float(np.median(spans)) if spans else None
                    if med is None:
                        _auto_class[code] = "recurring"
                        _class_reason[code] = "No date spans available — defaulting to recurring"
                    elif med > 60:
                        _auto_class[code] = "recurring"
                        _class_reason[code] = f"Median date span: {med:.0f} days (>60 = recurring)"
                    else:
                        _auto_class[code] = "onetime"
                        _class_reason[code] = f"Median date span: {med:.0f} days (≤60 = one-time)"

            # Build UI: one row per charge code with override dropdown
            _override_key = "charge_type_overrides"
            if _override_key not in st.session_state:
                st.session_state[_override_key] = {}

            _class_options = ["Auto-detect", "Recurring", "One-Time"]
            _override_cols = st.columns([3, 2, 2, 3])
            _override_cols[0].markdown("**Charge Code**")
            _override_cols[1].markdown("**Auto-Detected**")
            _override_cols[2].markdown("**Override**")
            _override_cols[3].markdown("**Reason**")

            for code in selected_codes:
                auto = _auto_class.get(code, "recurring")
                auto_label = "Recurring" if auto == "recurring" else "One-Time"
                reason = _class_reason.get(code, "")
                avg_amt = _pet_df[_pet_df['charge_code'] == code]['_amt'].mean()

                c1, c2, c3, c4 = st.columns([3, 2, 2, 3])
                c1.markdown(f"`{code}` (avg ${avg_amt:,.0f})")
                c2.markdown(f"{'🔄 Recurring' if auto == 'recurring' else '1️⃣ One-Time'}")
                override = c3.selectbox(
                    "Override",
                    options=_class_options,
                    index=0,
                    key=f"ct_override_{code}",
                    label_visibility="collapsed",
                )
                c4.caption(reason)

                if override == "Recurring":
                    st.session_state[_override_key][code] = "recurring"
                elif override == "One-Time":
                    st.session_state[_override_key][code] = "onetime"
                elif code in st.session_state[_override_key]:
                    del st.session_state[_override_key][code]

            if st.session_state.get(_override_key):
                _n_overrides = len(st.session_state[_override_key])
                st.info(f"📌 {_n_overrides} manual override{'s' if _n_overrides > 1 else ''} active. "
                        f"These override the auto-detection for revenue charts and lift calculations.")

        st.divider()

        # ─── Raw Data Exports (for Snowflake validation) ─────────────
        with st.expander("Raw Data Exports — Download API data for Snowflake validation", expanded=False):
            st.markdown(
                "Download the raw data that the app uses so you can upload it to "
                "Snowflake and independently recreate/validate the numbers with SQL.  \n"
                "**Date columns are auto-fixed** (century correction applied) — upload directly, no manual cleanup needed."
            )

            pmc_sys = st.session_state.get("pmc_system", "yardi")
            props_str_export = ", ".join(str(int(pid)) for pid in st.session_state.get("property_ids", []))

            # ── Fix dates before export ──────────────────────────────
            # Yardi API returns dates like "07/01/0025" instead of "07/01/2025".
            # Fix all date columns so the CSV can be uploaded to Snowflake directly.
            _date_cols = ['launch_date', 'lease_from', 'lease_to', 'move_in', 'move_out',
                          'charge_from_date', 'charge_to_date']

            def _fix_dates_for_export(export_df):
                """Fix century-shifted dates (0025 → 2025) in all date columns."""
                fixed = export_df.copy()
                for col in _date_cols:
                    if col not in fixed.columns:
                        continue
                    def _fix_date_val(val):
                        if val is None or (isinstance(val, float) and pd.isna(val)):
                            return val
                        s = str(val).strip()
                        if not s or s.lower() in ('nan', 'nat', 'none', ''):
                            return None
                        # Try parsing and fix year if < 1000
                        for fmt in ["%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"]:
                            try:
                                dt = datetime.strptime(s[:10], fmt)
                                if dt.year < 1000:
                                    dt = dt.replace(year=dt.year + 2000)
                                return dt.strftime("%Y-%m-%d")
                            except (ValueError, TypeError):
                                continue
                        # If it's already a datetime/Timestamp object
                        try:
                            if hasattr(val, 'year') and val.year < 1000:
                                val = val.replace(year=val.year + 2000)
                            return str(val)[:10]
                        except Exception:
                            return val
                    fixed[col] = fixed[col].apply(_fix_date_val)
                return fixed

            exp_c1, exp_c2, exp_c3, exp_c4 = st.columns(4)

            with exp_c1:
                st.markdown("**1. All API Charges**")
                st.caption("Every charge line from the API (all tenants, all codes). Dates are auto-fixed.")
                _export_all = _fix_dates_for_export(df)
                st.download_button(
                    "Download all_charges.csv",
                    data=_export_all.to_csv(index=False),
                    file_name=f"all_charges_{pmc_sys}_{datetime.now().strftime('%Y%m%d')}.csv",
                    mime="text/csv",
                    key="dl_all_charges",
                )

            with exp_c2:
                st.markdown("**2. Pet Charges Only**")
                st.caption("Filtered to your selected pet charge codes only. Dates are auto-fixed.")
                _pet_only = df[df['charge_code'].isin(selected_codes)]
                _export_pet = _fix_dates_for_export(_pet_only)
                st.download_button(
                    "Download pet_charges.csv",
                    data=_export_pet.to_csv(index=False),
                    file_name=f"pet_charges_{pmc_sys}_{datetime.now().strftime('%Y%m%d')}.csv",
                    mime="text/csv",
                    key="dl_pet_charges",
                )

            with exp_c3:
                st.markdown("**3. Paying Tenant Sets**")
                st.caption("Every (property_id, tenant_code) and (property_id, email) pair identified as paying after unit/lease expansion.")
                _pay_tc, _pay_em = _build_paying_sets(df, selected_codes)
                _pay_tc_df = pd.DataFrame(list(_pay_tc), columns=["property_id", "tenant_code"])
                _pay_em_df = pd.DataFrame(list(_pay_em), columns=["property_id", "email"])
                _pay_combined = pd.concat([
                    _pay_tc_df.assign(match_type="tenant_code", match_key=_pay_tc_df["tenant_code"]).drop(columns=["tenant_code"]),
                    _pay_em_df.assign(match_type="email", match_key=_pay_em_df["email"]).drop(columns=["email"]),
                ], ignore_index=True)
                st.download_button(
                    "Download paying_tenants.csv",
                    data=_pay_combined.to_csv(index=False),
                    file_name=f"paying_tenants_{pmc_sys}_{datetime.now().strftime('%Y%m%d')}.csv",
                    mime="text/csv",
                    key="dl_paying_sets",
                )

            with exp_c4:
                st.markdown("**4. PS Profiles + Paying Flag**")
                st.caption("PetScreening profiles from Snowflake with the paying flag applied — the raw join result before final filtering.")
                if st.button("Generate Profiles Export", key="gen_profiles_export"):
                    with st.spinner("Querying Snowflake for profiles..."):
                        _conn = get_snowflake_connection()
                        _cur = _conn.cursor(snowflake.connector.DictCursor)
                        _sql = f"""
                        SELECT DISTINCT
                            du.property_id,
                            p.property_name,
                            COALESCE(
                                l.lease_source_external_id:tenant_code::STRING,
                                l.lease_source_external_id:"customerId"::STRING,
                                l.lease_source_external_id:"customer_id"::STRING
                            ) AS tenant_code,
                            ue.user_email,
                            ue.user_first_name,
                            ue.user_last_name,
                            ue.compliance_status,
                            ue.user_pet_type,
                            ue.user_pet_status,
                            ue.user_profile_url
                        FROM PROD.common.d_units du
                        JOIN PROD.common.d_properties p ON du.property_id = p.property_id
                        JOIN PROD.petscreening.petscreening__user_enriched ue ON ue.unit_id = du.unit_id
                        JOIN PROD.common.f_leases l ON du.unit_key = l.unit_key AND l.user_key = ue.user_key
                        WHERE du.unit_source = '{pmc_sys}'
                          AND du.property_id IN ({props_str_export})
                          AND ue.compliance_status = 'compliant'
                          AND ue.user_pet_type = 'household'
                          AND ue.user_pet_status = 'active'
                          AND ue.user_email IS NOT NULL
                          AND TRIM(ue.user_email) <> ''
                          AND LOWER(TRIM(ue.user_email)) NOT IN ({JUNK_EMAILS})
                        """
                        _cur.execute(_sql)
                        _profiles = pd.DataFrame(_cur.fetchall())
                        if not _profiles.empty:
                            _profiles = _apply_paying_flag(_profiles, _pay_tc, _pay_em)
                            st.session_state["_export_profiles_df"] = _profiles
                            st.success(f"Loaded {len(_profiles):,} profiles. {(_profiles['pet_rent_paid'] == 1).sum():,} paying, {(_profiles['pet_rent_paid'] == 0).sum():,} not paying.")
                        else:
                            st.warning("No profiles found.")

                if "_export_profiles_df" in st.session_state and st.session_state["_export_profiles_df"] is not None:
                    _prof_df = st.session_state["_export_profiles_df"]
                    st.download_button(
                        "Download profiles_with_flag.csv",
                        data=_prof_df.to_csv(index=False),
                        file_name=f"profiles_with_paying_flag_{pmc_sys}_{datetime.now().strftime('%Y%m%d')}.csv",
                        mime="text/csv",
                        key="dl_profiles_flag",
                    )

            # ── 5. AR Transactions Export (Entrata only) ────────────
            _ar_export_df = st.session_state.get("ar_charges_df")
            if pmc_sys == "entrata" and _ar_export_df is not None and not _ar_export_df.empty:
                st.markdown("---")
                exp_ar1, exp_ar2, _ = st.columns([1, 1, 2])
                with exp_ar1:
                    st.markdown("**5. AR Transactions (Entrata)**")
                    st.caption(
                        "Actual posted AR transactions (pet-related only). "
                        "These are what was *actually billed* vs the scheduled charges above."
                    )
                    st.download_button(
                        "Download ar_transactions.csv",
                        data=_ar_export_df.to_csv(index=False),
                        file_name=f"ar_transactions_entrata_{datetime.now().strftime('%Y%m%d')}.csv",
                        mime="text/csv",
                        key="dl_ar_transactions",
                    )
                with exp_ar2:
                    st.markdown("**6. Scheduled + AR Flag**")
                    st.caption(
                        "All pet charges with an `ar_match` column: YES if a matching AR transaction "
                        "exists for the same lease + charge code, NO otherwise."
                    )
                    # Build the flagged export
                    _pet_sched = df[df['charge_code'].isin(selected_codes)].copy()
                    _ar_keys = set()
                    if 'lease_id' in _pet_sched.columns:
                        for _, _ar_row in _ar_export_df.iterrows():
                            _ar_keys.add((
                                str(_ar_row.get('lease_id', '')).strip(),
                                str(_ar_row.get('charge_code_name', '')).strip().lower(),
                            ))
                        _pet_sched['ar_match'] = _pet_sched.apply(
                            lambda r: 'YES' if (
                                str(r.get('lease_id', '')).strip(),
                                str(r.get('charge_code', '')).strip().lower(),
                            ) in _ar_keys else 'NO',
                            axis=1,
                        )
                    else:
                        _pet_sched['ar_match'] = 'N/A'
                    _export_flagged = _fix_dates_for_export(_pet_sched)
                    st.download_button(
                        "Download pet_charges_with_ar_flag.csv",
                        data=_export_flagged.to_csv(index=False),
                        file_name=f"pet_charges_ar_flag_{pmc_sys}_{datetime.now().strftime('%Y%m%d')}.csv",
                        mime="text/csv",
                        key="dl_pet_ar_flag",
                    )

            # ── 7. Raw Lease Arrays Export (Entrata only) ────────────
            _raw_arrays_df = st.session_state.get("raw_lease_arrays_df")
            if pmc_sys == "entrata" and _raw_arrays_df is not None and not _raw_arrays_df.empty:
                st.markdown("---")
                _rac1, _rac2, _ = st.columns([1, 1, 2])
                with _rac1:
                    st.markdown("**7. Raw Lease Arrays (Entrata)**")
                    st.caption(
                        "One row per lease with the raw `scheduledCharges` and `arTransactions` "
                        "JSON arrays exactly as returned by the Entrata API. "
                        "Open the CSV and expand the JSON columns to see every individual "
                        "charge and transaction."
                    )
                    st.download_button(
                        "Download raw_lease_arrays.csv",
                        data=_raw_arrays_df.to_csv(index=False),
                        file_name=f"raw_lease_arrays_entrata_{datetime.now().strftime('%Y%m%d')}.csv",
                        mime="text/csv",
                        key="dl_raw_lease_arrays",
                    )
                with _rac2:
                    _n_with_sched = (_raw_arrays_df['n_scheduled_charges'] > 0).sum()
                    _n_with_ar = (_raw_arrays_df['n_ar_transactions'] > 0).sum()
                    st.metric("Leases with scheduled charges", f"{_n_with_sched:,}")
                    st.metric("Leases with AR transactions", f"{_n_with_ar:,}")

            st.divider()
            st.markdown("##### Snowflake Upload Instructions")
            st.code(
                "-- 1. Create a stage and file format\n"
                "CREATE OR REPLACE FILE FORMAT my_csv_format TYPE = 'CSV' FIELD_OPTIONALLY_ENCLOSED_BY = '\"' SKIP_HEADER = 1;\n"
                "CREATE OR REPLACE STAGE my_validation_stage FILE_FORMAT = my_csv_format;\n\n"
                "-- 2. Upload via SnowSQL or Snowsight UI\n"
                "PUT file:///path/to/all_charges_*.csv @my_validation_stage;\n\n"
                "-- 3. Create table and load\n"
                "CREATE OR REPLACE TABLE sandbox.validation.api_charges AS\n"
                "SELECT $1 as parent_company, $2 as property_id, $3 as property_name,\n"
                "       $4 as property_code, $5 as launch_date, $6 as unit_code,\n"
                "       $7 as unit_type, $8 as market_rent, $9 as tenant_code,\n"
                "       $10 as first_name, $11 as last_name, $12 as tenant_status,\n"
                "       $13 as lease_from, $14 as lease_to, $15 as move_in,\n"
                "       $16 as move_out, $17 as email, $18 as charge_code,\n"
                "       $19 as charge_type, $20 as charge_amount,\n"
                "       $21 as charge_from_date, $22 as charge_to_date\n"
                "FROM @my_validation_stage/all_charges_*.csv;\n\n"
                "-- 4. Validate: count pet charges\n"
                "SELECT charge_code, COUNT(*) as cnt, SUM(charge_amount::float) as total\n"
                "FROM sandbox.validation.api_charges\n"
                "WHERE charge_code ILIKE '%pet%'\n"
                "GROUP BY 1 ORDER BY 2 DESC;",
                language="sql",
            )

        st.divider()

        # ═══════════════════════════════════════════════════════════════
        # TABS: Charts  |  Missing Pet Rent Report
        # ═══════════════════════════════════════════════════════════════
        tab_charts, tab_next, tab_report, tab_docs = st.tabs([
            "Fee Collection Charts",
            "Summary",
            "Missing Pet Rent Report",
            "Documentation & SQL",
        ])

        # ─── TAB 1: Charts ───────────────────────────────────────────
        with tab_charts:
            analyze_btn = st.button("Analyze & Visualize", type="primary", use_container_width=True, key="analyze_btn")

            # On button click: parse the raw charge data and store it.
            # The time-series aggregation is done BELOW using the live slider
            # value so changing the display window never requires re-fetching.
            if analyze_btn:
                # Clear cached data on new analysis
                for _key in list(st.session_state.keys()):
                    if _key.startswith("missing_rent_") or _key in ("export_html", "exec_html"):
                        del st.session_state[_key]

                label = st.session_state.selection_label
                filtered = df[df['charge_code'].isin(selected_codes)].copy()

                # Parse dates & amounts once
                filtered["from_date"] = filtered["charge_from_date"].apply(parse_date)
                filtered["_raw_to_date"] = filtered["charge_to_date"].apply(parse_date)
                filtered["_move_out_dt"] = filtered["move_out"].apply(parse_date)
                filtered["_lease_to_dt"] = filtered["lease_to"].apply(parse_date)
                filtered["amount"] = pd.to_numeric(filtered["charge_amount"], errors="coerce").fillna(0)

                # Effective to_date: coalesce(charge_to_date, move_out, lease_to)
                # For Past tenants with no charge end date, use move_out or lease end
                # so their charges don't count in months after they left.
                def _effective_to_date(row):
                    charge_end = row["_raw_to_date"]
                    if charge_end is not None and not pd.isna(charge_end):
                        return charge_end
                    # Current tenants: no coalescing — they're still active
                    status = str(row.get("tenant_status", "")).strip().lower()
                    if status == "current":
                        return None
                    # Non-current: coalesce move_out → lease_to
                    move_out_dt = row["_move_out_dt"]
                    if move_out_dt is not None and not pd.isna(move_out_dt):
                        return move_out_dt
                    lease_to_dt = row["_lease_to_dt"]
                    if lease_to_dt is not None and not pd.isna(lease_to_dt):
                        return lease_to_dt
                    # Past with no dates at all — treat as active (shouldn't happen often)
                    return None
                filtered["to_date"] = filtered.apply(_effective_to_date, axis=1)

                # ── Entrata: deduplicate charges (same charge × multiple customers) ──
                # Charges belong to the lease, not individual customers.  Every
                # customer row for the same lease gets identical charges.  For
                # revenue aggregation we keep only one copy per unique charge.
                if "_entrata_charge_dedup_key" in filtered.columns:
                    _pre = len(filtered)

                    # Sort so active customers (current/past/notice) come first.
                    # After dedup (keep="first"), the surviving row will represent
                    # an active customer if one exists on the lease.  If ALL
                    # customers are cancelled/applicant, the surviving row will
                    # have that status → we filter it out below.
                    _ACTIVE_STATUSES = {"current", "past", "notice"}
                    filtered["_status_sort"] = filtered["tenant_status"].apply(
                        lambda s: 0 if str(s).strip().lower() in _ACTIVE_STATUSES else 1
                    )
                    filtered = filtered.sort_values("_status_sort", kind="stable")

                    # Drop rows where the dedup key is non-empty and duplicated
                    mask_has_key = filtered["_entrata_charge_dedup_key"].astype(str).str.strip().ne("")
                    dedup_rows = filtered[mask_has_key].drop_duplicates(
                        subset=["_entrata_charge_dedup_key"], keep="first"
                    )
                    non_dedup_rows = filtered[~mask_has_key]
                    filtered = pd.concat([dedup_rows, non_dedup_rows], ignore_index=True)
                    filtered = filtered.drop(columns=["_status_sort"], errors="ignore")
                    _deduped = _pre - len(filtered)

                    # Exclude charges where ALL customers on the lease are
                    # cancelled/applicant — these aren't real revenue.
                    _INACTIVE_STATUSES = {"cancelled", "applicant", "denied", "future"}
                    _pre_inactive = len(filtered)
                    filtered = filtered[
                        ~filtered["tenant_status"].apply(
                            lambda s: str(s).strip().lower() in _INACTIVE_STATUSES
                        )
                    ]
                    _dropped_inactive = _pre_inactive - len(filtered)

                    _info_parts = []
                    if _deduped > 0:
                        _info_parts.append(
                            f"Deduplicated {_deduped:,} shared-lease charge rows"
                        )
                    if _dropped_inactive > 0:
                        _info_parts.append(
                            f"Excluded {_dropped_inactive:,} charges from fully cancelled/applicant leases"
                        )
                    if _info_parts:
                        st.caption(" · ".join(_info_parts) + ".")

                # Extract launch dates per property (once)
                launch_dates = {}
                for _, row in filtered.iterrows():
                    prop = row['property_name']
                    launch = row.get('launch_date')
                    if prop not in launch_dates and launch is not None:
                        try:
                            if pd.isna(launch):
                                continue
                        except (TypeError, ValueError):
                            pass
                        if isinstance(launch, str) and launch.strip():
                            try:
                                launch_dates[prop] = datetime.strptime(str(launch)[:10], "%Y-%m-%d")
                            except:
                                pass
                        elif hasattr(launch, 'year'):
                            try:
                                # Verify year/month are real numbers (not NaN)
                                int(launch.year)
                                launch_dates[prop] = launch
                            except (ValueError, TypeError, OverflowError):
                                pass

                # Store the parsed rows — aggregation happens dynamically below
                charge_cols = [
                    "property_id", "property_name", "from_date", "to_date", "amount",
                    "tenant_code", "first_name", "last_name",
                    "tenant_status", "charge_code", "charge_type",
                    "email", "move_in", "move_out", "lease_from", "lease_to",
                    "_raw_to_date", "_move_out_dt", "_lease_to_dt",
                ]
                # Only keep columns that exist (defensive)
                charge_cols = [c for c in charge_cols if c in filtered.columns]
                st.session_state.chart_data = {
                    "parsed_charges": filtered[charge_cols].to_dict("records"),
                    "launch_dates": launch_dates,
                    "label": label,
                    "filtered_csv": filtered.to_csv(index=False),
                }

            # ── Dynamically aggregate using the CURRENT slider value ────
            if "chart_data" in st.session_state and st.session_state.chart_data is not None:
                cd = st.session_state.chart_data
                launch_dates = cd["launch_dates"]
                label = cd["label"]

                # Build the display window from the actual data range
                today = datetime.now()
                window_end = datetime(today.year, today.month, 1)

                # Find the earliest charge date in the data
                _earliest = None
                for rec in cd["parsed_charges"]:
                    fd = rec.get("from_date")
                    if fd is not None and not pd.isna(fd):
                        fd_clean = datetime(int(fd.year), int(fd.month), 1)
                        if _earliest is None or fd_clean < _earliest:
                            _earliest = fd_clean
                window_start = _earliest if _earliest else datetime(today.year - 5, today.month, 1)
                # IMPORTANT: strip time components so month keys are midnight-aligned
                window_start = datetime(window_start.year, window_start.month, 1)
                months = [m.to_pydatetime() for m in pd.date_range(start=window_start, end=window_end, freq='MS')]

                # ── Classify charge codes as recurring vs one-time ──────────
                # Entrata: use explicit `frequency` field when available.
                # Yardi: infer from median date span per (property, charge_code).
                #   Median span > 60 days → recurring (charged monthly)
                #   Median span ≤ 60 days → one-time (deposit/fee)
                # One-time charges only count in their from_date month.
                _has_frequency = any('frequency' in rec for rec in cd["parsed_charges"])
                _code_class = {}  # {(property_name, charge_code): "recurring" | "onetime"}

                # Group records by (property_name, charge_code)
                _by_prop_code = defaultdict(list)
                for rec in cd["parsed_charges"]:
                    key = (rec["property_name"], rec["charge_code"])
                    _by_prop_code[key].append(rec)

                for (pname, code), recs in _by_prop_code.items():
                    if _has_frequency:
                        freqs = [str(r.get('frequency', '')).strip().lower() for r in recs if r.get('frequency')]
                        onetime_count = sum(1 for f in freqs if f == 'one-time')
                        monthly_count = sum(1 for f in freqs if f in ('monthly', 'recurring'))
                        _code_class[(pname, code)] = "onetime" if onetime_count > monthly_count else "recurring"
                    else:
                        spans = []
                        for r in recs:
                            f, t = r.get("from_date"), r.get("_raw_to_date") if "_raw_to_date" in r else r.get("to_date")
                            if f and t and not pd.isna(f) and not pd.isna(t):
                                try:
                                    spans.append((t - f).days)
                                except (TypeError, AttributeError):
                                    pass
                        # No valid date spans → assume recurring (missing dates ≠ one-time)
                        median_span = float(np.median(spans)) if spans else None
                        if median_span is None:
                            _code_class[(pname, code)] = "recurring"
                        else:
                            _code_class[(pname, code)] = "recurring" if median_span > 60 else "onetime"

                # Apply user overrides (from the charge type classification UI)
                _user_overrides = st.session_state.get("charge_type_overrides", {})
                if _user_overrides:
                    for (pname, code) in list(_code_class.keys()):
                        if code in _user_overrides:
                            _code_class[(pname, code)] = _user_overrides[code]

                # Aggregate charges into monthly buckets
                monthly_portfolio = defaultdict(float)
                monthly_portfolio_count = defaultdict(int)
                monthly_by_prop = defaultdict(lambda: defaultdict(float))

                for rec in cd["parsed_charges"]:
                    prop = rec["property_name"]
                    amt = rec["amount"]
                    from_dt = rec["from_date"]
                    to_dt = rec["to_date"]

                    if from_dt is None or pd.isna(from_dt) or amt <= 0:
                        continue

                    charge_start = datetime(int(from_dt.year), int(from_dt.month), 1)

                    # One-time charges (deposits/fees): only count in the from_date month
                    charge_type = _code_class.get((prop, rec.get("charge_code")), "recurring")
                    if charge_type == "onetime":
                        charge_end = charge_start
                    elif to_dt is not None and not pd.isna(to_dt) and isinstance(to_dt, datetime):
                        charge_end = datetime(int(to_dt.year), int(to_dt.month), 1)
                    else:
                        charge_end = window_end

                    for month in months:
                        if charge_start <= month <= charge_end:
                            monthly_portfolio[month] += amt
                            monthly_portfolio_count[month] += 1
                            monthly_by_prop[prop][month] += amt

                # Convert defaultdicts to regular dicts for consistency
                monthly_portfolio = dict(monthly_portfolio)
                monthly_portfolio_count = dict(monthly_portfolio_count)
                monthly_by_prop = {p: dict(v) for p, v in monthly_by_prop.items()}

                # Compute pre/post launch analysis
                launch_analysis = compute_launch_analysis(monthly_by_prop, months, launch_dates)

                st.header("Fee Collection Analysis")

                latest_month = months[-1]

                # ── Property funnel — consistent cascade ──────────────
                _launch_in_data_funnel = {p: d for p, d in launch_dates.items() if p in monthly_by_prop}
                _funnel_comparable = {
                    p: a for p, a in launch_analysis.items()
                    if a["n_pre"] > 0 and a.get("baseline_reliable", True) and a.get("baseline_meaningful", True)
                } if launch_analysis else {}
                _render_property_funnel(
                    n_total=st.session_state.get("total_parent_props") or None,
                    n_api=st.session_state.get("api_props_count") or None,
                    n_with_charges=len(monthly_by_prop),
                    n_with_launch=len(_launch_in_data_funnel),
                    n_comparable=len(_funnel_comparable),
                )

                # Compute a SINGLE sort order (by total revenue, descending)
                # shared across Fee Revenue, Unit Adoption, and Resident Adoption
                prop_totals = {p: sum(monthly_by_prop[p].values()) for p in monthly_by_prop}
                sorted_props = sorted(prop_totals.keys(), key=lambda p: prop_totals[p], reverse=True)

                # Pre-fetch compliance data once (shared by KPIs and charts)
                pid_lookup = _build_property_id_lookup(cd["parsed_charges"])
                all_pids = list(set(pid_lookup.values()))
                comp_data = {}
                if all_pids:
                    comp_data = fetch_compliance_data(tuple(sorted(all_pids)))

                # ── Chart overlay controls ──────────────────────────────
                overlay_col1, overlay_col2 = st.columns([3, 2])
                with overlay_col1:
                    adoption_overlay = st.radio(
                        "Adoption overlay:",
                        ["None", "Unit Adoption %", "Resident Adoption %"],
                        index=0,
                        horizontal=True,
                        key="adoption_overlay",
                        help="Overlay an adoption trend line (purple) on the revenue bars to see the correlation "
                             "between adoption going up and revenue going up.",
                    )
                with overlay_col2:
                    show_missing_rent = st.toggle(
                        "Show uncollected pet rent",
                        value=False,
                        key="show_missing_rent",
                        help="Show estimated uncollected pet rent (orange) from confirmed tenants with active pet screening "
                             "stacked on top of collected revenue.",
                    )
                    show_suspected = st.toggle(
                        "Show suspected undisclosed",
                        value=False,
                        key="show_suspected",
                        help="Show estimated revenue from suspected undisclosed pets (red, stacked). "
                             "These are residents who started a profile but abandoned it, or had an "
                             "unresolved assistance request — signals they likely have a pet.",
                    )

                # Derive overlay mode
                _overlay = None
                _overlay_mode_label = None
                if adoption_overlay == "Unit Adoption %":
                    _overlay = "unit"
                    _overlay_mode_label = "Unit"
                elif adoption_overlay == "Resident Adoption %":
                    _overlay = "resident"
                    _overlay_mode_label = "Resident"

                # ── Fetch missing pet rent data if toggle is on ──
                # We cache the Snowflake profiles, but recompute monthly missing
                # whenever the slider changes (months change) so the bars are correct.
                _missing_rent_data = {}
                if show_missing_rent:
                    prop_ids = st.session_state.get("property_ids", [])
                    if prop_ids and selected_codes:
                        # Cache the profiles from Snowflake; recompute monthly when slider changes
                        cache_key = f"missing_rent_{hash(tuple(sorted(selected_codes)))}"
                        if cache_key not in st.session_state:
                            with st.spinner("Matching PetScreening tenants to charge data..."):
                                _missing_rent_data = fetch_missing_pet_rent_by_property(
                                    df, selected_codes, prop_ids, launch_dates, months,
                                )
                            st.session_state[cache_key] = _missing_rent_data
                        else:
                            # Re-use cached result but recalculate if display window changed
                            cached = st.session_state[cache_key]
                            # Check if months match; if not, refetch with new window
                            sample_key = next(iter(cached), None)
                            if sample_key and set(cached[sample_key].get("monthly_missing", {}).keys()) != set(months):
                                with st.spinner("Updating missing rent for new time window..."):
                                    _missing_rent_data = fetch_missing_pet_rent_by_property(
                                        df, selected_codes, prop_ids, launch_dates, months,
                                    )
                                st.session_state[cache_key] = _missing_rent_data
                            else:
                                _missing_rent_data = cached
                else:
                    # Clear cached data when toggle is off
                    for k in list(st.session_state.keys()):
                        if k.startswith("missing_rent_") and isinstance(st.session_state[k], dict):
                            del st.session_state[k]

                # ── Fetch suspected undisclosed data if toggle is on ──
                _suspected_data = {}
                if show_suspected:
                    prop_ids = st.session_state.get("property_ids", [])
                    if prop_ids and selected_codes:
                        cache_key_s = f"suspected_{hash(tuple(sorted(selected_codes)))}"
                        if cache_key_s not in st.session_state:
                            with st.spinner("Identifying suspected undisclosed pets..."):
                                _suspected_data = fetch_suspected_undisclosed_by_property(
                                    df, selected_codes, prop_ids, launch_dates, months,
                                )
                            st.session_state[cache_key_s] = _suspected_data
                        else:
                            cached_s = st.session_state[cache_key_s]
                            sample_key_s = next(iter(cached_s), None)
                            if sample_key_s and set(cached_s[sample_key_s].get("monthly_missing", {}).keys()) != set(months):
                                with st.spinner("Updating suspected data for new time window..."):
                                    _suspected_data = fetch_suspected_undisclosed_by_property(
                                        df, selected_codes, prop_ids, launch_dates, months,
                                    )
                                st.session_state[cache_key_s] = _suspected_data
                            else:
                                _suspected_data = cached_s
                else:
                    for k in list(st.session_state.keys()):
                        if k.startswith("suspected_") and isinstance(st.session_state[k], dict):
                            del st.session_state[k]

                # ═══════════════════════════════════════════════════════════
                # KPI ROW 1: Launch impact metrics (always shown)
                # ═══════════════════════════════════════════════════════════

                if launch_analysis:
                    comparable = {p: a for p, a in launch_analysis.items()
                                  if a["n_pre"] > 0 and a.get("baseline_reliable", True) and a.get("baseline_meaningful", True)}
                    n_no_pre = len(launch_analysis) - len(comparable)

                    if comparable:
                        agg_diff_mo = sum(a["diff_monthly"] for a in comparable.values())
                        agg_diff = sum(a["diff_total"] for a in comparable.values())
                        sign_mo = "+" if agg_diff_mo >= 0 else ""
                        sign_t = "+" if agg_diff >= 0 else ""
                    else:
                        agg_diff = 0
                        agg_diff_mo = 0
                        sign_mo = ""
                        sign_t = ""

                    # Only count launch dates for properties that actually have charge data
                    _launch_in_data = {p: d for p, d in launch_dates.items() if p in monthly_by_prop}
                    n_with_launch = len(_launch_in_data)
                    n_in_analysis = len(launch_analysis)
                    n_comparable = len(comparable)
                    n_future = max(0, n_with_launch - n_in_analysis)
                    n_no_launch = max(0, len(monthly_by_prop) - n_with_launch)

                    lcol1, lcol2, lcol3 = st.columns(3)
                    lcol1.metric(
                        "Cumulative Pet Revenue Impact",
                        f"{sign_t}${agg_diff:,.0f}",
                        help="Sum of each comparable property's (Monthly Change × post months). "
                             "Matches the Total Change column in the table below."
                    )
                    lcol2.metric(
                        "Monthly Pet Revenue Change",
                        f"{sign_mo}${agg_diff_mo:,.0f}/mo",
                        help="Sum of each comparable property's Monthly Change. "
                             "You can verify: add up the Monthly Change column in the table below."
                    )

                    launch_detail = f"{n_comparable} comparable (pre & post data)"
                    if n_no_pre:
                        launch_detail += f"\n{n_no_pre} live before window (no pre data)"
                    if n_future:
                        launch_detail += f"\n{n_future} launched recently (no post data yet)"
                    if n_no_launch:
                        launch_detail += f"\n{n_no_launch} no launch date"

                    _total_props = len(st.session_state.get("property_ids", []))
                    lcol3.metric(
                        "Launch Dates",
                        f"{n_with_launch} of {_total_props} properties",
                        help=launch_detail,
                    )

                    caption_parts = [f"**{n_comparable}** properties have pre & post data for comparison."]
                    if n_no_pre:
                        caption_parts.append(f"**{n_no_pre}** launched before the window (excluded — no baseline).")
                    if n_future:
                        caption_parts.append(f"**{n_future}** launched too recently (no post-launch months yet).")
                    if n_no_launch:
                        caption_parts.append(f"**{n_no_launch}** have no launch date.")
                    st.caption(" ".join(caption_parts))

                    _is_entrata = st.session_state.get("pmc_system", "yardi") == "entrata"
                    if _is_entrata and launch_analysis:
                        n_unreliable = sum(1 for a in launch_analysis.values() if not a.get("baseline_reliable", True))
                        if n_unreliable > len(launch_analysis) / 2:
                            st.warning(
                                f"**Entrata data note:** {n_unreliable} of {len(launch_analysis)} properties "
                                f"have fewer than 3 months of pre-launch charge data. The Entrata API may not "
                                f"return full historical scheduled charges, making the before/after revenue "
                                f"comparison unreliable for this PMC. Post-launch trends are still accurate."
                            )

                # ── PetScreening impact methodology (always right below KPI row 1) ──
                with st.expander("How we calculate PetScreening impact — methodology & chart legend", expanded=False):
                    st.markdown("""
**Methodology**

For each property with a PetScreening launch date, we calculate the revenue impact by comparing
the average monthly pet fee revenue **before** and **after** launch.

| Metric | Formula | What it tells you |
|--------|---------|-------------------|
| **Before PetScreening (avg/mo)** | Average of up to 6 months before launch (uses whatever pre-launch data is available) | The property's baseline monthly pet fee collection before PetScreening was active — uses more data when available to smooth seasonal noise |
| **After PetScreening (avg/mo)** | Average of all completed post-launch months (excludes current partial month) | The property's average monthly pet fee collection since PetScreening launched |
| **Monthly Change** | Cumulative impact ÷ completed post months | Average monthly uplift since launch — mathematically consistent with cumulative total |
| **Total Change** | Sum(all post revenue) − (Pre avg × post months) | The actual observed cumulative revenue above the pre-launch baseline — real dollars, not projected |

**Reading the charts**

- <span style="color:#7D9BC1">**Blue bars**</span> = months **before** PetScreening was active
- <span style="color:#677848">**Green bars**</span> = months **after** PetScreening went live (including the launch month)
- <span style="color:#E2AB58">**Gold dotted line**</span> = the pre-launch monthly average (baseline from up to 6 months)
- <span style="color:#CF5A3F">**Red dashed line**</span> = the PetScreening launch date
- <span style="color:rgba(156,39,176,0.85)">**Purple line**</span> = adoption % overlay (when enabled)
- <span style="color:#DD7B45">**Orange bars (stacked)**</span> = estimated uncollected pet rent from confirmed tenants (when "Show uncollected pet rent" is on)
- <span style="color:#CF5A3F">**Red bars (stacked)**</span> = estimated uncollected from suspected undisclosed tenants (when "Show suspected undisclosed" is on)
- **Live** = property launched before the selected lookback window (all months are post-launch)
- **Grey bars** = no launch date available for this property

**Understanding the two toggles**

**Show uncollected pet rent (orange):** Shows revenue you're definitely not collecting from tenants
who have completed their PetScreening household pet screening but aren't being charged pet rent.

**Show suspected undisclosed (red):** Shows potential additional revenue from tenants who show signals
of having an undisclosed pet — abandoned screening, unresolved assistance requests, or suspicious no-pet
declarations. These are lower confidence but worth investigating.

**How we calculate both sets of bars (per month)**

1. We identify the relevant tenants (confirmed screening or suspected behavioral signals) not paying any of the selected charge codes
2. For each resident, we use their **actual lease dates** (move-in / move-out or lease start / lease end from the API) to determine which months they were living at the property
3. We only count months **on or after** the property's PetScreening launch date
4. We classify the selected charge codes as **recurring rent** or **one-time deposit** based on how paying tenants are being charged:
   - **Yardi:** inferred from median date span (> 60 days = recurring, ≤ 60 days = one-time)
   - **Entrata:** uses the explicit `frequency` field on each charge
   - **Recurring rent**: added to every month the missing tenant was active
   - **One-time deposit**: added only to the tenant's first active month
5. The estimated amount per missing tenant uses the average fee from tenants **who are paying** at that property

Both sets of bars **vary month to month** — they grow as more unpaid tenants move in, and shrink as tenants move out.
The subtitle on each chart shows "X unpaid" and/or "X suspected" badges.

**How launch month is handled**

The **launch month itself is counted as post-launch** (green). For example, if PetScreening launched
on Nov 19, the November bar is green because PetScreening was active for part of that month.
The pre-launch baseline uses up to 6 months *before* the launch month (e.g., May–Oct for a Nov launch).

**Important notes**

- The pre-launch baseline uses **up to 6 months** before the launch month (whatever data is available),
  giving a robust "business as usual" average that smooths out seasonal variation.
- The post-launch comparison uses **all completed post-launch months** (excluding the current partial month),
  reflecting the property's average performance since PetScreening launched.
- The cumulative impact ("Total Change") uses **actual observed revenue** — total post-launch revenue minus
  what the property would have collected at the pre-launch rate. This is real dollars, not a projection.
- Revenue changes may reflect factors beyond PetScreening (e.g., new units, rent adjustments, seasonal variation).
  This analysis measures the **observed change** in pet fee revenue coinciding with PetScreening's launch.
- Properties that launched before the lookback window have no pre-launch baseline available and are
  excluded from the impact calculation.
                    """, unsafe_allow_html=True)

                # ═══════════════════════════════════════════════════════════
                # KPI ROW 2: Revenue Opportunity at 100% (when overlay active)
                # ═══════════════════════════════════════════════════════════

                projected_100 = {}
                if _overlay and _overlay_mode_label:
                    _adoption_key = "unit_adoption" if _overlay == "unit" else "resident_adoption"
                    if comp_data:
                        for pname in sorted_props:
                            pid = pid_lookup.get(pname)
                            if not pid:
                                continue
                            prop_comp = comp_data.get(pid, {})
                            latest_adoption = None
                            for m in reversed(months):
                                entry = prop_comp.get(m)
                                if entry and entry.get(_adoption_key) is not None:
                                    latest_adoption = entry[_adoption_key] * 100
                                    break
                            latest_rev = monthly_by_prop.get(pname, {}).get(months[-1], 0) if months else 0
                            if latest_adoption and latest_adoption > 0 and latest_rev > 0:
                                projected_rev = latest_rev / (latest_adoption / 100)
                                projected_100[pname] = {
                                    "current_rev": latest_rev,
                                    "current_adoption": latest_adoption,
                                    "projected_rev_100": projected_rev,
                                    "additional_rev": projected_rev - latest_rev,
                                }

                    if projected_100:
                        total_current = sum(p["current_rev"] for p in projected_100.values())
                        total_projected = sum(p["projected_rev_100"] for p in projected_100.values())
                        total_additional = total_projected - total_current
                        avg_adoption = (
                            sum(p["current_adoption"] for p in projected_100.values())
                            / len(projected_100)
                        )

                        st.divider()
                        ic1, ic2, ic3 = st.columns(3)
                        ic1.metric(
                            "Current Monthly Pet-Related Revenue",
                            f"${total_current:,.0f}",
                            help="Sum of latest-month pet fee revenue for properties with adoption data.",
                        )
                        ic2.metric(
                            f"Projected at 100% {_overlay_mode_label} Adoption",
                            f"${total_projected:,.0f}",
                            delta=f"+${total_additional:,.0f}/mo",
                            delta_color="normal",
                            help="What the total monthly revenue would be if every property reached 100% adoption. "
                                 "Calculated as: current revenue ÷ current adoption rate.",
                        )
                        ic3.metric(
                            f"Avg {_overlay_mode_label} Adoption Now",
                            f"{avg_adoption:.1f}%",
                            help=f"Average {_overlay_mode_label.lower()}-level adoption "
                                 f"across {len(projected_100)} properties with data.",
                        )
                        st.caption(
                            f"Based on **{len(projected_100)}** properties with both pet fee revenue "
                            f"and {_overlay_mode_label.lower()}-level adoption data in the latest month."
                        )
                    elif comp_data:
                        st.info(f"No properties currently have both pet fee revenue **and** "
                                f"{_overlay_mode_label.lower()} adoption data to project.")

                if _overlay and _overlay_mode_label:
                    with st.expander(f"How we calculate Revenue Opportunity at 100% {_overlay_mode_label} Compliance", expanded=False):
                        st.markdown(f"""
**What is {_overlay_mode_label} Adoption?**

{'**Unit Adoption** = the percentage of units at a property that have at least one active PetScreening profile.' if _overlay == 'unit' else '**Resident Adoption** = the percentage of residents at a property that have created a PetScreening profile.'}

This data comes from the **Quarterly Business Review (QBR) reporting table** (`R_QUARTERLY_BUSINESS_REVIEW_REPORTING`),
which tracks adoption month-by-month for every property after their PetScreening launch date.

**How we calculate the projection**

The revenue projection answers: **"If this property reached 100% adoption, how much pet fee revenue could they collect?"**

For each property, we take two numbers from the **latest month**:

| Input | Source | Example |
|-------|--------|---------|
| **Current Monthly Pet-Related Revenue** | Sum of selected pet fee charge codes from Yardi (this app) | $5,000/mo |
| **Current {_overlay_mode_label} Adoption** | {'Active units ÷ Total units' if _overlay == 'unit' else 'Active users ÷ Total users'} from QBR table | 65% |

Then we calculate:

```
Projected Revenue at 100% = Current Revenue ÷ (Current Adoption / 100)
                          = $5,000 ÷ 0.65
                          = $7,692/mo

Additional Revenue = Projected − Current
                   = $7,692 − $5,000
                   = +$2,692/mo
```

**Why this works**: If a property earns $5,000/mo when only 65% of {'units' if _overlay == 'unit' else 'residents'} have completed screening,
then the average revenue per compliant {'unit' if _overlay == 'unit' else 'resident'} is $5,000 ÷ 65% = ~$76.92.
At 100% adoption, that same per-{'unit' if _overlay == 'unit' else 'resident'} rate would yield ~$7,692/mo.

**Important notes**

- This is a **linear extrapolation** — it assumes revenue scales proportionally with adoption.
  In reality, the last {'units' if _overlay == 'unit' else 'residents'} to comply may have fewer or no pets, so actual revenue may be lower.
- Only properties with **both** pet fee revenue **and** adoption data in the latest month are included.
- The <span style="color:rgba(156,39,176,0.85)">**purple line**</span> on each chart shows the adoption trend (right y-axis, 0–100%).
  When adoption goes up, you should see revenue (bars) go up too.
                        """, unsafe_allow_html=True)

                st.divider()

                # ═══════════════════════════════════════════════════════════
                # CHARTS — scrollable container
                # ═══════════════════════════════════════════════════════════

                # ── 3. Individual Property Trend Charts ─────────────────
                fig4 = None  # keep in scope for export
                if len(monthly_by_prop) > 0:
                    n_display_props = len(monthly_by_prop)
                    st.subheader("Individual Property Trends" if n_display_props > 1 else "Property Trend")

                    fig4 = build_individual_property_charts(
                        monthly_by_prop, months, launch_dates, label,
                        launch_analysis=launch_analysis,
                        overlay_mode=_overlay,
                        compliance_data=comp_data if _overlay else None,
                        prop_id_lookup=pid_lookup if _overlay else None,
                        missing_rent_data=_missing_rent_data,
                        show_missing_rent=show_missing_rent,
                        suspected_data=_suspected_data,
                        show_suspected=show_suspected,
                    )

                    # Scrollable container for many properties; plain for 1–4
                    use_scroll = n_display_props > 4
                    chart_container = st.container(height=900) if use_scroll else st.container()

                    with chart_container:
                        if fig4:
                            st.plotly_chart(fig4, use_container_width=True)

                    # ── Per-property projected revenue table (below charts) ──
                    if projected_100:
                        st.subheader(f"Projected Revenue by Property at 100% {_overlay_mode_label} Adoption")
                        st.caption(
                            f"Sorted by additional revenue opportunity. Based on latest-month revenue "
                            f"and {_overlay_mode_label.lower()}-level adoption for **{len(projected_100)}** properties."
                        )
                        proj_rows = []
                        for prop, p in sorted(projected_100.items(), key=lambda x: -x[1]["additional_rev"]):
                            short = prop.split(" - ", 1)[-1] if " - " in prop else prop
                            proj_rows.append({
                                "Property": short,
                                f"Current {_overlay_mode_label} Adoption": f"{p['current_adoption']:.1f}%",
                                "Current Revenue": f"${p['current_rev']:,.0f}",
                                "Projected (100%)": f"${p['projected_rev_100']:,.0f}",
                                "Additional Rev": f"+${p['additional_rev']:,.0f}/mo",
                            })
                        _render_table(pd.DataFrame(proj_rows))

                # ── 3b. Uncollected Pet Rent Summary (confirmed + suspected) ──
                _has_confirmed = show_missing_rent and _missing_rent_data
                _has_suspected = show_suspected and _suspected_data
                if _has_confirmed or _has_suspected:
                    st.divider()
                    latest_month = months[-1]
                    _total_fetched = len(st.session_state.get("property_ids", []))

                    # ── Confirmed stats ──
                    c_total = sum(v["missing_count"] for v in _missing_rent_data.values()) if _has_confirmed else 0
                    c_props = sum(1 for v in _missing_rent_data.values() if v["missing_count"] > 0) if _has_confirmed else 0
                    c_mo = sum(v["monthly_missing"].get(latest_month, 0) for v in _missing_rent_data.values()) if _has_confirmed else 0

                    # Split recurring vs one-time for confirmed
                    c_recurring_mo = 0
                    c_onetime_total = 0
                    if _has_confirmed:
                        for _v in _missing_rent_data.values():
                            _cnt = _v.get("missing_count", 0)
                            if _cnt == 0:
                                continue
                            _rec = _v.get("avg_recurring", 0)
                            _ot = _v.get("avg_onetime", 0)
                            c_recurring_mo += _cnt * _rec
                            c_onetime_total += _cnt * _ot

                    # ── Suspected stats ──
                    s_total = sum(v["missing_count"] for v in _suspected_data.values()) if _has_suspected else 0
                    s_props = sum(1 for v in _suspected_data.values() if v["missing_count"] > 0) if _has_suspected else 0
                    s_mo = sum(v["monthly_missing"].get(latest_month, 0) for v in _suspected_data.values()) if _has_suspected else 0

                    # ── Combined ──
                    combined_total = c_total + s_total
                    combined_mo = c_mo + s_mo
                    combined_props = len(set(
                        [p for p, v in _missing_rent_data.items() if v["missing_count"] > 0] if _has_confirmed else []
                    ) | set(
                        [p for p, v in _suspected_data.items() if v["missing_count"] > 0] if _has_suspected else []
                    ))

                    # ── Forward-looking impact ──
                    _annual_recurring = c_recurring_mo * 12
                    _annual_impact = _annual_recurring + c_onetime_total
                    _cap_rate = 0.05
                    _value_impact = _annual_impact / _cap_rate if _annual_impact > 0 else 0

                    st.subheader("Uncollected Pet Rent Summary")

                    # ── Confirmed: the billing correction ──
                    if _has_confirmed and c_total > 0:
                        st.markdown("**Confirmed** — tenants with active PetScreening profiles who aren't paying pet rent")
                        cc1, cc2, cc3 = st.columns(3)
                        cc1.metric(
                            "Tenants Not Paying",
                            f"{c_total:,}",
                            help=f"Active tenants with household profiles not being charged pet rent, across {c_props} properties.",
                        )
                        cc2.metric(
                            "Missing Monthly Pet Rent",
                            f"${c_recurring_mo:,.0f}/mo",
                            help=f"Recurring monthly pet rent not collected. Based on each property's avg fee from paying tenants.",
                        )
                        cc3.metric(
                            "Annual Revenue Impact",
                            f"${_annual_impact:,.0f}/yr",
                            help=f"Pet rent: ${c_recurring_mo:,.0f}/mo x 12 = ${_annual_recurring:,.0f}/yr"
                                 + (f"  |  One-time fees: ${c_onetime_total:,.0f}" if c_onetime_total > 0 else "")
                                 + f"  |  Total: ${_annual_impact:,.0f}/yr",
                        )
                        if _value_impact > 0:
                            st.caption(
                                f"At a 5% cap rate, this ${_annual_impact:,.0f}/yr represents "
                                f"**${_value_impact:,.0f} in unrealized property value**. "
                                f"This is a billing correction, not a sales effort."
                            )
                    elif _has_confirmed:
                        st.markdown("**Confirmed** — all profiled tenants are paying pet rent ✓")

                    # ── Suspected: the undisclosed opportunity ──
                    if _has_suspected and s_total > 0:
                        st.markdown("---")
                        st.markdown("**Suspected Undisclosed** — tenants who started screening but abandoned, had unresolved assistance requests, or declared no-pet after starting an assistance profile")

                        # Compute suspected recurring/one-time
                        s_recurring_mo = 0
                        s_onetime_total = 0
                        if _has_suspected:
                            for _v in _suspected_data.values():
                                _cnt = _v.get("missing_count", 0)
                                if _cnt == 0:
                                    continue
                                s_recurring_mo += _cnt * _v.get("avg_recurring", 0)
                                s_onetime_total += _cnt * _v.get("avg_onetime", 0)
                        s_annual_recurring = s_recurring_mo * 12
                        s_annual_impact = s_annual_recurring + s_onetime_total

                        sc1, sc2, sc3 = st.columns(3)
                        sc1.metric(
                            "Suspected Tenants",
                            f"{s_total:,}",
                            help=f"Tenants showing signals of undisclosed pets, across {s_props} properties.",
                        )
                        sc2.metric(
                            "Potential Pet Rent",
                            f"${s_recurring_mo:,.0f}/mo",
                            help="Estimated recurring pet rent if these tenants were confirmed and charged.",
                        )
                        sc3.metric(
                            "Potential Annual Impact",
                            f"${s_annual_impact:,.0f}/yr",
                            help=f"Estimated annual revenue if suspected undisclosed pets are confirmed and charged.",
                        )

                    # ── Combined: total opportunity ──
                    if _has_confirmed and c_total > 0 and _has_suspected and s_total > 0:
                        st.markdown("---")
                        _combined_recurring_mo = c_recurring_mo + s_recurring_mo
                        _combined_onetime = c_onetime_total + s_onetime_total
                        _combined_annual = _combined_recurring_mo * 12 + _combined_onetime
                        _combined_value = _combined_annual / _cap_rate if _combined_annual > 0 else 0
                        st.markdown(
                            f"**Combined opportunity:** If all confirmed and suspected tenants were charged, "
                            f"that would be **${_combined_recurring_mo:,.0f}/mo** in pet rent"
                            + (f" + ${_combined_onetime:,.0f} in one-time fees" if _combined_onetime > 0 else "")
                            + f" — **${_combined_annual:,.0f}/yr** in total revenue, "
                            f"adding **${_combined_value:,.0f}** in property value at a 5% cap rate."
                        )

                    # ── "Show the Math" transparency section ──
                    if _has_confirmed and c_total > 0 and c_recurring_mo > 0:
                        _avg_fee_overall = c_recurring_mo / c_total if c_total > 0 else 0

                        # Gather per-property fee details
                        _fee_ranges = []
                        for _p, _v in _missing_rent_data.items():
                            _rec = _v.get("avg_recurring", 0)
                            if _rec > 0 and _v.get("missing_count", 0) > 0:
                                _fee_ranges.append(_rec)
                        _fee_min = min(_fee_ranges) if _fee_ranges else 0
                        _fee_max = max(_fee_ranges) if _fee_ranges else 0

                        with st.expander("How we calculate these numbers", expanded=False):
                            st.markdown(f"""
**Monthly uncollected pet rent:**

| | |
|---|---|
| Tenants with pets, not paying selected charges | **{c_total:,}** |
| x Avg pet rent at their property | **${_avg_fee_overall:,.0f}/mo** |
| **= Missing pet rent** | **${c_recurring_mo:,.0f}/mo** |
""")
                            if c_onetime_total > 0:
                                _avg_ot_overall = c_onetime_total / c_total if c_total > 0 else 0
                                st.markdown(f"""
**One-time fees not collected:**

| | |
|---|---|
| Same {c_total:,} tenants | x ${_avg_ot_overall:,.0f} avg one-time fee |
| **= One-time fees** | **${c_onetime_total:,.0f}** |
""")
                            st.markdown(f"""
**Annual revenue impact:**

| | |
|---|---|
| Monthly pet rent | ${c_recurring_mo:,.0f} x 12 = **${_annual_recurring:,.0f}/yr** |
| One-time fees | **${c_onetime_total:,.0f}** |
| **Total annual impact** | **${_annual_impact:,.0f}** |

**Unrealized property value** (at 5% cap rate):

| | |
|---|---|
| Annual impact | ${_annual_impact:,.0f} |
| / 5% cap rate | |
| **= Unrealized value** | **${_value_impact:,.0f}** |

> The avg fee varies by property (${_fee_min:,.0f} -- ${_fee_max:,.0f}/mo). We use each property's actual average from paying tenants, not a flat number.
""")
                            # Show a few example properties
                            _example_rows = []
                            for _p in sorted(
                                _missing_rent_data.keys(),
                                key=lambda p: _missing_rent_data[p].get("missing_count", 0),
                                reverse=True,
                            )[:5]:
                                _v = _missing_rent_data[_p]
                                if _v.get("missing_count", 0) == 0:
                                    continue
                                _short = _p.split(" - ", 1)[-1] if " - " in _p else _p
                                _rec = _v.get("avg_recurring", 0)
                                _ot = _v.get("avg_onetime", 0)
                                _cnt = _v["missing_count"]
                                _mo_val = _cnt * _rec
                                _ot_val = _cnt * _ot
                                _row = {
                                    "Property": _short,
                                    "Missing Tenants": _cnt,
                                    "Avg Pet Rent": f"${_rec:,.0f}/mo",
                                    "= Monthly": f"${_mo_val:,.0f}/mo",
                                }
                                if c_onetime_total > 0:
                                    _row["Avg One-Time"] = f"${_ot:,.0f}"
                                    _row["= One-Time"] = f"${_ot_val:,.0f}"
                                _example_rows.append(_row)
                            if _example_rows:
                                st.markdown("**Example properties:**")
                                _render_table(pd.DataFrame(_example_rows))

                    # Per-property breakdown table
                    with st.expander(f"Uncollected pet rent by property ({combined_props} properties)", expanded=False):
                        mr_rows = []
                        all_pnames = set(
                            list(_missing_rent_data.keys() if _has_confirmed else []) +
                            list(_suspected_data.keys() if _has_suspected else [])
                        )
                        for pname in sorted(all_pnames, key=lambda p: -(
                            # Sort by annual impact (recurring*12 + one-time)
                            (lambda ci, si: (
                                ci.get("missing_count", 0) * ci.get("avg_recurring", 0) * 12
                                + ci.get("missing_count", 0) * ci.get("avg_onetime", 0)
                                + si.get("missing_count", 0) * si.get("avg_recurring", 0) * 12
                                + si.get("missing_count", 0) * si.get("avg_onetime", 0)
                            ))(
                                _missing_rent_data.get(p, {}) if _has_confirmed else {},
                                _suspected_data.get(p, {}) if _has_suspected else {},
                            )
                        )):
                            c_info = _missing_rent_data.get(pname, {}) if _has_confirmed else {}
                            s_info = _suspected_data.get(pname, {}) if _has_suspected else {}
                            c_cnt = c_info.get("missing_count", 0)
                            s_cnt = s_info.get("missing_count", 0)
                            if c_cnt == 0 and s_cnt == 0:
                                continue
                            short = pname.split(" - ", 1)[-1] if " - " in pname else pname
                            _row = {"Property": short}
                            if _has_confirmed:
                                _row["Confirmed"] = c_cnt
                            if _has_suspected:
                                _row["Suspected"] = s_cnt
                            _row["Total Tenants"] = c_cnt + s_cnt
                            # Pet rent per month
                            _p_rec_mo = c_cnt * c_info.get("avg_recurring", 0) + s_cnt * s_info.get("avg_recurring", 0)
                            _row["Pet Rent/Mo"] = f"${_p_rec_mo:,.0f}"
                            # One-time fees
                            _p_ot = c_cnt * c_info.get("avg_onetime", 0) + s_cnt * s_info.get("avg_onetime", 0)
                            if c_onetime_total > 0:
                                _row["One-Time Fees"] = f"${_p_ot:,.0f}"
                            # Annual impact
                            _p_annual = _p_rec_mo * 12 + _p_ot
                            _row["Annual Impact"] = f"${_p_annual:,.0f}"
                            # Property value at 5% cap
                            _p_value = _p_annual / 0.05 if _p_annual > 0 else 0
                            _row["Value Impact"] = f"${_p_value:,.0f}"
                            mr_rows.append(_row)
                        if mr_rows:
                            _render_table(pd.DataFrame(mr_rows))

                # ── 4. Current Snapshot Bar Chart ───────────────────────
                fig3 = build_current_snapshot_chart(monthly_by_prop, months, label)
                if fig3:
                    st.divider()
                    st.plotly_chart(fig3, use_container_width=True)

                # ── 5. Export Interactive Report ──────────────────────
                st.divider()
                export_col1, export_col2, export_col3 = st.columns([3, 1, 1])
                with export_col1:
                    st.markdown("**Export Interactive Report** — Share a fully interactive HTML report "
                                "with clients. Charts are scrollable, hoverable, and zoomable.")
                with export_col2:
                    if st.button("Generate Report", use_container_width=True,
                                 help="Build the HTML report with current charts & overlay settings"):
                        report_html = generate_html_report(
                            label=label,
                            fig_individual=fig4,
                            fig_snapshot=fig3,
                            launch_analysis=launch_analysis,
                            monthly_by_prop=monthly_by_prop,
                            months=months,
                            launch_dates=launch_dates,
                            projected_100=projected_100 if projected_100 else None,
                            overlay_mode_label=_overlay_mode_label,
                            missing_rent_data=_missing_rent_data if show_missing_rent else None,
                            show_missing_rent=show_missing_rent,
                            total_properties_fetched=len(st.session_state.get("property_ids", [])),
                        )
                        st.session_state["export_html"] = report_html
                        st.session_state["export_label"] = label
                        st.toast("Report ready for download!")
                with export_col3:
                    if "export_html" in st.session_state and st.session_state["export_html"]:
                        safe_name = st.session_state.get("export_label", "report").replace(" ", "_").replace("/", "-")
                        st.download_button(
                            label="Download HTML",
                            data=st.session_state["export_html"],
                            file_name=f"PetScreening_Report_{safe_name}_{datetime.now().strftime('%Y%m%d')}.html",
                            mime="text/html",
                            use_container_width=True,
                        )
                    else:
                        st.button("Download HTML", disabled=True, use_container_width=True,
                                  help="Click 'Generate Report' first")

                # ── 5b. AR Transactions: Scheduled vs Actual (Entrata only) ──
                _is_entrata_view = st.session_state.get("pmc_system", "yardi") == "entrata"
                ar_df = st.session_state.get("ar_charges_df")
                if _is_entrata_view and ar_df is not None and not ar_df.empty:
                    st.divider()
                    with st.expander("Scheduled vs Actual Revenue (AR Transactions)", expanded=False):
                        st.caption(
                            "Compares **scheduled charges** (what the lease says should be billed) "
                            "with **AR transactions** (what was actually posted to the ledger). "
                            "AR data comes from Entrata's `includeArTransactions` response."
                        )

                        ar_df_work = ar_df.copy()
                        ar_df_work['_amt'] = pd.to_numeric(ar_df_work['amount'], errors='coerce').fillna(0)
                        ar_df_work['_paid'] = pd.to_numeric(ar_df_work['amount_paid'], errors='coerce').fillna(0)

                        _ar_post_dates = pd.to_datetime(ar_df_work['post_month'], errors='coerce', format='mixed')
                        ar_df_work['_post_month_dt'] = _ar_post_dates.apply(
                            lambda d: datetime(d.year, d.month, 1) if pd.notna(d) else None
                        )
                        ar_in_window = ar_df_work[
                            ar_df_work['_post_month_dt'].notna()
                            & ar_df_work['_post_month_dt'].between(months[0], months[-1])
                        ]

                        ar_monthly_portfolio = defaultdict(float)
                        ar_monthly_by_prop = defaultdict(lambda: defaultdict(float))
                        for _, row in ar_in_window.iterrows():
                            m = row['_post_month_dt']
                            ar_monthly_portfolio[m] += row['_amt']
                            ar_monthly_by_prop[row['property_name']][m] += row['_amt']

                        # Summary metrics
                        total_ar = ar_in_window['_amt'].sum()
                        total_ar_paid = ar_in_window['_paid'].sum()
                        total_scheduled = sum(monthly_portfolio.get(m, 0) for m in months)

                        mc1, mc2, mc3, mc4 = st.columns(4)
                        mc1.metric("Scheduled Revenue", f"${total_scheduled:,.0f}",
                                   help="Total from scheduled charges over the display window")
                        mc2.metric("Actual Posted (AR)", f"${total_ar:,.0f}",
                                   help="Total from AR transactions posted over the display window")
                        mc3.metric("AR Collected", f"${total_ar_paid:,.0f}",
                                   help="Of the posted amount, how much has been paid")
                        _diff_pct = ((total_ar / total_scheduled - 1) * 100) if total_scheduled > 0 else 0
                        mc4.metric("Variance", f"{_diff_pct:+.1f}%",
                                   help="AR posted vs Scheduled: positive means actual > scheduled")

                        # Monthly comparison table
                        compare_rows = []
                        for m in months:
                            sched = monthly_portfolio.get(m, 0)
                            actual = ar_monthly_portfolio.get(m, 0)
                            compare_rows.append({
                                "Month": m.strftime("%b %Y"),
                                "Scheduled ($)": f"{sched:,.2f}",
                                "Actual AR ($)": f"{actual:,.2f}",
                                "Difference ($)": f"{(actual - sched):,.2f}",
                                "Variance (%)": f"{((actual / sched - 1) * 100):+.1f}%" if sched > 0 else "N/A",
                            })
                        compare_df = pd.DataFrame(compare_rows)
                        st.markdown("**Monthly Comparison**")
                        st.table(compare_df)

                        # Per-property breakdown
                        if ar_monthly_by_prop:
                            st.subheader("Per-Property: Scheduled vs Actual")
                            prop_compare = []
                            for prop in sorted(monthly_by_prop.keys()):
                                sched_total = sum(monthly_by_prop.get(prop, {}).get(m, 0) for m in months)
                                ar_total = sum(ar_monthly_by_prop.get(prop, {}).get(m, 0) for m in months)
                                short = prop.split(" - ", 1)[-1] if " - " in prop else prop
                                prop_compare.append({
                                    "Property": short,
                                    "Scheduled ($)": f"{sched_total:,.2f}",
                                    "Actual AR ($)": f"{ar_total:,.2f}",
                                    "Difference ($)": f"{(ar_total - sched_total):,.2f}",
                                    "Variance (%)": f"{((ar_total / sched_total - 1) * 100):+.1f}%" if sched_total > 0 else ("N/A" if ar_total == 0 else "New"),
                                })
                            prop_compare_df = pd.DataFrame(prop_compare)
                            st.table(prop_compare_df)

                        st.caption(
                            f"AR data: **{len(ar_in_window):,}** pet-related transactions in window. "
                            f"Total AR rows (all time): **{len(ar_df):,}**"
                        )

                        # ── Raw AR Transactions (individual posted charges) ──
                        st.markdown("---")
                        st.subheader("AR Transactions (Individual Posted Charges)")
                        st.caption(
                            "Each row = one actual charge posted to the ledger. "
                            "This is what the property management system actually billed, "
                            "not what the lease says *should* be billed."
                        )

                        # Build a clean display table from ar_df
                        _ar_display = ar_df.copy()
                        _ar_display['_amt'] = pd.to_numeric(_ar_display['amount'], errors='coerce').fillna(0)
                        _ar_display['_paid'] = pd.to_numeric(_ar_display['amount_paid'], errors='coerce').fillna(0)
                        _ar_display['_bal'] = pd.to_numeric(_ar_display['balance_due'], errors='coerce').fillna(0)

                        _ar_table_rows = []
                        for _, _r in _ar_display.iterrows():
                            _ar_table_rows.append({
                                "Unit": _r.get('unit', ''),
                                "Lease ID": _r.get('lease_id', ''),
                                "Charge": _r.get('charge_code_name', ''),
                                "Post Date": str(_r.get('post_date', ''))[:10],
                                "Post Month": str(_r.get('post_month', ''))[:7],
                                "Amount": f"${_r['_amt']:,.2f}",
                                "Paid": f"${_r['_paid']:,.2f}",
                                "Balance": f"${_r['_bal']:,.2f}",
                                "Description": str(_r.get('description', ''))[:50],
                            })

                        if _ar_table_rows:
                            _ar_table_df = pd.DataFrame(_ar_table_rows)

                            # Month filter
                            _ar_months = sorted(set(r["Post Month"] for r in _ar_table_rows if r["Post Month"]))
                            if _ar_months:
                                _sel_ar_month = st.selectbox(
                                    "Filter by post month:",
                                    ["All"] + _ar_months,
                                    index=0,
                                    key="ar_month_filter",
                                )
                                if _sel_ar_month != "All":
                                    _ar_table_df = _ar_table_df[_ar_table_df["Post Month"] == _sel_ar_month]

                            st.markdown(f"**{len(_ar_table_df)}** transactions shown")
                            _render_table(_ar_table_df, height=400)

                            st.download_button(
                                "Download AR transactions",
                                data=_ar_display.to_csv(index=False),
                                file_name=f"ar_transactions_{datetime.now().strftime('%Y%m%d')}.csv",
                                mime="text/csv",
                                key="dl_ar_inline",
                            )
                        else:
                            st.info("No AR transactions found for this property.")

                # ── 6. Collapsible Tables Section ───────────────────────
                st.divider()
                with st.expander("Show detailed tables (PetScreening impact breakdown & monthly revenue)", expanded=False):

                    # -- Impact Breakdown Table --
                    if launch_analysis:
                        st.subheader("PetScreening Impact by Property")
                        st.caption(
                            "Sorted by biggest monthly revenue increase after PetScreening launch. "
                            "Properties launched before the lookback window show 'Live before window' (no pre-launch data to compare)."
                        )

                        la_rows = []
                        for prop, a in launch_analysis.items():
                            short = prop.split(" - ", 1)[-1] if " - " in prop else prop

                            # Properties need pre-launch months AND reliable+meaningful baseline to compare
                            has_comparison = a["n_pre"] > 0 and a.get("baseline_reliable", True) and a.get("baseline_meaningful", True)

                            if has_comparison:
                                sign_m = "+" if a["diff_monthly"] >= 0 else ""
                                sign_t = "+" if a["diff_total"] >= 0 else ""
                                arrow = "↑" if a["diff_monthly"] >= 0 else "↓"
                                la_rows.append({
                                    "Property": short,
                                    "Launch": a["launch_month"].strftime("%b %Y"),
                                    "Pre-PS Avg ($/mo)": f"${a['pre_avg']:,.0f}",
                                    "Current Avg ($/mo)": f"${a['post_recent_avg']:,.0f}",
                                    "Monthly Lift": f"{arrow} {sign_m}${a['diff_monthly']:,.0f}/mo",
                                    "Cumulative Impact": f"{sign_t}${a['diff_total']:,.0f}",
                                    "Window": f"{a['n_pre']}mo pre · {a.get('n_recent_post', 0)}mo completed post · {a['n_post']}mo total",
                                    "_sort": a["diff_monthly"],
                                    "_comparable": True,
                                })
                            else:
                                la_rows.append({
                                    "Property": short,
                                    "Launch": a["launch_month"].strftime("%b %Y"),
                                    "Pre-PS Avg ($/mo)": "Live before window",
                                    "Current Avg ($/mo)": f"${a['post_monthly_avg']:,.0f}",
                                    "Monthly Lift": "—",
                                    "Cumulative Impact": "—",
                                    "Window": f"{a['n_post']}mo after (no pre data)",
                                    "_sort": -999999,
                                    "_comparable": False,
                                })

                        # Sort: comparable properties first (by monthly change desc),
                        # then non-comparable at the bottom
                        la_rows.sort(key=lambda r: (not r["_comparable"], -r["_sort"] if r["_comparable"] else 0))
                        for r in la_rows:
                            del r["_sort"]
                            del r["_comparable"]

                        la_df = pd.DataFrame(la_rows)
                        _render_table(la_df, height=500)

                        # Count how many are excluded from the aggregate
                        n_no_pre = sum(1 for a in launch_analysis.values() if a["n_pre"] == 0)
                        if n_no_pre > 0:
                            st.caption(
                                f"**{n_no_pre}** propert{'y' if n_no_pre == 1 else 'ies'} launched before the "
                                f"lookback window — shown as 'Live before window' and **excluded** from "
                                f"aggregate impact numbers above (no pre-launch baseline to compare)."
                            )

                    # -- Monthly Revenue Table --
                    st.divider()
                    st.subheader("Monthly Revenue Table")
                    monthly_table = []
                    for month in months:
                        row = {"Month": month.strftime("%b %Y"),
                               "Total Revenue": monthly_portfolio.get(month, 0),
                               "# Charges": monthly_portfolio_count.get(month, 0)}
                        for prop in sorted(monthly_by_prop.keys()):
                            short = prop.split(" - ", 1)[-1] if " - " in prop else prop
                            row[short] = monthly_by_prop[prop].get(month, 0)
                        monthly_table.append(row)

                    monthly_df = pd.DataFrame(monthly_table)
                    _render_table(monthly_df, height=500)

                    st.download_button(
                        "Download Selected Charges CSV",
                        data=cd["filtered_csv"],
                        file_name=f"pet_rent_{label.replace(' ', '_')}.csv",
                        mime="text/csv",
                    )

                # ── 6. Per-Property Data Explorer ───────────────────────
                st.divider()
                with st.expander("Property Data Explorer (debug / compare individual charge rows)", expanded=False):
                    st.caption(
                        "**to_date** = coalesce(charge_to_date, move_out, lease_to) for past tenants. "
                        "Current tenants show ACTIVE. **to_date_src** shows which date field was used."
                    )
                    all_props_sorted = sorted(monthly_by_prop.keys())
                    short_to_full = {(p.split(" - ", 1)[-1] if " - " in p else p): p for p in all_props_sorted}
                    explorer_prop = st.selectbox(
                        "Select a property to inspect:",
                        [""] + sorted(short_to_full.keys()),
                        key="explorer_prop",
                    )
                    if explorer_prop:
                        full_prop = short_to_full[explorer_prop]

                        # Filter charges for this property
                        prop_charges = [
                            r for r in cd["parsed_charges"]
                            if r["property_name"] == full_prop and r.get("amount", 0) > 0
                        ]
                        raw_df = pd.DataFrame(prop_charges) if prop_charges else pd.DataFrame()

                        if not raw_df.empty:
                            # ── Summary by charge code ──
                            st.markdown("##### Charge Code Breakdown")
                            code_summary = (
                                raw_df.groupby("charge_code")
                                .agg(
                                    tenants=("tenant_code", "nunique"),
                                    rows=("charge_code", "size"),
                                    total=("amount", "sum"),
                                    avg_amt=("amount", "mean"),
                                    min_amt=("amount", "min"),
                                    max_amt=("amount", "max"),
                                )
                                .sort_values("total", ascending=False)
                                .reset_index()
                            )
                            code_summary.columns = [
                                "Charge Code", "Unique Tenants", "Rows",
                                "Total $", "Avg $/charge", "Min $", "Max $",
                            ]
                            for col in ["Total $", "Avg $/charge", "Min $", "Max $"]:
                                code_summary[col] = code_summary[col].apply(lambda x: f"${x:,.2f}")
                            _render_table(code_summary)

                            # ── Monthly breakdown ──
                            st.markdown("##### Monthly Revenue")
                            prop_monthly = monthly_by_prop.get(full_prop, {})
                            prop_table = []
                            for m in months:
                                # Count charges active in this month
                                active_in_month = [
                                    rec for rec in prop_charges
                                    if rec["from_date"] is not None
                                    and not pd.isna(rec["from_date"])
                                    and datetime(int(rec["from_date"].year), int(rec["from_date"].month), 1) <= m
                                    and (
                                        datetime(int(rec["to_date"].year), int(rec["to_date"].month), 1) >= m
                                        if rec["to_date"] is not None and not pd.isna(rec["to_date"]) and isinstance(rec["to_date"], datetime)
                                        else window_end >= m
                                    )
                                ]
                                # Break down by charge code
                                code_breakdown = defaultdict(float)
                                for rec in active_in_month:
                                    code_breakdown[rec.get("charge_code", "?")] += rec["amount"]
                                breakdown_str = ", ".join(
                                    f"{code}: ${val:,.0f}" for code, val in sorted(code_breakdown.items())
                                ) if code_breakdown else ""

                                prop_table.append({
                                    "Month": m.strftime("%b %Y"),
                                    "Revenue": f"${prop_monthly.get(m, 0):,.2f}",
                                    "# Charges": len(active_in_month),
                                    "# Tenants": len(set(r.get("tenant_code", "") for r in active_in_month)),
                                    "By Code": breakdown_str,
                                })
                            st.caption(f"**{explorer_prop}** — monthly revenue for display window ({len(months)} months)")
                            _render_table(pd.DataFrame(prop_table), height=400)

                            # ── Full charge rows with tenant info ──
                            st.markdown("##### All Charge Line Items")

                            # Show which globally-selected codes exist for this property
                            available_codes = sorted(raw_df["charge_code"].dropna().unique().tolist())
                            missing_codes = [c for c in selected_codes if c not in available_codes]
                            if missing_codes:
                                st.info(
                                    f"This property only has **{', '.join(available_codes)}** from your selected codes. \n"
                                    f"Not found here: {', '.join(f'`{c}`' for c in missing_codes)} "
                                    f"(those codes may not apply to this property)."
                                )

                            # Optional: filter by charge code within explorer
                            filter_code = st.multiselect(
                                "Filter by charge code:",
                                available_codes,
                                default=available_codes,
                                key="explorer_code_filter",
                            )
                            display_df = raw_df[raw_df["charge_code"].isin(filter_code)].copy() if filter_code else raw_df.copy()

                            display_df = display_df.sort_values(
                                ["charge_code", "tenant_code", "from_date"],
                                ascending=[True, True, False],
                            )
                            # Format dates
                            display_df["from_date"] = display_df["from_date"].apply(
                                lambda d: d.strftime("%Y-%m-%d") if d is not None and not pd.isna(d) else "")
                            display_df["to_date"] = display_df["to_date"].apply(
                                lambda d: d.strftime("%Y-%m-%d") if d is not None and not pd.isna(d) and isinstance(d, datetime) else "ACTIVE")
                            display_df["amount"] = display_df["amount"].apply(lambda x: f"${x:,.2f}")

                            # Build tenant name column
                            display_df["tenant"] = (
                                display_df.get("first_name", pd.Series(dtype=str)).fillna("")
                                + " "
                                + display_df.get("last_name", pd.Series(dtype=str)).fillna("")
                            ).str.strip()

                            # to_date source indicator: shows where the end date came from
                            def _to_date_source(row):
                                raw = row.get("_raw_to_date")
                                mo = row.get("_move_out_dt")
                                lt = row.get("_lease_to_dt")
                                if raw is not None and not pd.isna(raw):
                                    return "charge"
                                if mo is not None and not pd.isna(mo):
                                    return "move_out"
                                if lt is not None and not pd.isna(lt):
                                    return "lease_end"
                                return ""
                            if "_raw_to_date" in display_df.columns:
                                display_df["to_date_src"] = display_df.apply(_to_date_source, axis=1)
                            else:
                                display_df["to_date_src"] = ""

                            show_cols = ["tenant_code", "tenant", "tenant_status",
                                         "charge_code", "charge_type", "amount",
                                         "from_date", "to_date", "to_date_src"]
                            # Only show columns that exist
                            show_cols = [c for c in show_cols if c in display_df.columns]

                            st.caption(
                                f"**{explorer_prop}** — {len(display_df)} charge line items "
                                f"({display_df['tenant_code'].nunique() if 'tenant_code' in display_df.columns else '?'} unique tenants)"
                            )
                            _render_table(display_df[show_cols], height=500)

                            # Download with all columns
                            st.download_button(
                                f"Download {explorer_prop} charges",
                                data=display_df[show_cols].to_csv(index=False),
                                file_name=f"charges_{explorer_prop.replace(' ', '_')}.csv",
                                mime="text/csv",
                                key="explorer_dl",
                            )
                        else:
                            st.info("No charge rows found for this property.")

        # ─── TAB 2: Summary ──────────────────────────────────────
        with tab_next:
            _cd = st.session_state.get("chart_data")
            if _cd is None:
                st.info("**Analyze your data first.** Go to the **Fee Collection Charts** tab, "
                        "select charge codes, and click **Analyze & Visualize** to populate this summary.")
            else:
                _label = st.session_state.selection_label
                _prop_ids = st.session_state.get("property_ids", [])

                # ── Rebuild the same aggregates used in the charts tab ──
                _today = datetime.now()
                _we = datetime(_today.year, _today.month, 1)
                # Derive window from data (same as charts tab)
                _earliest = None
                for rec in _cd["parsed_charges"]:
                    fd = rec.get("from_date")
                    if fd is not None and not pd.isna(fd):
                        fd_clean = datetime(int(fd.year), int(fd.month), 1)
                        if _earliest is None or fd_clean < _earliest:
                            _earliest = fd_clean
                _ws = _earliest if _earliest else datetime(_today.year - 5, _today.month, 1)
                _ws = datetime(_ws.year, _ws.month, 1)
                _months = [m.to_pydatetime() for m in pd.date_range(start=_ws, end=_we, freq='MS')]

                # Classify charge codes (same logic as charts tab)
                _has_freq2 = any('frequency' in rec for rec in _cd["parsed_charges"])
                _cc2 = {}
                _by_pc2 = defaultdict(list)
                for rec in _cd["parsed_charges"]:
                    _by_pc2[(rec["property_name"], rec["charge_code"])].append(rec)
                for (pn, cc), recs in _by_pc2.items():
                    if _has_freq2:
                        freqs = [str(r.get('frequency', '')).strip().lower() for r in recs if r.get('frequency')]
                        ot = sum(1 for f in freqs if f == 'one-time')
                        mo = sum(1 for f in freqs if f in ('monthly', 'recurring'))
                        _cc2[(pn, cc)] = "onetime" if ot > mo else "recurring"
                    else:
                        spans = []
                        for r in recs:
                            f, t = r.get("from_date"), r.get("_raw_to_date") if "_raw_to_date" in r else r.get("to_date")
                            if f and t and not pd.isna(f) and not pd.isna(t):
                                try:
                                    spans.append((t - f).days)
                                except (TypeError, AttributeError):
                                    pass
                        # No valid date spans → assume recurring (missing dates ≠ one-time)
                        _median = float(np.median(spans)) if spans else None
                        if _median is None:
                            _cc2[(pn, cc)] = "recurring"
                        else:
                            _cc2[(pn, cc)] = "recurring" if _median > 60 else "onetime"

                _monthly_portfolio = defaultdict(float)
                _monthly_by_prop = defaultdict(lambda: defaultdict(float))
                for rec in _cd["parsed_charges"]:
                    prop = rec["property_name"]
                    amt = rec["amount"]
                    from_dt = rec["from_date"]
                    to_dt = rec["to_date"]
                    if from_dt is None or pd.isna(from_dt) or amt <= 0:
                        continue
                    cs = datetime(int(from_dt.year), int(from_dt.month), 1)
                    _ct2 = _cc2.get((prop, rec.get("charge_code")), "recurring")
                    if _ct2 == "onetime":
                        ce = cs
                    elif to_dt is not None and not pd.isna(to_dt) and isinstance(to_dt, datetime):
                        ce = datetime(int(to_dt.year), int(to_dt.month), 1)
                    else:
                        ce = _we
                    for month in _months:
                        if cs <= month <= ce:
                            _monthly_portfolio[month] += amt
                            _monthly_by_prop[prop][month] += amt
                _monthly_by_prop = {p: dict(v) for p, v in _monthly_by_prop.items()}

                _launch_dates = _cd.get("launch_dates", {})
                _launch_analysis = compute_launch_analysis(_monthly_by_prop, _months, _launch_dates)

                # Comparable properties (with reliable pre & post data)
                _comparable = {p: a for p, a in _launch_analysis.items()
                               if a["n_pre"] > 0 and a.get("baseline_reliable", True) and a.get("baseline_meaningful", True)} if _launch_analysis else {}
                _agg_diff_mo = sum(a["diff_monthly"] for a in _comparable.values()) if _comparable else 0
                _agg_diff = sum(a["diff_total"] for a in _comparable.values()) if _comparable else 0

                # Latest month revenue
                _latest_month = _months[-1] if _months else None
                _current_monthly_rev = sum(
                    _monthly_by_prop[p].get(_latest_month, 0) for p in _monthly_by_prop
                ) if _latest_month else 0

                # Adoption data
                _pid_lookup = _build_property_id_lookup(_cd["parsed_charges"])
                _all_pids = list(set(_pid_lookup.values()))
                _comp_data = fetch_compliance_data(tuple(sorted(_all_pids))) if _all_pids else {}

                # Determine adoption type from Charts tab overlay selection
                _overlay_sel = st.session_state.get("adoption_overlay", "None")
                if _overlay_sel == "Resident Adoption %":
                    _adopt_key = "resident_adoption"
                    _adopt_type_label = "Resident"
                else:
                    _adopt_key = "unit_adoption"
                    _adopt_type_label = "Unit"

                # Projected revenue at 100% adoption + adoption average
                # (match the Charts tab logic: only properties with BOTH adoption AND revenue)
                _projected_100 = {}
                for pname in _monthly_by_prop:
                    pid = _pid_lookup.get(pname)
                    if not pid:
                        continue
                    prop_comp = _comp_data.get(pid, {})
                    latest_adopt = None
                    for m in reversed(_months):
                        entry = prop_comp.get(m)
                        if entry and entry.get(_adopt_key) is not None:
                            latest_adopt = entry[_adopt_key] * 100
                            break
                    latest_rev = _monthly_by_prop.get(pname, {}).get(_latest_month, 0) if _latest_month else 0
                    if latest_adopt and latest_adopt > 0 and latest_rev > 0:
                        projected = latest_rev / (latest_adopt / 100)
                        _projected_100[pname] = {
                            "current_rev": latest_rev,
                            "current_adoption": latest_adopt,
                            "projected_rev_100": projected,
                            "additional_rev": projected - latest_rev,
                        }

                _total_current_for_proj = sum(p["current_rev"] for p in _projected_100.values())
                _total_projected = sum(p["projected_rev_100"] for p in _projected_100.values())
                _total_additional = _total_projected - _total_current_for_proj
                _n_proj_props = len(_projected_100)

                # Average adoption — same set as projected_100 (matches Charts tab exactly)
                _avg_adoption = (
                    sum(p["current_adoption"] for p in _projected_100.values())
                    / len(_projected_100)
                ) if _projected_100 else None

                # Missing pet rent summary (quick fetch if not cached)
                _mr_total_profiles = 0
                _mr_current_mo = 0
                _mr_data = {}
                for k in list(st.session_state.keys()):
                    if k.startswith("missing_rent_") and isinstance(st.session_state[k], dict):
                        _mr_data = st.session_state[k]
                        break
                if _mr_data:
                    _mr_total_profiles = sum(v["missing_count"] for v in _mr_data.values())
                    _mr_current_mo = sum(
                        v["monthly_missing"].get(_latest_month, 0) for v in _mr_data.values()
                    ) if _latest_month else 0

                # Suspected undisclosed summary
                _su_total_profiles = 0
                _su_current_mo = 0
                _su_data = {}
                for k in list(st.session_state.keys()):
                    if k.startswith("suspected_") and isinstance(st.session_state[k], dict):
                        _su_data = st.session_state[k]
                        break
                if _su_data:
                    _su_total_profiles = sum(v["missing_count"] for v in _su_data.values())
                    _su_current_mo = sum(
                        v["monthly_missing"].get(_latest_month, 0) for v in _su_data.values()
                    ) if _latest_month else 0

                # ═══════════════════════════════════════════════════════════════
                #  TRANCHE-BASED IMPACT SUMMARY
                # ═══════════════════════════════════════════════════════════════

                n_props_total = len(_prop_ids)
                n_props_with_data = len(_monthly_by_prop)
                # Calculate total unique units from charge data
                _total_units = 0
                if 'unit_code' in df.columns and 'property_name' in df.columns:
                    _unit_df = df[df['property_name'].isin(_monthly_by_prop.keys())]
                    _unit_df = _unit_df[_unit_df['unit_code'].notna() & (_unit_df['unit_code'] != '')]
                    _total_units = _unit_df.drop_duplicates(subset=['property_name', 'unit_code']).shape[0]
                st.session_state["_total_units"] = _total_units
                _launch_in_data_wn = {p: d for p, d in _launch_dates.items() if p in _monthly_by_prop}
                n_with_launch = len(_launch_in_data_wn)

                # Pre-PS baseline: sum of pre_avg across comparable properties
                _pre_baseline_total = sum(a["pre_avg"] for a in _comparable.values()) if _comparable else 0

                # Tranche 1 numbers
                _t1_mo = _agg_diff_mo
                _t1_total = _agg_diff
                _t1_pct = (_t1_mo / _pre_baseline_total * 100) if _pre_baseline_total > 0 else 0
                _t1_months = max(a["n_post"] for a in _comparable.values()) if _comparable else 0

                # Tranche 2 numbers
                _t2_tenants = _mr_total_profiles
                _t2_mo = _mr_current_mo
                _t2_props = sum(1 for v in _mr_data.values() if v["missing_count"] > 0) if _mr_data else 0
                _t1_t2_combined = _t1_mo + _t2_mo
                _t1_t2_pct = (_t1_t2_combined / _pre_baseline_total * 100) if _pre_baseline_total > 0 else 0

                # Tranche 3 numbers
                _t3_adoption = _avg_adoption
                _t3_additional = _total_additional
                _t3_total_impact = _t1_mo + _t2_mo + _t3_additional
                _t3_pct = (_t3_total_impact / _pre_baseline_total * 100) if _pre_baseline_total > 0 else 0

                # ── Branded header ──
                _logo_img = f'<img src="{_PS_LOGO_WHITE_URI}" height="22" alt="PetScreening">' if _PS_LOGO_WHITE_URI else ""
                _today_str = datetime.now().strftime("%B %d, %Y")
                st.markdown(
                    f'<div style="background:linear-gradient(135deg,#1F2257 0%,#2a2d6e 100%);'
                    f'border-radius:12px;padding:28px 32px 20px 32px;margin-bottom:28px">'
                    f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">'
                    f'{_logo_img}'
                    f'<span style="color:#E2AB58;font-family:Lora,Georgia,serif;font-size:22px;'
                    f'font-weight:700;letter-spacing:-0.3px">PetScreening Impact Analysis</span>'
                    f'</div>'
                    f'<p style="color:#DAEBF5;font-family:Poppins,Arial,sans-serif;font-size:15px;'
                    f'margin:0;opacity:0.9">{_label} &middot; {_today_str}</p>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # ── Property funnel — consistent cascade ──────────────
                _render_property_funnel(
                    n_total=st.session_state.get("total_parent_props") or None,
                    n_api=st.session_state.get("api_props_count") or None,
                    n_with_charges=n_props_with_data,
                    n_with_launch=n_with_launch,
                    n_with_adoption=_n_proj_props if _n_proj_props > 0 else None,
                    n_comparable=len(_comparable) if _comparable else None,
                )

                # ═══════════════════════════════════════════════════════════════
                #  SECTION 1: VALUE CREATED
                # ═══════════════════════════════════════════════════════════════
                def _summary_card(value_text, label_text, sublabel_text="", color="#677848", bg="#fff"):
                    """Render a single big-number card."""
                    _sub_html = ""
                    if sublabel_text:
                        _sub_html = f'<div style="font-size:12px;color:#86868B;margin-top:4px">{sublabel_text}</div>'
                    return (
                        f'<div style="background:{bg};border:1px solid #E8E4DA;border-radius:12px;'
                        f'padding:32px 24px;text-align:center">'
                        f'<div style="font-size:42px;font-weight:700;color:{color};letter-spacing:-1px;'
                        f'font-family:Poppins,sans-serif">{value_text}</div>'
                        f'<div style="font-size:13px;font-weight:600;color:#636569;margin-top:8px;'
                        f'text-transform:uppercase;letter-spacing:0.5px">{label_text}</div>'
                        f'{_sub_html}'
                        f'</div>'
                    )

                _latest_str = _latest_month.strftime("%b %Y") if _latest_month else "N/A"

                st.markdown(
                    '<p style="font-family:Lora,Georgia,serif;font-size:20px;font-weight:700;'
                    'color:#1F2257;margin:24px 0 12px 0">Value Created</p>',
                    unsafe_allow_html=True,
                )

                _vc1, _vc2, _vc3 = st.columns(3)
                with _vc1:
                    st.markdown(_summary_card(
                        f"${_current_monthly_rev:,.0f}",
                        "Current Monthly Pet-Related Revenue",
                        _latest_str,
                        color="#677848",
                    ), unsafe_allow_html=True)
                with _vc2:
                    if _comparable and _t1_mo != 0:
                        _sign = "+" if _t1_mo > 0 else ""
                        st.markdown(_summary_card(
                            f"{_sign}${_t1_mo:,.0f}/mo",
                            "Pet Revenue Change Since PS",
                            f"Across {len(_comparable)} comparable properties",
                            color="#677848" if _t1_mo > 0 else "#DD7B45",
                        ), unsafe_allow_html=True)
                    else:
                        st.markdown(_summary_card(
                            "N/A",
                            "Pet Revenue Change Since PS",
                            "Requires launch dates and pre-launch data",
                            color="#636569",
                        ), unsafe_allow_html=True)
                with _vc3:
                    if _comparable and _t1_total != 0:
                        _sign = "+" if _t1_total > 0 else ""
                        st.markdown(_summary_card(
                            f"{_sign}${_t1_total:,.0f}",
                            "Cumulative Pet Revenue Impact",
                            f"Over {_t1_months} months",
                            color="#677848" if _t1_total > 0 else "#DD7B45",
                        ), unsafe_allow_html=True)
                    else:
                        st.markdown(_summary_card(
                            "N/A",
                            "Cumulative Pet Revenue Impact",
                            "Requires launch dates and pre-launch data",
                            color="#636569",
                        ), unsafe_allow_html=True)

                if _comparable and _t1_mo != 0 and _pre_baseline_total > 0:
                    st.markdown(
                        f'<p style="font-family:Poppins,Arial,sans-serif;font-size:13px;color:#636569;'
                        f'text-align:center;margin:8px 0 28px 0">'
                        f'Pre-PS baseline was ${_pre_baseline_total:,.0f}/mo across {len(_comparable)} properties'
                        f'{f" -- a {_t1_pct:.1f}% increase" if _t1_pct > 0 else ""}.</p>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown('<div style="margin-bottom:28px"></div>', unsafe_allow_html=True)

                # ═══════════════════════════════════════════════════════════════
                #  SECTION 2: REVENUE OPPORTUNITY
                # ═══════════════════════════════════════════════════════════════
                _has_missing_rent_data = any(
                    k.startswith("missing_rent_") and isinstance(st.session_state[k], dict)
                    for k in st.session_state.keys()
                )

                # ── Split recurring vs one-time from missing_rent_data ──
                _s2_recurring_mo = 0
                _s2_onetime_total = 0
                if _mr_data:
                    for _v in _mr_data.values():
                        _cnt = _v.get("missing_count", 0)
                        if _cnt == 0:
                            continue
                        _s2_recurring_mo += _cnt * _v.get("avg_recurring", 0)
                        _s2_onetime_total += _cnt * _v.get("avg_onetime", 0)
                else:
                    _s2_recurring_mo = _t2_mo  # fallback

                _s2_annual_recurring = _s2_recurring_mo * 12
                _s2_annual_impact = _s2_annual_recurring + _s2_onetime_total
                _s2_cap_rate = 0.05
                _s2_value_impact = _s2_annual_impact / _s2_cap_rate if _s2_annual_impact > 0 else 0

                st.markdown(
                    '<p style="font-family:Lora,Georgia,serif;font-size:20px;font-weight:700;'
                    'color:#1F2257;margin:0 0 12px 0">Revenue Opportunity</p>',
                    unsafe_allow_html=True,
                )

                # ── Single row: 3 cards ──
                _ro1, _ro2, _ro3 = st.columns(3)
                with _ro1:
                    if _has_missing_rent_data:
                        if _t2_tenants > 0:
                            st.markdown(_summary_card(
                                f"{_t2_tenants:,}",
                                "Tenants Not Paying",
                                f"Across {_t2_props} properties",
                                color="#DD7B45",
                            ), unsafe_allow_html=True)
                        else:
                            st.markdown(_summary_card(
                                "0",
                                "Tenants Not Paying",
                                "All profiled tenants are paying",
                                color="#677848",
                            ), unsafe_allow_html=True)
                    else:
                        st.markdown(_summary_card(
                            "--",
                            "Tenants Not Paying",
                            "Enable on Charts tab to calculate",
                            color="#636569",
                        ), unsafe_allow_html=True)
                with _ro2:
                    if _has_missing_rent_data:
                        if _s2_recurring_mo > 0:
                            st.markdown(_summary_card(
                                f"${_s2_recurring_mo:,.0f}/mo",
                                "Missing Monthly Pet Rent",
                                "Recurring monthly pet rent not collected",
                                color="#DD7B45",
                            ), unsafe_allow_html=True)
                        else:
                            st.markdown(_summary_card(
                                "$0",
                                "Missing Monthly Pet Rent",
                                "Fully collected",
                                color="#677848",
                            ), unsafe_allow_html=True)
                    else:
                        st.markdown(_summary_card(
                            "--",
                            "Missing Monthly Pet Rent",
                            "Enable on Charts tab to calculate",
                            color="#636569",
                        ), unsafe_allow_html=True)
                with _ro3:
                    if _has_missing_rent_data:
                        if _s2_annual_impact > 0:
                            st.markdown(_summary_card(
                                f"${_s2_annual_impact:,.0f}/yr",
                                "Annual Revenue Impact",
                                f"${_s2_recurring_mo:,.0f}/mo × 12" + (f" + ${_s2_onetime_total:,.0f} one-time" if _s2_onetime_total > 0 else ""),
                                color="#DD7B45",
                            ), unsafe_allow_html=True)
                        else:
                            st.markdown(_summary_card(
                                "$0",
                                "Annual Revenue Impact",
                                "No revenue gap identified",
                                color="#677848",
                            ), unsafe_allow_html=True)
                    else:
                        st.markdown(_summary_card(
                            "--",
                            "Annual Revenue Impact",
                            "Enable on Charts tab to calculate",
                            color="#636569",
                        ), unsafe_allow_html=True)

                # ── Narrative — tells the full story including cap rate ──
                if _has_missing_rent_data and _t2_tenants > 0:
                    _narrative_parts = [
                        f'{_t2_tenants:,} tenants have completed PetScreening profiles but are not being charged pet rent — '
                        f'that is <strong>${_s2_recurring_mo:,.0f}/mo</strong> in recurring pet rent'
                    ]
                    if _s2_onetime_total > 0:
                        _narrative_parts.append(f' plus <strong>${_s2_onetime_total:,.0f}</strong> in uncollected one-time fees')
                    _narrative_parts.append(f' across {_t2_props} properties.')
                    if _s2_annual_impact > 0:
                        _narrative_parts.append(
                            f' On an annual basis, that is <strong>${_s2_annual_impact:,.0f}</strong> in revenue not being captured.'
                        )
                        _narrative_parts.append(
                            f' At a 5% cap rate, collecting these fees would add '
                            f'<strong>${_s2_value_impact:,.0f} in property value</strong>.'
                        )
                    _narrative_parts.append(' This is a billing correction, not a sales effort.')
                    if _su_total_profiles > 0:
                        _narrative_parts.append(
                            f' Additionally, {_su_total_profiles:,} tenants show signals of undisclosed pets, '
                            f'representing ~${_su_current_mo:,.0f}/mo in potential additional revenue.'
                        )
                    st.markdown(
                        f'<p style="font-family:Poppins,Arial,sans-serif;font-size:13px;color:#636569;'
                        f'text-align:center;margin:8px 0 4px 0">'
                        f'{"".join(_narrative_parts)}</p>',
                        unsafe_allow_html=True,
                    )
                elif _has_missing_rent_data and _t2_tenants == 0:
                    st.markdown(
                        '<p style="font-family:Poppins,Arial,sans-serif;font-size:13px;color:#677848;'
                        'text-align:center;margin:8px 0 4px 0">'
                        'All tenants with active household profiles are paying pet rent. No gaps identified.</p>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        '<p style="font-family:Poppins,Arial,sans-serif;font-size:13px;color:#636569;'
                        'text-align:center;margin:8px 0 4px 0">'
                        'Enable "Show uncollected pet rent" on the Charts tab to populate this section.</p>',
                        unsafe_allow_html=True,
                    )

                # ── "Show the Math" expander ──
                if _has_missing_rent_data and _t2_tenants > 0 and _s2_recurring_mo > 0:
                    _avg_fee_s2 = _s2_recurring_mo / _t2_tenants if _t2_tenants > 0 else 0

                    # Per-property fee ranges
                    _fee_ranges_s2 = []
                    for _v in _mr_data.values():
                        if _v.get("missing_count", 0) > 0 and _v.get("avg_recurring", 0) > 0:
                            _fee_ranges_s2.append(_v["avg_recurring"])
                    _fee_min_s2 = min(_fee_ranges_s2) if _fee_ranges_s2 else 0
                    _fee_max_s2 = max(_fee_ranges_s2) if _fee_ranges_s2 else 0

                    with st.expander("How we calculate these numbers", expanded=False):
                        _math_md = f"""**Monthly uncollected pet rent:**

| | |
|---|---|
| Tenants with pets, not paying selected charges | **{_t2_tenants:,}** |
| × Avg pet rent at their property | **${_avg_fee_s2:,.0f}/mo** |
| **= Missing pet rent** | **${_s2_recurring_mo:,.0f}/mo** |
"""
                        if _s2_onetime_total > 0:
                            _avg_ot_s2 = _s2_onetime_total / _t2_tenants if _t2_tenants > 0 else 0
                            _math_md += f"""
**One-time fees not collected:**

| | |
|---|---|
| Same {_t2_tenants:,} tenants | × ${_avg_ot_s2:,.0f} avg one-time fee |
| **= One-time fees** | **${_s2_onetime_total:,.0f}** |
"""
                        _math_md += f"""
**Annual revenue impact:**

| | |
|---|---|
| Monthly pet rent | ${_s2_recurring_mo:,.0f} × 12 = **${_s2_annual_recurring:,.0f}/yr** |
| One-time fees | **${_s2_onetime_total:,.0f}** |
| **Total annual impact** | **${_s2_annual_impact:,.0f}** |

**Unrealized property value** (at 5% cap rate):

| | |
|---|---|
| Annual impact | ${_s2_annual_impact:,.0f} |
| ÷ 5% cap rate | |
| **= Unrealized value** | **${_s2_value_impact:,.0f}** |

> The avg fee varies by property (${_fee_min_s2:,.0f} – ${_fee_max_s2:,.0f}/mo). We use each property's actual average from paying tenants, not a flat number.
"""
                        st.markdown(_math_md)

                        # Per-property example table
                        _example_rows_s2 = []
                        for _p, _v in sorted(_mr_data.items(), key=lambda x: -(x[1].get("missing_count", 0) * x[1].get("avg_recurring", 0))):
                            if _v.get("missing_count", 0) == 0:
                                continue
                            _short = _p.split(" - ", 1)[-1] if " - " in _p else _p
                            _rec = _v.get("avg_recurring", 0)
                            _ot = _v.get("avg_onetime", 0)
                            _cnt = _v["missing_count"]
                            _mo_val = _cnt * _rec
                            _ot_val = _cnt * _ot
                            _ann = (_mo_val * 12) + _ot_val
                            _val = _ann / _s2_cap_rate if _ann > 0 else 0
                            _example_rows_s2.append({
                                "Property": _short,
                                "Tenants": _cnt,
                                "Pet Rent/Mo": f"${_mo_val:,.0f}",
                                "One-Time Fees": f"${_ot_val:,.0f}" if _ot_val > 0 else "--",
                                "Annual Impact": f"${_ann:,.0f}",
                                "Value Impact": f"${_val:,.0f}",
                            })
                        if _example_rows_s2:
                            st.markdown("**Per-property breakdown:**")
                            st.dataframe(
                                pd.DataFrame(_example_rows_s2),
                                use_container_width=True,
                                hide_index=True,
                            )

                st.markdown('<div style="margin-bottom:28px"></div>', unsafe_allow_html=True)

                # ═══════════════════════════════════════════════════════════════
                #  SECTION 3: PORTFOLIO HEALTH
                # ═══════════════════════════════════════════════════════════════
                st.markdown(
                    '<p style="font-family:Lora,Georgia,serif;font-size:20px;font-weight:700;'
                    'color:#1F2257;margin:0 0 12px 0">Portfolio Health</p>',
                    unsafe_allow_html=True,
                )

                _ph1, _ph2, _ph3 = st.columns(3)
                with _ph1:
                    if _avg_adoption is not None:
                        _adopt_color = "#677848" if _avg_adoption >= 70 else ("#E2AB58" if _avg_adoption >= 40 else "#DD7B45")
                        st.markdown(_summary_card(
                            f"{_avg_adoption:.1f}%",
                            f"Average {_adopt_type_label} Adoption",
                            f"Across {_n_proj_props} properties",
                            color=_adopt_color,
                        ), unsafe_allow_html=True)
                    else:
                        st.markdown(_summary_card(
                            "--",
                            "Average Adoption",
                            "Enable adoption overlay on Charts tab",
                            color="#636569",
                        ), unsafe_allow_html=True)
                with _ph2:
                    st.markdown(_summary_card(
                        f"{n_with_launch} of {n_props_total}",
                        "Properties with Launch Date",
                        f"{n_props_with_data} have charge data",
                        color="#1F2257",
                    ), unsafe_allow_html=True)
                with _ph3:
                    if _su_total_profiles > 0:
                        st.markdown(_summary_card(
                            f"{_su_total_profiles:,}",
                            "Suspected Undisclosed",
                            f"~${_su_current_mo:,.0f}/mo potential revenue",
                            color="#DD7B45",
                        ), unsafe_allow_html=True)
                    else:
                        st.markdown(_summary_card(
                            "0",
                            "Suspected Undisclosed",
                            "No signals detected",
                            color="#677848",
                        ), unsafe_allow_html=True)

                # ── Adoption narrative + Projected at 100% ──
                if _avg_adoption is not None:
                    _adopt_narrative = f'{_adopt_type_label} adoption averaged across properties with both revenue and adoption data.'
                    if _t3_adoption is not None and _total_projected > 0:
                        _adopt_narrative += (
                            f' At 100% {_adopt_type_label.lower()} adoption, projected pet fee revenue would be '
                            f'<strong>${_total_projected:,.0f}/mo</strong> — '
                            f'an additional <strong>${_t3_additional:,.0f}/mo</strong> opportunity.'
                        )
                    st.markdown(
                        f'<p style="font-family:Poppins,Arial,sans-serif;font-size:13px;color:#636569;'
                        f'text-align:center;margin:8px 0 28px 0">'
                        f'{_adopt_narrative}</p>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown('<div style="margin-bottom:28px"></div>', unsafe_allow_html=True)

                # ── Detailed metrics (collapsed) ──
                _quick_rows = []
                _quick_rows.append({"Metric": "Pre-PS Baseline", "Value": f"${_pre_baseline_total:,.0f}/mo", "Period": f"across {len(_comparable)} properties"})
                _quick_rows.append({"Metric": "Current Monthly Pet Fee Revenue", "Value": f"${_current_monthly_rev:,.0f}", "Period": _latest_str})
                if _comparable:
                    _quick_rows.append({"Metric": "Monthly Revenue Lift", "Value": f"+${_t1_mo:,.0f}/mo", "Period": f"across {len(_comparable)} properties"})
                    _quick_rows.append({"Metric": "Cumulative Revenue Impact", "Value": f"${_t1_total:,.0f}", "Period": f"over {_t1_months} months"})
                if _t2_tenants > 0:
                    _quick_rows.append({"Metric": "Tenants Not Paying", "Value": f"{_t2_tenants:,}", "Period": f"across {_t2_props} properties"})
                    _quick_rows.append({"Metric": "Uncollected Revenue", "Value": f"${_t2_mo:,.0f}/mo", "Period": _latest_str})
                if _t3_adoption is not None:
                    _quick_rows.append({"Metric": f"Current {_adopt_type_label} Adoption", "Value": f"{_t3_adoption:.1f}%", "Period": f"across {_n_proj_props} properties"})
                if _t3_additional > 0:
                    _quick_rows.append({"Metric": "Additional Opportunity at 100%", "Value": f"+${_t3_additional:,.0f}/mo", "Period": "at 100% adoption"})
                if _su_total_profiles > 0:
                    _quick_rows.append({"Metric": "Suspected Undisclosed Pets", "Value": f"{_su_total_profiles:,}", "Period": f"~${_su_current_mo:,.0f}/mo potential"})
                if _total_units > 0:
                    _quick_rows.append({"Metric": "Total Units", "Value": f"{_total_units:,}", "Period": f"across {n_props_with_data} properties"})
                    if _current_monthly_rev > 0:
                        _quick_rows.append({"Metric": "Pet Revenue per Unit", "Value": f"${_current_monthly_rev / _total_units:,.2f}/mo", "Period": ""})
                _quick_rows.append({"Metric": "Properties with Launch Date", "Value": f"{n_with_launch} of {n_props_total}", "Period": ""})
                _quick_rows.append({"Metric": "Properties with Charge Data", "Value": f"{n_props_with_data} of {n_props_total}", "Period": ""})

                with st.expander("Detailed metrics breakdown", expanded=False):
                    _render_table(pd.DataFrame(_quick_rows))

                # ═══════════════════════════════════════════════════════════════
                #  PROPERTY MANAGERS & EMAIL
                # ═══════════════════════════════════════════════════════════════

                st.markdown("---")

                st.markdown(
                    '<p style="font-family:Lora,Georgia,serif;font-size:20px;font-weight:700;'
                    'color:#1F2257;margin:0 0 4px 0">Property Managers</p>'
                    '<p style="font-family:Poppins,Arial,sans-serif;font-size:13px;color:#636569;'
                    'margin:0 0 16px 0">Load property manager contacts to send a compliance reminder.</p>',
                    unsafe_allow_html=True,
                )

                # Fetch PM emails
                if "pm_emails_cache" not in st.session_state:
                    st.session_state.pm_emails_cache = None

                _pm_col1, _pm_col2 = st.columns([3, 1])
                with _pm_col2:
                    if st.button("Load Property Managers", use_container_width=True,
                                 key="load_pms_btn",
                                 help="Query Snowflake for property manager email addresses"):
                        with st.spinner("Fetching property manager emails..."):
                            _sel_ancestry = st.session_state.get("selected_ancestry_id")
                            _sel_parent = st.session_state.get("selected_parent_company")
                            pm_rows = fetch_property_manager_emails(
                                _prop_ids,
                                ancestry_id=_sel_ancestry,
                                parent_company_name=_sel_parent,
                            )
                            st.session_state.pm_emails_cache = pm_rows

                pm_rows = st.session_state.pm_emails_cache

                with _pm_col1:
                    if pm_rows is None:
                        st.markdown(
                            '<div style="background:#FAFAF8;border:1px solid #E8E4DA;border-radius:8px;'
                            'padding:14px 18px;font-family:Poppins,Arial,sans-serif;font-size:13px;color:#636569">'
                            'Click <strong>Load Property Managers</strong> to find email addresses '
                            'for all PMs across your selected properties.</div>',
                            unsafe_allow_html=True,
                        )
                    elif len(pm_rows) == 0:
                        st.warning("No property manager emails found for the selected properties.")
                    else:
                        unique_emails = sorted(set(
                            r['PM_EMAIL'] for r in pm_rows
                            if r.get('PM_EMAIL') and r['PM_EMAIL'].strip()
                        ))
                        n_properties_covered = len(set(r.get('PROPERTY_ID') for r in pm_rows if r.get('PROPERTY_ID')))

                        st.markdown(
                            f'<div style="background:#FAFAF8;border:1px solid #E8E4DA;border-radius:8px;'
                            f'padding:14px 18px;font-family:Poppins,Arial,sans-serif;font-size:13px;color:#4F5155">'
                            f'Found <strong>{len(unique_emails)}</strong> property managers '
                            f'across <strong>{n_properties_covered}</strong> properties.</div>',
                            unsafe_allow_html=True,
                        )

                # Show email action if PMs are loaded
                if pm_rows and len(pm_rows) > 0:
                    unique_emails = sorted(set(
                        r['PM_EMAIL'] for r in pm_rows
                        if r.get('PM_EMAIL') and r['PM_EMAIL'].strip()
                    ))

                    if unique_emails:
                        st.markdown("")

                        # Build email content
                        _email_subject = f"PetScreening Compliance Reminder — {_label}"
                        _email_body_parts = [
                            f"Hi team,",
                            f"",
                            f"Quick reminder: all new leases and lease renewals require a completed PetScreening profile before move-in.",
                            f"",
                            f"This ensures we're capturing pet fee revenue for every pet-owning resident.",
                        ]
                        if _comparable and _agg_diff_mo > 0:
                            _email_body_parts.append(
                                f"Properties with high PetScreening adoption are seeing +${_agg_diff_mo:,.0f}/mo more in pet fee revenue."
                            )
                        if _avg_adoption is not None:
                            _email_body_parts.append(f"Current portfolio adoption: {_avg_adoption:.1f}%")
                        if _mr_total_profiles > 0 and _mr_current_mo > 0:
                            _email_body_parts.append(
                                f"We've identified {_mr_total_profiles} tenants with completed PetScreening screening who aren't being charged pet rent "
                                f"(~${_mr_current_mo:,.0f}/mo in uncollected revenue)."
                            )
                        if _su_total_profiles > 0 and _su_current_mo > 0:
                            _email_body_parts.append(
                                f"Additionally, {_su_total_profiles} tenants show signals of having undisclosed pets "
                                f"(~${_su_current_mo:,.0f}/mo in potential additional revenue)."
                            )
                        _email_body_parts += [
                            f"",
                            f"Please ensure your teams are following up with all new and renewing residents.",
                            f"",
                            f"If you have questions about the PetScreening process, please reach out.",
                            f"",
                            f"Thank you!",
                        ]
                        _email_body = "\n".join(_email_body_parts)

                        # Determine mailto approach based on email count
                        _MAX_MAILTO_EMAILS = 30  # safe limit for mailto: URL length
                        _use_mailto = len(unique_emails) <= _MAX_MAILTO_EMAILS

                        if _use_mailto:
                            bcc_str = ",".join(unique_emails)
                            mailto_url = (
                                f"mailto:?bcc={urllib.parse.quote(bcc_str)}"
                                f"&subject={urllib.parse.quote(_email_subject)}"
                                f"&body={urllib.parse.quote(_email_body)}"
                            )
                            st.markdown(
                                f'<a href="{mailto_url}" target="_blank" style="'
                                f'display:inline-block;background:#1F2257;color:#FFFFFF;'
                                f'font-family:Poppins,Arial,sans-serif;font-size:15px;font-weight:600;'
                                f'padding:14px 28px;border-radius:8px;text-decoration:none;'
                                f'letter-spacing:0.3px;transition:background 0.2s">'
                                f'Email All Property Managers ({len(unique_emails)})'
                                f'</a>'
                                f'<p style="font-family:Poppins,Arial,sans-serif;font-size:12px;'
                                f'color:#AFB2B3;margin:8px 0 0 0">'
                                f'Opens your email client with all PMs in BCC. You still have to click Send.</p>',
                                unsafe_allow_html=True,
                            )
                        else:
                            # Too many emails for mailto: — provide copy-to-clipboard
                            st.markdown(
                                f'<div style="background:#FEF9EF;border:1px solid #E2AB58;border-radius:8px;'
                                f'padding:14px 18px;font-family:Poppins,Arial,sans-serif;font-size:13px;color:#4F5155;'
                                f'margin-bottom:12px">'
                                f'<strong>{len(unique_emails)} property managers</strong> is too many for a '
                                f'mailto: link (browsers limit URL length). Use the fields below to copy into your '
                                f'email client.</div>',
                                unsafe_allow_html=True,
                            )

                            _ec1, _ec2 = st.columns(2)
                            with _ec1:
                                st.text_area(
                                    "BCC List (copy & paste into your email BCC field)",
                                    value="; ".join(unique_emails),
                                    height=120,
                                    key="pm_bcc_list",
                                    help="Select all, copy, and paste into your email's BCC field.",
                                )
                            with _ec2:
                                st.text_area(
                                    "Subject",
                                    value=_email_subject,
                                    height=68,
                                    key="pm_email_subject",
                                )

                            st.text_area(
                                "Email Body (copy & paste)",
                                value=_email_body,
                                height=200,
                                key="pm_email_body",
                            )

                        # PM details expander
                        with st.expander(f"View all {len(unique_emails)} property manager emails", expanded=False):
                            # Group by property
                            _pm_by_prop = defaultdict(list)
                            for r in pm_rows:
                                pname = r.get('PROPERTY_NAME', 'Unknown')
                                email = r.get('PM_EMAIL', '')
                                if email and email.strip():
                                    _pm_by_prop[pname].append(email)

                            _pm_table_rows = []
                            for pname in sorted(_pm_by_prop.keys()):
                                emails = sorted(set(_pm_by_prop[pname]))
                                short = pname.split(" - ", 1)[-1] if " - " in pname else pname
                                _pm_table_rows.append({
                                    "Property": short,
                                    "Property Managers": ", ".join(emails),
                                    "Count": len(emails),
                                })
                            if _pm_table_rows:
                                _render_table(pd.DataFrame(_pm_table_rows))

                # ═══════════════════════════════════════════════════════════════
                #  DOWNLOAD IMPACT ANALYSIS
                # ═══════════════════════════════════════════════════════════════

                st.markdown("---")
                st.markdown(
                    '<p style="font-family:Lora,Georgia,serif;font-size:20px;font-weight:700;'
                    'color:#1F2257;margin:0 0 4px 0">Download Impact Analysis</p>'
                    '<p style="font-family:Poppins,Arial,sans-serif;font-size:13px;color:#636569;'
                    'margin:0 0 16px 0">Generate a branded report to share with stakeholders.</p>',
                    unsafe_allow_html=True,
                )

                _include_pm_in_report = False
                _pm_cache = st.session_state.get("pm_emails_cache")
                if _pm_cache and len(_pm_cache) > 0:
                    _include_pm_in_report = st.checkbox(
                        "Include property manager emails in report",
                        value=False,
                        key="include_pm_report",
                    )

                _exec_col1, _exec_col2, _exec_col3 = st.columns(3)

                # ── Generate button ──
                with _exec_col1:
                    if st.button("Generate Report", use_container_width=True,
                                 key="gen_exec_btn",
                                 help="Build branded PDF and HTML reports"):
                        _pm_for_report = _pm_cache if _include_pm_in_report else None

                        # Generate PDF
                        pdf_bytes = generate_tranche_pdf(
                            label=_label,
                            today_str=_today_str,
                            pre_baseline=_pre_baseline_total,
                            comparable_count=len(_comparable),
                            t1_mo=_t1_mo, t1_total=_t1_total, t1_pct=_t1_pct, t1_months=_t1_months,
                            t2_tenants=_t2_tenants, t2_mo=_t2_mo, t2_props=_t2_props,
                            t1_t2_combined=_t1_t2_combined, t1_t2_pct=_t1_t2_pct,
                            t3_adoption=_t3_adoption, t3_additional=_t3_additional,
                            t3_total_impact=_t3_total_impact, t3_pct=_t3_pct,
                            adopt_type_label=_adopt_type_label,
                            current_monthly_rev=_current_monthly_rev,
                            n_props_total=n_props_total,
                            n_with_launch=n_with_launch,
                            n_props_with_data=n_props_with_data,
                            include_pm=_include_pm_in_report,
                            pm_rows=_pm_for_report,
                            su_total_profiles=_su_total_profiles,
                            su_current_mo=_su_current_mo,
                            total_projected=_total_projected,
                            missing_rent_data=_mr_data,
                            total_units=st.session_state.get("_total_units", 0),
                        )
                        st.session_state["exec_pdf"] = pdf_bytes

                        # Generate HTML (existing)
                        _exec_email_subject = f"PetScreening Compliance Reminder — {_label}"
                        _exec_email_body_parts = [
                            "Hi team,", "",
                            "Quick reminder: all new leases and lease renewals require a completed PetScreening profile before move-in.",
                            "", "This ensures we're capturing pet fee revenue for every pet-owning resident.",
                        ]
                        if _comparable and _agg_diff_mo > 0:
                            _exec_email_body_parts.append(f"Properties with high PetScreening adoption are seeing +${_agg_diff_mo:,.0f}/mo more in pet fee revenue.")
                        if _avg_adoption is not None:
                            _exec_email_body_parts.append(f"Current portfolio adoption: {_avg_adoption:.1f}%")
                        if _mr_total_profiles > 0 and _mr_current_mo > 0:
                            _exec_email_body_parts.append(f"We've identified {_mr_total_profiles} tenants with completed PetScreening screening who aren't being charged pet rent (~${_mr_current_mo:,.0f}/mo in uncollected revenue).")
                        _exec_email_body_parts += ["", "Please ensure your teams are following up with all new and renewing residents.", "", "Thank you!"]
                        _exec_email_body = "\n".join(_exec_email_body_parts)

                        exec_html = generate_exec_summary_html(
                            label=_label, rev_change_mo=_agg_diff_mo, rev_change_total=_agg_diff,
                            avg_adoption=_avg_adoption, adopt_type_label=_adopt_type_label,
                            total_projected=_total_projected, total_additional=_total_additional,
                            n_proj_props=_n_proj_props, mr_total_profiles=_mr_total_profiles,
                            mr_current_mo=_mr_current_mo, comparable_count=len(_comparable),
                            current_monthly_rev=_current_monthly_rev, n_props_total=n_props_total,
                            n_with_launch=len(_launch_in_data_wn), quick_rows=_quick_rows,
                            pm_rows=_pm_for_report,
                            email_subject=_exec_email_subject, email_body=_exec_email_body,
                            su_total_profiles=_su_total_profiles, su_current_mo=_su_current_mo,
                        )
                        st.session_state["exec_html"] = exec_html
                        st.session_state["exec_label"] = _label
                        st.toast("Reports ready for download!")

                # ── PDF download ──
                with _exec_col2:
                    if "exec_pdf" in st.session_state and st.session_state["exec_pdf"]:
                        safe_name = st.session_state.get("exec_label", "report").replace(" ", "_").replace("/", "-")
                        st.download_button(
                            label="Download PDF",
                            data=st.session_state["exec_pdf"],
                            file_name=f"PetScreening_Value_Report_{safe_name}_{datetime.now().strftime('%Y%m%d')}.pdf",
                            mime="application/pdf",
                            use_container_width=True,
                            key="dl_exec_pdf",
                        )
                    else:
                        st.button("Download PDF", disabled=True,
                                  use_container_width=True, key="dl_pdf_disabled")

                # ── HTML download ──
                with _exec_col3:
                    if "exec_html" in st.session_state and st.session_state["exec_html"]:
                        safe_name = st.session_state.get("exec_label", "report").replace(" ", "_").replace("/", "-")
                        st.download_button(
                            label="Download HTML",
                            data=st.session_state["exec_html"],
                            file_name=f"PetScreening_Value_Report_{safe_name}_{datetime.now().strftime('%Y%m%d')}.html",
                            mime="text/html",
                            use_container_width=True,
                            key="dl_exec_html",
                        )
                    else:
                        st.button("Download HTML", disabled=True,
                                  use_container_width=True, key="dl_html_disabled")


        # ─── TAB 3: Missing Pet Rent Report ─────────────────────────
        with tab_report:
            st.header("Missing Pet Rent Report")

            # ── Property funnel — consistent cascade ──────────────
            _report_cd = st.session_state.get("chart_data") or {}
            _report_parsed = _report_cd.get("parsed_charges", []) if isinstance(_report_cd, dict) else []
            _report_n_charges = len(_report_parsed)
            _report_n_with_charges = len(set(
                r["property_name"] for r in _report_parsed
                if r.get("amount", 0) > 0
            )) if _report_n_charges > 0 else None
            _render_property_funnel(
                n_total=st.session_state.get("total_parent_props") or None,
                n_api=st.session_state.get("api_props_count") or None,
                n_with_charges=_report_n_with_charges,
            )

            st.markdown(
                """
                This report identifies **current tenants** who have an **active household pet** screening
                in PetScreening but are **NOT paying** any of the selected pet charge codes.

                **How it works:**
                1. Uses the **live rent roll data** you just fetched (replaces the staging table)
                2. Queries Snowflake for **PetScreening tenants** with household pets (`petscreening__user_enriched`)
                3. Matches tenants by `(property_id, tenant_code)` to determine who's paying vs. who's not
                4. Pulls detailed pet info from `R_MONTHLY_EXECUTIVE_SUMMARY`
                5. Returns tenants with status **Profile_No_Rent** — has a pet, not paying

                **Selected charge codes:** """ + ", ".join(f"`{c}`" for c in selected_codes) + """
                """
            )

            st.info(
                f"**Properties in scope:** {len(st.session_state.property_ids)}  ·  "
                f"**Charge codes used:** {', '.join(selected_codes)}"
            )

            report_btn = st.button(
                "Generate Missing Pet Rent Report",
                type="primary",
                use_container_width=True,
                key="report_btn",
            )

            if report_btn:
                prop_ids = st.session_state.property_ids
                if not prop_ids:
                    st.error("No property IDs found. Please fetch rent roll data first.")
                else:
                    with st.spinner("Matching live rent roll data with PetScreening tenants... (this may take 30-60 seconds)"):
                        try:
                            report_df = generate_missing_pet_rent_report(df, selected_codes, prop_ids)

                            if report_df.empty:
                                st.success("No missing pet rent records found — all tenants with household pets are paying!")
                            else:
                                # Count unique TENANTS (by email) — report rows are per-pet,
                                # but KPIs should match the Charts/Summary tabs which count per-tenant.
                                _n_tenants = report_df['USER_EMAIL'].nunique() if 'USER_EMAIL' in report_df.columns else len(report_df)
                                n_props = report_df['PROPERTY_NAME'].nunique() if 'PROPERTY_NAME' in report_df.columns else 0
                                n_pets = report_df['PET_NAME'].nunique() if 'PET_NAME' in report_df.columns else 0

                                st.warning(
                                    f"Found **{_n_tenants:,}** tenants with active household pets "
                                    f"who are **not paying** any of the selected pet charge codes "
                                    f"({n_pets} pets across {n_props} properties)."
                                )

                                # KPI metrics — tenant count matches Charts & Summary tabs
                                rcol1, rcol2, rcol3 = st.columns(3)
                                rcol1.metric("Missing Pet Rent Tenants", f"{_n_tenants:,}")
                                rcol2.metric("Properties Affected", f"{n_props}")
                                rcol3.metric("Unique Pets", f"{n_pets}")

                                # Breakdown by property
                                st.divider()
                                st.subheader("By Property")
                                if 'PROPERTY_NAME' in report_df.columns:
                                    prop_summary = (
                                        report_df.groupby('PROPERTY_NAME')
                                        .agg(
                                            tenants=('USER_EMAIL', 'nunique'),
                                            pets=('PET_NAME', 'nunique'),
                                        )
                                        .sort_values('tenants', ascending=False)
                                        .reset_index()
                                    )
                                    prop_summary.columns = ['Property', '# Tenants Not Paying', '# Pets']
                                    _render_table(prop_summary)

                                # Full data table
                                st.divider()
                                st.subheader("Full Report Data")
                                display_cols = [
                                    c for c in [
                                        'PROPERTY_NAME', 'USER_FIRST_NAME', 'USER_LAST_NAME',
                                        'USER_EMAIL', 'PET_NAME', 'BREED', 'SPECIES',
                                        'FULL_UNIT_ADDRESS', 'PET_PROFILE_URL',
                                        'LEASE_START_DATE', 'LEASE_END_DATE',
                                        'PET_LEVEL_COMPLIANCE_STATUS',
                                        'USER_LEVEL_COMPLIANCE_STATUS',
                                        'PET_PROFILE_TYPE', 'PET_PROFILE_STATUS',
                                        'OVERALL_STATUS',
                                    ] if c in report_df.columns
                                ]
                                _render_table(
                                    report_df[display_cols] if display_cols else report_df,
                                    height=500,
                                )

                                # Download buttons
                                st.divider()
                                label = st.session_state.selection_label
                                csv_report = report_df.to_csv(index=False)
                                st.download_button(
                                    "Download Missing Pet Rent Report (CSV)",
                                    data=csv_report,
                                    file_name=f"missing_pet_rent_{label.replace(' ', '_')}.csv",
                                    mime="text/csv",
                                    key="download_csv",
                                )

                                # Also offer Excel download
                                import io
                                buffer = io.BytesIO()
                                with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                                    report_df.to_excel(writer, sheet_name='Missing Pet Rent', index=False)
                                    if 'PROPERTY_NAME' in report_df.columns:
                                        prop_summary.to_excel(writer, sheet_name='By Property', index=False)
                                buffer.seek(0)
                                st.download_button(
                                    "Download Missing Pet Rent Report (Excel)",
                                    data=buffer,
                                    file_name=f"missing_pet_rent_{label.replace(' ', '_')}.xlsx",
                                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                    key="download_xlsx",
                                )

                        except Exception as e:
                            st.error(f"Error running report: {e}")
                            with st.expander("Full error details"):
                                import traceback
                                st.code(traceback.format_exc())

            # ── Suspected Undisclosed Pets Report ──────────────────────
            st.divider()
            st.header("Suspected Undisclosed Pets Report")
            st.markdown(
                """
                This report identifies **current residents** who show **signals of having an undisclosed pet**
                but are **NOT paying** any of the selected pet charge codes.

                **Who qualifies as "suspected":**
                - **Abandoned household profile** — started a household pet profile in PetScreening but never completed it
                - **Unresolved assistance request** — submitted an assistance animal request that was declined, left in draft, or returned
                - **No-pet after assistance started** — declared "no pet" after starting an assistance animal profile
                
                *Note: Assistance profiles with `recommended` or `expired` status are excluded.*

                These patterns suggest the resident likely has a pet but hasn't completed the proper screening process.
                """
            )
            st.info(
                f"**Properties in scope:** {len(st.session_state.property_ids)}  ·  "
                f"**Charge codes used:** {', '.join(selected_codes)}"
            )

            suspected_btn = st.button(
                "Generate Suspected Undisclosed Report",
                type="primary",
                use_container_width=True,
                key="suspected_btn",
            )

            if suspected_btn:
                prop_ids = st.session_state.property_ids
                if not prop_ids:
                    st.error("No property IDs found. Please fetch rent roll data first.")
                else:
                    with st.spinner("Identifying suspected undisclosed pets... (this may take 30-60 seconds)"):
                        try:
                            suspected_df = generate_suspected_undisclosed_report(df, selected_codes, prop_ids)

                            if suspected_df.empty:
                                st.success("No suspected undisclosed pets found — great compliance!")
                            else:
                                _n_suspected_tenants = suspected_df['USER_EMAIL'].nunique() if 'USER_EMAIL' in suspected_df.columns else len(suspected_df)
                                st.warning(
                                    f"Found **{_n_suspected_tenants:,}** tenants with suspected undisclosed pets "
                                    f"who are **not paying** any of the selected pet charge codes."
                                )

                                # KPI metrics
                                scol1, scol2, scol3 = st.columns(3)
                                scol1.metric("Suspected Tenants", f"{_n_suspected_tenants:,}")
                                n_props_s = suspected_df['PROPERTY_NAME'].nunique() if 'PROPERTY_NAME' in suspected_df.columns else 0
                                scol2.metric("Properties Affected", f"{n_props_s}")
                                # Breakdown by reason
                                if 'SUSPECTED_REASON' in suspected_df.columns:
                                    reason_counts = suspected_df['SUSPECTED_REASON'].value_counts()
                                    top_reason = reason_counts.index[0] if len(reason_counts) > 0 else "—"
                                    scol3.metric("Top Reason", top_reason)

                                # Breakdown by reason
                                st.divider()
                                st.subheader("By Reason")
                                if 'SUSPECTED_REASON' in suspected_df.columns:
                                    reason_summary = (
                                        suspected_df.groupby('SUSPECTED_REASON')
                                        .agg(count=('SUSPECTED_REASON', 'size'))
                                        .sort_values('count', ascending=False)
                                        .reset_index()
                                    )
                                    reason_summary.columns = ['Reason', 'Count']
                                    _render_table(reason_summary)

                                # Breakdown by property
                                if 'PROPERTY_NAME' in suspected_df.columns:
                                    st.subheader("By Property")
                                    prop_summary_s = (
                                        suspected_df.groupby('PROPERTY_NAME')
                                        .agg(tenants=('USER_EMAIL', 'nunique') if 'USER_EMAIL' in suspected_df.columns else ('PROPERTY_NAME', 'size'))
                                        .sort_values('tenants', ascending=False)
                                        .reset_index()
                                    )
                                    prop_summary_s.columns = ['Property', 'Suspected Tenants']
                                    _render_table(prop_summary_s)

                                # Full data
                                st.subheader("Full Report Data")
                                s_display_cols = [
                                    c for c in [
                                        'PROPERTY_NAME', 'FULL_UNIT_ADDRESS',
                                        'USER_FIRST_NAME', 'USER_LAST_NAME',
                                        'USER_EMAIL', 'SUSPECTED_REASON',
                                        'LEASE_START_DATE', 'LEASE_END_DATE',
                                        'USER_PET_TYPE', 'USER_PET_STATUS',
                                        'COMPLIANCE_STATUS', 'USER_PROFILE_URL',
                                    ] if c in suspected_df.columns
                                ]
                                _render_table(
                                    suspected_df[s_display_cols] if s_display_cols else suspected_df,
                                    height=500,
                                )

                                # Download buttons
                                st.divider()
                                s_label = st.session_state.selection_label
                                csv_suspected = suspected_df.to_csv(index=False)
                                st.download_button(
                                    "Download Suspected Undisclosed Report (CSV)",
                                    data=csv_suspected,
                                    file_name=f"suspected_undisclosed_{s_label.replace(' ', '_')}.csv",
                                    mime="text/csv",
                                    key="download_suspected_csv",
                                )

                                import io as _io_s
                                buffer_s = _io_s.BytesIO()
                                with pd.ExcelWriter(buffer_s, engine='openpyxl') as writer:
                                    suspected_df.to_excel(writer, sheet_name='Suspected Undisclosed', index=False)
                                    if 'PROPERTY_NAME' in suspected_df.columns:
                                        prop_summary_s.to_excel(writer, sheet_name='By Property', index=False)
                                    if 'SUSPECTED_REASON' in suspected_df.columns:
                                        reason_summary.to_excel(writer, sheet_name='By Reason', index=False)
                                buffer_s.seek(0)
                                st.download_button(
                                    "Download Suspected Undisclosed Report (Excel)",
                                    data=buffer_s,
                                    file_name=f"suspected_undisclosed_{s_label.replace(' ', '_')}.xlsx",
                                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                    key="download_suspected_xlsx",
                                )

                        except Exception as e:
                            st.error(f"Error running suspected report: {e}")
                            with st.expander("Full error details"):
                                import traceback
                                st.code(traceback.format_exc())

        # ─── TAB 4: Documentation & SQL ──────────────────────────────
        with tab_docs:
            st.header("Documentation & SQL Reference")
            st.markdown("Everything that happens behind the scenes. Organized by **Both Systems**, **Yardi-specific**, and **Entrata-specific**.")

            # ══════════════════════════════════════════════════════════
            #  SECTION: BOTH SYSTEMS
            # ══════════════════════════════════════════════════════════
            st.subheader("Both Systems — Shared Logic")

            with st.expander("**Property Selection — How properties are loaded**", expanded=False):
                st.markdown("""
**Parent Company dropdown** counts ALL properties from `d_properties` (total count)
plus the subset with active API integrations (api count). The difference = properties
missing integration records that will be skipped during fetch.

**When you click Fetch**, only properties with active integrations (Yardi or Entrata)
are loaded. The app shows you how many of the total properties have API access.
                """)

            with st.expander("**Tenant Identification — Who counts as 'missing pet rent'?**"):
                st.markdown("""
The same Snowflake query is used **everywhere** (Charts tab orange bars, Summary tab,
Missing Pet Rent Report tab) to ensure numbers match:

```sql
SELECT DISTINCT du.property_id, tenant_code, ue.user_email, ...
FROM PROD.common.d_units du
JOIN PROD.petscreening.petscreening__user_enriched ue ON ue.unit_id = du.unit_id
JOIN PROD.common.f_leases l ON du.unit_key = l.unit_key AND l.user_key = ue.user_key
WHERE du.unit_source = '<yardi|entrata>'
  AND ue.compliance_status = 'compliant'
  AND ue.user_pet_type = 'household'
  AND ue.user_pet_status = 'active'     -- active household tenants only
```

**Matching priority:**

| Method | How it works |
|--------|-------------|
| **Tenant code** | Case-insensitive match of Snowflake `tenant_code` → API `tenant_code` |
| **Email** | Case-insensitive match of Snowflake `user_email` → API `email` |
| **Unit expansion** | If anyone on the same `(property, unit)` is paying, all tenants on that unit are paying |
| **Lease expansion (Entrata)** | If anyone on the same `lease_id` is paying, all customers on that lease are paying |

**Entrata note:** The deep dive on Hillpointe showed that tenant code matching
is **0% effective** for some Entrata PMCs (Snowflake `tenant_code` is NULL). Email
matching is the primary path for Entrata. The lease-level expansion was added to
catch co-tenants that unit-level matching alone would miss.

**Stale profile filtering (Entrata):** Profiles whose email does not appear in the
current API data are excluded from missing-rent counts. This prevents ex-tenants
with lingering PetScreening profiles from inflating the numbers.
                """)

            with st.expander("**Roommate & Co-Tenant Handling**"):
                st.markdown("""
**How roommates are handled throughout the app:**

Pet rent is typically charged **per-lease** (or per-unit), not per-person. When multiple
tenants share a unit, only one usually has the pet charge on their name. The app handles
this at several levels:

| Layer | How it works |
|-------|-------------|
| **Yardi (API)** | GetRentroll returns one charge row per `(unit, tenant, charge_code)`. If roommate A has "Pet Rent" but roommate B does not, only A has the charge row. |
| **Entrata (API)** | `getLeases` returns scheduled charges at the **lease** level. Every customer on the same lease gets an identical copy of the charge. The app deduplicates by `(lease_id, interval_id, charge_code, start_date, amount)` so the charge is counted **once** for revenue, but every customer is marked as "paying". |
| **Paying flag (unit-level)** | If *any* tenant on a `(property_id, unit_code)` has a pet charge, **all** tenants on that unit are marked paying. |
| **Paying flag (lease-level, Entrata)** | If *any* customer on the same `lease_id` has a pet charge, all other customers on that lease are also marked paying. This handles cases where unit-level matching is incomplete due to tenant code mismatches. |
| **Paying flag (email)** | The `_apply_paying_flag` function propagates payment status by **email**: if _any_ row for a given `USER_EMAIL` is paying, _all_ rows for that email are marked paying. |
| **Revenue aggregation** | For Entrata, the dedup key ensures charges shared across multiple customers are summed once. For Yardi, each charge row is naturally unique per `(unit, tenant, charge_code, from_date)`. |

**Edge case:** If two roommates each independently have a pet, both should ideally have their own
pet charge. The app will correctly identify the second roommate as "not paying" if they have a
completed PetScreening screening but no charge in their name.

**Why this matters for counts:** The "tenants not paying" count uses `USER_EMAIL.nunique()`,
so a person appearing in multiple lease rows is counted once.
                """)

            with st.expander("**Suspected Undisclosed Pets — What gets excluded?**"):
                st.markdown("""
The suspected undisclosed report identifies residents whose PetScreening profile signals
a likely undisclosed pet. The following are **excluded**:

| Excluded | Why |
|----------|-----|
| **Assistance profiles with `recommended` status** | They completed the process successfully — not suspicious |
| **Assistance profiles with `expired` status** | Expired requests are no longer actionable |
| **Pet profiles with `archive_reason` set** | Already resolved/archived by the property |
| **Profiles already paying** any selected charge code | They're paying — not missing |

**Who IS included (suspected reasons):**
- **Abandoned household profile** — started a household pet profile but never completed it
- **Unresolved assistance request** — assistance request was declined, left in draft, or returned
- **No-pet after assistance started** — declared "no pet" after starting an assistance animal profile
                """)

            with st.expander("**Charge Classification — Recurring vs One-Time**"):
                st.markdown("""
The app classifies each selected charge code at each property as either **recurring rent**
or **one-time deposit**. This determines how uncollected revenue is estimated:

- **Recurring rent**: added to every month the missing tenant was active
- **One-time deposit**: added only to the tenant's first active month

| System | How classification works |
|--------|------------------------|
| **Yardi** | Inferred from median date span of actual charges: > 60 days = recurring, ≤ 60 days = one-time |
| **Entrata** | Uses the explicit `frequency` field on each charge (`Monthly` → recurring, `One-Time` → one-time) |

The estimated uncollected amount per missing tenant uses the **average (mean)** charge
amount from tenants who ARE paying at that specific property (with portfolio-wide
average as fallback if no payers at that property).
                """)

            with st.expander("**Launch Date Handling — Charts & Impact Calculation**"):
                st.markdown("""
- **Source:** `PROD.PETSCREENING.PETSCREENING__PROPERTY_KEY_FACTS.PROPERTY_LAUNCH_DATE`
- **Launch month is counted as post-launch** (green bar). The red line sits at the boundary
  between the last pre-launch month and the launch month.
- **Pre-launch baseline** = average of up to 6 months before the launch month (uses whatever data is available)
- **Post-launch current lift** = average of all completed post-launch months (excludes current partial month)
- **Cumulative impact** = total actual post-launch revenue − (pre_avg × number of post months)
- **Baseline reliability** (Entrata): if fewer than 3 pre-launch months of charge data
  are available, the baseline is flagged as unreliable. Charts show "-- insufficient data"
  and the impact table adds a "(low data)" badge. A warning banner appears if most
  properties in the portfolio have unreliable baselines.
- **Properties launched before the lookback window** have no pre-launch data and are excluded
  from the impact calculation
                """)

            # ══════════════════════════════════════════════════════════
            #  SECTION: YARDI-SPECIFIC
            # ══════════════════════════════════════════════════════════
            st.subheader("Yardi-Specific Behavior")

            with st.expander("**Yardi API — GetRentroll endpoint**"):
                st.markdown("""
**How data is fetched:**
- SOAP API call to each property's `GetRentroll` endpoint
- Requests wide date range (charges from `2000-01-01`, move dates from 10 years back)
- The lookback slider controls only the **display window**, not the API request

**Data structure:**
- One row per unit → tenant → charge (naturally deduplicated)
- Each tenant's charges are listed individually under their tenant node
- `ChargeCode`, `ChargeType`, `ChargeAmount`, `FromDate` are extracted per charge

**Charge classification (Yardi):**
- No explicit `frequency` field available
- Classification inferred from **median date span**: if the median span between
  `FromDate` and end date is > 60 days → recurring, otherwise → one-time

**Tenant matching:**
- `tenant_code` from API maps directly to `lease_source_external_id:tenant_code` in Snowflake
- Normalized: stripped + uppercased for case-insensitive matching
- Email fallback: stripped + lowercased
                """)

            with st.expander("**Yardi — End date logic**"):
                st.markdown("""
Since Yardi does not have a `frequency` field, the end date is handled as follows:

| Scenario | End date used |
|----------|--------------|
| Charge has explicit `charge_to_date` | Used as-is |
| Current tenant with no `charge_to_date` | Treated as ongoing (no end) |
| Past tenant with no `charge_to_date` | Falls back to `move_out_date` → `lease_to_date` |
                """)

            # ══════════════════════════════════════════════════════════
            #  SECTION: ENTRATA-SPECIFIC
            # ══════════════════════════════════════════════════════════
            st.subheader("Entrata-Specific Behavior")

            with st.expander("**Entrata API — getLeases endpoint**"):
                st.markdown("""
**How data is fetched:**
- REST API call to each property's `getLeases` endpoint (paginated, **500 per page**)
- Pagination uses a fallback: if the API returns fewer leases than the page size, we stop;
  otherwise we keep paging (Entrata's `meta` response is unreliable for pagination)
- Returns all leases including historical, cancelled, and applicant leases
- **AR Transactions** are also fetched (`includeArTransactions=1`) for scheduled-vs-actual comparison

**Data structure (key differences from Yardi):**
- One JSON object per lease, containing arrays of customers, intervals, charges, activities, and AR transactions
- **Multiple customers per lease**: Primary, Co-Applicant, Guarantor, Roommate
- **Multiple intervals per lease**: Original, renewals, cancelled renewal attempts
- **Charges belong to the lease** (not individual customers) — shared across all customer rows

**Scheduled vs. Actual Charges:**

The primary revenue data uses **scheduled charges** (`scheduledCharges`). A separate
**AR Transactions** view (in Fee Collection Analysis) shows actual posted/collected amounts.

| | Yardi (GetRentroll) | Entrata (getLeases) |
|---|---|---|
| **What we pull** | Lease charges (tied to active rent roll) | Scheduled charges + AR transactions |
| **Confirmation level** | Reflects what's on the current rent roll — closer to "billed" | Scheduled = what *should* be billed; AR = what *was* posted |
| **Implication** | Higher confidence that the charge is actively being collected | Use the AR comparison view to gauge collection accuracy |

For Entrata properties, the charts show **scheduled** revenue. Use the "Scheduled vs Actual
Revenue" expander in Fee Collection Analysis to compare against actual AR postings.
                """)

            with st.expander("**Entrata — Interval & Status Filtering**"):
                st.markdown("""
Entrata has **two status fields** that track different things:

| Field | Tracks | Used for |
|-------|--------|----------|
| `leaseIntervalStatus` | Did the lease go live? | **This is what we filter on** |
| `leaseCustomerStatus` | Is this person still on the lease? | Don't use for revenue filtering |

**Why it matters:** A guarantor with `leaseCustomerStatus: "Cancelled"` on a
`leaseIntervalStatus: "Notice"` lease should still have their charges counted —
the lease is active, the guarantor was just removed.

**Exclusion-based filter:** We use an **exclusion list** rather than inclusion list.
The following statuses are explicitly rejected:
- `cancelled` — lease never went live
- `applicant` — hasn't started
- `denied` — application denied
- `future` — not yet active

**Everything else is accepted**, including empty/unknown statuses. This is critical
because some PMCs (e.g. Hillpointe) return empty `leaseIntervalStatus` on 100% of
charges. An inclusion-based filter would reject all their data.

Each scheduled charge carries a `leaseIntervalId` that links it to a specific interval.
We only include charges from non-excluded intervals — this prevents counting charges
from cancelled renewal attempts while preserving valid data with empty status fields.
                """)

            with st.expander("**Entrata — Deduplication & Lease-Level Matching**"):
                st.markdown("""
**For revenue aggregation:**
Charges are deduplicated by a composite key:
`lease_id | interval_id | charge_code | start_date | amount`

Since every customer on the same lease gets identical charges, without dedup we'd
triple-count a lease with 3 customers. The app shows a caption when dedup occurs.

**For missing pet rent matching (two-level expansion):**

| Level | How it works |
|-------|-------------|
| **Unit-level** | If *any* tenant on a `(property_id, unit_code)` has a pet charge, all tenants on that unit are marked paying |
| **Lease-level** | If *any* customer on the same `lease_id` has a pet charge, all other customers on that lease are also marked paying |

This two-level approach prevents co-tenants from being flagged as "missing" when
their roommate's lease already includes a pet charge. It was added after the
Hillpointe deep dive revealed that unit-level expansion alone missed cases where
the `tenant_code` matching was incomplete.
                """)

            with st.expander("**Entrata — End Date Logic (uses `frequency` field)**"):
                st.markdown("""
Entrata explicitly provides a `frequency` field on each charge. This is **better** than
Yardi where we have to infer.

| Charge type | End date logic |
|-------------|---------------|
| **Monthly** charges with `endDate` | Used as-is |
| **Monthly** charges without `endDate` | Falls back to interval's `lease_to_date` |
| **One-Time** charges | `charge_to = charge_from` (same day). Does **NOT** inherit the lease end date — prevents a one-time $300 deposit from looking like recurring revenue across 12 months |

The `frequency` field is also used for **charge classification** (recurring vs one-time)
when estimating uncollected pet rent.
                """)

            with st.expander("**Entrata — Stale Profile Filtering**"):
                st.markdown("""
**Problem discovered:** The Entrata deep dive on Hillpointe revealed that **96%** of
PetScreening profiles flagged as "missing pet rent" did not appear in the Entrata API
data at all. These are likely past tenants whose PetScreening profiles were never
deactivated, inflating the missing-rent count.

**How the freshness check works:**

1. After fetching Entrata lease data, the app builds a set of **known tenant emails**
   per property from the API response
2. Before running missing pet rent or suspected undisclosed analysis, each PetScreening
   profile is checked: does this tenant's email appear in the API data for their property?
3. Profiles that do NOT appear in the API data are **excluded** from the counts (unless
   they are already marked as paying)
4. An info message shows how many stale profiles were filtered out

**Why this only applies to Entrata:** Yardi's `GetRentroll` endpoint returns only
current rent roll tenants, so stale profiles are less of a concern. Entrata's
`getLeases` returns historical leases, but the tenant population visible in the
API is still constrained by pagination and integration timing.

**Caveat:** Pagination limits (500 per page) mean some active tenants may not appear
in the API data. The freshness check is conservative — it only excludes profiles that
clearly don't match any API tenant, and preserves profiles already marked as paying.
                """)

            with st.expander("**Entrata — Pre-PetScreening Baseline Reliability**"):
                st.markdown("""
**Problem discovered:** For many Entrata PMCs, the `getLeases` API returns scheduled
charges only from around the time the PetScreening integration was configured — not
the full historical charge ledger. This means pre-launch revenue data is often
incomplete or missing entirely.

**What the app does:**

- `compute_launch_analysis` now tracks `baseline_reliable` for each property:
  reliable = at least **3 months** of pre-launch charge data available (baseline uses up to 6 months when available)
- Properties with unreliable baselines are flagged in the charts with
  "-- insufficient data" annotations
- The impact summary table shows a **(low data)** badge next to unreliable properties
- If the majority of Entrata properties have unreliable baselines, a warning banner
  appears advising caution with the "Revenue Change Since PetScreening" metric

**Recommendation:** For Entrata PMCs with limited pre-launch data, focus on
post-launch trends and the missing pet rent analysis rather than before/after
revenue comparisons.
                """)

            with st.expander("**Entrata — Scheduled vs Actual Revenue (AR Transactions)**"):
                st.markdown("""
**What it is:** A comparison view in the Fee Collection Analysis section that shows
scheduled charges alongside actual AR (Accounts Receivable) transactions.

**How it works:**

| Data source | What it represents |
|-------------|-------------------|
| **Scheduled charges** | What the lease says *should* be billed (from `scheduledCharges`) |
| **AR transactions** | What was *actually posted* to the ledger (from `arTransactions`) |
| **AR collected** | Of the posted amount, how much has been *paid* (`amountPaid`) |

The app fetches AR transactions via `includeArTransactions=1` on the Entrata API,
filters for pet-related charge codes, and aggregates by month and property.

**Metrics shown:**
- **Scheduled Revenue** — total from scheduled charges in the display window
- **Actual Posted (AR)** — total from AR transactions posted in the same window
- **AR Collected** — how much of the posted amount has been paid
- **Variance** — percentage difference between AR posted and scheduled

**Monthly and per-property tables** let you drill into where scheduled and actual
amounts diverge. A large negative variance may indicate charges that are scheduled
but not yet billed, or tenants who have moved out before the scheduled charge period.

**Important:** AR transactions are only available if the PMC's Entrata integration
supports `includeArTransactions`. If no AR data is returned, this section won't appear.
                """)

            with st.expander("**Entrata — Property Count (X/Y format)**"):
                st.markdown("""
The parent company dropdown shows `X / Y` where:
- **X** = properties with active Entrata API integrations (will be fetched)
- **Y** = total properties under that parent company in `d_properties` (regardless of source)

This matches the Yardi behavior. Properties without active integrations are skipped during fetch.
                """)

            # ══════════════════════════════════════════════════════════
            # SECTION A  —  FREQUENTLY ASKED QUESTIONS
            # ══════════════════════════════════════════════════════════
            st.subheader("Frequently Asked Questions")

            with st.expander("**Why do the property counts change at each step? (e.g. 41 → 36 → 33 → 29)**", expanded=True):
                st.markdown("""
Each number represents a progressively smaller slice — here's what each one means:

| Step | Count | What it represents |
|------|-------|--------------------|
| **Total properties** | e.g. 41 | All properties under the parent company in `d_properties` |
| **API access** | e.g. 36 | Properties with active integration credentials (Yardi or Entrata) in `STG_PETSCREENING__INTEGRATIONS`. Missing ones have no API credentials configured. |
| **Properties with charge data** | e.g. 33 | Properties where the API returned data **and** at least one charge matched your selected charge codes. If a property has no tenants with the codes you selected (e.g., it uses a different charge code name), it drops out. |
| **Properties in adoption KPIs** | e.g. 29 | Properties that have **both** (a) charge revenue > $0 in the latest month **and** (b) adoption data in the QBR reporting table. Properties with $0 revenue or no QBR data are excluded from the average adoption and projected revenue calculations. |
| **Comparable properties** | varies | Properties with launch dates where we have **both pre-launch and post-launch** charge data. Used for the "Revenue Change Since PetScreening" KPI. Properties launched before the lookback window or too recently are excluded. |

**Why it matters:** Each filter ensures the math is apples-to-apples. If a property has $0 revenue, including it in the adoption average would be misleading. If a property has no pre-launch baseline, we can't calculate a meaningful "revenue change."
                """)

            with st.expander("**How is 'Revenue Change Since PetScreening' calculated?**"):
                st.markdown("""
For each property with a PetScreening launch date:

1. **Pre-launch baseline** = average monthly pet fee revenue in **up to 6 months before** the launch month (uses whatever pre-launch data is available — more months = less seasonal noise)
2. **Post-launch avg** = average of all completed post-launch months (excludes the current partial month to avoid undercounting)
3. **Monthly Change** = Cumulative impact ÷ completed post months (average monthly uplift since launch)
4. **Total Change** = Sum(all post revenue) − (Pre avg × post months) — actual observed cumulative impact in real dollars

The aggregate KPIs sum these values across all **comparable** properties (those with both pre & post data).

**Why up to 6 months for pre?** Using more months smooths out seasonal variation and one-time charges. The monthly lift uses all completed post months so it's mathematically consistent with the cumulative total (monthly × months ≈ cumulative).

**The launch month itself is counted as post-launch** (green bar on charts). The red line sits at the boundary between the last pre-launch month and the launch month.

**Properties excluded from this calculation:**
- No launch date in `PETSCREENING__PROPERTY_KEY_FACTS`
- Launch date is before the lookback window (no pre-launch data available)
- Launch date is too recent (no post-launch months yet)

**Entrata caveat:** Many Entrata PMCs have limited pre-launch charge data (the API often
only returns charges from around when the integration was configured). Properties with
fewer than 3 pre-launch months are flagged as "low data" and should be interpreted with
caution.
                """)

            with st.expander("**How does the adoption overlay work?**"):
                st.markdown("""
The adoption overlay adds a **purple line** on top of each property's revenue bar chart, showing how the adoption rate has trended over the same time period.

- **Unit Adoption** = Active Units ÷ Total Units (from `R_QUARTERLY_BUSINESS_REVIEW_REPORTING`)
- **Resident Adoption** = Active Users ÷ Total Users (same table)

The overlay uses a **secondary Y-axis** (right side, 0–100%) so the scales don't clash.

**When an overlay is active**, additional KPIs appear:
- **Current Monthly Pet-Related Revenue** — what the portfolio earns today in pet fees
- **Projected at 100% Adoption** — linear extrapolation: `current_revenue ÷ (adoption_rate / 100)`
- **Average Adoption** — mean across properties that have **both** revenue and adoption data

A per-property table also appears below the charts showing each property's current adoption, current revenue, and projected revenue at 100%.
                """)

            with st.expander("**How does 'Projected Revenue at 100% Adoption' work?**"):
                st.markdown("""
For each property, we take two data points from the **latest month**:
- **Current Monthly Pet-Related Revenue** (from the selected pet fee charge codes)
- **Current Adoption Rate** (unit or resident, from the QBR table)

Then: `Projected Revenue = Current Revenue ÷ (Current Adoption / 100)`

**Example:** A property earns $5,000/mo at 65% unit adoption.
The average revenue per compliant unit ≈ $5,000 ÷ 0.65 = $7,692.
At 100% adoption → $7,692/mo. Additional = $2,692/mo.

**Caveats:**
- This is a **linear extrapolation** — the last units to comply may have fewer or no pets
- Only properties with **both** revenue > $0 **and** adoption data are included (this is why you may see fewer properties in the "Summary" tab than in the charts)
- The adoption type (unit vs. resident) matches your Charts tab overlay selection
                """)

            with st.expander("**How does 'Show uncollected pet rent' (orange bars) work?**"):
                st.markdown("""
When you toggle **Show uncollected pet rent**, the app:

**1. Identifies missing tenants** — queries Snowflake to find current tenants at each property who:
   - Have an active household pet in PetScreening (`compliance_status = 'compliant'`, `user_pet_type = 'household'`)
   - Are **NOT** being charged any of the selected pet fee charge codes

**2. Estimates the missing revenue per tenant** — this is property-specific, not a flat number:

   a. The app looks at **tenants who ARE paying** at that specific property and collects all their actual charge amounts for the codes you selected.

   b. It classifies each charge code as **recurring rent** or **one-time deposit**:
      - **Yardi:** inferred from median date span (> 60 days = recurring, ≤ 60 days = one-time)
      - **Entrata:** uses the explicit `frequency` field (`Monthly` → recurring, `One-Time` → one-time)

   c. It computes the **average (mean)** charge amount across paying tenants at that property, separately for recurring and one-time charges.

   d. That average becomes the estimate for each non-paying tenant.

   **Example:** If at "Miro" there are 20 tenants paying `PetRent` at amounts like $35, $40, $35, $38, etc., the average is ~$37/mo. Each non-paying tenant at Miro is estimated at $37/mo of uncollected revenue.

   **Fallback:** If a property has **zero** tenants paying the selected codes (so there's no data to average), the app falls back to the **portfolio-wide average** — the mean across all other properties in the parent company.

**3. Distributes across months** — each missing tenant's uncollected revenue is only shown for months where:
   - Their lease was active (`lease_from` to `lease_to` or ongoing, from the API)
   - The month is **on or after** the property's PetScreening launch date
   - The month is within the display window
   - For recurring rent → added to every active month
   - For one-time deposits → added only to the tenant's first active month

**4. Displays as orange bars** stacked on top of the green/blue collected revenue bars

**Below each property chart**, you'll see a count like "12 tenants not paying pet rent" — this is the number of unique tenants at that property identified in Step 1.

**In the summary section**, you'll see aggregate KPIs: total tenants not paying, estimated uncollected $/mo, and total uncollected across the window.

**Important:** This data comes from **PetScreening's internal profile data** (not the PMC system). It may not be 100% accurate if profile data and PMC charges are out of sync.

**Entrata stale profile filter:** For Entrata properties, profiles whose email does not
appear in the current API data are excluded before the uncollected revenue calculation.
This prevents ex-tenants with lingering PetScreening profiles from inflating the count.
An info message shows how many profiles were filtered out.
                """)

            with st.expander("**How does the Missing Pet Rent Report tab differ from the orange bars?**"):
                st.markdown("""
They answer the same core question — "who has a pet profile but isn't paying?" — and now
use the **exact same profile identification** so the tenant counts match across all tabs.

| Feature | Orange bars (Charts tab) | Missing Pet Rent Report tab |
|---------|-------------------------|----------------------------------|
| **Purpose** | Visual storytelling — show the revenue gap | Actionable download — send to property managers |
| **Output** | Stacked bars on charts + summary KPIs | Downloadable CSV/Excel with individual names |
| **Granularity** | Per-tenant count & estimated $ per property | Per-pet: each pet is a separate row (tenant may have 2+ pets) |
| **Charge estimate** | Avg charge from paying tenants at that property | Identifies tenants not paying ANY selected code |
| **Time dimension** | Distributed across months by lease dates | Point-in-time snapshot (current tenants only) |

**Important:** The KPIs in all tabs (Charts summary, Summary, Report) use the same
profile identification query (`compliant` + `household` + `active`). The Report tab's
"Missing Pet Rent Tenants" counts unique tenants (by email), not rows (which are per-pet).

Both use the **same live API data** + Snowflake profile data. Neither uses stale staging tables.
                """)

            with st.expander("**What does the 'Summary' tab show and how is it calculated?**"):
                st.markdown("""
The **Summary** tab is designed for **VP-level skimming** — high-level numbers with a narrative.

**KPIs shown (Row 1 — Revenue):**
- **Current Monthly Pet Fee Revenue** — sum of all properties' latest-month revenue for selected charge codes
- **Revenue Change Since PetScreening** — aggregate monthly lift based on all completed post-launch months vs pre-launch baseline (same calc as Charts tab)
- **Projected at 100% Adoption** — what total revenue could be if every property reached 100% adoption

**KPIs shown (Row 2 — Compliance):**
- **Average Adoption** — mean adoption across properties with both revenue and adoption data. The adoption type (unit/resident) **matches your Charts tab overlay selection**.
- **Tenants Not Paying Pet Rent** — total count from the uncollected pet rent analysis (requires toggle on Charts tab)

**Natural language story:** Auto-generated paragraph summarizing the key numbers in plain English.

**"Email All Property Managers" button:** Populates a mailto: link with all PMs in BCC and a pre-written reminder about PetScreening compliance.

**Why does the property count differ from Charts?** See the first FAQ above — adoption KPIs only include properties with BOTH revenue > $0 AND adoption data in the QBR table.
                """)

            with st.expander("**Why might the adoption % on 'Summary' differ from Charts?**"):
                st.markdown("""
They should now match exactly. Both use the **same calculation**:

```
avg_adoption = mean(latest_adoption for each property in projected_100)
```

Where `projected_100` = properties that have **both**:
1. Latest-month revenue > $0 (from selected charge codes)
2. Latest-month adoption data (from `R_QUARTERLY_BUSINESS_REVIEW_REPORTING`)

If they ever differ, it means the data changed between tab renders (e.g., Streamlit reran with different slider settings). The adoption type (unit vs. resident) is controlled by the Charts tab overlay radio button and applies to both tabs.
                """)

            with st.expander("**What is the lookback window slider and how does it affect results?**"):
                st.markdown("""
The **Display Window** slider in the sidebar (default: 24 months) controls how far back the charts show.

**Important:** The API always fetches the **full charge history** (10+ years of lease charge data). The slider only filters which months appear on charts and in calculations.

This means:
- Changing the slider **does NOT re-fetch data** from the API
- The same raw data is re-aggregated dynamically when you move the slider
- A shorter window (e.g., 6 months) gives a more recent view but may exclude pre-launch data
- A longer window (e.g., 60 months) shows more history but may include noise from old charge codes

**Tip:** For the "Revenue Change Since PetScreening" calculation, the pre-launch baseline (up to 6 months) is computed within the lookback window. If your window is too short, some properties may not have enough pre-launch data for comparison.
                """)

            with st.expander("**How are charge codes selected and why does it matter?**"):
                st.markdown("""
After fetching rent roll data, the app shows **all unique charge codes** found across all properties. You then select which ones represent pet fees (e.g., `PetRent`, `Pet Fee`, `PetDeposit`).

**This selection affects everything downstream:**
- Which charges are included in the monthly revenue charts
- Which tenants are considered "paying" for the Missing Pet Rent analysis
- Which revenue numbers appear in KPIs and the Summary summary

**Common pitfalls:**
- Different properties may use **different charge code names** for the same thing (e.g., `PetRent` vs `Pet Rent` vs `Pet Fee`). If you miss one, that property's revenue will appear lower.
- If a property uses a code you didn't select, it won't show up in the charts at all (this is why "properties with charge data" may be fewer than "properties fetched").
- The preview table below the multiselect shows sample rows from each selected code so you can verify.
                """)

            with st.expander("**What is 'Suspected Undisclosed Pets' and how does it work?**"):
                st.markdown("""
"Suspected undisclosed pets" are residents who show **behavioral signals** in PetScreening that suggest they
likely have a pet they haven't properly disclosed. Unlike the "confirmed" missing pet rent (residents with
active household pet screening who simply aren't being charged), suspected undisclosed tenants fall into three categories:

| Category | What it means | Why it matters |
|----------|---------------|----------------|
| **Abandoned household profile** | Resident started a household pet profile but never completed it | They likely have a pet but abandoned the process — possibly to avoid pet rent |
| **Unresolved assistance request** | Submitted an assistance animal request that was declined, left in draft, or returned | The request signals they have an animal. A denied request doesn't mean the pet left. |
| **No-pet after assistance started** | Declared "no pet" after beginning an assistance animal profile | Suspicious pattern — why start an assistance profile if you have no pet? |

**Exclusions:** Assistance profiles with `recommended` or `expired` status are excluded from suspected cases.

**How the red bars on charts are calculated:**

The estimated revenue methodology is the same as confirmed missing rent (orange bars):
1. We identify suspected residents not paying any of the selected charge codes
2. We use their lease dates to determine which months they were at the property
3. We use the **average charge amount from paying tenants at that property** as the per-tenant estimate
4. We apply the charge only for months on/after the property's PetScreening launch date

**Key differences from confirmed missing rent:**

| Aspect | Confirmed (orange) | Suspected (red) |
|--------|-------------------|-----------------|
| **Who** | Active household pet profile — **confirmed** pet owner | Behavioral signals — **likely** pet owner |
| **Confidence** | High — they told us they have a pet | Medium — inferred from profile behavior |
| **Source** | `user_pet_type = 'household'` AND `user_pet_status = 'active'` | Abandoned profiles, unresolved requests, suspicious no-pet declarations |
| **Use case** | Revenue you're definitely leaving on the table | Additional revenue opportunity — worth investigating |

**Important notes:**
- Suspected residents are **excluded** if they're already paying any of the selected charge codes
- Suspected and confirmed never overlap — a resident is in one category or neither
- The suspected report includes a `SUSPECTED_REASON` column so you can prioritize follow-up
- Archived profiles are excluded (only active/current records are considered)
                """)

            # ══════════════════════════════════════════════════════════
            # SECTION B  —  TECHNICAL REFERENCE
            # ══════════════════════════════════════════════════════════
            st.markdown("---")
            st.subheader("Technical Reference")

            with st.expander("Data Sources & Key Tables"):
                st.markdown("""
**Shared tables (both Yardi & Entrata):**

| Table | Purpose |
|-------|---------|
| `PROD.COMMON.D_PROPERTIES` | Property master data, parent company info, ancestry IDs |
| `PROD.STAGING.STG_PETSCREENING__INTEGRATIONS` | API credentials per integration (Yardi SOAP or Entrata REST) |
| `PROD.PETSCREENING.PETSCREENING__PROPERTY_KEY_FACTS` | PetScreening launch dates per property |
| `PROD.COMMON.D_UNITS` | Unit dimension (joins profiles to properties via `unit_source`) |
| `PROD.PETSCREENING.PETSCREENING__USER_ENRICHED` | PetScreening user profiles, compliance status, pet type |
| `PROD.COMMON.F_LEASES` | Lease facts (links tenant_code/customerId to PetScreening user_key) |
| `PROD.COMMON.F_USER_PETS` | Links users to pet profiles (suspected undisclosed filtering) |
| `PROD.COMMON.D_PET_PROFILES` | Pet profile details — kind, status, archive reason |
| `PROD.REPORTING.R_MONTHLY_EXECUTIVE_SUMMARY` | Detailed pet/profile data for the downloadable report |
| `PROD.REPORTING.R_QUARTERLY_BUSINESS_REVIEW_REPORTING` | Monthly compliance/adoption rates (unit & resident level) |
| `PROD.STAGING.STG_PETSCREENING__PROPERTY_MANAGER_PERMISSIONS_ONLY` | Property manager permissions (for email feature) |

**Yardi-only:**

| Table | Purpose |
|-------|---------|
| `PROD.STAGING.STG_PETSCREENING__UNITS` | Property code mapping (Yardi property code → PetScreening property) |

**Entrata-only:**

| Table | Purpose |
|-------|---------|
| `RAW.PMC_EXTERNAL_INTEGRATIONS.ENTRATA_GETLEASES` | Raw Entrata getLeases responses (fallback historical data) |
                """)

            with st.expander("Yardi GetRentroll API Call"):
                st.markdown("For each property with credentials, we call the Yardi `GetRentroll` SOAP endpoint:")
                st.code("""
POST {resident_data_url}
Content-Type: text/xml; charset=utf-8
SOAPAction: http://tempuri.org/YSI.Interfaces.WebServices/ItfResidentData/GetRentroll

<GetRentroll>
    <UserName>{user_name}</UserName>
    <Password>{password}</Password>
    <ServerName>{server_name}</ServerName>
    <Database>{database_name}</Database>
    <Platform>Yardi</Platform>
    <InterfaceEntity>PetScreening</InterfaceEntity>
    <InterfaceLicense>{license_token}</InterfaceLicense>
    <YardiPropertyId>{property_code}</YardiPropertyId>
    <MoveIn>{10_years_ago}</MoveIn>         <!-- Wide range to get all history -->
    <MoveOut>{10_years_ago}</MoveOut>
    <LeaseChgFrom>2000-01-01</LeaseChgFrom> <!-- Get ALL charges ever -->
    <LeaseChgTo>{today}</LeaseChgTo>
</GetRentroll>
                """, language="xml")
                st.markdown("""
The XML response is parsed into a flat table with one row per **lease charge**:

| Column | Source |
|--------|--------|
| `property_id`, `property_name` | From our Snowflake query (`d_properties`) |
| `unit_code`, `unit_type`, `market_rent` | `<Unit>` element in XML response |
| `tenant_code`, `first_name`, `last_name`, `tenant_status` | `<Tenant>` element |
| `lease_from`, `lease_to`, `move_in` | `<Tenant>` element |
| `charge_code`, `charge_type`, `charge_amount` | `<LeaseCharge>` element |
| `charge_from_date`, `charge_to_date` | `<LeaseCharge>` element |

**This live API output replaces `stg_pmc_integrations_yardi__getrentroll_new`.**
                """)

            with st.expander("Entrata getLeases API Call"):
                st.markdown("""For each property with credentials, we call the Entrata `getLeases` REST endpoint:""")
                st.code("""
POST https://{corp_id}.entrata.com/api/v1/leases
Content-Type: application/json

{
    "method": { "name": "getLeases", "version": "r3" },
    "params": {
        "propertyId": "<entrata_property_id>",
        "includeScheduledCharges": 1,
        "includeArTransactions": 1,
        "page": 1,
        "per_page": 500
    }
}
                """, language="json")
                st.markdown("""
**Pagination:** 500 leases per page. The app pages forward until the API returns fewer
than 500 results (Entrata's `meta.currentPage`/`meta.lastPage` fields are unreliable).

The JSON response produces **two** flat tables:

**1. Scheduled charges** — one row per charge x customer:

| Column | Source |
|--------|--------|
| `property_id`, `property_name` | From our Snowflake query (`d_properties`) |
| `tenant_code` | `customer.id` from the lease JSON |
| `first_name`, `last_name` | `customer.firstName`, `customer.lastName` |
| `charge_code` | `scheduledCharge.chargeCodeDescription` |
| `charge_amount` | `scheduledCharge.amount` |
| `charge_from_date` | `scheduledCharge.startDate` |
| `charge_to_date` | `scheduledCharge.endDate` (or lease end for Monthly charges) |
| `frequency` | `scheduledCharge.frequency` — Monthly vs One-Time |
| `lease_id` | Used for lease-level deduplication and co-tenant matching |
| `lease_interval_status` | Used with exclusion filter (see Interval & Status Filtering) |
| `lease_interval_id` | Links charges to specific lease intervals |

**2. AR transactions** — one row per posted transaction:

| Column | Source |
|--------|--------|
| `property_name`, `lease_id` | From the lease context |
| `charge_code` | `arTransaction.chargeCodeName` (filtered for pet-related codes) |
| `amount`, `amount_paid` | Posted amount and paid amount |
| `post_date`, `post_month` | When the transaction was posted to the ledger |

**Key differences from Yardi:**
- Has explicit `frequency` field (no need to infer from date spans)
- Charges belong to the lease (shared across all customers)
- Deduplication is required to avoid counting shared charges multiple times
- AR transactions provide actual posted/collected amounts for comparison
                """)

            with st.expander("Chart Aggregation Logic (SQL equivalent)"):
                codes_display = ", ".join(f"'{c}'" for c in selected_codes)
                st.code(f"""
-- SQL equivalent of the chart aggregation logic
-- (this runs in Python/pandas, not against Snowflake)

WITH selected_charges AS (
    SELECT *
    FROM live_api_charges
    WHERE charge_code IN ({codes_display})
      AND charge_amount > 0
      AND charge_from_date IS NOT NULL
),
charge_months AS (
    SELECT c.property_name, m.month_dt, c.charge_amount
    FROM selected_charges c
    CROSS JOIN months m
    WHERE DATE_TRUNC('month', c.charge_from_date) <= m.month_dt
      AND COALESCE(DATE_TRUNC('month', c.charge_to_date), CURRENT_DATE()) >= m.month_dt
)
SELECT property_name, month_dt, SUM(charge_amount) AS revenue
FROM charge_months
GROUP BY property_name, month_dt;
                """, language="sql")

            with st.expander("Missing Pet Rent Report — Step-by-step SQL"):
                props_display = ", ".join(str(pid) for pid in st.session_state.property_ids[:5])
                if len(st.session_state.property_ids) > 5:
                    props_display += f", ... ({len(st.session_state.property_ids)} total)"
                st.markdown("""
**Step 1:** Identify tenants paying selected charges (from live API data)

**Step 2:** Query PetScreening household tenants from Snowflake (`d_units` → `user_enriched` → `f_leases`)

**Step 3:** Left join — tenants with completed screening but no matching charge = `Profile_No_Rent`

**Step 4:** Pull detailed pet data from `R_MONTHLY_EXECUTIVE_SUMMARY`

**Step 5:** Final join to get the downloadable report with names, emails, pet info, addresses
                """)

            with st.expander("Property Manager Email Query"):
                st.code("""
SELECT DISTINCT
    CASE
        WHEN u.user_email = 'parkatvenetopm@stylresidential.com'
        THEN 'parkatvenetoteam@stylresidential.com'
        WHEN u.user_email = 'admin@endeavourhsv.com'
        THEN 'contracts@endeavourhsv.com'
        ELSE u.user_email
    END AS pm_email,
    pm.entity_id AS property_id,
    p.property_name
FROM PROD.staging.stg_petscreening__property_manager_permissions_only pm
LEFT JOIN PROD.petscreening.petscreening__user_enriched u
    ON u.user_id = pm.user_id
LEFT JOIN PROD.common.d_properties p
    ON pm.entity_id = p.property_id
WHERE u.user_status = 'active'
  AND pm.name IN (
      'manager_admin_permission', 'manager_view_hipaa_permission',
      'manager_reviewer_permission', 'manager_view_only_permission'
  )
  AND u.compliance_status != 'n/a|manual|individual_level'
  AND u.user_role = 'property_manager'
  AND u.parent_company_ancestry_id = '<selected_ancestry_id>';
                """, language="sql")
                st.markdown("""
**Note:** `pm.entity_id` doesn't always match `d_properties.property_id`, so we filter by
`u.parent_company_ancestry_id` when selecting by parent company. For single-property
selections, we look up the ancestry_id from `d_properties` first.

The mailto: link (≤30 PMs) opens the email client with all PMs in BCC.
For >30 PMs, a copy-to-clipboard fallback is shown instead.
                """)

            with st.expander("End-to-End Data Flow"):
                st.markdown("""
```
┌─────────────────────────────────────────────────────────────────────┐
│                        SIDEBAR SELECTION                            │
│  User picks: System (Yardi/Entrata)                                 │
│  + Parent Company Name / Ancestry ID / Property ID                  │
└─────────────────┬───────────────────────────────────────────────────┘
                  │
                  
┌─────────────────────────────────────────────────────────────────────┐
│  SNOWFLAKE: d_properties + integrations → property list + creds     │
│  Count: Total Properties (e.g. 41)                                  │
│  Count: With API access (e.g. 36)                                   │
└─────────────────┬───────────────────────────────────────────────────┘
                  │
         ┌────────┴────────┐
                          
  ┌──────────────┐  ┌──────────────┐
  │  YARDI API   │  │ ENTRATA API  │
  │  GetRentroll │  │  getLeases   │
  │  SOAP/XML    │  │  REST/JSON   │
  └──────┬───────┘  └──────┬───────┘
         └────────┬────────┘
                  
┌─────────────────────────────────────────────────────────────────────┐
│  Flat table of ALL lease charges (full history, both systems)       │
│  → Same column shape: charge_code, amount, from/to dates, etc.     │
│  Count: Successfully fetched (e.g. 36, minus errors)                │
└─────────────────┬───────────────────────────────────────────────────┘
                  │
    ┌─────────────┼──────── User selects charge codes ────────┐
    │             │                                            │
    v             v                                            v
┌──────────────────────┐  ┌──────────────────┐  ┌────────────────────────┐
│ CHARTS TAB │ │ WHAT'S NEXT │ │ REPORT TAB │
│                      │  │                  │  │                        │
│  Filter to selected  │  │  VP summary KPIs │  │  Paying vs not-paying  │
│  charge codes        │  │  Natural language │  │  → Profile_No_Rent    │
│  Count: props with   │  │  story            │  │  → CSV/Excel download │
│  matching charges    │  │  Count: props with│  │                        │
│  (e.g. 33)           │  │  revenue+adoption │  │  Same profile query    │
│  → monthly charts    │  │  (e.g. 29)        │  │  as Charts tab         │
│  → adoption overlay  │  │  → Email All PMs  │  │  (counts match)        │
│  → uncollected bars  │  │                  │  │                        │
└──────────────────────┘  └──────────────────┘  └────────────────────────┘
```
                """)

            # ══════════════════════════════════════════════════════════
            # SECTION C  —  ASSUMPTIONS & LIMITATIONS
            # ══════════════════════════════════════════════════════════
            st.markdown("---")
            st.subheader("Assumptions & Limitations")
            st.markdown("""
**Both Systems:**
- **Live API data only:** All charge data comes from the PMC API in real-time (Yardi GetRentroll SOAP or Entrata getLeases REST). We do NOT use stale Snowflake staging tables. If the API returns incomplete data (e.g., during maintenance windows), charts will reflect that.
- **Charge duration assumption:** A charge is assumed active for every month between its `charge_from_date` and `charge_to_date`. If `charge_to_date` is null and the tenant is "Current", the charge is assumed ongoing.
- **Adoption = linear proxy for revenue:** The "projected at 100%" calculation assumes revenue scales linearly with adoption. The last units to comply may have fewer pets, so actual revenue at 100% may be lower.
- **Uncollected pet rent estimate:** Uses the **average (mean) charge amount** from tenants who ARE paying at each specific property. Falls back to portfolio-wide average if no payers at a property.
- **PetScreening tenants vs PMC charges:** Pet data (who has a pet) comes from PetScreening's internal tables. Charge data (who's paying) comes from the PMC API. These update independently — brief sync delays are possible.
- **Launch date = month boundary:** The launch month is counted as post-launch. Pre-launch baseline uses up to 6 months before the launch month. Post-launch monthly lift = cumulative impact ÷ completed post months (excludes current partial month). Cumulative impact uses actual observed total post-launch revenue minus the projected baseline.
- **PM emails:** Filtered to active PMs with specific permission types, using `parent_company_ancestry_id` from `user_enriched`.
- **Suspected undisclosed exclusions:** Assistance profiles with `recommended` or `expired` status are excluded. Pet profiles with a non-empty `archive_reason` are excluded.
- **Missing pet rent tenant criteria:** Only `compliant` + `household` + `active` tenants are considered. This is consistent across ALL tabs (Charts, Summary, Report).

**Yardi-specific:**
- **Charge classification:** Inferred from median date span (> 60 days = recurring, ≤ 60 = one-time). No explicit frequency field available.
- **End date fallback:** For past tenants with no `charge_to_date`, falls back to `move_out_date` → `lease_to_date`.

**Entrata-specific:**
- **Charge classification:** Uses the explicit `frequency` field (`Monthly` → recurring, `One-Time` → one-time).
- **One-time charge end dates:** `charge_to` is set to `charge_from` (same day) to prevent one-time deposits from appearing as recurring revenue across many months.
- **Deduplication:** Charges are deduplicated by `(lease_id, interval_id, charge_code, start_date, amount)` for revenue aggregation. Without this, leases with multiple customers would over-count.
- **Interval status filtering:** Only intervals with status `Current`, `Past`, or `Notice` are included. `Cancelled`, `Applicant`, and `Future` are excluded.
            """)
