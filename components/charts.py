"""Chart builder functions for the Pet Rent analysis app."""

from datetime import datetime, timedelta

import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from analytics.launch_analysis import _resolve_launch_dt  # noqa: F401 (re-exported)


def _property_monthly_lift_for_display(prop_data, months, analysis):
    """Return one property's default Monthly Lift: latest revenue minus pre-PS baseline.

    The portfolio headline uses current/latest month revenue minus the pre-PS
    baseline. Individual property trend badges should use the same methodology
    property-by-property, not the post-launch average stored in diff_monthly.
    """
    if not prop_data or not months or not analysis:
        return 0
    latest_month = months[-1]
    return prop_data.get(latest_month, 0) - analysis.get("pre_avg", 0)


# ─── Portfolio-level charts ───────────────────────────────────────────

def build_portfolio_chart(monthly_data, monthly_counts, months, title_prefix, cumulative=False, launch_analysis=None):
    """Build the aggregated portfolio bar + line chart."""
    import itertools
    portfolio_values = [monthly_data.get(m, 0) for m in months]
    portfolio_counts = [monthly_counts.get(m, 0) for m in months]

    if cumulative:
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


# ─── Individual property charts ───────────────────────────────────────

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
            elif a and a["n_pre"] > 0:
                # Use the canonical baseline_meaningful flag computed in
                # compute_launch_analysis. It now incorporates BOTH the 2%
                # rule AND the data-history-after-launch guard, so we don't
                # need to recompute the threshold here.
                if a.get("baseline_meaningful", True):
                    display_lift = _property_monthly_lift_for_display(monthly_by_prop.get(p, {}), months, a)
                    sign = "+" if display_lift >= 0 else ""
                    color = "#677848" if display_lift >= 0 else "#CF5A3F"
                    arrow = "↑" if display_lift >= 0 else "↓"
                    short += (
                        f'  <b><span style="color:{color}">'
                        f'{arrow} {sign}{_fmt_dollar(display_lift)}/mo'
                        f'</span></b>'
                    )
                elif a.get("data_starts_after_launch", False):
                    # Be specific about why we can't show lift — helps users
                    # understand it's a data coverage issue, not a real zero.
                    short += f'  <span style="color:#999;font-size:0.85em">data starts after launch</span>'
                else:
                    short += f'  <span style="color:#999;font-size:0.85em">no meaningful pre-PS baseline</span>'
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
        # Only show the canonical baseline if compute_launch_analysis marked it
        # meaningful. Do not recompute here; that can bypass source-history guards.
        _bl_meaningful = (
            a
            and a["pre_avg"] > 0
            and a["n_pre"] > 0
            and a.get("baseline_meaningful", True)
        )
        if _bl_meaningful and a:
            fig.add_hline(
                y=a["pre_avg"],
                row=r, col=c,
                line=dict(color="#E2AB58", width=1.5, dash="dot"),
            )
            _month_label = a.get("baseline_month_label", f"{a['n_pre']} observed pre months")
            _baseline_label = f"Pre-PS baseline ${a['pre_avg']:,.0f}/mo ({_month_label})"
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
    # Use property name as title when only 1 property
    _chart_title_prefix = title_prefix
    if n_props == 1:
        _single_prop_name = sorted_props[0]
        _short_single = _single_prop_name.split(" - ", 1)[-1] if " - " in _single_prop_name else _single_prop_name
        _chart_title_prefix = _short_single

    fig.update_layout(
        height=max(450, rows * row_height),
        autosize=True,
        template="plotly_white",
        title=dict(
            text=(
                f"{_chart_title_prefix}: {'Pet Fee Trend' if n_props == 1 else f'Individual Property Fee Trends ({n_props} properties)'}<br>"
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
