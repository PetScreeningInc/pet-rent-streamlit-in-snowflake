"""PetScreening brand CSS injection, logo URIs, and shared UI helpers."""

import os
import base64 as _b64

import streamlit as st


# ─── Logo URIs (computed at module load) ─────────────────────────────

def _load_logo_b64(fill_color=None):
    """Load logo SVG, optionally recolor, return base64 data URI."""
    try:
        _logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logo.svg")
        with open(_logo_path, "r") as _f:
            _svg = _f.read()
        if fill_color:
            _svg = _svg.replace('fill="white"', f'fill="{fill_color}"')
        return "data:image/svg+xml;base64," + _b64.b64encode(_svg.encode()).decode()
    except FileNotFoundError:
        return ""


_PS_LOGO_WHITE_URI = _load_logo_b64()           # white for dark backgrounds
_PS_LOGO_DARK_URI  = _load_logo_b64("#1F2257")  # Pack Blue for light backgrounds


# ─── Brand CSS injection ──────────────────────────────────────────────

def inject_brand_css():
    """Inject PetScreening brand CSS + force-light-theme script into the page."""
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
    /* Sidebar collapse/expand button — let Streamlit handle its own icon */
    button[data-testid="stSidebarCollapseButton"],
    button[data-testid="baseButton-headerNoPadding"] {
        color: #4F5155 !important;
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


# ─── Styled HTML table helper ────────────────────────────────────────

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


# ─── Property Funnel ──────────────────────────────────────────────────

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
