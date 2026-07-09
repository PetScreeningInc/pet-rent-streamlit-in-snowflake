"""Branded executive PDF (fpdf2) — cards, narratives, charts, appendices."""

from datetime import datetime
from collections import defaultdict
import io
from datetime import timedelta

def _n_props(n):
    """'1 property' / '3 properties' — avoids '1 properties' in reports."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return f"{n} properties"
    return f"{n} propert{'y' if n == 1 else 'ies'}"


def _n_tenants(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return f"{n} tenants"
    return f"{n:,} tenant{'' if n == 1 else 's'}"


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
    comparable_data=None,
    total_portfolio_units=0,
    property_doors=None,
    monthly_revenue_series=None,
    comparable_current_rev=0,
    pmc_system="yardi",
    use_avg_lift=False,
    asset_class="conventional",
    monthly_by_prop=None,
    latest_month=None,
    selected_charge_codes=None,
    avg_pet_fee=0,
    include_property_charts=False,
    realpage_prop_count=0,
    benchmarks=None,
    prev_snapshot=None,
    prev_snapshot_ts=None,
    current_snapshot=None,
):
    """Generate a branded PDF with card-based KPIs + narrative storytelling.

    Matches the Summary tab visual style: big numbers in cards, short
    narrative sentences, and the key metrics table Eduardo likes.

    Returns PDF bytes ready for st.download_button.
    """
    # Auto-set use_avg_lift for student_housing and seasonal asset classes
    if asset_class in ("student_housing", "seasonal"):
        use_avg_lift = True

    from fpdf import FPDF

    class PDF(FPDF):
        @staticmethod
        def _safe_pdf_text(text):
            """Convert Unicode punctuation to core-font-safe text for fpdf Helvetica."""
            if text is None:
                return ""
            replacements = {
                "\u2013": "-",      # en dash
                "\u2014": "--",     # em dash
                "\u2010": "-",      # hyphen
                "\u2011": "-",      # non-breaking hyphen
                "\u2012": "-",      # figure dash
                "\u2212": "-",      # minus sign
                "\u2018": "'",
                "\u2019": "'",
                "\u201a": ",",
                "\u201c": '"',
                "\u201d": '"',
                "\u201e": '"',
                "\u2026": "...",
                "\u2022": "-",
                "\u00a0": " ",
            }
            safe = str(text)
            for src, dst in replacements.items():
                safe = safe.replace(src, dst)
            # Built-in PDF core fonts are Latin-1 only; replace anything else
            # instead of failing the entire report download.
            return safe.encode("latin-1", "replace").decode("latin-1")

        def normalize_text(self, text):
            return super().normalize_text(self._safe_pdf_text(text))

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
            _gen_ts = datetime.now().strftime("%B %d, %Y at %I:%M %p") + " ET"
            self.cell(0, 10, f'PetScreening Value Report  |  {label}  |  Page {self.page_no()}  |  Generated {_gen_ts}', align='C')

    pdf = PDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # ── Brand colors ──
    dark_blue = (31, 34, 87)
    light_blue = (70, 130, 180)  # steel blue for actuals row
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
    CARD_H = 24          # compact cards to fit 4 rows + How This Scales on page 1

    # ── Helper: sanitize text for fpdf (replace unicode dashes) ──
    def _pdf_safe(text):
        """Replace unicode dashes with ASCII equivalents for fpdf compatibility."""
        if text is None:
            return ""
        return str(text).replace("\u2013", "-").replace("\u2014", "--").replace("\u2019", "'").replace("\u2018", "'")

    # ── Helper: draw a row of 3 KPI cards ──
    # Colors for styled rows
    _sage_green_bg = (235, 243, 235)  # light sage/green for total row cards 2&3
    _teal_blue = (58, 134, 155)  # teal/blue for actuals values
    _actuals_pill_bg = (230, 242, 245)  # light blue pill background
    _opportunity_pill_bg = (255, 240, 230)  # light orange pill background

    def draw_rounded_rect(x, y, w, h, r, style='DF'):
        """Draw a rectangle with rounded corners using arcs."""
        # fpdf2 supports rounded_rect; fallback to regular rect if not available
        try:
            pdf.set_line_width(0.3)
            pdf.rounded_rect(x, y, w, h, r, style=style)
        except AttributeError:
            pdf.rect(x, y, w, h, style)

    def draw_pill_label(x, y, text, bg_color, text_color):
        """Draw a small pill/badge with text."""
        pdf.set_font('Helvetica', 'B', 5.5)
        text_w = pdf.get_string_width(text) + 4
        pill_h = 4
        # Pill background
        pdf.set_fill_color(*bg_color)
        try:
            pdf.rounded_rect(x, y, text_w, pill_h, 1, style='F')
        except AttributeError:
            pdf.rect(x, y, text_w, pill_h, 'F')
        # Pill text
        pdf.set_text_color(*text_color)
        pdf.set_xy(x + 2, y + 0.5)
        pdf.cell(text_w - 4, pill_h - 1, text, align='C')
        return text_w

    def draw_card_row(cards, row_label=None, row_label_color=None, row_style=None):
        """Draw 3 cards side-by-side. Each card is a dict:
        {value: str, label: str, sub: str|None, color: tuple, bg: tuple|None}
        row_label: optional label in a pill above first card (e.g., 'ACTUALS')
        row_label_color: color for the row label text
        row_style: 'total' for combined row styling
        """
        start_y = pdf.get_y()
        # Check if we need a page break (card row + narrative below ~50mm)
        if start_y + CARD_H + 10 > pdf.h - pdf.b_margin:
            pdf.add_page()
            start_y = pdf.get_y()

        # Row label pill above first card
        if row_label and row_style != "total":
            pill_bg = _actuals_pill_bg if row_label_color == _teal_blue else _opportunity_pill_bg
            draw_pill_label(PAGE_L + 2, start_y - 4, row_label.upper(), pill_bg, row_label_color or dark_blue)

        for i, card in enumerate(cards):
            x = PAGE_L + i * (CARD_W + CARD_GAP)
            # Determine card background and text color
            card_bg = card.get("bg", card_fill)
            text_color = card.get("color", dark_blue)

            # Special handling for 'total' row style — all cards get sage green bg
            if row_style == "total":
                card_bg = _sage_green_bg  # light sage/green for all total row cards
                text_color = green

            # Card background with rounded corners
            pdf.set_fill_color(*card_bg)
            pdf.set_draw_color(*card_border)
            draw_rounded_rect(x, start_y, CARD_W, CARD_H, 2, 'DF')

            # Big number
            pdf.set_font('Helvetica', 'B', 16)
            pdf.set_text_color(*text_color)
            pdf.set_xy(x + 2, start_y + 2)
            pdf.cell(CARD_W - 4, 7, card["value"], align='C')

            # Label
            pdf.set_font('Helvetica', '', 6.5)
            pdf.set_text_color(*light_gray)
            pdf.set_xy(x + 2, start_y + 10)
            pdf.cell(CARD_W - 4, 4, card["label"].upper(), align='C')

            # Sub-label OR pills for total row first card
            if row_style == "total" and i == 0 and card.get("actuals_val") and card.get("found_val"):
                # Draw colored pills: "$X actuals" + "+" + "$Y found"
                pill_y = start_y + 14.5
                actuals_text = f"${card['actuals_val']:,.0f} actuals"
                found_text = f"${card['found_val']:,.0f} found"
                actuals_w = pdf.get_string_width(actuals_text) + 6
                found_w = pdf.get_string_width(found_text) + 6
                plus_w = 6
                total_w = actuals_w + plus_w + found_w
                start_x = x + (CARD_W - total_w) / 2
                # Actuals pill (teal/blue bg to match row 2)
                draw_pill_label(start_x, pill_y, actuals_text, _actuals_pill_bg, _teal_blue)
                # Plus sign
                pdf.set_font('Helvetica', 'B', 6)
                pdf.set_text_color(*green)
                pdf.set_xy(start_x + actuals_w + 1, pill_y + 0.5)
                pdf.cell(4, 3, "+", align='C')
                # Found pill (orange bg)
                draw_pill_label(start_x + actuals_w + plus_w, pill_y, found_text, _opportunity_pill_bg, orange)
            elif card.get("sub"):
                pdf.set_font('Helvetica', '', 5.5)
                pdf.set_text_color(150, 150, 150)
                pdf.set_xy(x + 2, start_y + 15)
                pdf.cell(CARD_W - 4, 4, card["sub"], align='C')

        pdf.set_y(start_y + CARD_H + 2)

    def draw_separator_text(text):
        """Draw a small centered separator text in a sage green pill with green text."""
        pdf.set_font('Helvetica', 'B', 5)
        # Measure text width AFTER setting font
        text_upper = text.upper()
        measured_w = pdf.get_string_width(text_upper)
        pill_w = measured_w + 12  # generous padding
        pill_h = 6
        pill_x = PAGE_L + (USABLE_W - pill_w) / 2
        start_y = pdf.get_y()
        pdf.set_fill_color(*_sage_green_bg)  # same sage green as total row
        try:
            pdf.rounded_rect(pill_x, start_y, pill_w, pill_h, 1.5, style='F')
        except AttributeError:
            pdf.rect(pill_x, start_y, pill_w, pill_h, 'F')
        pdf.set_text_color(*green)  # green text
        pdf.set_xy(pill_x + 6, start_y + 1.5)
        pdf.cell(measured_w, 3, text_upper, align='C')
        pdf.set_y(start_y + pill_h + 2)

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
    def section_heading(title, color=dark_blue, min_space=45):
        # Check for page break
        if pdf.get_y() + min_space > pdf.h - pdf.b_margin:
            pdf.add_page()
        pdf.set_font('Helvetica', 'B', 11)
        pdf.set_text_color(*color)
        pdf.cell(0, 6, title, ln=True)
        # Colored underline
        y = pdf.get_y()
        pdf.set_draw_color(*color)
        pdf.set_line_width(0.6)
        pdf.line(PAGE_L, y, PAGE_L + 50, y)
        pdf.set_line_width(0.2)
        pdf.ln(2)

    # ── Helper: narrative text ──
    def narrative(text):
        pdf.set_font('Helvetica', '', 8.5)
        pdf.set_text_color(*body_gray)
        pdf.multi_cell(0, 4, text)
        pdf.ln(1)

    # ── Helper: divider ──
    def divider():
        pdf.set_draw_color(*card_border)
        y = pdf.get_y()
        pdf.line(PAGE_L, y, PAGE_R, y)
        pdf.ln(3)

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

    # ── Helper: "show your work" italic explanation line ──
    def show_work(text):
        """Draw a small italic calculation explanation line below card rows."""
        pdf.set_font('Helvetica', 'I', 7)
        pdf.set_text_color(140, 140, 140)
        pdf.cell(0, 3.5, text, ln=True)
        pdf.ln(1)

    # ── Helper: color for a lift value ──
    def lift_color(val):
        """Color for lift/actuals metrics — teal for positive, orange for negative."""
        if val is None:
            return (160, 160, 160)  # gray for N/A
        return _teal_blue if val >= 0 else orange

    def combined_color(val):
        """Color for combined/total metrics — green for positive."""
        if val is None:
            return (160, 160, 160)
        return green if val >= 0 else orange

    # ── Helper: format large currency values with K/M suffixes ──
    def _format_large_currency(val):
        """Format a currency value with K/M suffix for readability."""
        if val is None or val == 0:
            return "$0"
        sign = "-" if val < 0 else ""
        val = abs(val)
        if val >= 1_000_000:
            return f"{sign}${val / 1_000_000:,.1f}M"
        elif val >= 100_000:
            return f"{sign}${val / 1_000:,.0f}K"
        else:
            return f"{sign}${val:,.0f}"

    # ═══════════════════════════════════════════════════════════
    #  PAGE 1: AT A GLANCE  (comparable-only = apples-to-apples)
    # ═══════════════════════════════════════════════════════════
    _cap_rate = 0.05

    # ── Comparable-only analysis ──
    # Headline numbers use ONLY properties with pre AND post data.
    _comp_current_rev = comparable_current_rev or 0
    _comp_units = 0
    _noncomp_count = 0
    _noncomp_current_rev = 0
    _noncomp_units = 0
    if comparable_data and property_doors:
        _comp_names = set(comparable_data.keys())
        for pname in comparable_data:
            _comp_units += property_doors.get(pname, 0)
        _noncomp_count = n_props_with_data - comparable_count
        _noncomp_current_rev = current_monthly_rev - _comp_current_rev
        for pname in (property_doors or {}):
            if pname not in _comp_names:
                _noncomp_units += property_doors.get(pname, 0)
    elif comparable_data:
        _noncomp_count = n_props_with_data - comparable_count
        _noncomp_current_rev = current_monthly_rev - _comp_current_rev

    # Compute comparable post-launch average (sum of each property's post avg)
    _comp_post_avg = 0
    if comparable_data:
        _comp_post_avg = sum(
            pdata.get("post_recent_avg", pdata.get("post_monthly_avg", 0))
            for pdata in comparable_data.values()
        )

    # RealPage data history doesn't extend far enough to compute pre-PS
    # baselines for properties launched before Jan 2025. We honor that
    # constraint by skipping lift entirely for RealPage and showing
    # current-state revenue + opportunity instead. See the Documentation
    # tab → RealPage → Why Lift Analysis Is Not Shown for the full case.
    _is_realpage = (pmc_system or "").lower() == "real_page"

    # Use comparable numbers for headline when available, fall back to full portfolio
    _has_comparable = comparable_count > 0 and _comp_current_rev > 0 and pre_baseline > 0
    if _has_comparable:
        _headline_current = _comp_current_rev
        _headline_pre = pre_baseline
        _headline_units = _comp_units
        _headline_props = comparable_count
    else:
        _headline_current = current_monthly_rev
        _headline_pre = pre_baseline
        _headline_units = total_units
        _headline_props = n_props_with_data

    # Simple lift (current - pre)
    _monthly_lift = _headline_current - _headline_pre if _headline_pre > 0 else 0
    _simple_pct = (_monthly_lift / _headline_pre * 100) if _headline_pre > 0 and _monthly_lift else 0

    # Average monthly lift (property-by-property average)
    _adjusted_lift = t1_mo if comparable_count > 0 else 0

    # Choose methodology based on use_avg_lift toggle
    # For single-property without pre-PS data, skip lift calculations entirely.
    _no_pre_data = (not _has_comparable and pre_baseline <= 0)
    
    if _no_pre_data:
        # No pre-PS data — leave lift metrics blank, focus on opportunity
        _active_lift = 0
        _lift_per_unit = 0
        _annual_lift = 0
        _asset_value_impact = 0
    elif use_avg_lift:
        # Use average monthly lift (t1_mo) for all calculations
        _active_lift = _adjusted_lift
        _lift_per_unit = _active_lift / _headline_units if _headline_units and _headline_units > 0 and _active_lift else 0
        _annual_lift = _active_lift * 12
        _asset_value_impact = _annual_lift / _cap_rate if _annual_lift > 0 else 0
    else:
        # Use simple lift (current - pre) for all calculations
        _active_lift = _monthly_lift
        _lift_per_unit = _active_lift / _headline_units if _headline_units and _headline_units > 0 and _active_lift else 0
        _annual_lift = _active_lift * 12
        _asset_value_impact = _annual_lift / _cap_rate if _annual_lift > 0 else 0

    # Full portfolio rev-per-unit (used if no comparable)
    _rev_per_unit = current_monthly_rev / total_units if total_units and total_units > 0 else 0

    # Title
    pdf.set_font('Helvetica', 'B', 14)
    pdf.set_text_color(*dark_blue)
    pdf.cell(0, 8, 'At a Glance', ln=True)
    y_line = pdf.get_y()
    pdf.set_draw_color(*dark_blue)
    pdf.set_line_width(0.6)
    pdf.line(PAGE_L, y_line, PAGE_L + 50, y_line)
    pdf.set_line_width(0.2)
    pdf.ln(6)

    # ── Narrative paragraph ──
    # When avg lift toggled, show post avg instead of current
    _display_rev = _comp_post_avg if (use_avg_lift and _comp_post_avg > 0) else _headline_current
    _display_rev_label = "average" if use_avg_lift else "generate"

    _glance_parts = []
    if _has_comparable and _headline_units > 0:
        _glance_parts.append(
            f"Across {_n_props(comparable_count).replace('propert', 'comparable propert')} ({_headline_units:,} units) with "
            f"pre- and post-launch data, pet revenue before PetScreening was "
            f"${_headline_pre:,.0f}/mo."
        )
        if use_avg_lift:
            _glance_parts.append(
                f" Post-launch, those properties average ${_display_rev:,.0f}/mo -- "
                f"a ${_active_lift:,.0f}/mo average monthly lift, or "
                f"${_lift_per_unit:,.2f} per unit per month."
            )
        else:
            _glance_parts.append(
                f" Today those same properties generate ${_display_rev:,.0f}/mo -- "
                f"a ${_active_lift:,.0f}/mo increase ({_simple_pct:+.1f}%), or "
                f"${_lift_per_unit:,.2f} per unit per month."
            )
        if _asset_value_impact > 0:
            _glance_parts.append(
                f" At a 5% cap rate, that represents ~${_asset_value_impact:,.0f} in added asset value."
            )
    elif _has_comparable:
        if use_avg_lift:
            _glance_parts.append(
                f"Across {comparable_count} comparable properties, pet revenue was "
                f"${_headline_pre:,.0f}/mo before PetScreening. Post-launch average is "
                f"${_display_rev:,.0f}/mo -- a ${_active_lift:,.0f}/mo average monthly lift."
            )
        else:
            _glance_parts.append(
                f"Across {comparable_count} comparable properties, pet revenue was "
                f"${_headline_pre:,.0f}/mo before PetScreening and is now "
                f"${_display_rev:,.0f}/mo -- a ${_active_lift:,.0f}/mo increase ({_simple_pct:+.1f}%)."
            )
    elif _no_pre_data:
        # No pre-PS data available
        _glance_parts.append(
            f"This property generates ${current_monthly_rev:,.0f}/mo in pet fee revenue. "
            f"No pre-PetScreening baseline is available for lift comparison -- see Opportunity section below for actionable revenue."
        )
    else:
        _glance_parts.append(
            f"This portfolio currently generates ${current_monthly_rev:,.0f}/mo in pet fee revenue "
            f"across {n_props_with_data} properties."
        )
    # Add opportunity context (missing rent + suspected) -- emphasize this is
    # across the entire portfolio, not just the comparable subset analyzed above.
    _glance_leakage_mo = (_opp_recurring_mo or 0) + (su_current_mo or 0)
    _glance_leakage_tenants = (t2_tenants or 0) + (su_total_profiles or 0)
    if _glance_leakage_mo > 0 and _glance_leakage_tenants > 0:
        _glance_parts.append(
            f" Across the entire portfolio, {_glance_leakage_tenants:,} residents are not paying pet "
            f"fees today -- a combined ~${_glance_leakage_mo:,.0f}/mo opportunity (confirmed missing "
            f"rent + suspected undisclosed pets)."
        )

    narrative("".join(_glance_parts))

    # ── RealPage current-state disclosure ──
    # Keeps the story straight when the selection includes RealPage
    # properties: they contribute current revenue and the opportunity
    # numbers (missing + suspected), but no before/after lift, because the
    # RealPage API only exposes today's snapshot (ledger access pending).
    if realpage_prop_count and realpage_prop_count > 0:
        pdf.ln(1)
        _rp_note_y = pdf.get_y()
        pdf.set_fill_color(249, 244, 230)  # Dog Bone White accent
        pdf.set_draw_color(226, 171, 88)   # golden accent bar
        pdf.rect(PAGE_L, _rp_note_y, PAGE_R - PAGE_L, 11, style='F')
        pdf.rect(PAGE_L, _rp_note_y, 1.2, 11, style='F')
        pdf.set_line_width(0.8)
        pdf.line(PAGE_L, _rp_note_y, PAGE_L, _rp_note_y + 11)
        pdf.set_line_width(0.2)
        pdf.set_xy(PAGE_L + 3, _rp_note_y + 1.5)
        pdf.set_font('Helvetica', 'B', 8)
        pdf.set_text_color(*dark_blue)
        pdf.cell(0, 4, _pdf_safe('Data note - RealPage properties'), ln=True)
        pdf.set_x(PAGE_L + 3)
        pdf.set_font('Helvetica', '', 7.5)
        pdf.set_text_color(*body_gray)
        pdf.multi_cell(PAGE_R - PAGE_L - 6, 3.2, _pdf_safe(
            f"{_n_props(realpage_prop_count)} in this report run{'s' if realpage_prop_count == 1 else ''} "
            f"on RealPage, which provides a current-state snapshot only. RealPage data is "
            f"included at today's collected revenue and in the uncollected/suspected "
            f"opportunity figures, and is excluded from the before/after lift analysis."
        ))
        pdf.ln(2)

    pdf.ln(1)

    # ── KPI cards: 3 summary cards ──
    _glance_row1 = []
    if _has_comparable and _headline_units > 0:
        _glance_row1.append({
            "value": f"{_headline_units:,}",
            "label": "Comparable Units",
            "sub": f"{comparable_count} properties with pre & post data",
            "color": dark_blue,
        })
    elif total_units and total_units > 0:
        _glance_row1.append({
            "value": f"{total_units:,}",
            "label": "Live Units",
            "sub": f"Across {_n_props(n_props_with_data)}",
            "color": dark_blue,
        })
    else:
        _glance_row1.append({
            "value": f"{n_props_with_data}",
            "label": "Properties Analyzed",
            "sub": f"of {n_props_total} total",
            "color": dark_blue,
        })

    _glance_row1.append({
        "value": f"${_headline_pre:,.0f}/mo" if _headline_pre > 0 else "--",
        "label": "Pre-PS Revenue",
        "sub": (
            f"Avg baseline across {comparable_count} properties" if _has_comparable
            else ("No pre-launch data" if _no_pre_data else None)
        ),
        "color": dark_blue if _headline_pre > 0 else (160, 160, 160),
    })
    if use_avg_lift and _comp_post_avg > 0:
        _glance_row1.append({
            "value": f"${_comp_post_avg:,.0f}/mo",
            "label": "Post-PS Avg Revenue",
            "sub": f"Avg across {comparable_count} properties, all post-launch months",
            "color": dark_blue,
        })
    else:
        _glance_row1.append({
            "value": f"${_headline_current:,.0f}/mo" if _has_comparable else f"${current_monthly_rev:,.0f}/mo",
            "label": "Current Revenue",
            "sub": f"Same {comparable_count} properties, latest month" if _has_comparable else "Most recently completed month",
            "color": dark_blue,
        })
    draw_card_row(_glance_row1)
    pdf.ln(1)

    # ── KPI cards -- row 2: lift + per-unit + cap rate ──
    _glance_row2 = []
    if _no_pre_data:
        _glance_row2.append({
            "value": "--",
            "label": "Monthly Lift",
            "sub": "No pre-launch baseline",
            "color": (160, 160, 160),
        })
        _glance_row2.append({
            "value": "--",
            "label": "Lift per Unit",
            "sub": "No pre-launch baseline",
            "color": (160, 160, 160),
        })
    elif _active_lift != 0:
        _lift_sign = "+" if _active_lift > 0 else ""
        _lift_label = "Average Monthly Lift" if use_avg_lift else "Monthly Lift"
        _lift_sub = f"${_comp_post_avg:,.0f} avg - ${_headline_pre:,.0f} pre" if use_avg_lift else f"${_headline_current:,.0f} - ${_headline_pre:,.0f} ({_simple_pct:+.1f}%)"
        _glance_row2.append({
            "value": f"{_lift_sign}${_active_lift:,.0f}/mo",
            "label": _lift_label,
            "sub": _lift_sub,
            "color": _teal_blue if _active_lift > 0 else orange,
        })
        if _headline_units and _headline_units > 0 and _lift_per_unit != 0:
            _lpu_sign = "+" if _lift_per_unit > 0 else ""
            _glance_row2.append({
                "value": f"{_lpu_sign}${_lift_per_unit:,.2f}/unit/mo",
                "label": "Lift per Unit",
                "sub": f"${_active_lift:,.0f} / {_headline_units:,} units",
                "color": _teal_blue if _lift_per_unit > 0 else orange,
            })
    elif total_units and total_units > 0 and _rev_per_unit > 0:
        _glance_row2.append({
            "value": f"${_rev_per_unit:,.2f}/unit/mo",
            "label": "Revenue per Unit",
            "sub": f"${current_monthly_rev:,.0f} / {total_units:,} units",
            "color": _teal_blue,
        })
    if _asset_value_impact > 0:
        _avi_str = _format_large_currency(_asset_value_impact)
        _glance_row2.append({
            "value": _avi_str,
            "label": "Est. Asset Value Impact",
            "sub": f"${_active_lift:,.0f}/mo x 12 / 5% cap rate",
            "color": _teal_blue,
        })

    # Pad row 2 to 3 cards (but skip padding for no-pre-data case)
    if not _no_pre_data:
        while len(_glance_row2) < 3:
            if _opp_recurring_mo > 0 and t2_tenants > 0:
                _glance_row2.append({
                    "value": f"${_opp_recurring_mo:,.0f}/mo",
                    "label": "Revenue Left on Table",
                    "sub": f"{t2_tenants:,} tenants not paying pet rent",
                    "color": orange,
                })
            else:
                _glance_row2.append({"value": "", "label": "", "sub": None, "color": dark_blue})
            break

    # Only show Actuals row if we have pre-PS data to compare against
    if _glance_row2 and not _no_pre_data:
        pdf.ln(3)  # space for label pill
        draw_card_row(_glance_row2, row_label="Actuals", row_label_color=_teal_blue)
    elif _no_pre_data:
        # Skip Actuals row entirely for no pre-PS data — the Opportunity row will show actionable data
        pass

    pdf.ln(1)

    # ── KPI cards -- row 3: Pet Revenue Found (formerly Leakage) ──
    # Found = confirmed missing rent + suspected undisclosed pets
    _leakage_mo = _opp_recurring_mo + (su_current_mo or 0)
    _leakage_tenants = (t2_tenants or 0) + (su_total_profiles or 0)
    _units_for_per_unit = _headline_units if _headline_units and _headline_units > 0 else total_units
    _leakage_per_unit = _leakage_mo / _units_for_per_unit if _units_for_per_unit and _units_for_per_unit > 0 and _leakage_mo > 0 else 0
    _cumulative_lift_per_unit = _lift_per_unit + _leakage_per_unit  # actuals + found
    _total_combined_mo = _active_lift + _leakage_mo  # actuals lift + found
    _projected_annual_combined = _total_combined_mo * 12
    _combined_asset_value = _projected_annual_combined / _cap_rate if _projected_annual_combined > 0 else 0

    _leakage_annual = _leakage_mo * 12
    _leakage_asset_value = _leakage_annual / _cap_rate if _leakage_annual > 0 else 0

    if _leakage_mo > 0:
        _glance_row3 = []
        _glance_row3.append({
            "value": f"${_leakage_mo:,.0f}/mo",
            "label": "Pet Revenue Found",
            "sub": f"{_leakage_tenants:,} residents not paying pet fees",
            "color": orange,
        })
        if _units_for_per_unit and _units_for_per_unit > 0:
            _glance_row3.append({
                "value": f"${_leakage_per_unit:,.2f}/unit/mo",
                "label": "Found per Unit",
                "sub": f"${_leakage_mo:,.0f} / {_units_for_per_unit:,} units  |  Combined: ${_cumulative_lift_per_unit:,.2f}/unit/mo",
                "color": orange,
            })
        _glance_row3.append({
            "value": _format_large_currency(_leakage_asset_value),
            "label": "Est. Value Impact (Found)",
            "sub": f"${_leakage_mo:,.0f}/mo x 12 / 5% cap rate",
            "color": orange,
        })
        # Pad to 3 cards
        while len(_glance_row3) < 3:
            _glance_row3.append({"value": "", "label": "", "sub": None, "color": dark_blue})

        pdf.ln(2)  # space for label
        draw_card_row(_glance_row3, row_label="Opportunity", row_label_color=orange)
        pdf.ln(1)

        # Separator text
        draw_separator_text("Total = Monthly Lift (Actuals) + Pet Revenue Found (Opportunity)")

        # ── KPI cards -- row 4: Combined (Actuals + Found) ──
        _glance_row4 = []
        _glance_row4.append({
            "value": f"${_total_combined_mo:,.0f}/mo",
            "label": "Total Opportunity",
            "sub": "Monthly Lift + Pet Revenue Found",
            "color": green,
            "actuals_val": _active_lift,
            "found_val": _leakage_mo,
        })
        if _units_for_per_unit and _units_for_per_unit > 0:
            _glance_row4.append({
                "value": f"${_cumulative_lift_per_unit:,.2f}/unit/mo",
                "label": "Combined Lift per Unit",
                "sub": f"${_lift_per_unit:,.2f} actual + ${_leakage_per_unit:,.2f} found",
                "color": green,
            })
        _glance_row4.append({
            "value": _format_large_currency(_combined_asset_value),
            "label": "Combined Est. Value Impact",
            "sub": f"${_total_combined_mo:,.0f}/mo x 12 / 5% cap rate",
            "color": green,
        })
        while len(_glance_row4) < 3:
            _glance_row4.append({"value": "", "label": "", "sub": None, "color": dark_blue})

        draw_card_row(_glance_row4, row_label="Total Opportunity", row_label_color=green, row_style="total")

    elif _asset_value_impact > 0:
        # No found revenue — show cap rate callout instead
        pdf.ln(1)
        callout_box(
            f"Estimated Asset Value Impact: ${_asset_value_impact:,.0f} "
            f"(${_active_lift:,.0f}/mo x 12 = ${_annual_lift:,.0f}/yr at 5% cap rate)"
        )

    # ── Portfolio extrapolation callout (if non-comparable properties exist) ──
    # Portfolio-wide extrapolation ("how this scales") is OPT-IN: it only
    # renders when the Total Portfolio Units optional input was filled in
    # before fetching. No manual value -> no projection anywhere in the PDF.
    _effective_portfolio_units = total_portfolio_units if total_portfolio_units and total_portfolio_units > 0 else 0
    if _effective_portfolio_units > 0 and _noncomp_count > 0 and _noncomp_units > 0 and _lift_per_unit > 0:
        pdf.ln(2)
        # Use the combined per-unit (actuals lift + found) so the zinger reflects
        # the same number we surface in the Total Opportunity row when possible.
        _zinger_per_unit = _cumulative_lift_per_unit if (_leakage_mo > 0 and _cumulative_lift_per_unit > 0) else _lift_per_unit
        _extrapolated_noncomp_lift = _zinger_per_unit * _noncomp_units
        _total_portfolio_lift = (_active_lift + (_leakage_mo or 0)) + _extrapolated_noncomp_lift
        _total_portfolio_lift_annual = _total_portfolio_lift * 12

        # Prominent multi-line callout (matches callout_box visual style but
        # supports wrapped narrative text + a colored total number).
        _cb_x = PAGE_L + 4
        _cb_w = USABLE_W - 8
        _cb_start_y = pdf.get_y()
        _cb_text = (
            f"Applying the combined ${_zinger_per_unit:,.2f}/unit/mo across ALL "
            f"{_effective_portfolio_units:,} units in {label}'s portfolio -- including "
            f"{_noncomp_count} properties ({_noncomp_units:,} units) without pre-launch "
            f"data -- the estimated total portfolio impact would be "
            f"~${_total_portfolio_lift:,.0f}/mo (${_total_portfolio_lift_annual:,.0f}/yr)."
        )
        # Measure required height
        pdf.set_font('Helvetica', 'B', 9.5)
        _cb_line_h = 5
        _cb_pad = 3
        _cb_lines = pdf.multi_cell(_cb_w - 8, _cb_line_h, _cb_text, split_only=True) if hasattr(pdf, 'multi_cell') else [_cb_text]
        try:
            _cb_line_count = max(1, len(_cb_lines))
        except Exception:
            _cb_line_count = 3
        _cb_h = _cb_line_count * _cb_line_h + _cb_pad * 2
        # Background + left accent bar
        pdf.set_fill_color(245, 250, 240)
        pdf.set_draw_color(*green)
        pdf.rect(_cb_x, _cb_start_y, _cb_w, _cb_h, 'F')
        pdf.set_fill_color(*green)
        pdf.rect(_cb_x, _cb_start_y, 2.5, _cb_h, 'F')
        # Text body in green-tinted dark color
        pdf.set_xy(_cb_x + 6, _cb_start_y + _cb_pad)
        pdf.set_text_color(*green)
        pdf.set_font('Helvetica', 'B', 9.5)
        pdf.multi_cell(_cb_w - 8, _cb_line_h, _cb_text)
        pdf.set_y(_cb_start_y + _cb_h + 2)
        pdf.ln(1)

    # ═══════════════════════════════════════════════════════════
    #  HOW THIS SCALES — PORTFOLIO EXTRAPOLATION (same page)
    # ═══════════════════════════════════════════════════════════
    # Use combined lift per unit (actuals + found) for scaling
    # Skip "How This Scales" for single-property PDFs -- only show for parent company reports
    # Opt-in: requires the manually-entered Total Portfolio Units (see above).
    _scale_lift_per_unit = _cumulative_lift_per_unit if _leakage_mo > 0 and _cumulative_lift_per_unit > 0 else _lift_per_unit
    _show_scaling = (n_props_total > 1 and _effective_portfolio_units > 0
                     and _scale_lift_per_unit != 0 and _scale_lift_per_unit > 0)
    if _show_scaling:
        pdf.ln(2)
        section_heading("How This Scales", green, min_space=30)

        _proj_monthly = _scale_lift_per_unit * _effective_portfolio_units
        _proj_annual = _proj_monthly * 12
        _proj_asset_value = _proj_annual / _cap_rate

        _lift_source = "combined lift + found" if _leakage_mo > 0 else "lift"
        _scale_text = (
            f"If {label} were to roll PetScreening across their full portfolio of "
            f"~{_effective_portfolio_units:,} units, the ${_scale_lift_per_unit:,.2f}/unit/mo {_lift_source} "
            f"would translate to approximately ${_proj_annual:,.0f}/yr in additional pet fee revenue. "
            f"Capitalized at a 5% cap rate, that represents ~${_proj_asset_value:,.0f} in added asset value."
        )
        narrative(_scale_text)

        pdf.ln(1)
        draw_card_row([
            {
                "value": f"~{_effective_portfolio_units:,}",
                "label": "Total Portfolio Units",
                "sub": "Full portfolio rollout target",
                "color": dark_blue,
            },
            {
                "value": f"${_proj_annual:,.0f}/yr",
                "label": "Projected Annual Lift",
                "sub": f"${_scale_lift_per_unit:,.2f}/unit/mo x {_effective_portfolio_units:,} units x 12",
                "color": green,
            },
            {
                "value": _format_large_currency(_proj_asset_value),
                "label": "Projected Asset Value Impact",
                "sub": f"${_proj_annual:,.0f}/yr at 5% cap rate",
                "color": green,
            },
        ])

        show_work(
            f"Per-unit {_lift_source}: ${_scale_lift_per_unit:,.2f}/unit/mo x {_effective_portfolio_units:,} units "
            f"= ${_proj_monthly:,.0f}/mo x 12 = ${_proj_annual:,.0f}/yr / 0.05 = ${_proj_asset_value:,.0f}"
        )

    # Force new page for detailed sections
    pdf.add_page()

    # ═══════════════════════════════════════════════════════════
    #  SECTION 1: VALUE CREATED
    # ═══════════════════════════════════════════════════════════
    section_heading("Value Created", green)

    # Use comparable or full portfolio for Value Created cards
    # When avg lift toggled, show post avg instead of current
    if use_avg_lift and _comp_post_avg > 0:
        _vc_current = _comp_post_avg
        _vc_current_label = "Post-PS Avg Pet Revenue"
    else:
        _vc_current = _headline_current if _has_comparable else current_monthly_rev
        _vc_current_label = "Current Monthly Pet-Related Revenue"
    _vc_props_label = f"{comparable_count} comparable properties" if _has_comparable else f"{n_props_with_data} properties"

    # Choose lift based on methodology toggle (same as page 1)
    if use_avg_lift:
        _vc_lift = _adjusted_lift
        _vc_lift_label = "Average Monthly Lift"
        _vc_lift_sub = "Property-by-property average" if comparable_count > 0 and _vc_lift != 0 else None
    else:
        _vc_lift = _monthly_lift
        _vc_lift_label = "Monthly Lift"
        _vc_lift_sub = f"{_simple_pct:+.1f}% vs baseline" if _has_comparable and _vc_lift != 0 else None

    _sign_vc = "+" if _vc_lift > 0 else ""
    _vc_color = _teal_blue if _vc_lift >= 0 else orange  # teal for actuals, matches At a Glance

    # Calculate asset value impact from chosen methodology
    _vc_annual_lift = _vc_lift * 12
    _vc_asset_value = _vc_annual_lift / _cap_rate if _vc_annual_lift > 0 else 0

    draw_card_row([
        {
            "value": f"${_vc_current:,.0f}/mo",
            "label": _vc_current_label,
            "sub": f"Across {_vc_props_label}",
            "color": dark_blue,
        },
        {
            "value": f"{_sign_vc}${_vc_lift:,.0f}/mo" if _vc_lift != 0 else "--",
            "label": _vc_lift_label,
            "sub": _vc_lift_sub,
            "color": _vc_color,
        },
        {
            "value": _format_large_currency(_vc_asset_value) if _vc_asset_value > 0 else "--",
            "label": "Est. Asset Value Impact",
            "sub": f"${_vc_lift:,.0f}/mo x 12 / 5% cap" if _vc_asset_value > 0 else None,
            "color": _teal_blue if _vc_asset_value > 0 else dark_blue,
        },
    ])

    # Show your work
    show_work(f"Current revenue: sum of pet fee charges across {_vc_props_label} for the latest month of data.")

    if _vc_lift != 0 and (_has_comparable or _vc_current > 0):
        if _vc_lift > 0:
            narrative(
                f"Across {_vc_props_label}, pet revenue is ${_vc_current:,.0f}/mo, "
                f"representing a ${_vc_lift:,.0f}/mo lift from the pre-launch baseline. "
                f"At a 5% cap rate, this represents ~${_vc_asset_value:,.0f} in added asset value."
            )
        else:
            # Negative lift — acknowledge and pivot to opportunity
            _neg_mo = abs(_vc_lift)
            _pivot_parts = [
                f"Pet fee revenue is currently ${_neg_mo:,.0f}/mo below the pre-launch baseline. "
            ]
            # Pivot to opportunity
            if t2_tenants > 0 and _opp_recurring_mo > 0:
                _pivot_parts.append(
                    f"However, ${_opp_recurring_mo:,.0f}/mo in pet rent is going uncollected from "
                    f"tenants who have already completed screening -- a billing correction that would "
                    f"more than close this gap. "
                )
            if t3_adoption is not None and t3_adoption < 100 and total_projected and total_projected > 0:
                _base_rev_for_100 = _comp_post_avg if (use_avg_lift and _comp_post_avg > 0) else (current_monthly_rev or 0)
                _additional_at_100 = total_projected - _base_rev_for_100
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

    # ── DATA TRANSPARENCY ──
    # Friendly PMC label — covers Yardi/Entrata, plus the underscore'd 'real_page'
    _pmc_label = {
        "yardi": "Yardi",
        "entrata": "Entrata",
        "real_page": "RealPage",
    }.get((pmc_system or "").lower(), (pmc_system.capitalize() if pmc_system else "PMS"))
    _dt_lines = []

    # Methodology note when using average lift
    if use_avg_lift:
        _dt_lines.append(
            "This report uses Average Monthly Lift methodology. For portfolios with "
            "seasonal fluctuation (student housing, vacation properties), comparing any "
            "single month to pre-launch can be misleading. Instead, we compare each "
            "property's post-launch average to its pre-launch baseline for a more "
            "stable view of PetScreening's impact."
        )

    # PMC scope disclaimer
    _dt_lines.append(
        f"This report covers {_pmc_label} data only. If this portfolio uses "
        f"multiple property management systems, properties on other platforms "
        f"are not included in these numbers."
    )

    # Comparable vs non-comparable breakdown
    if _has_comparable:
        if use_avg_lift:
            # When using avg lift, emphasize the post-avg increase
            _active_lift_dt = _adjusted_lift if _adjusted_lift else 0
            if _active_lift_dt > 0:
                _dt_lines.append(
                    f"Across {comparable_count} comparable properties, post-launch pet revenue averages "
                    f"${_comp_post_avg:,.0f}/mo -- an estimated +${_active_lift_dt:,.0f}/mo increase "
                    f"({(_active_lift_dt / pre_baseline * 100):.1f}%) compared to the pre-launch baseline of ${pre_baseline:,.0f}/mo."
                )
        elif _noncomp_count > 0:
            _nc_detail = f"{_noncomp_count} additional properties"
            if _noncomp_units > 0:
                _nc_detail += f" ({_noncomp_units:,} units)"
            _nc_detail += f" generate ${_noncomp_current_rev:,.0f}/mo in pet revenue but have no pre-launch data for comparison, so are excluded from the lift analysis."
            _dt_lines.append(_nc_detail)
            _dt_lines.append(
                f"Total portfolio pet revenue across all {n_props_with_data} {_pmc_label} properties: "
                f"${current_monthly_rev:,.0f}/mo."
            )
        else:
            _dt_lines.append(
                f"All {n_props_with_data} properties with charge data have pre-launch baselines."
            )

    if _dt_lines:
        pdf.ln(1)
        pdf.set_font('Helvetica', 'B', 9)
        pdf.set_text_color(*light_blue)
        pdf.cell(0, 5, 'Data Transparency', ln=True)
        pdf.ln(1)

        pdf.set_font('Helvetica', '', 8.5)
        pdf.set_text_color(*body_gray)
        for _dt_line in _dt_lines:
            pdf.multi_cell(USABLE_W, 4.5, _dt_line)
            pdf.ln(1)
        pdf.ln(1)

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
            "sub": f"Across {_n_props(t2_props)}" if t2_tenants > 0 else "All tenants compliant",
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

    # Show your work
    if t2_tenants > 0 and _opp_recurring_mo > 0:
        _avg_fee_sw = _opp_recurring_mo / t2_tenants
        show_work(f"{t2_tenants:,} tenants x ${_avg_fee_sw:,.0f}/mo avg fee = ${_opp_recurring_mo:,.0f}/mo uncollected")

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
            if avg_pet_fee and avg_pet_fee > 0:
                narrative(
                    f"Additionally, {_n_tenants(su_total_profiles)} show{'s' if su_total_profiles == 1 else ''} signals of undisclosed pets "
                    f"(abandoned screening, unresolved requests). Using the same ${avg_pet_fee:,.0f}/mo "
                    f"avg fee, that represents ~${su_current_mo:,.0f}/mo in potential additional revenue."
                )
            else:
                narrative(
                    f"Additionally, {_n_tenants(su_total_profiles)} show{'s' if su_total_profiles == 1 else ''} signals of undisclosed pets "
                    f"(abandoned screening, unresolved requests), representing an estimated "
                    f"${su_current_mo:,.0f}/mo in potential additional revenue."
                )
    elif su_total_profiles and su_total_profiles > 0:
        if avg_pet_fee and avg_pet_fee > 0:
            narrative(
                f"No confirmed tenants are missing pet rent charges. However, "
                f"{_n_tenants(su_total_profiles)} show{'s' if su_total_profiles == 1 else ''} signals of undisclosed pets. Using the same "
                f"${avg_pet_fee:,.0f}/mo avg fee, that represents ~${su_current_mo:,.0f}/mo in "
                f"potential additional revenue."
            )
        else:
            narrative(
                f"No confirmed tenants are missing pet rent charges. However, "
                f"{_n_tenants(su_total_profiles)} show{'s' if su_total_profiles == 1 else ''} signals of undisclosed pets, representing "
                f"an estimated ${su_current_mo:,.0f}/mo in potential additional revenue."
            )
    else:
        _opportunity_note = (
            "All screened tenants are currently being charged pet rent -- no billing gaps identified."
        )
        if t3_adoption is not None and t3_adoption < 100 and total_projected and total_projected > 0:
            _opp_base_rev = _comp_post_avg if (use_avg_lift and _comp_post_avg > 0) else (current_monthly_rev or 0)
            _additional_at_100 = total_projected - _opp_base_rev
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
            "sub": f"Across {_n_props(n_props_with_data)}",
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
            _ph_base_rev = _comp_post_avg if (use_avg_lift and _comp_post_avg > 0) else (current_monthly_rev or 0)
            _additional_at_100 = total_projected - _ph_base_rev
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

    # ── Suspected Undisclosed explainer (separate block with divider) ──
    if su_total_profiles and su_total_profiles > 0:
        pdf.set_draw_color(*card_border)
        _sep_y = pdf.get_y()
        pdf.line(PAGE_L, _sep_y, PAGE_R, _sep_y)
        pdf.ln(2)
        pdf.set_font('Helvetica', 'I', 6.5)
        pdf.set_text_color(*light_gray)
        pdf.multi_cell(USABLE_W, 3,
            "What are Suspected Undisclosed Pets?  Tenants who started a PetScreening profile "
            "but never completed it, have an unresolved assistance animal request, or declared "
            "'no pet' after starting an assistance profile.  Not confirmed pet owners "
            "-- directional signals for follow-up, not billing."
        )

    # ═══════════════════════════════════════════════════════════
    #  PROGRESS SINCE LAST REPORT (needs a prior snapshot)
    # ═══════════════════════════════════════════════════════════
    if prev_snapshot and current_snapshot:
        try:
            _good_c = (103, 120, 72)   # sage green
            _bad_c = (207, 90, 63)     # rust orange
            _ps_date = str(prev_snapshot_ts)[:10] if prev_snapshot_ts else "previous run"
            _delta_specs = [
                ("current_monthly_rev", "Current pet revenue", "${:,.0f}/mo", True),
                ("monthly_lift", "Monthly lift", "${:,.0f}/mo", True),
                ("missing_tenants", "Tenants missing pet rent", "{:,.0f}", False),
                ("missing_monthly", "Uncollected revenue", "${:,.0f}/mo", False),
                ("suspected_tenants", "Suspected undisclosed", "{:,.0f}", False),
                ("avg_adoption", "Avg adoption", "{:.1f}%", True),
            ]
            _delta_rows = []
            for _dk, _dl, _dfmt, _up_good in _delta_specs:
                _nv, _ov = current_snapshot.get(_dk), prev_snapshot.get(_dk)
                if _nv is None or _ov is None:
                    continue
                try:
                    _nv, _ov = float(_nv), float(_ov)
                except (TypeError, ValueError):
                    continue
                _delta_rows.append((_dl, _dfmt.format(_ov), _dfmt.format(_nv),
                                    _nv - _ov, _dfmt, _up_good))
            if _delta_rows:
                section_heading('Progress Since Last Report', dark_blue)
                narrative(f"Movement in the headline numbers since the report generated on {_ps_date}. "
                          f"Green marks improvement.")
                _pw = [58, 38, 38, 46]
                pdf.set_fill_color(249, 244, 230)
                pdf.set_draw_color(*card_border)
                pdf.set_font('Helvetica', 'B', 8)
                pdf.set_text_color(*dark_blue)
                for _w, _h in zip(_pw, ("METRIC", "LAST REPORT", "NOW", "CHANGE")):
                    pdf.cell(_w, 6, f" {_h}", border=1, fill=True)
                pdf.ln()
                pdf.set_font('Helvetica', '', 8)
                for _i, (_dl, _olds, _news, _d, _dfmt, _up_good) in enumerate(_delta_rows):
                    pdf.set_fill_color(255, 255, 255) if _i % 2 == 0 else pdf.set_fill_color(250, 250, 248)
                    pdf.set_text_color(*body_gray)
                    pdf.cell(_pw[0], 6, _pdf_safe(f" {_dl}"), border='LR', fill=True)
                    pdf.cell(_pw[1], 6, f" {_olds}", border='LR', fill=True)
                    pdf.set_text_color(*dark_blue)
                    pdf.cell(_pw[2], 6, f" {_news}", border='LR', fill=True)
                    _improved = (_d > 0) == _up_good and _d != 0
                    pdf.set_text_color(*(_good_c if (_improved or _d == 0) else _bad_c))
                    _chg = ("+" if _d >= 0 else "-") + _dfmt.format(abs(_d))
                    if _d == 0:
                        _chg = "no change"
                    pdf.cell(_pw[3], 6, f" {_chg}", border='LR', fill=True)
                    pdf.ln()
                pdf.cell(sum(_pw), 0, '', border='T', ln=True)
                pdf.ln(4)
        except Exception as _prog_err:  # noqa: BLE001 — never break the report
            pdf.set_font('Helvetica', 'I', 7)
            pdf.set_text_color(200, 100, 100)
            pdf.cell(0, 4, f'Progress section error: {str(_prog_err)[:80]}', ln=True)

    # ═══════════════════════════════════════════════════════════
    #  PET RENT PRICING — what this portfolio charges (own data)
    # ═══════════════════════════════════════════════════════════
    if benchmarks and benchmarks.get("pricing"):
        try:
            section_heading('Pet Rent Pricing', dark_blue)
            _bmp = benchmarks["pricing"]
            _n_bp = _bmp["n_props"]
            if _n_bp > 1:
                draw_card_row([
                    {"value": f"${_bmp['median']:,.0f}/mo", "label": "TYPICAL PET RENT",
                     "sub": f"median across {_n_bp:,} properties"},
                    {"value": f"${_bmp['min']:,.0f}-${_bmp['max']:,.0f}",
                     "label": "FULL RANGE", "sub": "lowest to highest property"},
                    {"value": f"${_bmp['p25']:,.0f}-${_bmp['p75']:,.0f}",
                     "label": "MIDDLE 50%", "sub": "25th-75th percentile"},
                ])
                narrative(
                    f"Across the {_n_bp:,} properties with recurring pet rent in this report, "
                    f"monthly fees range from ${_bmp['min']:,.0f} to ${_bmp['max']:,.0f} "
                    f"(median ${_bmp['median']:,.0f}/mo; the middle half charge "
                    f"${_bmp['p25']:,.0f}-${_bmp['p75']:,.0f}/mo). A wide spread can be a "
                    f"pricing opportunity: properties at the low end may support rates closer "
                    f"to the portfolio median."
                )
            else:
                draw_card_row([
                    {"value": f"${_bmp['median']:,.0f}/mo", "label": "TYPICAL PET RENT",
                     "sub": "recurring pet rent at this property"},
                ])
            pdf.set_font('Helvetica', 'I', 6.5)
            pdf.set_text_color(*light_gray)
            pdf.multi_cell(USABLE_W, 3, _pdf_safe(
                "Basis: recurring pet-rent charges pulled live from your property management "
                "system for this report (per-property typical fee, per-tenant medians)."
            ))
            pdf.ln(3)
        except Exception as _bm_err:  # noqa: BLE001
            pdf.set_font('Helvetica', 'I', 7)
            pdf.set_text_color(200, 100, 100)
            pdf.cell(0, 4, f'Pricing section error: {str(_bm_err)[:80]}', ln=True)

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

    # metrics: list of (label, value_str, color_override_or_None)
    metrics = []
    # Revenue metric label/value based on toggle
    _km_rev = _comp_post_avg if (use_avg_lift and _comp_post_avg > 0) else current_monthly_rev
    _km_rev_label = "Post-PS Avg Pet Revenue (comparable)" if use_avg_lift else "Current Monthly Pet-Related Revenue (total portfolio)"
    # "--" instead of "$0/mo" when there is no pre-launch data — a zero
    # implies an observed baseline that does not exist (e.g. RealPage).
    metrics.append((
        "Pre-PS Baseline",
        f"${pre_baseline:,.0f}/mo" if comparable_count > 0 else "--",
        None,
    ))
    metrics.append((_km_rev_label, f"${_km_rev:,.0f}/mo", None))
    if comparable_count > 0 and (_monthly_lift != 0 or t1_mo != 0):
        # Only show the methodology being used — less noise, fewer questions
        _active_lift_m = _adjusted_lift if use_avg_lift else _monthly_lift
        _active_lift_label = "Average Monthly Lift" if use_avg_lift else "Monthly Lift"
        _active_sign = "+" if _active_lift_m > 0 else ""
        metrics.append((_active_lift_label, f"{_active_sign}${_active_lift_m:,.0f}/mo", lift_color(_active_lift_m)))
        _annual_lift_m = _active_lift_m * 12
        metrics.append(("Annualized Lift", f"${_annual_lift_m:,.0f}/yr", lift_color(_annual_lift_m)))
        if _annual_lift_m > 0:
            _avi_m = _annual_lift_m / 0.05
            metrics.append(("Est. Asset Value Impact (5% Cap)", _format_large_currency(_avi_m), _teal_blue))
    if t2_tenants > 0:
        metrics.append(("Tenants Not Paying Pet Rent", f"{t2_tenants:,}", orange))
        metrics.append(("Uncollected Revenue", f"${_opp_recurring_mo:,.0f}/mo", orange))
    if su_total_profiles and su_total_profiles > 0:
        metrics.append(("Suspected Undisclosed Pets", f"{su_total_profiles:,}", orange))
        metrics.append(("Suspected Undisclosed Revenue", f"~${su_current_mo:,.0f}/mo", orange))
    if t3_adoption is not None:
        metrics.append((f"{adopt_type_label} Adoption", f"{t3_adoption:.1f}%", green if t3_adoption >= 50 else orange))
    # Use methodology-appropriate revenue for Additional Opportunity at 100%
    if total_projected and total_projected > 0:
        _additional_at_100_km = total_projected - _km_rev
        if _additional_at_100_km > 0:
            metrics.append(("Additional Opportunity at 100%", f"+${_additional_at_100_km:,.0f}/mo", green))
    if total_projected and total_projected > 0:
        metrics.append(("Projected Revenue at 100% Adoption", f"${total_projected:,.0f}/mo", None))
    # Use same units as At a Glance (comparable units when available)
    _km_units = _comp_units if (_has_comparable and _comp_units > 0) else total_units
    if _km_units and _km_units > 0:
        metrics.append(("Units (in analysis)", f"{_km_units:,}", None))
        # Combined lift per unit = actual lift + found, consistent with At a Glance row 4
        if comparable_count > 0 and (_monthly_lift != 0 or t1_mo != 0):
            _km_lift_per_unit = _active_lift_m / _km_units
            _km_found_mo = _opp_recurring_mo + (su_current_mo or 0)
            _km_found_per_unit = _km_found_mo / _km_units if _km_found_mo > 0 else 0
            _km_combined_lpu = _km_lift_per_unit + _km_found_per_unit
            _km_combined_sign = "+" if _km_combined_lpu > 0 else ""
            metrics.append(("Combined Lift per Unit", f"{_km_combined_sign}${_km_combined_lpu:,.2f}/unit/mo", combined_color(_km_combined_lpu)))
            if _km_found_per_unit > 0:
                _km_lpu_sign = "+" if _km_lift_per_unit > 0 else ""
                metrics.append(("", f"  Actual: {_km_lpu_sign}${_km_lift_per_unit:,.2f}  |  Found: +${_km_found_per_unit:,.2f}", None))
    # ── Launch date (for single property) ──
    if n_props_total == 1 and comparable_data and len(comparable_data) > 0:
        _single_prop_name = list(comparable_data.keys())[0]
        _single_comp = comparable_data[_single_prop_name]
        _n_post = _single_comp.get("n_post", 0)
        _n_pre = _single_comp.get("n_pre", 0)
        if _n_post > 0 and latest_month:
            # Calculate launch date from n_post months before latest_month
            from dateutil.relativedelta import relativedelta
            _launch_date_calc = latest_month - relativedelta(months=_n_post - 1)
            metrics.append(("PetScreening Launch Date", _launch_date_calc.strftime("%B %Y"), dark_blue))
            metrics.append(("Months Since Launch", f"{_n_post}", None))
            if _n_pre > 0:
                metrics.append(("Pre-Launch Months (baseline)", f"{_n_pre}", None))
    
    # ── Analysis scope ──
    if comparable_count > 0 and n_props_total > 1:
        metrics.append(("Comparable Properties (in analysis)", f"{comparable_count} of {n_props_with_data}", None))
    if n_props_total > 1:
        metrics.append(("Properties with Launch Date", f"{n_with_launch} of {n_props_total}", None))
        metrics.append(("Properties with Charge Data", f"{n_props_with_data} of {n_props_total}", None))

    # ── Portfolio totals (context, not used in lift) ──
    if total_units and total_units > 0:
        metrics.append(("Total Portfolio Units", f"{total_units:,}", None))
    metrics.append(("Total Properties in Portfolio", f"{n_props_total}", None))

    pdf.set_font('Helvetica', '', 9)
    for i, (label_m, value_m, val_color) in enumerate(metrics):
        # Alternate row shading
        if i % 2 == 0:
            pdf.set_fill_color(255, 255, 255)
        else:
            pdf.set_fill_color(250, 250, 248)
        pdf.set_text_color(*body_gray)
        pdf.cell(100, 6, f'  {label_m}', border='LR', fill=True)
        pdf.set_text_color(*(val_color if val_color else dark_blue))
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
            pdf.cell(90, 5, _pdf_safe(pname[:45]), border='LR')
            pdf.cell(90, 5, ", ".join(emails)[:80], border='LR', ln=True)
        # Close table bottom
        pdf.cell(180, 0, '', border='T', ln=True)

    # ═══════════════════════════════════════════════════════════
    #  PROPERTY-LEVEL IMPACT TABLE + CHART (for single-property PDFs)
    # ═══════════════════════════════════════════════════════════
    _doors_dict = property_doors if property_doors else {}
    # Single property = requested 1 property (n_props_total == 1)
    # Don't require monthly_by_prop to have exactly 1 entry - it might have 0 or different names
    _is_single_property = (n_props_total == 1)
    
    if comparable_data and len(comparable_data) > 0:
        pdf.add_page()
        section_heading("Impact by Property", dark_blue)
        _impact_method = "average monthly lift" if use_avg_lift else "most recent completed month vs pre-launch average"
        narrative(
            f"Each property's lift is calculated using {_impact_method}. "
            f"Pre-launch baseline uses up to 6 months before launch date."
        )

        # Table header — streamlined columns
        _col_widths = [48, 14, 26, 26, 26, 22, 18]  # total = 180
        _rev_col_label = 'Post Avg' if use_avg_lift else 'Current'
        _col_headers = ['Property', 'Units', 'Pre Avg', _rev_col_label, 'Lift/mo', 'Lift/Unit', '%']
        pdf.set_fill_color(249, 244, 230)
        pdf.set_draw_color(*card_border)
        pdf.set_font('Helvetica', 'B', 7)
        pdf.set_text_color(*dark_blue)
        for ci, hdr in enumerate(_col_headers):
            pdf.cell(_col_widths[ci], 6, f' {hdr}', border=1, fill=True)
        pdf.ln()

        # Sort by methodology-appropriate lift descending
        if use_avg_lift:
            _sorted_props = sorted(comparable_data.items(), key=lambda x: x[1].get("diff_monthly", 0), reverse=True)
        else:
            def _simple_lift_sort(item):
                pname, pdata_s = item
                _pre_s = pdata_s.get("pre_avg", 0)
                if monthly_by_prop and latest_month and pname in monthly_by_prop:
                    _cur_s = monthly_by_prop[pname].get(latest_month, 0)
                else:
                    _cur_s = pdata_s.get("post_recent_avg", pdata_s.get("post_monthly_avg", 0))
                return _cur_s - _pre_s
            _sorted_props = sorted(comparable_data.items(), key=_simple_lift_sort, reverse=True)
        pdf.set_font('Helvetica', '', 7)
        for ri, (prop_name, pdata) in enumerate(_sorted_props):
            # Page break check
            if pdf.get_y() + 5 > pdf.h - pdf.b_margin:
                pdf.add_page()
                # Reprint header
                pdf.set_fill_color(249, 244, 230)
                pdf.set_font('Helvetica', 'B', 7)
                pdf.set_text_color(*dark_blue)
                for ci, hdr in enumerate(_col_headers):
                    pdf.cell(_col_widths[ci], 6, f' {hdr}', border=1, fill=True)
                pdf.ln()
                pdf.set_font('Helvetica', '', 7)

            # Alternate shading
            if ri % 2 == 0:
                pdf.set_fill_color(255, 255, 255)
            else:
                pdf.set_fill_color(250, 250, 248)

            _pre = pdata.get("pre_avg", 0)
            _post = pdata.get("post_recent_avg", pdata.get("post_monthly_avg", 0))
            _adj = pdata.get("diff_monthly", 0)
            _prop_units = _doors_dict.get(prop_name, 0)

            # Get latest month revenue for this property (most recent completed month)
            _current_mo_rev = 0
            if monthly_by_prop and latest_month and prop_name in monthly_by_prop:
                _current_mo_rev = monthly_by_prop[prop_name].get(latest_month, 0)
            else:
                _current_mo_rev = _post  # fallback to post avg

            # Lift: simple = current month - pre avg; avg = property-by-property average
            if use_avg_lift:
                _prop_lift = _adj
            else:
                _prop_lift = _current_mo_rev - _pre
            _lift_per_u = _prop_lift / _prop_units if _prop_units > 0 else 0

            # Truncate property name
            _short_name = prop_name.split(" - ", 1)[-1] if " - " in prop_name else prop_name
            _short_name = _pdf_safe(_short_name[:28])

            pdf.set_text_color(*body_gray)
            pdf.cell(_col_widths[0], 5, f' {_short_name}', border='LR', fill=True)

            # Units column
            pdf.set_text_color(*dark_blue)
            _units_str = str(_prop_units) if _prop_units > 0 else "--"
            pdf.cell(_col_widths[1], 5, f' {_units_str}', border='LR', fill=True)

            # Show post avg or current month depending on toggle
            _rev_display = _post if use_avg_lift else _current_mo_rev

            pdf.cell(_col_widths[2], 5, f' ${_pre:,.0f}', border='LR', fill=True)
            pdf.cell(_col_widths[3], 5, f' ${_rev_display:,.0f}', border='LR', fill=True)

            # Color-code lift
            pdf.set_text_color(*lift_color(_prop_lift))
            _prop_lift_sign = "+" if _prop_lift > 0 else ""
            pdf.cell(_col_widths[4], 5, f' {_prop_lift_sign}${_prop_lift:,.0f}', border='LR', fill=True)

            # Lift per unit
            pdf.set_text_color(*lift_color(_lift_per_u))
            if _prop_units > 0:
                _lpu_sign = "+" if _lift_per_u > 0 else ""
                pdf.cell(_col_widths[5], 5, f' {_lpu_sign}${_lift_per_u:,.2f}', border='LR', fill=True)
            else:
                pdf.set_text_color(160, 160, 160)
                pdf.cell(_col_widths[5], 5, ' --', border='LR', fill=True)

            # Lift %
            _lift_pct = ((_rev_display - _pre) / _pre * 100) if _pre > 0 else 0
            pdf.set_text_color(*lift_color(_lift_pct))
            _pct_sign = "+" if _lift_pct > 0 else ""
            pdf.cell(_col_widths[6], 5, f' {_pct_sign}{_lift_pct:.0f}%', border='LR', fill=True)
            pdf.ln()

        # Close table
        pdf.cell(sum(_col_widths), 0, '', border='T', ln=True)

        # ── Excluded properties (no pre-launch data) ──
        if _noncomp_count > 0 and monthly_by_prop and latest_month:
            _comp_names = set(comparable_data.keys())
            _excluded_props = []
            for pname in sorted(property_doors.keys()) if property_doors else []:
                if pname not in _comp_names and pname in monthly_by_prop:
                    _ep_units = _doors_dict.get(pname, 0)
                    _ep_current = monthly_by_prop[pname].get(latest_month, 0)
                    if _ep_current > 0 or _ep_units > 0:
                        _excluded_props.append((pname, _ep_units, _ep_current))

            if _excluded_props:
                pdf.ln(4)
                pdf.set_font('Helvetica', 'B', 9)
                pdf.set_text_color(*light_gray)
                pdf.cell(0, 5, f'Properties Excluded from Lift Analysis ({len(_excluded_props)})', ln=True)
                pdf.set_font('Helvetica', 'I', 7)
                pdf.cell(0, 3.5, 'No pre-launch charge data available for comparison.', ln=True)
                pdf.ln(2)

                # Mini table: Property | Units | Current Revenue
                _ex_widths = [80, 30, 70]  # total = 180
                _ex_headers = ['Property', 'Units', 'Current Revenue']
                pdf.set_fill_color(249, 244, 230)
                pdf.set_draw_color(*card_border)
                pdf.set_font('Helvetica', 'B', 7)
                pdf.set_text_color(*dark_blue)
                for ci, hdr in enumerate(_ex_headers):
                    pdf.cell(_ex_widths[ci], 6, f' {hdr}', border=1, fill=True)
                pdf.ln()

                pdf.set_font('Helvetica', '', 7)
                for ei, (ep_name, ep_units, ep_rev) in enumerate(_excluded_props):
                    if pdf.get_y() + 5 > pdf.h - pdf.b_margin:
                        pdf.add_page()
                        pdf.set_fill_color(249, 244, 230)
                        pdf.set_font('Helvetica', 'B', 7)
                        pdf.set_text_color(*dark_blue)
                        for ci, hdr in enumerate(_ex_headers):
                            pdf.cell(_ex_widths[ci], 6, f' {hdr}', border=1, fill=True)
                        pdf.ln()
                        pdf.set_font('Helvetica', '', 7)

                    if ei % 2 == 0:
                        pdf.set_fill_color(255, 255, 255)
                    else:
                        pdf.set_fill_color(250, 250, 248)

                    _short_ep = ep_name.split(" - ", 1)[-1] if " - " in ep_name else ep_name
                    _short_ep = _pdf_safe(_short_ep[:45])
                    pdf.set_text_color(*body_gray)
                    pdf.cell(_ex_widths[0], 5, f' {_short_ep}', border='LR', fill=True)
                    pdf.set_text_color(*dark_blue)
                    pdf.cell(_ex_widths[1], 5, f' {ep_units:,}' if ep_units > 0 else ' --', border='LR', fill=True)
                    pdf.cell(_ex_widths[2], 5, f' ${ep_rev:,.0f}/mo', border='LR', fill=True)
                    pdf.ln()
                pdf.cell(sum(_ex_widths), 0, '', border='T', ln=True)

    # ═══════════════════════════════════════════════════════════
    #  MONTHLY REVENUE CHART (single-property PDFs only)
    #  Matches Fee Collection chart: green post-launch, blue pre-launch,
    #  red dashed launch line, golden baseline, purple adoption overlay
    # ═══════════════════════════════════════════════════════════
    # Render chart for single-property PDFs
    if _is_single_property and monthly_by_prop and len(monthly_by_prop) > 0 and latest_month:
        try:
            import matplotlib
            matplotlib.use('Agg')  # Non-GUI backend
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from matplotlib.ticker import FuncFormatter
            
            # Get the first (or only) property's monthly data
            _chart_prop_name = list(monthly_by_prop.keys())[0]
            _chart_data = monthly_by_prop[_chart_prop_name]
            
            # Get all months with data, sorted (no cap - show full history)
            _chart_months = sorted([m for m in _chart_data.keys()])
            _chart_values = [_chart_data.get(m, 0) for m in _chart_months]
            
            if _chart_months and len(_chart_months) >= 2:
                # Create figure - taller to fill page space
                fig, ax1 = plt.subplots(figsize=(7, 3.5), dpi=150)
                
                # Determine launch date and bar colors
                _launch_dt = None
                _launch_month = None
                _pre_avg = 0
                _bar_colors = []
                
                if comparable_data and _chart_prop_name in comparable_data:
                    _comp_entry = comparable_data[_chart_prop_name]
                    _n_pre = _comp_entry.get("n_pre", 0)
                    _n_post = _comp_entry.get("n_post", 0)
                    _pre_avg = _comp_entry.get("pre_avg", 0)
                    
                    if _n_post > 0:
                        # Calculate launch month position
                        _total_chart_months = len(_chart_months)
                        _launch_idx = max(0, _total_chart_months - _n_post)
                        if _launch_idx < _total_chart_months:
                            _launch_month = _chart_months[_launch_idx]
                        
                        for i, m in enumerate(_chart_months):
                            if i < _launch_idx:
                                _bar_colors.append('#7D9BC1')  # Blue for pre-launch
                            else:
                                _bar_colors.append('#677848')  # Green for post-launch
                
                # Default to all green if no launch data (all post-launch)
                if not _bar_colors:
                    _bar_colors = ['#677848'] * len(_chart_months)
                
                # Plot bars
                bar_width = 20  # days
                ax1.bar(_chart_months, _chart_values, color=_bar_colors, width=bar_width, 
                        edgecolor='white', linewidth=0.3, zorder=2)
                
                # Add golden dotted baseline line (pre-launch average)
                if _pre_avg > 0:
                    ax1.axhline(y=_pre_avg, color='#E2AB58', linestyle=':', linewidth=1.5, 
                                label=f'Pre-PS baseline ${_pre_avg:,.0f}/mo', zorder=3)
                
                # Add red dashed launch line
                if _launch_month:
                    # Position line just before the launch month
                    _line_x = _launch_month - timedelta(days=15)
                    ax1.axvline(x=_line_x, color='#CF5A3F', linestyle='--', linewidth=2, 
                                label='PS Launch', zorder=4)
                
                # Style primary axis (revenue)
                ax1.set_facecolor('#FAFAF8')
                fig.patch.set_facecolor('#FAFAF8')
                ax1.spines['top'].set_visible(False)
                ax1.spines['right'].set_visible(False)
                ax1.spines['left'].set_color('#CCCCCC')
                ax1.spines['bottom'].set_color('#CCCCCC')
                ax1.tick_params(axis='both', colors='#4F5155', labelsize=8)
                ax1.yaxis.set_major_formatter(FuncFormatter(lambda x, p: f'${x:,.0f}'))
                ax1.xaxis.set_major_formatter(mdates.DateFormatter('%b %y'))
                ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
                ax1.set_ylabel('Monthly Revenue', fontsize=9, color='#4F5155')
                plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha='right')
                
                # Secondary y-axis for adoption % would go here if compliance_data is passed
                # For now, keep chart clean without adoption overlay
                
                # Add legend
                _legend_handles = []
                _legend_labels = []
                
                from matplotlib.patches import Patch
                from matplotlib.lines import Line2D
                
                _legend_handles.append(Patch(facecolor='#677848', edgecolor='white'))
                _legend_labels.append('Post-Launch')
                
                if any(c == '#7D9BC1' for c in _bar_colors):
                    _legend_handles.append(Patch(facecolor='#7D9BC1', edgecolor='white'))
                    _legend_labels.append('Pre-Launch')
                
                if _launch_month:
                    _legend_handles.append(Line2D([0], [0], color='#CF5A3F', linestyle='--', linewidth=2))
                    _legend_labels.append('PS Launch')
                
                if _pre_avg > 0:
                    _legend_handles.append(Line2D([0], [0], color='#E2AB58', linestyle=':', linewidth=1.5))
                    _legend_labels.append(f'Pre-PS Baseline (${_pre_avg:,.0f}/mo)')
                
                if _legend_handles:
                    ax1.legend(_legend_handles, _legend_labels, loc='upper left', fontsize=7, 
                               framealpha=0.9, facecolor='white')
                
                plt.tight_layout()
                
                # Save to bytes
                _chart_buf = io.BytesIO()
                fig.savefig(_chart_buf, format='png', bbox_inches='tight', facecolor='#FAFAF8')
                _chart_buf.seek(0)
                plt.close(fig)
                
                # Check if we need a new page or can fit on current page
                if pdf.get_y() + 75 > pdf.h - pdf.b_margin:
                    pdf.add_page()
                else:
                    pdf.ln(10)  # More space between table and chart
                
                # Embed image (no title, just the chart)
                pdf.image(_chart_buf, x=15, y=pdf.get_y(), w=180)
                pdf.ln(65)
                
        except Exception as _chart_err:
            # Log error for debugging
            import traceback
            _tb = traceback.format_exc()
            # Add error note to PDF
            pdf.set_font('Helvetica', 'I', 7)
            pdf.set_text_color(200, 100, 100)
            pdf.cell(0, 4, f'Chart error: {str(_chart_err)[:80]}', ln=True)
            pdf.set_text_color(*body_gray)

    # ═══════════════════════════════════════════════════════════
    #  PORTFOLIO BEFORE/AFTER CHART (parent-company PDFs)
    #  Aggregated pet fee revenue across the comparable properties —
    #  the same set behind the lift KPIs — with the PS rollout window
    #  and the combined pre-PS baseline. Blue = before first launch,
    #  sage = during rollout, green = all comparable properties live.
    # ═══════════════════════════════════════════════════════════
    elif monthly_by_prop and len(monthly_by_prop) > 1 and latest_month:
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from matplotlib.ticker import FuncFormatter
            from matplotlib.patches import Patch
            from matplotlib.lines import Line2D

            _comp_names = [p for p in (comparable_data or {}) if p in monthly_by_prop]
            _scope_names = _comp_names if _comp_names else list(monthly_by_prop.keys())
            _port_monthly = {}
            for _pn in _scope_names:
                for _m, _v in monthly_by_prop[_pn].items():
                    _port_monthly[_m] = _port_monthly.get(_m, 0) + _v
            _chart_months = sorted(_port_monthly.keys())
            _chart_values = [_port_monthly[m] for m in _chart_months]

            if len(_chart_months) >= 2:
                _launches = sorted(
                    comparable_data[_pn]["launch_month"] for _pn in _comp_names
                    if comparable_data[_pn].get("launch_month")
                )
                _rollout_start = _launches[0] if _launches else None
                _rollout_end = _launches[-1] if _launches else None
                _pre_avg_total = sum(
                    comparable_data[_pn].get("pre_avg", 0) for _pn in _comp_names
                ) if _comp_names else 0

                _bar_colors = []
                for _m in _chart_months:
                    if _rollout_start and _m < _rollout_start:
                        _bar_colors.append('#7D9BC1')   # before any PS launch
                    elif _rollout_end and _m < _rollout_end:
                        _bar_colors.append('#A9B79A')   # rollout in progress
                    else:
                        _bar_colors.append('#677848')   # all comparable live

                fig, ax1 = plt.subplots(figsize=(7, 3.5), dpi=150)
                ax1.bar(_chart_months, _chart_values, color=_bar_colors, width=20,
                        edgecolor='white', linewidth=0.3, zorder=2)
                if _pre_avg_total > 0:
                    ax1.axhline(y=_pre_avg_total, color='#E2AB58', linestyle=':',
                                linewidth=1.5, zorder=3)
                if _rollout_start:
                    ax1.axvline(x=_rollout_start - timedelta(days=15), color='#CF5A3F',
                                linestyle='--', linewidth=2, zorder=4)

                ax1.set_facecolor('#FAFAF8')
                fig.patch.set_facecolor('#FAFAF8')
                ax1.spines['top'].set_visible(False)
                ax1.spines['right'].set_visible(False)
                ax1.spines['left'].set_color('#CCCCCC')
                ax1.spines['bottom'].set_color('#CCCCCC')
                ax1.tick_params(axis='both', colors='#4F5155', labelsize=8)
                ax1.yaxis.set_major_formatter(FuncFormatter(lambda x, p: f'${x:,.0f}'))
                ax1.xaxis.set_major_formatter(mdates.DateFormatter('%b %y'))
                ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
                ax1.set_ylabel('Monthly Pet Fee Revenue', fontsize=9, color='#4F5155')
                _scope_label = (
                    f'Comparable Properties ({len(_scope_names)})' if _comp_names
                    else f'All Properties ({len(_scope_names)})'
                )
                ax1.set_title(f'Before & After PetScreening — {_scope_label}',
                              fontsize=10, color='#1F2257', pad=10)
                plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha='right')

                _legend_handles = [Patch(facecolor='#677848', edgecolor='white')]
                _legend_labels = ['Post-rollout']
                if any(c == '#A9B79A' for c in _bar_colors):
                    _legend_handles.append(Patch(facecolor='#A9B79A', edgecolor='white'))
                    _legend_labels.append('PS rollout in progress')
                if any(c == '#7D9BC1' for c in _bar_colors):
                    _legend_handles.append(Patch(facecolor='#7D9BC1', edgecolor='white'))
                    _legend_labels.append('Pre-PetScreening')
                if _rollout_start:
                    _legend_handles.append(Line2D([0], [0], color='#CF5A3F', linestyle='--', linewidth=2))
                    _legend_labels.append('First PS Launch')
                if _pre_avg_total > 0:
                    _legend_handles.append(Line2D([0], [0], color='#E2AB58', linestyle=':', linewidth=1.5))
                    _legend_labels.append(f'Pre-PS Baseline (${_pre_avg_total:,.0f}/mo)')
                ax1.legend(_legend_handles, _legend_labels, loc='upper left', fontsize=7,
                           framealpha=0.9, facecolor='white')

                plt.tight_layout()
                _chart_buf = io.BytesIO()
                fig.savefig(_chart_buf, format='png', bbox_inches='tight', facecolor='#FAFAF8')
                _chart_buf.seek(0)
                plt.close(fig)

                if pdf.get_y() + 75 > pdf.h - pdf.b_margin:
                    pdf.add_page()
                else:
                    pdf.ln(10)
                pdf.image(_chart_buf, x=15, y=pdf.get_y(), w=180)
                pdf.ln(65)
        except Exception as _chart_err:
            pdf.set_font('Helvetica', 'I', 7)
            pdf.set_text_color(200, 100, 100)
            pdf.cell(0, 4, f'Portfolio chart error: {str(_chart_err)[:80]}', ln=True)
            pdf.set_text_color(*body_gray)

    # ═══════════════════════════════════════════════════════════
    #  INDIVIDUAL PROPERTY CHARTS APPENDIX (optional)
    #  The same before/after charts shown in the Fee Collection tab,
    #  one per comparable property, sorted by monthly lift.
    # ═══════════════════════════════════════════════════════════
    if (include_property_charts and not _is_single_property
            and monthly_by_prop and comparable_data):
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from matplotlib.ticker import FuncFormatter

            _MAX_PROP_CHARTS = 40
            _appendix_props = sorted(
                (p for p in comparable_data if p in monthly_by_prop),
                key=lambda p: -comparable_data[p].get("diff_monthly", 0),
            )[:_MAX_PROP_CHARTS]

            if _appendix_props:
                pdf.add_page()
                pdf.set_font('Helvetica', 'B', 13)
                pdf.set_text_color(*dark_blue)
                pdf.cell(0, 8, _pdf_safe('Appendix: Property Fee Collection Trends'), ln=True)
                pdf.set_font('Helvetica', '', 8)
                pdf.set_text_color(*body_gray)
                _cap_note = (
                    f'Before/after pet fee revenue for the {len(_appendix_props)} '
                    f'comparable properties, sorted by monthly lift.'
                )
                if len(comparable_data) > _MAX_PROP_CHARTS:
                    _cap_note += f' (Top {_MAX_PROP_CHARTS} of {len(comparable_data)} shown.)'
                pdf.cell(0, 5, _pdf_safe(_cap_note), ln=True)
                pdf.ln(2)

            for _ap_name in _appendix_props:
                _ap = comparable_data[_ap_name]
                _ap_data = monthly_by_prop.get(_ap_name, {})
                _ap_months = sorted(_ap_data.keys())
                if len(_ap_months) < 2:
                    continue
                _ap_values = [_ap_data.get(m, 0) for m in _ap_months]
                _ap_launch = _ap.get("launch_month")
                _ap_pre = _ap.get("pre_avg", 0)
                _ap_lift = _ap.get("diff_monthly", 0)

                _colors = [
                    '#7D9BC1' if (_ap_launch and m < _ap_launch) else '#677848'
                    for m in _ap_months
                ]
                fig, ax = plt.subplots(figsize=(7, 2.1), dpi=140)
                ax.bar(_ap_months, _ap_values, color=_colors, width=20,
                       edgecolor='white', linewidth=0.3, zorder=2)
                if _ap_pre > 0:
                    ax.axhline(y=_ap_pre, color='#E2AB58', linestyle=':', linewidth=1.2, zorder=3)
                if _ap_launch:
                    ax.axvline(x=_ap_launch - timedelta(days=15), color='#CF5A3F',
                               linestyle='--', linewidth=1.5, zorder=4)
                ax.set_facecolor('#FAFAF8')
                fig.patch.set_facecolor('#FAFAF8')
                for side in ('top', 'right'):
                    ax.spines[side].set_visible(False)
                for side in ('left', 'bottom'):
                    ax.spines[side].set_color('#CCCCCC')
                ax.tick_params(axis='both', colors='#4F5155', labelsize=7)
                ax.yaxis.set_major_formatter(FuncFormatter(lambda x, p: f'${x:,.0f}'))
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %y'))
                ax.xaxis.set_major_locator(mdates.MonthLocator(interval=max(1, len(_ap_months) // 8)))
                _sign = '+' if _ap_lift >= 0 else ''
                ax.set_title(
                    f'{_ap_name[:60]}  ({_sign}${_ap_lift:,.0f}/mo lift, '
                    f'baseline ${_ap_pre:,.0f}/mo)',
                    fontsize=8.5, color='#1F2257', loc='left', pad=6,
                )
                plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha='right')
                plt.tight_layout()

                _buf = io.BytesIO()
                fig.savefig(_buf, format='png', bbox_inches='tight', facecolor='#FAFAF8')
                _buf.seek(0)
                plt.close(fig)

                if pdf.get_y() + 52 > pdf.h - pdf.b_margin:
                    pdf.add_page()
                pdf.image(_buf, x=15, y=pdf.get_y(), w=180)
                pdf.ln(50)
        except Exception as _chart_err:
            pdf.set_font('Helvetica', 'I', 7)
            pdf.set_text_color(200, 100, 100)
            pdf.cell(0, 4, f'Property charts appendix error: {str(_chart_err)[:80]}', ln=True)
            pdf.set_text_color(*body_gray)

    # ═══════════════════════════════════════════════════════════
    #  MISSING RENT APPENDIX — TENANT LIST
    # ═══════════════════════════════════════════════════════════
    _has_tenant_detail = False
    if missing_rent_data:
        for _v in missing_rent_data.values():
            if _v.get("missing_tenants") and len(_v.get("missing_tenants", [])) > 0:
                _has_tenant_detail = True
                break

    if _has_tenant_detail:
        pdf.add_page()
        section_heading("Appendix: Missing Pet Rent -- Tenant Detail", orange)
        narrative(
            "The following tenants have completed PetScreening profiles with active household "
            "pets but are not being charged pet rent. This list is actionable -- hand it to "
            "your property managers for billing corrections."
        )

        _app_col_w = [38, 14, 36, 48, 22, 22]  # total = 180
        _app_headers = ['Property', 'Unit', 'Tenant Name', 'Email', 'Status', 'Avg Fee']
        pdf.set_fill_color(249, 244, 230)
        pdf.set_draw_color(*card_border)
        pdf.set_font('Helvetica', 'B', 7)
        pdf.set_text_color(*dark_blue)
        for ci, hdr in enumerate(_app_headers):
            pdf.cell(_app_col_w[ci], 6, f' {hdr}', border=1, fill=True)
        pdf.ln()

        pdf.set_font('Helvetica', '', 7)
        _app_ri = 0
        for prop_name, pdata in sorted(missing_rent_data.items()):
            tenants = pdata.get("missing_tenants", [])
            if not tenants:
                continue
            _avg_fee = pdata.get("avg_recurring", 0)
            _short_prop = prop_name.split(" - ", 1)[-1] if " - " in prop_name else prop_name
            _short_prop = _pdf_safe(_short_prop[:28])
            for tenant in tenants:
                # Page break check
                if pdf.get_y() + 5 > pdf.h - pdf.b_margin:
                    pdf.add_page()
                    pdf.set_fill_color(249, 244, 230)
                    pdf.set_font('Helvetica', 'B', 7)
                    pdf.set_text_color(*dark_blue)
                    for ci, hdr in enumerate(_app_headers):
                        pdf.cell(_app_col_w[ci], 6, f' {hdr}', border=1, fill=True)
                    pdf.ln()
                    pdf.set_font('Helvetica', '', 7)

                if _app_ri % 2 == 0:
                    pdf.set_fill_color(255, 255, 255)
                else:
                    pdf.set_fill_color(250, 250, 248)

                _tname = str(tenant.get("name", "Unknown"))[:20]
                _tunit = str(tenant.get("unit", ""))[:8]
                _temail = str(tenant.get("email", ""))[:30]
                _tstatus = str(tenant.get("profile_status", tenant.get("status", "Compliant")))[:12]

                pdf.set_text_color(*body_gray)
                pdf.cell(_app_col_w[0], 5, f' {_short_prop}', border='LR', fill=True)
                pdf.cell(_app_col_w[1], 5, f' {_tunit}', border='LR', fill=True)
                pdf.cell(_app_col_w[2], 5, f' {_tname}', border='LR', fill=True)
                pdf.cell(_app_col_w[3], 5, f' {_temail}', border='LR', fill=True)
                pdf.cell(_app_col_w[4], 5, f' {_tstatus}', border='LR', fill=True)
                pdf.set_text_color(*orange)
                pdf.cell(_app_col_w[5], 5, f' ${_avg_fee:,.0f}/mo', border='LR', fill=True)
                pdf.ln()
                _app_ri += 1

        # Close table
        pdf.cell(sum(_app_col_w), 0, '', border='T', ln=True)

    # ═══════════════════════════════════════════════════════════
    #  DATA COVERAGE + METHODOLOGY
    # ═══════════════════════════════════════════════════════════
    pdf.add_page()

    # Data Coverage Box
    _coverage_pct = (n_props_with_data / n_props_total * 100) if n_props_total > 0 else 0
    _cov_y = pdf.get_y()
    pdf.set_fill_color(245, 248, 250)
    pdf.set_draw_color(180, 190, 200)
    pdf.rect(PAGE_L, _cov_y, USABLE_W, 26, 'DF')
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_text_color(*dark_blue)
    pdf.set_xy(PAGE_L + 4, _cov_y + 2)
    pdf.cell(0, 5, 'Data Coverage', ln=True)
    pdf.set_font('Helvetica', '', 8)
    pdf.set_text_color(*body_gray)
    pdf.set_x(PAGE_L + 4)
    pdf.cell(0, 4, f'Data covers {n_props_with_data} of {n_props_total} properties ({_coverage_pct:.0f}%)', ln=True)
    pdf.set_x(PAGE_L + 4)
    pdf.cell(0, 4, f'Data as of {today_str}', ln=True)
    pdf.set_x(PAGE_L + 4)
    _gen_ts_cov = datetime.now().strftime("%B %d, %Y at %I:%M %p") + " ET"
    pdf.cell(0, 4, f'Report generated {_gen_ts_cov}', ln=True)
    # List excluded properties if we can infer them
    if n_props_with_data < n_props_total:
        _excluded_count = n_props_total - n_props_with_data
        pdf.set_x(PAGE_L + 4)
        pdf.set_font('Helvetica', 'I', 7)
        pdf.set_text_color(140, 140, 140)
        pdf.cell(0, 4, f'{_excluded_count} properties excluded due to insufficient charge data.', ln=True)
    pdf.set_y(_cov_y + 30)

    section_heading("Methodology", dark_blue)

    # Describe the methodology actually used in this report
    if use_avg_lift:
        narrative(
            "Monthly Lift: Property-by-property comparison of post-launch average "
            "vs pre-launch average (up to 6 months before launch). Accounts for "
            "properties added or removed since launch."
        )
    else:
        narrative(
            "Monthly Lift: Most recently completed month's pet fee revenue minus "
            "the pre-launch baseline (average of up to 6 months before launch). "
            "The portfolio-level lift is the sum across all comparable properties."
        )

    # Uncollected / missing rent methodology
    _uncollected_parts = [
        "Pet Revenue Found: Tenants with active PetScreening profiles and household pets "
        "who are not being charged pet rent, plus suspected undisclosed pets."
    ]
    if t2_tenants > 0 and _opp_recurring_mo > 0:
        _avg_fee_meth = _opp_recurring_mo / t2_tenants
        _uncollected_parts.append(
            f" Recurring uncollected revenue is estimated at ${_opp_recurring_mo:,.0f}/mo: "
            f"{t2_tenants:,} tenants x ${_avg_fee_meth:,.0f}/mo average fee. "
            f"The average fee is derived from each property's actual charges to paying tenants "
            f"with the same pet type."
        )
    if _opp_onetime_total > 0:
        _uncollected_parts.append(
            f" One-time fees (${_opp_onetime_total:,.0f} total) are charges classified as "
            f"non-recurring in the selected charge codes. The amount per tenant is based on "
            f"each property's average one-time charge to paying tenants."
        )
    narrative("".join(_uncollected_parts))

    narrative(
        "Asset Value Impact: Annual lift divided by a 5% capitalization rate."
    )

    # Charge codes used
    if selected_charge_codes and len(selected_charge_codes) > 0:
        _codes_list = ", ".join(selected_charge_codes)
        narrative(
            f"Charge Codes Used: {_codes_list}. Only properties with at least one "
            f"matching charge are included in the analysis."
        )
    else:
        narrative(
            "Charge Types: Revenue figures are based on the pet-related charge codes selected "
            "during data configuration. Only properties with at least one matching charge are "
            "included in the analysis."
        )

    # Return bytes
    buf = io.BytesIO()
    pdf.output(buf)
    return buf.getvalue()
