"""Branded HTML report + executive summary HTML."""

from datetime import datetime
from collections import defaultdict
import urllib.parse
from analytics.launch_analysis import _launch_analysis_is_comparable
from components.charts import _latest_observed_revenue_month
from components.ui_helpers import _PS_LOGO_DARK_URI, _PS_LOGO_WHITE_URI

def generate_html_report(
    label, fig_individual, fig_snapshot, launch_analysis, monthly_by_prop, months,
    launch_dates, projected_100=None, overlay_mode_label=None,
    missing_rent_data=None, show_missing_rent=False, total_properties_fetched=0,
    use_avg_lift=False,
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
                      if _launch_analysis_is_comparable(a)}
    agg_diff_mo = sum(a["diff_monthly"] for a in comparable.values()) if comparable else 0
    agg_diff = sum(a["diff_total"] for a in comparable.values()) if comparable else 0
    _launch_in_data = {p: d for p, d in launch_dates.items() if p in monthly_by_prop}
    n_with_launch = len(_launch_in_data)
    n_props_total = total_properties_fetched if total_properties_fetched else len(monthly_by_prop)
    n_comparable = len(comparable)

    # Simple lift: current month - pre baseline (across comparable)
    _html_pre_baseline = sum(a["pre_avg"] for a in comparable.values()) if comparable else 0
    _html_latest = _latest_observed_revenue_month(monthly_by_prop, months, comparable.keys()) if months else None
    _html_current_rev = sum(monthly_by_prop[p].get(_html_latest, 0) for p in comparable.keys()) if comparable and _html_latest else 0
    _html_simple_lift = _html_current_rev - _html_pre_baseline if _html_pre_baseline > 0 else 0

    # Post avg
    _html_post_avg = sum(a.get("post_recent_avg", a.get("post_monthly_avg", 0)) for a in comparable.values()) if comparable else 0

    # Display based on toggle
    _html_display_lift = agg_diff_mo if use_avg_lift else _html_simple_lift
    _html_lift_label = "Average Monthly Lift" if use_avg_lift else "Monthly Lift"
    _html_lift_caption = f"Property-by-property post avg vs pre avg across {n_comparable} properties" if use_avg_lift else f"Current month minus pre-PS baseline across {n_comparable} properties"
    _html_rev_val = _html_post_avg if (use_avg_lift and _html_post_avg > 0) else _html_current_rev
    _html_rev_label = "Post-PS Avg Pet Revenue" if use_avg_lift else "Current Pet Revenue"

    sign_mo = "+" if _html_display_lift >= 0 else ""
    sign_t = "+" if agg_diff >= 0 else ""

    # ── Impact table rows ─────────────────────────────────────────────
    impact_rows_html = ""
    if launch_analysis:
        sorted_la = sorted(launch_analysis.items(), key=lambda x: -x[1].get("diff_monthly", 0))
        for prop, a in sorted_la:
            short = prop.split(" - ", 1)[-1] if " - " in prop else prop
            if _launch_analysis_is_comparable(a):
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
        <div class="kpi-label">{_html_rev_label}</div>
        <div class="kpi-value">${_html_rev_val:,.0f}/mo</div>
        <div class="kpi-caption">Across {n_comparable} comparable properties</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">{_html_lift_label}</div>
        <div class="kpi-value">{sign_mo}${_html_display_lift:,.0f}/mo</div>
        <div class="kpi-caption">{_html_lift_caption}</div>
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
    {"<p>This report uses <b>Average Monthly Lift</b>. For seasonal portfolios, comparing a single month can be misleading. Instead, each property's post-launch average is compared to its pre-launch baseline.</p>" if use_avg_lift else "<p>For each property, we compare the most recently completed month's pet fee revenue to the pre-launch baseline:</p>"}
    <ul>
      <li><b>Before PetScreening (avg/mo)</b> -- Average of up to 6 months before launch</li>
      {"<li><b>After PetScreening (avg/mo)</b> -- Average of all completed post-launch months</li>" if use_avg_lift else "<li><b>Current Revenue</b> -- Most recently completed month's pet fee charges</li>"}
      <li><b>{_html_lift_label}</b> = {"Post-launch average minus pre-launch average (property by property)" if use_avg_lift else "Current month revenue minus pre-launch baseline"}</li>
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
def generate_exec_summary_html(
    label, rev_change_mo, rev_change_total, avg_adoption, adopt_type_label,
    total_projected, total_additional, n_proj_props,
    mr_total_profiles, mr_current_mo, comparable_count,
    current_monthly_rev, n_props_total, n_with_launch,
    quick_rows, pm_rows=None, email_subject="", email_body="",
    su_total_profiles=0, su_current_mo=0,
    use_avg_lift=False, display_rev=None,
):
    """Generate a self-contained HTML executive summary for VP-level sharing.

    Includes KPIs, narrative, detailed metrics table, and an optional
    'Email All Property Managers' button (mailto link).
    """
    today_str = datetime.now().strftime("%B %d, %Y")
    _logo_white = _PS_LOGO_WHITE_URI
    _logo_dark = _PS_LOGO_DARK_URI

    # Use display_rev if provided (post avg when toggled), otherwise current
    _html_rev = display_rev if display_rev is not None else current_monthly_rev
    _rev_label = "Post-PS Avg Pet Revenue" if use_avg_lift else "Current Monthly Pet Revenue"
    sign = "+" if rev_change_mo >= 0 else ""
    color_rev = "#677848" if rev_change_mo >= 0 else "#CF5A3F"
    _lift_label = "Average Monthly Lift" if use_avg_lift else "Pet Revenue Change"
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
        _collecting_label = "Post-launch average" if use_avg_lift else "Currently collecting"
        story_parts.append(
            f"{_collecting_label} <b>${_html_rev:,.0f}/mo</b> in pet fees. "
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
