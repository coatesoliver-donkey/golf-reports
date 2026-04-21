"""
report_builder.py
Generates a golf group prep report HTML file.

Usage:
    py report_builder.py ^
        --course "Irish Hills Golf & Country Club" ^
        --date 2026-04-26 ^
        --time 07:08 ^
        --players Nick Brett Ollie ^
        --output 2026-04-26_0708_irish-hills.html

(`courses.json` must live in the same folder as this script. The output path
can be relative — e.g. just a filename — or absolute.)
"""

import json, argparse, random, urllib.request, urllib.error, os
from datetime import datetime, timedelta

# ── Load course data ──────────────────────────────────────────────────────────
# Look for courses.json next to this script (works on any machine, any OS)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_COURSES_PATH = os.path.join(_SCRIPT_DIR, 'courses.json')
with open(_COURSES_PATH, encoding='utf-8') as f:
    COURSES = json.load(f)


# ── String escaping helpers (player names with apostrophes etc.) ─────────────
def _attr(s):
    """Escape a value for safe inclusion inside an HTML attribute (double-quoted)."""
    return (str(s).replace('&', '&amp;')
                  .replace('"', '&quot;')
                  .replace('<', '&lt;')
                  .replace('>', '&gt;'))

def _js(s):
    """Escape a string for safe inclusion inside a single-quoted JS string literal,
    where that literal is itself inside an HTML attribute (e.g. onclick="foo('NAME')").
    Apostrophes need to look like &#39; in HTML so they don't close the JS string,
    backslashes need doubling, and < / > need escaping for the HTML layer."""
    return (str(s).replace('\\', '\\\\')
                  .replace("'", '&#39;')
                  .replace('"', '&quot;')
                  .replace('<', '\\u003c')
                  .replace('>', '\\u003e'))

# ── Colours ───────────────────────────────────────────────────────────────────
STOP_COLOURS = ['#e05a8a', '#d4860a', '#2a8a6a', '#7a5cc4', '#c4401a']
# Scorecard palettes — keyed by tee color (from course.tee in courses.json).
# Each course's scorecard adopts the color scheme of the tees being played,
# echoing the paper-scorecard aesthetic where each tee-color row is a bold
# horizontal band. For White tees (the most common, visually neutral), the
# Par row carries the visual weight with a saturated gold — per user pick V3
# from the white_tee_variants mockup.
TEE_PALETTES = {
    # White tees — quiet yardage row + saturated gold Par row does the work.
    'White': {
        'yds': '#ffffff', 'yds_fg': '#1a1a16', 'yds_tot': '#e8e4d6',
        'par': '#e8b030', 'par_fg': '#2a1a00', 'par_tot': '#c8911a',
        'yds_border': '#c0bcae',  # subtle border on the yardage row so it reads against cream page bg
    },

    # Yellow tees — saturated yellow yardage + rich gold Par.
    'Yellow': {
        'yds': '#f5c842', 'yds_fg': '#1a1a16', 'yds_tot': '#d4a820',
        'par': '#e8b030', 'par_fg': '#2a1a00', 'par_tot': '#c8911a',
    },

    # Orange tees — saturated orange + cream Par.
    'Orange': {
        'yds': '#f08c28', 'yds_fg': '#fff', 'yds_tot': '#c86a10',
        'par': '#fff1c8', 'par_fg': '#5a4800', 'par_tot': '#f5d878'},

    # Red tees — saturated red + cream Par.
    'Red': {
        'yds': '#c8220e', 'yds_fg': '#fff', 'yds_tot': '#9a1a08',
        'par': '#fff1c8', 'par_fg': '#5a4800', 'par_tot': '#f5d878'},

    # Blue tees — saturated blue + cream Par.
    'Blue': {
        'yds': '#2568c4', 'yds_fg': '#fff', 'yds_tot': '#1a4a94',
        'par': '#fff1c8', 'par_fg': '#5a4800', 'par_tot': '#f5d878'},

    # Black / Championship tees — near-black + gold text.
    'Black': {
        'yds': '#1a1a1a', 'yds_fg': '#e8d288', 'yds_tot': '#3a3530',
        'par': '#fff1c8', 'par_fg': '#5a4800', 'par_tot': '#f5d878'},

    # Green tees — deep forest + cream Par.
    'Green': {
        'yds': '#1e3a2e', 'yds_fg': '#e8d288', 'yds_tot': '#2e5242',
        'par': '#fff1c8', 'par_fg': '#5a4800', 'par_tot': '#f5d878'},

    # Gold / Senior tees — metallic olive + cream Par.
    'Gold': {
        'yds': '#a88830', 'yds_fg': '#fff', 'yds_tot': '#7a6018',
        'par': '#fff1c8', 'par_fg': '#5a4800', 'par_tot': '#f5d878'},
}


def pick_palette(tee_color):
    """Palette selection from the tee color played. Falls back to White if the
    tee isn't in TEE_PALETTES (since the group plays White most often)."""
    return TEE_PALETTES.get(tee_color, TEE_PALETTES['White'])


# Header + HCP row colors are stable across all palettes — they carry the
# report's section identity and the color-coded handicap dots.
SC_STABLE = {
    'hdr': '#1a1f3a', 'hdr_fg': '#fff', 'hdr_tot': '#0e1228',
    'hcp': '#f5f0e8', 'hcp_fg': '#555', 'hcp_tot': '#e8e0cc',
}


def merge_sc(palette):
    """Combine the tee-color yardage/par palette with the stable header/HCP
    colors into the full SC dict the scorecard builder expects."""
    return {**SC_STABLE, **{k: v for k, v in palette.items()}}


# Default SC for module-level callers (weather cards, legends, etc.) that need
# some default values when no round context is available.
SC = merge_sc(TEE_PALETTES['White'])


# ════════════════════════════════════════════════════════════════════════════
# WEATHER  (Open-Meteo — free, no API key, 16-day hourly)
# ════════════════════════════════════════════════════════════════════════════

def fetch_weather(lat, lng, tee_date, tee_time_str):
    """
    Fetch hourly forecast from Open-Meteo for the tee time and +2h/+4h.
    Returns (cards, sunrise_str) — cards is a list of 3 dicts; sunrise_str
    is a human-readable sunrise time like "6:07 AM" for the tee_date, or
    None if unavailable.
    Falls back to placeholder data if API is unreachable.
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lng}"
        f"&hourly=temperature_2m,apparent_temperature,relative_humidity_2m,"
        f"precipitation_probability,wind_speed_10m,wind_direction_10m,weathercode"
        f"&daily=temperature_2m_max,temperature_2m_min,sunrise"
        f"&timezone=America%2FToronto"
        f"&forecast_days=16"
    )
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  [weather] Open-Meteo unavailable: {e} — using estimates")
        return _weather_fallback(tee_date), None

    hourly = data['hourly']
    times  = hourly['time']
    daily  = data['daily']

    # Find the date's daily hi/lo + sunrise
    date_str = tee_date.strftime('%Y-%m-%d')
    hi_lo = ('?', '?')
    sunrise_str = None
    if date_str in daily['time']:
        di = daily['time'].index(date_str)
        hi_lo = (f"{round(daily['temperature_2m_max'][di])}°C",
                 f"{round(daily['temperature_2m_min'][di])}°C")
        # sunrise comes back like "2026-04-26T06:07" — parse + format 12h
        sr_iso = daily.get('sunrise', [None]*len(daily['time']))[di]
        if sr_iso:
            try:
                sr_dt = datetime.strptime(sr_iso, '%Y-%m-%dT%H:%M')
                hr = sr_dt.hour
                ampm = 'AM' if hr < 12 else 'PM'
                hr12 = hr if 1 <= hr <= 12 else (12 if hr == 0 else hr - 12)
                sunrise_str = f"{hr12}:{sr_dt.minute:02d} {ampm}"
            except (ValueError, TypeError):
                pass

    # Parse tee_time_str "HH:MM" → find matching hours
    h, m = map(int, tee_time_str.split(':'))
    tee_dt = datetime(tee_date.year, tee_date.month, tee_date.day, h)

    cards = []
    for offset in [0, 2, 4]:
        target = tee_dt + timedelta(hours=offset)
        target_str = target.strftime('%Y-%m-%dT%H:00')
        if target_str not in times:
            cards.append(_weather_placeholder(offset, hi_lo))
            continue
        i = times.index(target_str)
        wc = hourly['weathercode'][i]
        cards.append({
            'temp':     f"{round(hourly['temperature_2m'][i])}°C",
            'feels':    f"{round(hourly['apparent_temperature'][i])}°C",
            'humidity': f"{round(hourly['relative_humidity_2m'][i])}%",
            'rain':     f"{round(hourly['precipitation_probability'][i])}%",
            'wind_kmh': round(hourly['wind_speed_10m'][i]),
            'wind_dir': _wind_dir(hourly['wind_direction_10m'][i]),
            'hi':       hi_lo[0],
            'lo':       hi_lo[1],
            'icon':     _wmo_icon(wc),
            'condition': _wmo_label(wc),
        })
    return cards, sunrise_str


def _wind_dir(deg):
    dirs = ['N','NE','E','SE','S','SW','W','NW']
    return dirs[round(deg / 45) % 8]


def _wmo_icon(code):
    # Use variation-selector-16 (FE0F) to force emoji (color) rendering
    # instead of the B&W text glyph on platforms that default to text style.
    if code == 0:            return '&#9728;&#65039;'   # clear (sun, color)
    if code in (1, 2):       return '&#9925;&#65039;'   # partly cloudy
    if code == 3:            return '&#9729;&#65039;'   # overcast
    if code in (51,53,55,
                61,63,65):   return '&#127783;'         # rain (already full emoji)
    if code in (71,73,75,
                77):         return '&#10052;&#65039;'  # snow
    if code in (80,81,82):   return '&#127783;'         # showers
    if code in (95,96,99):   return '&#9928;&#65039;'   # thunderstorm
    return '&#9925;&#65039;'


def _wmo_label(code):
    if code == 0:            return 'Clear Sky'
    if code == 1:            return 'Mainly Clear'
    if code == 2:            return 'Partly Cloudy'
    if code == 3:            return 'Overcast'
    if code in (51, 53):     return 'Light Drizzle'
    if code == 55:           return 'Dense Drizzle'
    if code in (61, 63):     return 'Rain'
    if code == 65:           return 'Heavy Rain'
    if code in (71, 73):     return 'Snow'
    if code in (80, 81):     return 'Showers'
    if code == 82:           return 'Heavy Showers'
    if code == 95:           return 'Thunderstorm'
    return 'Mixed Conditions'


def _weather_placeholder(offset, hi_lo):
    return {'temp': '--°C', 'feels': '--°C', 'humidity': '--%', 'rain': '0%',
            'wind_kmh': 0, 'wind_dir': 'N', 'hi': hi_lo[0], 'lo': hi_lo[1],
            'icon': '&#9925;&#65039;', 'condition': 'Forecast pending'}


def _weather_fallback(tee_date):
    return [_weather_placeholder(i, ('?', '?')) for i in range(3)]


def _weather_note(cards):
    """Generate 3 callout bullet texts from weather data."""
    rain0 = int(cards[0]['rain'].replace('%','')) if cards[0]['rain'] != '?' else 0
    rain2 = int(cards[1]['rain'].replace('%','')) if cards[1]['rain'] != '?' else 0
    wind  = cards[0].get('wind_kmh', 0)

    bullets = []
    if rain0 >= 50:
        bullets.append('Expect rain at tee time — pack the wet gear')
    elif rain0 >= 25:
        bullets.append('Chance of a shower at tee time')
    else:
        bullets.append('Dry at tee time')

    if rain2 < rain0:
        bullets.append('Conditions improving through the round')
    elif rain2 > rain0:
        bullets.append('Rain chance increases through the round')
    else:
        bullets.append('Conditions holding steady through the round')

    if wind >= 30:
        bullets.append(f'Breezy — {cards[0]["wind_kmh"]} km/h {cards[0]["wind_dir"]} wind, club up')
    elif wind >= 20:
        bullets.append(f'Moderate wind — {cards[0]["wind_kmh"]} km/h {cards[0]["wind_dir"]}')
    else:
        bullets.append('Light wind — good scoring conditions')

    header_icon = '&#9928;' if rain0 >= 50 else '&#127780;' if rain0 >= 25 else '&#9728;'
    header_text = 'Wet morning ahead' if rain0 >= 50 else 'Keep an eye on it' if rain0 >= 25 else 'Nice morning for it'
    return header_icon, header_text, bullets


# ════════════════════════════════════════════════════════════════════════════
# SCORECARD
# ════════════════════════════════════════════════════════════════════════════

def _hcp_style(hcp):
    if hcp <= 6:  return '#e8735a', '#fff'
    if hcp <= 12: return '#c8a030', '#fff'
    return '#5a9a5a', '#fff'


def _kid_name_td(name, bg, initial_only=False, rng=None):
    """Render player name in the scorecard name cell, styled as pencil-on-card.
    Uses Kalam Light (Google Font) — thin strokes, slight slant, pencil feel.
    Color is a dark graphite (#2a2a2a), not pure black, not colored ink."""
    if initial_only:
        return (f'<td style="background:{bg};padding:4px 4px;white-space:nowrap;max-width:38px;">'
                f'<span style="display:inline-flex;align-items:center;">'
                f'<span style="font-family:\'Kalam\',cursive;font-weight:300;font-size:15px;color:#2a2a2a;">{name[0].upper()}</span>'
                f'</span></td>')
    return (f'<td style="background:{bg};padding:4px 4px;white-space:nowrap;max-width:38px;">'
            f'<span style="display:inline-flex;align-items:center;">'
            f'<span style="font-family:\'Kalam\',cursive;font-weight:300;font-size:15px;color:#2a2a2a;">{name.upper()}</span>'
            f'</span></td>')


def _score_input(bg, player, hole, tabindex):
    safe_player = _attr(player)
    return (f'<td style="background:{bg};padding:2px;text-align:center;min-width:22px;position:relative;">'
            f'<input type="number" min="1" max="15" inputmode="numeric" pattern="[0-9]*" '
            f'data-player="{safe_player}" data-hole="{hole}" tabindex="{tabindex}" '
            f'style="width:100%;min-width:20px;height:28px;border:none;background:transparent;'
            f'text-align:center;font-size:13px;font-weight:600;color:#1a1a16;'
            f'-webkit-appearance:none;-moz-appearance:textfield;appearance:none;'
            f'padding:0;touch-action:manipulation;cursor:pointer;outline:none;border-radius:4px;" '
            f'onclick="this.select()"></td>')


def _static_cell(bg):
    """Empty, non-interactive cell — used for Ollie's row (display only, no input)."""
    return (f'<td style="background:{bg};padding:2px;text-align:center;min-width:22px;height:32px;'
            f'pointer-events:none;user-select:none;"></td>')


def build_scorecard_table(holes, yards_list, total_label, yards_total, par_total,
                           players, tee_name='White', use_initials=False, rng=None,
                           is_front=True, sc=None):
    # Use per-round palette if provided, else module default
    if sc is None:
        sc = SC
    row_bgs = ['#f4f4f4', '#fafafa']
    # Header row
    hdr = (f'<th style="background:{sc["hdr"]};color:{sc["hdr_fg"]};font-weight:700;font-size:10px;'
           f'text-transform:uppercase;letter-spacing:.08em;padding:8px 10px;min-width:38px;text-align:left;">Hole</th>')
    hdr += ''.join(f'<th style="background:{sc["hdr"]};color:{sc["hdr_fg"]};font-weight:700;font-size:12px;padding:8px 4px;text-align:center;">{h["h"]}</th>' for h in holes)
    hdr += f'<th style="background:{sc["hdr_tot"]};color:{sc["hdr_fg"]};font-weight:700;font-size:12px;padding:8px 10px 8px 4px;text-align:center;">{total_label}</th>'
    # Yardage row — optional bottom border for palettes where the background
    # is very light (e.g. White tee yardage row needs a border so it doesn't
    # vanish into the cream page background).
    yds_border = sc.get('yds_border')
    yds_border_css = f'border-bottom:1px solid {yds_border};' if yds_border else ''
    yds = (f'<td style="background:{sc["yds"]};color:{sc["yds_fg"]};font-weight:700;font-size:10px;'
           f'text-transform:uppercase;letter-spacing:.07em;padding:7px 10px;text-align:left;{yds_border_css}">{tee_name}</td>')
    yds += ''.join(f'<td style="background:{sc["yds"]};color:{sc["yds_fg"]};font-weight:700;font-size:13px;padding:7px 4px;text-align:center;{yds_border_css}">{y}</td>' for y in yards_list)
    # Yardage total — use yds_fg not hardcoded white (so light palettes don't show invisible text)
    yds += f'<td style="background:{sc["yds_tot"]};color:{sc["yds_fg"]};font-weight:700;font-size:14px;padding:7px 10px 7px 6px;text-align:center;{yds_border_css}">{yards_total}</td>'
    # Par row (section-break class = heavier top border)
    par = f'<td style="background:{sc["par"]};color:#333;font-weight:400;font-size:11px;padding:7px 10px;text-align:left;">Par</td>'
    par += ''.join(f'<td style="background:{sc["par"]};color:{sc["par_fg"]};font-weight:700;font-size:12px;padding:7px 4px;text-align:center;">{h["par"]}</td>' for h in holes)
    par += f'<td style="background:{sc["par_tot"]};color:{sc["par_fg"]};font-weight:700;font-size:11px;padding:7px 10px 7px 4px;text-align:center;">{par_total}</td>'
    # HCP row (section-break class = heavier top border)
    hcp = f'<td style="background:{sc["hcp"]};color:{sc["hcp_fg"]};font-weight:400;font-size:11px;padding:7px 10px;text-align:left;">HDCP</td>'
    for h in holes:
        bg2, fg2 = _hcp_style(h['hcp'])
        dot = f'<span style="display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:50%;background:{bg2};color:{fg2};font-size:10px;font-weight:700;">{h["hcp"]}</span>'
        hcp += f'<td style="background:{sc["hcp"]};padding:4px 4px;text-align:center;">{dot}</td>'
    hcp += f'<td style="background:{sc["hcp_tot"]};color:#999;font-weight:700;font-size:11px;padding:7px 10px 7px 4px;text-align:center;">—</td>'
    # Player rows — Nick/Brett get interactive inputs; Ollie's row is display-only.
    # No more explicit divider <tr> — the first player row carries the section-break class,
    # and every player row has a 'player-row' class that gets a bottom border.
    player_rows = ''
    hole_offset = 0 if is_front else 9
    for i, name in enumerate(players):
        bg = row_bgs[i % 2]
        name_td = _kid_name_td(name, bg, initial_only=use_initials, rng=rng)
        cells = ''
        if name == 'Ollie':
            cells = ''.join(_static_cell(bg) for _ in range(9))
        else:
            for k in range(9):
                hole_num = hole_offset + k + 1    # 1..9 on front, 10..18 on back
                tabindex = i * 18 + hole_num      # Nick:1..18, Brett:19..36
                cells += _score_input(bg, name, hole_num, tabindex)
        tot = f'<td style="background:#f0ede6;padding:6px 10px 6px 6px;text-align:center;"></td>'
        # First player row (i==0) gets section-break class too, to separate players from HDCP
        cls = 'player-row' + (' section-break' if i == 0 else '')
        player_rows += f'<tr data-player-row="{name}" class="{cls}">{name_td}{cells}{tot}</tr>'

    # Scoped style for the scorecard. Only emit on the front-nine call (is_front=True)
    # so we don't duplicate the same rules when the back nine also renders.
    if is_front:
        scoped_css = (
            '<style>'
            # Vertical column dividers on every cell except the last column
            '.sc-tbl td, .sc-tbl th { border-right: 1px solid #d5d1c5; }'
            '.sc-tbl td:last-child, .sc-tbl th:last-child { border-right: none; }'
            # Heavier horizontal rule at section breaks (Par, HDCP, and first player row)
            '.sc-tbl tr.section-break td { border-top: 1.5px solid #8a8577; }'
            # Thin horizontal rule between player rows (runs through empty cells)
            '.sc-tbl tr.player-row td { border-bottom: 1px solid #c8c4b8; }'
            # No bottom border on the last player row (avoids doubling with the card border)
            '.sc-tbl tr.player-row:last-child td { border-bottom: none; }'
            '</style>'
        )
    else:
        scoped_css = ''

    return (f'{scoped_css}'
            f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:8px;border-radius:8px;overflow:hidden;">'
            f'<table class="sc-tbl" style="width:100%;border-collapse:collapse;font-family:-apple-system,sans-serif;border:1.5px solid {sc["hdr"]};border-radius:8px;overflow:hidden;">'
            f'<thead><tr>{hdr}</tr></thead>'
            f'<tbody><tr>{yds}</tr><tr class="section-break">{par}</tr><tr class="section-break">{hcp}</tr>{player_rows}</tbody>'
            f'</table></div>')


def build_hcp_legend():
    return ('<div style="display:flex;gap:12px;flex-wrap:wrap;padding:8px 4px 2px;font-size:10px;align-items:center;justify-content:center;">'
            '<span style="color:#aaa;font-weight:600;">HDCP:</span>'
            '<span style="display:flex;align-items:center;gap:4px;"><span style="width:18px;height:18px;border-radius:50%;background:#e8735a;display:inline-flex;align-items:center;justify-content:center;font-size:8px;color:#fff;font-weight:700;">1</span><span style="color:#888;">Hard (1&ndash;6)</span></span>'
            '<span style="display:flex;align-items:center;gap:4px;"><span style="width:18px;height:18px;border-radius:50%;background:#c8a030;display:inline-flex;align-items:center;justify-content:center;font-size:8px;color:#fff;font-weight:700;">9</span><span style="color:#888;">Mid (7&ndash;12)</span></span>'
            '<span style="display:flex;align-items:center;gap:4px;"><span style="width:18px;height:18px;border-radius:50%;background:#5a9a5a;display:inline-flex;align-items:center;justify-content:center;font-size:8px;color:#fff;font-weight:700;">15</span><span style="color:#888;">Easier (13&ndash;18)</span></span>'
            '</div>')


def build_send_scores_sections(players):
    """One send-scores block per player. Each shows when that player's 18 holes are filled.
    Ollie is the recipient, not a submitter — skip generating one for that name."""
    blocks = []
    for i, name in enumerate(players):
        if name == 'Ollie':
            continue
        attr_name = _attr(name)
        # Use player index in IDs and JS args — avoids any escaping issue with names
        # containing apostrophes or other special chars.
        blocks.append(
            f'<div id="send-scores-section-p{i}" class="send-scores-section" data-player="{attr_name}" '
            f'style="display:none;background:#fff;border-radius:12px;padding:1rem 1.1rem;margin-top:0.75rem;margin-bottom:.75rem;border:2px solid #1a1f3a;box-shadow:0 2px 12px rgba(26,31,58,.12);">'
            f'<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">'
            f'<div>'
            f'<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:#1a1f3a;margin-bottom:2px;">&#9993; Send Your Scores</div>'
            f'<div style="font-size:13px;font-weight:700;color:#c4621a;">{attr_name}</div>'
            f'</div>'
            f'<div style="font-size:24px;">&#9971;</div>'
            f'</div>'
            # Feeling selector
            f'<div style="margin-bottom:14px;">'
            f'<div style="font-size:11px;font-weight:600;color:#555;margin-bottom:8px;text-transform:uppercase;letter-spacing:.08em;">How did you play?</div>'
            f'<div style="display:flex;gap:8px;">'
            f'<button type="button" onclick="selectFeeling({i},\'good\')" data-feel="good" '
            f'style="flex:1;padding:10px 6px;border-radius:8px;border:1.5px solid #e5e3de;background:#f9f7f3;font-size:13px;font-weight:600;cursor:pointer;touch-action:manipulation;transition:all .15s;">'
            f'&#128994; Played well</button>'
            f'<button type="button" onclick="selectFeeling({i},\'average\')" data-feel="average" '
            f'style="flex:1;padding:10px 6px;border-radius:8px;border:1.5px solid #e5e3de;background:#f9f7f3;font-size:13px;font-weight:600;cursor:pointer;touch-action:manipulation;transition:all .15s;">'
            f'&#128993; Average</button>'
            f'<button type="button" onclick="selectFeeling({i},\'struggled\')" data-feel="struggled" '
            f'style="flex:1;padding:10px 6px;border-radius:8px;border:1.5px solid #e5e3de;background:#f9f7f3;font-size:13px;font-weight:600;cursor:pointer;touch-action:manipulation;transition:all .15s;">'
            f'&#128308; Struggled</button>'
            f'</div>'
            f'</div>'
            # Status + Send button
            f'<div class="scores-status" style="font-size:12px;color:#2a7a3e;font-weight:500;margin-bottom:10px;min-height:16px;text-align:center;">&#10003; All 18 holes filled &mdash; ready to send</div>'
            f'<button type="button" id="send-btn-p{i}" class="send-btn" onclick="sendScores({i})" '
            f'style="width:100%;padding:14px;border-radius:10px;border:none;background:linear-gradient(135deg,#1a2e1a,#2d4a1e);color:#fff;font-size:15px;font-weight:700;cursor:pointer;touch-action:manipulation;letter-spacing:.02em;">'
            f'&#9993; Send Scores to Ollie</button>'
            f'</div>'
        )
    return ''.join(blocks)


# ════════════════════════════════════════════════════════════════════════════
# STAT BOXES
# ════════════════════════════════════════════════════════════════════════════

def _diff_scale(pct, left, mid, right):
    return (f'<div class="diff-scale"><div class="diff-scale-track">'
            f'<div class="diff-scale-fill" style="width:100%;"></div>'
            f'<div class="diff-scale-marker" style="left:{pct}%;"></div></div>'
            f'<div style="display:flex;justify-content:space-between;font-size:10px;margin-top:3px;">'
            f'<span style="color:#aaa;">{left}</span>'
            f'<span style="color:#666;font-weight:500;">{mid}</span>'
            f'<span style="color:#aaa;">{right}</span></div></div>')


def _course_table(sorted_list, key, current_name):
    rows = ''
    for val, cname in sorted_list:
        curr  = cname == current_name
        short = (cname.replace('Golf & Country Club', 'G&CC')
                      .replace('Golf Course', 'GC')
                      .replace('Golf Club', 'GC')
                      .replace('Country Club', 'CC')
                      .replace('& Event Lodge', ''))[:28]
        bg  = 'background:#f5c96e22;font-weight:700;' if curr else ''
        col = '#c4621a' if curr else '#ccc'
        fw  = '700' if curr else '400'
        rows += (f'<tr style="{bg}"><td style="padding:3px 6px;font-size:10px;color:{col};">{short}</td>'
                 f'<td style="padding:3px 6px;font-size:10px;font-weight:{fw};color:{col};text-align:right;">{val}</td></tr>')
    return (f'<table style="width:100%;border-collapse:collapse;margin-top:8px;">'
            f'<thead><tr>'
            f'<th style="padding:3px 6px;font-size:9px;color:#888;text-align:left;font-weight:600;text-transform:uppercase;letter-spacing:.08em;">Course</th>'
            f'<th style="padding:3px 6px;font-size:9px;color:#888;text-align:right;font-weight:600;text-transform:uppercase;letter-spacing:.08em;">{key.title()}</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>')


def build_stat_boxes(course_name, c):
    all_yards   = sorted((v['yards'],  k) for k, v in COURSES.items() if 'yards'  in v)
    all_ratings = sorted((v['rating'], k) for k, v in COURSES.items() if 'rating' in v)
    all_slopes  = sorted((v['slope'],  k) for k, v in COURSES.items() if 'slope'  in v)
    total = len(all_yards)

    def pct(val, lst):
        return round(len([x for x, _ in lst if x < val]) / total * 100)

    yp = pct(c['yards'],  all_yards)
    rp = pct(c['rating'], all_ratings)
    sp = pct(c['slope'],  all_slopes)

    yt  = _course_table(all_yards,   'yards',  course_name)
    rt  = _course_table(all_ratings, 'rating', course_name)
    st  = _course_table(all_slopes,  'slope',  course_name)

    y_mid  = f'Longer than {yp}% of courses'   if yp >= 50 else f'Shorter than {100-yp}% of courses'
    r_mid  = f'Harder than {rp}% of courses'   if rp >= 50 else f'Easier than {100-rp}% of courses'
    s_mid  = f'Demanding vs {sp}% of courses'  if sp >= 50 else f'Forgiving vs {100-sp}% of courses'

    yards_ex  = f'<div class="explainer" id="yards-ex"><p><strong>Yardage</strong> is the total distance from your tee box. {course_name} plays <strong>{c["yards"]:,} yards</strong>.</p>{yt}</div>'
    rating_ex = f'<div class="explainer" id="rating-ex"><p><strong>Course Rating</strong> of {c["rating"]} is the expected score for a scratch golfer.</p>{rt}</div>'
    slope_ex  = f'<div class="explainer" id="slope-ex"><p><strong>Slope Rating</strong> of {c["slope"]} measures difficulty for higher handicap players. Standard is 113.</p>{st}</div>'

    boxes = (
        f'<div class="stat clickable" onclick="toggleEx(\'yards-ex\')"><div style="display:flex;justify-content:flex-start;align-items:center;gap:5px;margin-bottom:2px;">'
        f'<span style="font-size:10px;font-weight:700;color:#c4621a;letter-spacing:.08em;">YARDAGE</span></div>'
        f'<div class="stat-val" style="margin-left:calc({yp}% - 18px);">{c["yards"]:,}</div>'
        f'{_diff_scale(yp, "Shorter", y_mid, "Longer")}{yards_ex}</div>'

        f'<div class="stat clickable" onclick="toggleEx(\'rating-ex\')"><div style="display:flex;justify-content:flex-start;align-items:center;gap:5px;margin-bottom:2px;">'
        f'<span style="font-size:10px;font-weight:700;color:#c4621a;letter-spacing:.08em;">RATING</span></div>'
        f'<div class="stat-val" style="margin-left:calc({rp}% - 18px);">{c["rating"]}</div>'
        f'{_diff_scale(rp, "Easier", r_mid, "Harder")}{rating_ex}</div>'

        f'<div class="stat clickable" onclick="toggleEx(\'slope-ex\')"><div style="display:flex;justify-content:flex-start;align-items:center;gap:5px;margin-bottom:2px;">'
        f'<span style="font-size:10px;font-weight:700;color:#c4621a;letter-spacing:.08em;">SLOPE</span></div>'
        f'<div class="stat-val" style="margin-left:calc({sp}% - 18px);">{c["slope"]}</div>'
        f'{_diff_scale(sp, "Forgiving", s_mid, "Demanding")}{slope_ex}</div>'

        f'{_build_walk_box(course_name, c)}'
    )
    return boxes


def _build_walk_box(course_name, c):
    """Avg walk distance box, driven by FIT-tracked rounds (roundData.avgDistKm)."""
    wt = c.get('roundData') or {}
    avg = wt.get('avgDistKm')
    n   = wt.get('nDistSamples', 0)

    # Build comparison across all courses that have walk data
    all_walks = sorted(
        ((v['roundData']['avgDistKm'], k)
         for k, v in COURSES.items()
         if v.get('roundData', {}).get('avgDistKm') is not None)
    )
    total = len(all_walks)

    if avg is None:
        # No distance data for this course — show a muted placeholder
        body = (
            f'<div class="stat-val" style="color:#999;">—<span style="font-size:12px;font-weight:500;color:#aaa;"> km</span></div>'
            f'<div style="font-size:10px;color:#aaa;margin-top:6px;">No distance tracked yet at this course</div>'
        )
        walk_ex = (
            f'<div class="explainer" id="walk-ex">'
            f'<p><strong>Avg. Walk</strong> is the group\'s mean walking distance per 18-hole round, based on previous rounds played.</p>'
            f'<p>No distance has been recorded here yet. Courses with data:</p>'
            f'{_course_table(all_walks, "km", course_name)}'
            f'</div>'
        )
        return (
            f'<div class="stat clickable" onclick="toggleEx(\'walk-ex\')"><div style="display:flex;justify-content:flex-start;align-items:center;gap:5px;margin-bottom:2px;">'
            f'<span style="font-size:10px;font-weight:700;color:#c4621a;letter-spacing:.08em;">AVG. WALK</span></div>'
            f'{body}{walk_ex}</div>'
        )

    pct = round(len([x for x, _ in all_walks if x < avg]) / total * 100) if total else 50
    mid = (f'Longer than {pct}% of courses' if pct >= 50
           else f'Shorter than {100-pct}% of courses')

    walk_rows = [(f"{v:.2f} km", k) for v, k in all_walks]
    sample_note = f'{n} previous round{"s" if n != 1 else ""}'
    walk_ex = (
        f'<div class="explainer" id="walk-ex">'
        f'<p><strong>Avg. Walk</strong> is the group\'s mean walking distance per 18-hole round, based on previous rounds played.</p>'
        f'<p>{course_name} averages <strong>{avg:.2f} km</strong> over <strong>{sample_note}</strong>.</p>'
        f'{_course_table(walk_rows, "km", course_name)}'
        f'</div>'
    )

    return (
        f'<div class="stat clickable" onclick="toggleEx(\'walk-ex\')"><div style="display:flex;justify-content:flex-start;align-items:center;gap:5px;margin-bottom:2px;">'
        f'<span style="font-size:10px;font-weight:700;color:#c4621a;letter-spacing:.08em;">AVG. WALK</span></div>'
        f'<div class="stat-val" style="margin-left:calc({pct}% - 18px);">{avg:.1f} <span style="font-size:14px;font-weight:600;">km</span></div>'
        f'{_diff_scale(pct, "Shorter", mid, "Longer")}{walk_ex}</div>'
    )


def _build_round_time_ex(course_name, c):
    """Explainer block shown when ? is tapped on the Avg Round Time box."""
    wt = c.get('roundData') or {}
    avg = wt.get('avgTimeMin')
    n   = wt.get('nTimeSamples', 0)

    def fmt_hm(mins):
        if mins is None: return '—'
        h = int(mins // 60); m = int(round(mins - h * 60))
        if m == 60: h, m = h + 1, 0
        return f'{h}:{m:02d}'

    all_times = sorted(
        ((v['roundData']['avgTimeMin'], k)
         for k, v in COURSES.items()
         if v.get('roundData', {}).get('avgTimeMin') is not None)
    )
    time_rows = [(fmt_hm(m), cn) for m, cn in all_times]

    if avg is None:
        return (
            f'<div class="explainer" id="round-time-ex" style="margin-top:5px;">'
            f'<p><strong>Avg. Round Time</strong> is the group\'s mean total round time, based on previous rounds played.</p>'
            f'<p>No round time has been recorded here yet. Courses with data:</p>'
            f'{_course_table(time_rows, "time", course_name)}'
            f'</div>'
        )

    sample = f'{n} previous round{"s" if n != 1 else ""}'
    return (
        f'<div class="explainer" id="round-time-ex" style="margin-top:5px;">'
        f'<p><strong>Avg. Round Time</strong> is the group\'s mean total round time, based on previous rounds played &mdash; including tee-box waits and the walk from 9 to 10.</p>'
        f'<p>{course_name} averages <strong>{fmt_hm(avg)}</strong> over <strong>{sample}</strong>. 4 hours is industry-standard for an 18-hole round.</p>'
        f'{_course_table(time_rows, "time", course_name)}'
        f'</div>'
    )


def _build_round_time_caption(rd, tee_time_str, drive_min=None):
    """Short caption shown under the round-time LCD. Computes estimated
    'home by' time = tee time + avg round duration + drive back to Kanata.
    Also shows the number of rounds the average is based on."""
    avg_min = (rd or {}).get('avgTimeMin')
    n_samples = (rd or {}).get('nTimeSamples', 0)
    if not avg_min or not tee_time_str:
        return '&#8987; typical round pace'
    # Parse tee time "HH:MM" (24h) and add avg_min + drive_min
    try:
        h, m = map(int, tee_time_str.split(':'))
    except (ValueError, AttributeError):
        return f'&#8987; based on {n_samples} rounds' if n_samples else '&#8987; typical round pace'
    total_min = h * 60 + m + int(round(avg_min)) + (drive_min or 0)
    rh, rm = (total_min // 60) % 24, total_min % 60
    # Format in 12h with AM/PM for human readability
    am_pm = 'AM' if rh < 12 else 'PM'
    h12 = rh if 1 <= rh <= 12 else (12 if rh == 0 else rh - 12)
    label = 'home by' if drive_min else 'back by'
    sample_note = f' &middot; {n_samples} round' + ('s' if n_samples != 1 else '') if n_samples else ''
    return f'&#8987; {label} ~{h12}:{rm:02d} {am_pm}{sample_note}'


def _build_elev_caption(rd):
    """Short caption shown under the elevation SVG."""
    span = (rd or {}).get('avgAltSpanM')
    asc  = (rd or {}).get('avgSmoothAscentM')
    if span and asc:
        return f'&#8597; {span:.0f}m range &middot; ~{asc:.0f}m cumulative climb per round'
    if span:
        return f'&#8597; {span:.0f}m range'
    return '&#8597; approx. elevation range'


def _build_elev_ex(course_name, c):
    """Explainer block for Elevation."""
    rd = c.get('roundData') or {}
    asc = rd.get('avgSmoothAscentM')
    span = rd.get('avgAltSpanM')
    lo   = rd.get('altMinM')
    hi   = rd.get('altMaxM')
    n    = rd.get('nAscentSamples', 0)

    all_asc = sorted(
        ((v['roundData']['avgSmoothAscentM'], k)
         for k, v in COURSES.items()
         if v.get('roundData', {}).get('avgSmoothAscentM') is not None)
    )
    asc_rows = [(f'{m:.0f} m', k) for m, k in all_asc]

    if asc is None:
        return (
            f'<div class="explainer" id="elev-ex" style="margin-top:5px;">'
            f'<p><strong>Elevation</strong> is based on altitude data from previous rounds played.</p>'
            f'<p>No altitude data for this course yet. Courses with data:</p>'
            f'{_course_table(asc_rows, "climb", course_name)}'
            f'</div>'
        )

    sample = f'{n} previous round{"s" if n != 1 else ""}'
    span_txt = f'{span:.0f}m' if span else '—'
    range_txt = f'{lo:.0f}–{hi:.0f}m' if (lo is not None and hi is not None) else '—'
    return (
        f'<div class="explainer" id="elev-ex" style="margin-top:5px;">'
        f'<p>The <strong>elevation curve</strong> is the average altitude profile across <strong>{sample}</strong> at this course.</p>'
        f'<p>{course_name} spans <strong>{range_txt}</strong> ({span_txt} of vertical range). '
        f'The group climbs about <strong>{asc:.0f}m cumulatively</strong> over 18 holes here &mdash; the sum of every rise, not the high-point minus low-point.</p>'
        f'{_course_table(asc_rows, "climb", course_name)}'
        f'</div>'
    )


# ════════════════════════════════════════════════════════════════════════════
# COURSE INTEL
# ════════════════════════════════════════════════════════════════════════════

def build_course_intel(course_name, c, short_name):
    all_slopes   = sorted((v['slope'],  k) for k, v in COURSES.items() if 'slope'  in v)
    all_yards    = sorted((v['yards'],  k) for k, v in COURSES.items() if 'yards'  in v)
    all_ratings  = sorted((v['rating'], k) for k, v in COURSES.items() if 'rating' in v)
    total        = len(all_slopes)

    def ordinal(n):
        return {1:'1st',2:'2nd',3:'3rd'}.get(n, f'{n}th')

    slope_rank  = [k for _,k in all_slopes].index(course_name)  + 1
    yards_rank  = [k for _,k in all_yards].index(course_name)   + 1
    rating_rank = [k for _,k in all_ratings].index(course_name) + 1
    rounds      = c.get('rounds', 0)
    max_rounds  = max(v.get('rounds', 0) for v in COURSES.values())
    layout      = c.get('layout', [])
    par3s       = sum(1 for h in layout if h['par'] == 3)
    par5s       = sum(1 for h in layout if h['par'] == 5)
    all_par3s   = [sum(1 for h in v['layout'] if h['par'] == 3) for v in COURSES.values() if 'layout' in v]

    # Rotation-wide ranks for walk / ascent / time — only include courses that
    # have roundData so we don't compare against missing data.
    courses_with_rd = [(k, v) for k, v in COURSES.items() if v.get('roundData')]

    def rotation_rank(value, field):
        """Return (rank, n_total) for this course's `field` value against all
        courses with roundData. Rank 1 = smallest (e.g., shortest walk)."""
        vals = sorted((v['roundData'][field], k) for k, v in courses_with_rd
                      if v['roundData'].get(field) is not None)
        names = [k for _, k in vals]
        if course_name in names:
            return names.index(course_name) + 1, len(vals)
        return None, len(vals)

    bullets = []

    # ── EXISTING BULLETS ────────────────────────────────────────────────────

    # Slope — always fires
    if slope_rank == 1:
        bullets.append(('&#127948;', '<strong>Most forgiving slope</strong> in your rotation &mdash; below the standard of 113'))
    elif slope_rank <= 3:
        bullets.append(('&#127948;', f'<strong>{ordinal(slope_rank)} easiest slope</strong> in your rotation at {c["slope"]}'))
    elif slope_rank >= total - 2:
        bullets.append(('&#127948;', f'<strong>One of your toughest slopes</strong> at {c["slope"]} &mdash; {ordinal(slope_rank)} hardest in your rotation'))
    else:
        bullets.append(('&#127948;', f'<strong>Slope {c["slope"]}</strong> &mdash; {ordinal(slope_rank)} easiest in your rotation (standard is 113)'))

    # Yardage — fires for top/bottom 3
    if yards_rank == 1:
        bullets.append(('&#128207;', f'<strong>Shortest course</strong> in your rotation at {c["yards"]:,} yds'))
    elif yards_rank <= 3:
        bullets.append(('&#128207;', f'<strong>{ordinal(yards_rank)} shortest course</strong> you play at {c["yards"]:,} yds'))
    elif yards_rank >= total - 2:
        bullets.append(('&#128207;', f'<strong>{ordinal(yards_rank)} longest course</strong> you play at {c["yards"]:,} yds'))

    # Par-3/5 count
    if par3s >= max(all_par3s):
        bullets.append(('&#9971;', f'<strong>{par3s} par-3s</strong> &mdash; most of any course in your rotation. Short iron game is key.'))
    if par5s >= 3:
        bullets.append(('&#9971;', f'<strong>{par5s} par-5s</strong> on the card &mdash; scoring opportunities if you can reach in two'))

    # Rounds played
    if rounds == 1:
        bullets.append(('&#128313;', '<strong>Only played once</strong> &mdash; flying a bit blind out there'))
    elif rounds == max_rounds:
        bullets.append(('&#127942;', f'<strong>Your most-played course</strong> with {rounds} rounds &mdash; you know this one well'))
    elif rounds >= 10:
        bullets.append(('&#127942;', f'<strong>{rounds} rounds played here</strong> &mdash; one of your most familiar courses'))

    # ── NEW BULLETS (course-fact only, rotation-context) ────────────────────

    rd = c.get('roundData') or {}

    # Walking distance — always fires when data is available, with rotation context
    walk_km = rd.get('avgDistKm')
    if walk_km:
        w_rank, w_total = rotation_rank(walk_km, 'avgDistKm')
        if w_rank and w_total >= 3:
            if w_rank == 1:
                bullets.append(('&#128694;', f'<strong>Shortest walk</strong> in your rotation at {walk_km:.1f} km per round'))
            elif w_rank <= 3:
                bullets.append(('&#128694;', f'<strong>{ordinal(w_rank)} shortest walk</strong> in your rotation at {walk_km:.1f} km'))
            elif w_rank >= w_total - 2:
                from_top = w_total - w_rank + 1
                label = 'Longest walk' if from_top == 1 else f'{ordinal(from_top)} longest walk'
                bullets.append(('&#128694;', f'<strong>{label}</strong> in your rotation at {walk_km:.1f} km &mdash; wear comfortable shoes'))
            else:
                # Middle ranks: flip to whichever framing (shortest/longest) has the smaller ordinal
                from_short = w_rank
                from_long = w_total - w_rank + 1
                if from_long < from_short:
                    bullets.append(('&#128694;', f'<strong>{walk_km:.1f} km walked</strong> per round &mdash; {ordinal(from_long)} longest of {w_total} in your rotation'))
                else:
                    bullets.append(('&#128694;', f'<strong>{walk_km:.1f} km walked</strong> per round &mdash; {ordinal(from_short)} shortest of {w_total} in your rotation'))

    # Elevation / ascent — always fires when data is available, with rotation context
    ascent_m = rd.get('avgSmoothAscentM')
    if ascent_m:
        a_rank, a_total = rotation_rank(ascent_m, 'avgSmoothAscentM')
        if a_rank and a_total >= 3:
            if a_rank >= a_total - 1:  # top 2 hilliest
                bullets.append(('&#9968;&#65039;', f'<strong>One of the hilliest rounds</strong> you play &mdash; about {ascent_m:.0f} m of climbing'))
            elif a_rank <= 2:  # bottom 2 flattest
                bullets.append(('&#9968;&#65039;', f'<strong>One of the flattest courses</strong> in your rotation at {ascent_m:.0f} m of climbing'))
            else:
                from_flat = a_rank
                from_hilly = a_total - a_rank + 1
                if from_hilly < from_flat:
                    bullets.append(('&#9968;&#65039;', f'<strong>About {ascent_m:.0f} m of climbing</strong> across the round &mdash; {ordinal(from_hilly)} hilliest of {a_total} in rotation'))
                else:
                    bullets.append(('&#9968;&#65039;', f'<strong>About {ascent_m:.0f} m of climbing</strong> across the round &mdash; {ordinal(from_flat)} flattest of {a_total} in rotation'))

    # Round time — always fires when data is available, with rotation context
    time_min = rd.get('avgTimeMin')
    if time_min:
        hrs = int(time_min // 60)
        mins = int(round(time_min - hrs * 60))
        time_str = f'{hrs}h {mins}m' if mins else f'{hrs}h'
        t_rank, t_total = rotation_rank(time_min, 'avgTimeMin')
        if t_rank and t_total >= 3:
            if t_rank >= t_total - 1:
                bullets.append(('&#128337;', f'<strong>One of your longer rounds</strong> &mdash; {time_str} on average'))
            elif t_rank <= 2:
                bullets.append(('&#128337;', f'<strong>Quick round</strong> &mdash; averages just {time_str}, one of the shorter in your rotation'))
            else:
                bullets.append(('&#128337;', f'<strong>Averages {time_str}</strong> per round &mdash; typical pace for your rotation'))

    # Par-4 mix — count short (<350) and long (>420) par-4s
    hole_yards = c.get('holeYards', [])
    short_p4 = 0
    long_p4  = 0
    for h, y in zip(layout, hole_yards):
        if h['par'] == 4 and y is not None:
            if y < 350: short_p4 += 1
            elif y > 420: long_p4 += 1
    # Only include if one side dominates notably (>=4 of that type)
    if short_p4 >= 4:
        bullets.append(('&#127919;', f'<strong>{short_p4} short par-4s</strong> under 350 yds &mdash; lots of chances to attack'))
    elif long_p4 >= 4:
        bullets.append(('&#127919;', f'<strong>{long_p4} long par-4s</strong> over 420 yds &mdash; driver discipline matters'))

    # Cap at 7 bullets (keep the first 7 — slope always fires so it's always
    # present, and the others prioritize by declaration order).
    bullets = bullets[:7]

    items = ''.join(
        f'<div style="display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.06);">'
        f'<span style="font-size:16px;flex-shrink:0;line-height:1.4;">{icon}</span>'
        f'<span style="font-size:12px;color:#d4d0c8;line-height:1.5;">{text}</span>'
        f'</div>'
        for icon, text in bullets
    )
    return (f'<div style="background:#1a1a16;border-radius:12px;padding:1rem 1.1rem;margin-bottom:.75rem;">'
            f'<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:#c4621a;margin-bottom:4px;">&#128202; Course Intel</div>'
            f'<div style="font-size:11px;color:#888;margin-bottom:8px;">How {short_name} stacks up in your rotation</div>'
            f'{items}</div>')


# ════════════════════════════════════════════════════════════════════════════
# WEATHER CARD HTML
# ════════════════════════════════════════════════════════════════════════════

def build_wx_card(time_str, sub, card_data, note, tee=False):
    border = '2px solid #3a6a9a' if tee else '1px solid #c0ccd8'
    pip    = '<div style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#7ecb6a;margin-right:4px;vertical-align:middle;"></div>' if tee else ''
    wind   = card_data.get('wind_kmh', 0)
    wpct   = min(100, round(wind / 60 * 100))
    wcol   = '#e8735a' if wind >= 30 else '#f5c96e' if wind >= 20 else '#7ab648'
    # Always render the sub-line div so all three cards have equal blue-header
    # height. When sub is empty, emit a non-breaking space so the line has the
    # same baseline as the cards with actual text.
    sub_content = sub if sub else '&nbsp;'
    sub_html = (f'<div style="font-size:10px;color:rgba(255,255,255,.85);margin-bottom:8px;font-weight:500;">{sub_content}</div>')
    note_html = (f'<div style="font-size:9px;color:#667;font-weight:600;text-align:center;margin-top:2px;">{note}</div>'
                 if note else '')
    return (
        f'<div class="wx-card" style="border-radius:10px;overflow:hidden;border:{border};">'
        f'<div style="background:linear-gradient(170deg,#3a5878,#6a90b0);padding:12px 10px 10px;text-align:center;">'
        f'<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:rgba(255,255,255,.95);margin-bottom:3px;">{pip}{time_str}</div>'
        f'{sub_html}'
        f'<div style="font-size:30px;line-height:1;margin-bottom:6px;">{card_data["icon"]}</div>'
        f'<div style="font-size:24px;font-weight:700;color:#fff;">{card_data["temp"]}</div></div>'
        f'<div style="background:#fff;padding:8px 10px;">'
        f'<div style="font-size:10px;font-weight:600;color:#334;margin-bottom:6px;text-align:center;">{card_data["condition"]}</div>'
        f'<div style="display:flex;align-items:center;gap:5px;margin-bottom:4px;">'
        f'<span style="font-size:10px;">&#128168;</span>'
        f'<div style="flex:1;height:3px;background:#e8ecf0;border-radius:2px;overflow:hidden;">'
        f'<div style="width:{wpct}%;height:100%;background:{wcol};border-radius:2px;"></div></div>'
        f'<span style="font-size:9px;color:#667;white-space:nowrap;">{card_data["wind_dir"]} {wind} km/h</span></div>'
        f'{note_html}'
        f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-top:8px;border-top:1px solid #e8ecf0;padding-top:6px;">'
        f'<div style="text-align:center;"><div style="font-size:9px;color:#999;text-transform:uppercase;letter-spacing:.06em;">Feels like</div><div style="font-size:13px;font-weight:700;color:#334;">{card_data["feels"]}</div></div>'
        f'<div style="text-align:center;"><div style="font-size:9px;color:#999;text-transform:uppercase;letter-spacing:.06em;">Humidity</div><div style="font-size:13px;font-weight:700;color:#334;">{card_data["humidity"]}</div></div>'
        f'<div style="text-align:center;"><div style="font-size:9px;color:#999;text-transform:uppercase;letter-spacing:.06em;">High / Low</div><div style="font-size:12px;font-weight:600;color:#334;">{card_data["hi"]} / {card_data["lo"]}</div></div>'
        f'<div style="text-align:center;"><div style="font-size:9px;color:#999;text-transform:uppercase;letter-spacing:.06em;">Rain chance</div><div style="font-size:13px;font-weight:700;color:#3a6a9a;">{card_data["rain"]}</div></div>'
        f'</div></div></div>'
    )


# ════════════════════════════════════════════════════════════════════════════
# ELEVATION SVG  — driven by real mean altitude curve from FIT files
# ════════════════════════════════════════════════════════════════════════════

def build_elevation_svg(curve=None, lo_m=None, hi_m=None):
    """
    Render an elevation profile.
    If `curve` (a list of altitudes, sampled evenly across the round) is given,
    draw the real shape. Otherwise fall back to a generic placeholder sweep.
    lo_m / hi_m override the axis labels if provided (recommended when using a curve).
    """
    VIEW_W, VIEW_H = 280, 110
    LEFT_PAD   = 22
    RIGHT_PAD  = 18
    TOP_PAD    = 14
    BOTTOM_PAD = 22
    plot_w = VIEW_W - LEFT_PAD - RIGHT_PAD
    plot_h = VIEW_H - TOP_PAD - BOTTOM_PAD

    # Determine axis range
    if curve and len(curve) >= 2:
        c_lo, c_hi = min(curve), max(curve)
        # Pad axis range ±3m so the curve isn't flush against the edges
        pad = max(3.0, (c_hi - c_lo) * 0.12)
        axis_lo = round(c_lo - pad)
        axis_hi = round(c_hi + pad)
        if axis_hi - axis_lo < 10:  # minimum 10m axis range for very flat courses
            mid = (axis_hi + axis_lo) / 2
            axis_lo = round(mid - 5); axis_hi = round(mid + 5)
    else:
        axis_lo = lo_m if lo_m is not None else 90
        axis_hi = hi_m if hi_m is not None else 115

    axis_mid = (axis_hi + axis_lo) // 2
    axis_range = max(axis_hi - axis_lo, 1)

    def y_of(m):
        """Map altitude in meters to SVG y-coordinate."""
        frac = (m - axis_lo) / axis_range
        return TOP_PAD + (1 - frac) * plot_h

    def x_of(i, n):
        return LEFT_PAD + (i / (n - 1)) * plot_w

    # Build path
    if curve and len(curve) >= 2:
        n = len(curve)
        # Smoothed line path (polyline — curve is already resampled to ~60 pts)
        pts = [(x_of(i, n), y_of(curve[i])) for i in range(n)]
        line_d = 'M' + ' L'.join(f'{x:.1f} {y:.1f}' for x, y in pts)
        fill_d = line_d + f' L{pts[-1][0]:.1f} {VIEW_H - BOTTOM_PAD} L{pts[0][0]:.1f} {VIEW_H - BOTTOM_PAD} Z'
        start_x, start_y = pts[0]
        end_x,   end_y   = pts[-1]
        # For the labels, use actual start and end altitudes
        start_alt_label = f'{curve[0]:.0f}m'
        end_alt_label   = f'{curve[-1]:.0f}m'
    else:
        # Generic sweep (placeholder bezier — matches the old look)
        line_d = ('M22 88 C60 82 90 70 120 55 C148 42 168 26 190 22 '
                  'C210 18 232 24 262 34')
        fill_d = line_d + ' L262 96 L22 96 Z'
        start_x, start_y = 22, 88
        end_x,   end_y   = 262, 34
        start_alt_label = f'{axis_lo}m'
        end_alt_label   = f'{axis_hi}m'

    # Gridlines (3 horizontal refs)
    y_hi   = y_of(axis_hi)
    y_mid  = y_of(axis_mid)
    y_lo   = y_of(axis_lo)

    return (
        f'<svg viewBox="0 0 {VIEW_W} {VIEW_H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;display:block;">'
        f'<defs><linearGradient id="elevFill" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="#3a5878" stop-opacity="0.75"/>'
        f'<stop offset="100%" stop-color="#b8d0e8" stop-opacity="0.1"/></linearGradient></defs>'
        f'<line x1="{LEFT_PAD}" y1="{y_hi:.1f}"  x2="{VIEW_W-RIGHT_PAD}" y2="{y_hi:.1f}"  stroke="#e8e5de" stroke-width="0.7" stroke-dasharray="3,4"/>'
        f'<line x1="{LEFT_PAD}" y1="{y_mid:.1f}" x2="{VIEW_W-RIGHT_PAD}" y2="{y_mid:.1f}" stroke="#e8e5de" stroke-width="0.7" stroke-dasharray="3,4"/>'
        f'<line x1="{LEFT_PAD}" y1="{y_lo:.1f}"  x2="{VIEW_W-RIGHT_PAD}" y2="{y_lo:.1f}"  stroke="#e8e5de" stroke-width="0.7" stroke-dasharray="3,4"/>'
        f'<text x="0" y="{y_hi+3.5:.1f}"  font-family="-apple-system,sans-serif" font-size="7.5" fill="#ccc">{axis_hi}m</text>'
        f'<text x="0" y="{y_mid+3.5:.1f}" font-family="-apple-system,sans-serif" font-size="7.5" fill="#ccc">{axis_mid}m</text>'
        f'<text x="0" y="{y_lo+3.5:.1f}"  font-family="-apple-system,sans-serif" font-size="7.5" fill="#ccc">{axis_lo}m</text>'
        f'<path d="{fill_d}" fill="url(#elevFill)"/>'
        f'<path d="{line_d}" fill="none" stroke="#3a5878" stroke-width="2.5" stroke-linecap="round"/>'
        f'<circle cx="{start_x:.1f}" cy="{start_y:.1f}" r="5" fill="#c4621a" stroke="#fff" stroke-width="1.5"/>'
        f'<circle cx="{end_x:.1f}"   cy="{end_y:.1f}"   r="5" fill="#3a5878" stroke="#fff" stroke-width="1.5"/>'
        f'<text x="{max(3, start_x-14):.1f}" y="{max(TOP_PAD+4, start_y-6):.1f}" font-family="-apple-system,sans-serif" font-size="11" font-weight="700" fill="#1a1a16" letter-spacing="-0.5">{start_alt_label}</text>'
        f'<text x="{min(VIEW_W-28, end_x+6):.1f}" y="{max(TOP_PAD+4, end_y-4):.1f}" font-family="-apple-system,sans-serif" font-size="11" font-weight="700" fill="#1a1a16" letter-spacing="-0.5">{end_alt_label}</text>'
        f'</svg>'
    )


# ════════════════════════════════════════════════════════════════════════════
# LCD 7-SEGMENT CLOCK
# ════════════════════════════════════════════════════════════════════════════

def build_lcd(time_str='3:41'):
    SEG_ON, SEG_DIM = '#38bdf8', '#001828'
    SEGS = {'0':(1,1,1,1,1,1,0),'1':(0,1,1,0,0,0,0),'2':(1,1,0,1,1,0,1),
            '3':(1,1,1,1,0,0,1),'4':(0,1,1,0,0,1,1),'5':(1,0,1,1,0,1,1),
            '6':(1,0,1,1,1,1,1),'7':(1,1,1,0,0,0,0),'8':(1,1,1,1,1,1,1),
            '9':(1,1,1,1,0,1,1)}

    def seg(ch, ox, oy, w=24, h=40, t=4):
        if ch == ':':
            cx = ox + w / 2
            return (f'<circle cx="{round(cx,1)}" cy="{round(oy+h*0.32,1)}" r="{t*0.8}" fill="{SEG_ON}"/>'
                    f'<circle cx="{round(cx,1)}" cy="{round(oy+h*0.68,1)}" r="{t*0.8}" fill="{SEG_ON}"/>')
        sv = SEGS.get(ch, (0,)*7); g = 2.0
        col = lambda on: SEG_ON if on else SEG_DIM
        def hp(x, y, on): return f'<polygon points="{x+t*0.6},{y} {x+w-t*0.6},{y} {x+w-g},{y+t} {x+g},{y+t}" fill="{col(on)}"/>'
        def vp(x, y, on, top=True):
            pts = (f"{x},{y+t*0.6} {x+t},{y+g} {x+t},{y+h/2-g} {x},{y+h/2}" if top else
                   f"{x},{y+h/2} {x+t},{y+h/2+g} {x+t},{y+h-t*0.6} {x},{y+h-t*0.6+g}")
            return f'<polygon points="{pts}" fill="{col(on)}"/>'
        r = ''
        r += hp(ox+g, oy, sv[0]); r += vp(ox+w-t, oy, sv[1], True); r += vp(ox+w-t, oy, sv[2], False)
        r += hp(ox+g, oy+h-t, sv[3]); r += vp(ox, oy, sv[4], False); r += vp(ox, oy, sv[5], True)
        r += hp(ox+g, oy+h/2-t/2, sv[6])
        return r

    cw = {'0':24,'1':24,'2':24,'3':24,'4':24,'5':24,'6':24,'7':24,'8':24,'9':24,':':14}
    chars = list(time_str)
    total_w = sum(cw.get(ch, 24) for ch in chars) + 7 * (len(chars) - 1)
    vw, vh = 280, 96; ph = 64; py = (vh-ph)/2; dh = 40
    ox = (vw - total_w) / 2; oy = py + (ph - dh) / 2
    digits = ''; x = ox
    for ch in chars:
        digits += seg(ch, round(x,1), round(oy,1))
        x += cw.get(ch, 24) + 7
    return (f'<svg viewBox="0 0 {vw} {vh}" xmlns="http://www.w3.org/2000/svg" style="width:100%;display:block;">'
            f'<rect x="4" y="{round(py,1)}" width="{vw-8}" height="{ph}" rx="8" fill="#001020" stroke="#002040" stroke-width="1.5"/>'
            f'<rect x="8" y="{round(py+4,1)}" width="{vw-16}" height="{ph-8}" rx="5" fill="none" stroke="#001830" stroke-width="0.75"/>'
            f'{digits}'
            f'<text x="{vw-14}" y="{round(py+ph-9,1)}" text-anchor="end" font-family="monospace" font-size="8" fill="{SEG_ON}" opacity="0.45" letter-spacing="1">AM</text>'
            f'</svg>')


# ════════════════════════════════════════════════════════════════════════════
# POST-ROUND STOPS
# ════════════════════════════════════════════════════════════════════════════

def build_stops(meta, location_str):
    stops = meta.get('stops', [])
    cards = ''
    for i, s in enumerate(stops):
        colour = s.get('color', STOP_COLOURS[i % len(STOP_COLOURS)])
        cards += (
            f'<div class="treat-card">'
            f'<div class="treat-icon">{s["icon"]}</div>'
            f'<div class="treat-info">'
            f'<div class="treat-name" style="color:{colour};font-weight:700;">{s["name"]}</div>'
            f'<div class="treat-addr">{s["addr"]} &middot; {s["dist"]}</div>'
            f'<div class="treat-desc">{s["desc"]}</div>'
            f'<div class="treat-meta">'
            f'<span class="treat-rating">&#9733; {s["rating"]}</span>'
            f'<span class="treat-badge">{s["badge"]}</span>'
            f'<span class="treat-hours">{s["hours"]}</span>'
            f'</div></div></div>'
        )
    return (
        f'<div class="section">'
        f'<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;padding-bottom:8px;">'
        f'<div><div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:#c4621a;margin-bottom:2px;">'
        f'<span id="sec-postround"></span>&#127846; Post-Round Stop Options</div>'
        f'<div style="font-size:13px;font-weight:500;color:#4a7c6f;font-style:italic;">On the way home from {location_str}</div></div></div>'
        f'<div class="treat-list">{cards}</div></div>'
    )


# ════════════════════════════════════════════════════════════════════════════
# REVIEWS
# ════════════════════════════════════════════════════════════════════════════

def build_reviews(meta, rng):
    reviews = meta.get('reviews', [])
    if not reviews:
        return ''
    # Pick 2 random 1-star reviews
    bad = [r for r in reviews if r['stars'] == 1]
    if len(bad) >= 2:
        picks = rng.sample(bad, 2)
    else:
        picks = bad[:2]
    cards = ''.join(
        f'<div class="stat" style="background:#fffbf2;border:1px solid #f5c96e33;">'
        f'<div style="font-size:11px;color:#666;line-height:1.5;font-style:italic;border-left:2px solid #f5c96e;padding-left:8px;">"{r["text"]}"</div>'
        f'<div style="font-size:10px;color:#aaa;margin-top:5px;">&#9733;&#9734;&#9734;&#9734;&#9734; &middot; {r["source"]}</div>'
        f'</div>'
        for r in picks
    )
    return f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:0;">{cards}</div>'


# ════════════════════════════════════════════════════════════════════════════
# CSS
# ════════════════════════════════════════════════════════════════════════════

CSS = '''html{scroll-behavior:smooth;}*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f1eb;color:#1a1a16;padding:12px;max-width:680px;margin:0 auto;}
.header{background:#f4f1eb;margin-bottom:.75rem;}
.section{background:#fff;border-radius:12px;padding:1rem 1.1rem;margin-bottom:.75rem;}
.stat{background:#fff;border-radius:10px;padding:.75rem 1rem;}
.stat-grid{display:grid;grid-template-columns:1fr;gap:5px;}
.stat-label{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.1em;color:#999;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid #e5e3de;}
.stat-val{font-size:22px;font-weight:700;color:#c4621a;line-height:1;}
.diff-scale{margin-top:8px;}
.diff-scale-track{position:relative;height:6px;background:#ece9e3;border-radius:3px;overflow:hidden;margin-top:18px;}
.diff-scale-fill{position:absolute;top:0;left:0;height:100%;background:linear-gradient(90deg,#52a06e,#c4621a);border-radius:3px;}
.diff-scale-marker{position:absolute;top:50%;transform:translate(-50%,-50%);width:12px;height:12px;background:#1a1a16;border:2px solid #fff;border-radius:50%;box-shadow:0 1px 3px rgba(0,0,0,.3);}
.info-btn{display:none;}  /* deprecated — stat boxes are clickable whole now */
.stat.clickable{position:relative;cursor:pointer;transition:background .15s,transform .1s;-webkit-tap-highlight-color:transparent;}
.stat.clickable::after{content:"";position:absolute;top:10px;right:10px;width:7px;height:7px;border-right:1.5px solid #c4621a;border-bottom:1.5px solid #c4621a;transform:rotate(-45deg);opacity:.5;transition:opacity .15s,transform .15s;}
.stat.clickable:hover{background:#fffaf2;}
.stat.clickable:hover::after{opacity:1;transform:rotate(-45deg) translate(1px,1px);}
.stat.clickable:active{transform:scale(0.99);}
.explainer{display:none;background:#1a1a16;color:#f5f3ee;border-radius:10px;padding:1rem 1.1rem;margin-top:10px;font-size:12px;line-height:1.6;}
.explainer.visible{display:block;}
.explainer strong{color:#f5c96e;}
.treat-list{display:flex;flex-direction:column;gap:8px;}
.treat-card{background:#fff;border-radius:10px;padding:1rem;display:flex;gap:14px;align-items:flex-start;}
.treat-icon{font-size:26px;flex-shrink:0;margin-top:2px;}
.treat-info{flex:1;min-width:0;}
.treat-name{font-size:14px;font-weight:600;color:#1a1a16;}
.treat-addr{font-size:12px;color:#aaa;margin-top:2px;}
.treat-desc{font-size:13px;color:#666;margin-top:4px;line-height:1.5;}
.treat-meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;align-items:center;}
.treat-rating{font-size:11px;color:#c8a85a;font-weight:500;}
.treat-hours{font-size:10px;color:#aaa;}
.treat-badge{font-size:10px;background:#f0ede8;color:#888;padding:2px 7px;border-radius:8px;}
.fun-fact{background:#1a1a16;color:#f5f3ee;border-radius:12px;padding:1rem 1.1rem;margin-bottom:.75rem;}
.fun-fact-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#7ecb6a;margin-bottom:6px;}
.fun-fact-text{font-size:13px;line-height:1.6;color:#d4d0c8;}
.footer{margin-top:2rem;padding-top:1rem;border-top:1px solid #e5e3de;font-size:11px;color:#aaa;text-align:center;padding-bottom:2rem;}
input[type=number]::-webkit-inner-spin-button,input[type=number]::-webkit-outer-spin-button{-webkit-appearance:none;margin:0;}
input[type=number]{-moz-appearance:textfield;}
@media(max-width:480px){.wx-grid{grid-template-columns:1fr !important;}}'''

def build_js(course_name, date_str, time_str, players, front_par, back_par,
             lat, lng, sunrise_str=None):
    """Build the <script> block, templating in course/date/player specifics."""
    import json as _json
    fp = _json.dumps(front_par)
    bp = _json.dumps(back_par)
    players_json = _json.dumps(players)
    # For mailto: date display + subject date (Mon DD)
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    mon = dt.strftime('%b')
    dd  = dt.day
    subject_date = f'{mon} {dd}'
    long_date = dt.strftime(f'%A, %B {dt.day}, %Y')
    # Tee time hours for weather refresh (matches the 3 hourly cards: tee, +2h, +4h)
    # time_str is HH:MM (24h)
    try:
        tee_h = int(time_str.split(':')[0])
    except Exception:
        tee_h = 8
    tee_hours = [tee_h, tee_h + 2, tee_h + 4]
    tee_hours_json = _json.dumps(tee_hours)
    # Safe JS literal for course name + time string
    course_js   = _json.dumps(course_name)
    date_js     = _json.dumps(date_str)
    time_js     = _json.dumps(time_str)
    subject_js  = _json.dumps(subject_date)
    longdate_js = _json.dumps(long_date)
    sweeper_js  = _json.dumps('coates.oliver@gmail.com')
    # Short course name for email subject
    short = (course_name.replace('Golf & Country Club', 'G&CC')
                        .replace('Golf Course', 'GC')
                        .replace('Golf Club', 'GC')
                        .replace('Country Club', 'CC')
                        .replace('& Event Lodge', '').strip())
    short_js = _json.dumps(short)
    # Sunrise: either real time (e.g., "5:59 AM") or empty string if unavailable
    sunrise_js = _json.dumps(sunrise_str if sunrise_str else "")

    return f'''
<script>
document.addEventListener("touchstart",function(){{}},{{passive:true}});

function toggleEx(id){{
  document.querySelectorAll('.explainer').forEach(function(el){{
    if(el.id===id){{el.classList.toggle('visible');}}
    else{{el.classList.remove('visible');}}
  }});
}}

// ── Course / round context (templated at build time) ─────────────────────
var COURSE={course_js}, COURSE_SHORT={short_js}, DATE={date_js}, TIME={time_js};
var SUBJECT_DATE={subject_js}, LONG_DATE={longdate_js};
var SWEEPER={sweeper_js};
var FRONT_PAR={fp}, BACK_PAR={bp};
var PLAYERS={players_json};
var LAT={lat}, LNG={lng};
var TEE_HOURS={tee_hours_json};
var SUNRISE={sunrise_js};  // e.g. "5:59 AM" or empty string if unavailable

// ══════════════════════════════════════════════════════════════════════
// SCORECARD — setup + live totals + per-player completion watcher
// ══════════════════════════════════════════════════════════════════════
(function(){{
  function sym(score,par){{
    var d=score-par;
    var w='position:absolute;top:0;left:0;width:100%;height:100%;display:flex;align-items:center;justify-content:center;pointer-events:none;';
    if(d<=-2) return '<div style="'+w+'"><span style="position:absolute;width:26px;height:26px;border-radius:50%;border:1.5px solid #1a6e2e;top:50%;left:50%;transform:translate(-50%,-50%);"></span><span style="position:absolute;width:19px;height:19px;border-radius:50%;border:1.5px solid #1a6e2e;top:50%;left:50%;transform:translate(-50%,-50%);"></span><span style="color:#1a6e2e;font-size:12px;font-weight:800;position:relative;z-index:2;">'+score+'</span></div>';
    if(d===-1) return '<div style="'+w+'"><span style="position:absolute;width:24px;height:24px;border-radius:50%;border:1.5px solid #2a7a3e;top:50%;left:50%;transform:translate(-50%,-50%);"></span><span style="color:#2a7a3e;font-size:12px;font-weight:800;position:relative;z-index:2;">'+score+'</span></div>';
    if(d===0)  return '<div style="'+w+'"><span style="color:#1a1a16;font-size:12px;font-weight:700;position:relative;z-index:2;">'+score+'</span></div>';
    if(d===1)  return '<div style="'+w+'"><span style="position:absolute;width:24px;height:24px;border:1.5px solid #c4621a;top:50%;left:50%;transform:translate(-50%,-50%);border-radius:2px;"></span><span style="color:#c4621a;font-size:12px;font-weight:800;position:relative;z-index:2;">'+score+'</span></div>';
    if(d===2)  return '<div style="'+w+'"><span style="position:absolute;width:26px;height:26px;border:1.5px solid #c8220e;top:50%;left:50%;transform:translate(-50%,-50%);border-radius:2px;"></span><span style="position:absolute;width:19px;height:19px;border:1.5px solid #c8220e;top:50%;left:50%;transform:translate(-50%,-50%);border-radius:1px;"></span><span style="color:#c8220e;font-size:12px;font-weight:800;position:relative;z-index:2;">'+score+'</span></div>';
    return '<div style="'+w+'"><span style="position:absolute;width:24px;height:24px;background:#c8220e;opacity:0.12;top:50%;left:50%;transform:translate(-50%,-50%);border-radius:2px;"></span><span style="position:absolute;width:24px;height:24px;border:2px solid #c8220e;top:50%;left:50%;transform:translate(-50%,-50%);border-radius:2px;"></span><span style="color:#c8220e;font-size:12px;font-weight:800;position:relative;z-index:2;">'+score+'</span></div>';
  }}

  function parForHole(h){{ return h<=9 ? FRONT_PAR[h-1] : BACK_PAR[h-10]; }}

  function setupCell(td){{
    var inp=td.querySelector('input[type=number]');
    if(!inp) return;
    var hole=parseInt(inp.getAttribute('data-hole'));
    var par=parForHole(hole);
    td.style.position='relative';
    var ov=document.createElement('div');
    ov.style.cssText='display:none;position:absolute;top:0;left:0;width:100%;height:100%;z-index:1;background:inherit;';
    td.appendChild(ov);
    function showSym(){{
      var v=parseInt(inp.getAttribute('data-score')||inp.value);
      if(!isNaN(v)&&v>0){{ov.innerHTML=sym(v,par);ov.style.display='block';inp.style.opacity='0';}}
    }}
    function hideSym(){{ov.style.display='none';inp.style.opacity='1';}}
    ov.addEventListener('pointerdown',function(e){{e.preventDefault();hideSym();inp.focus();inp.select();}});
    inp.addEventListener('focus',hideSym);
    inp.addEventListener('input',function(){{
      inp.setAttribute('data-score',inp.value);
      setTimeout(checkReadyToSend,10);
    }});
    inp.addEventListener('blur',function(){{
      inp.setAttribute('data-score',inp.value);
      setTimeout(function(){{if(document.activeElement!==inp)showSym();}},80);
      setTimeout(checkReadyToSend,100);
    }});
  }}

  function setupTotals(table,isFront){{
    var pa=isFront?FRONT_PAR:BACK_PAR;
    var np=pa.reduce(function(a,b){{return a+b;}},0);
    var rows=[];
    table.querySelectorAll('tbody tr').forEach(function(r){{
      if(r.querySelectorAll('input[type=number]').length===9) rows.push(r);
    }});
    rows.forEach(function(row){{
      var inputs=row.querySelectorAll('input[type=number]');
      var tds=row.querySelectorAll('td');
      var tc=tds[tds.length-1];
      tc.style.cssText='background:#f0ede6;font-weight:700;font-size:13px;padding:4px 6px;text-align:center;vertical-align:middle;min-width:32px;';
      function upd(){{
        var s=0,f=0;
        inputs.forEach(function(i){{
          var v=parseInt(i.getAttribute('data-score')||i.value);
          if(!isNaN(v)&&v>0){{s+=v;f++;}}
        }});
        if(f>0){{
          var d=s-np, col=d>0?'#c8220e':d<0?'#2a7a3e':'#888', str=d>0?'+'+d:d===0?'E':''+d;
          tc.innerHTML='<span style="font-size:13px;font-weight:700;color:#1a1a16;">'+s+'</span><br><span style="font-size:9px;font-weight:700;color:'+col+';">'+str+'</span>';
        }} else {{ tc.innerHTML=''; }}
      }}
      inputs.forEach(function(i){{
        i.addEventListener('input',upd);
        i.addEventListener('blur',function(){{setTimeout(upd,90);}});
      }});
    }});
  }}

  function initScorecard(){{
    var tables=[];
    document.querySelectorAll('table').forEach(function(t){{
      if(t.querySelectorAll('input[type=number]').length>0) tables.push(t);
    }});
    tables.forEach(function(t,ti){{
      var isFront=ti%2===0;
      var rows=[];
      t.querySelectorAll('tbody tr').forEach(function(r){{
        if(r.querySelectorAll('input[type=number]').length===9) rows.push(r);
      }});
      rows.forEach(function(row){{
        row.querySelectorAll('td').forEach(function(td){{
          if(td.querySelector('input[type=number]')) setupCell(td);
        }});
      }});
      setupTotals(t,isFront);
    }});
    // Tab from hole-18 input → focus this player's Send button (skip over
    // other players' rows, which is where Tab would naturally go next).
    // Only intercepts when the Send section is actually visible (i.e. all 18
    // scores are filled). Shift+Tab still goes back to hole 17.
    document.querySelectorAll('input[type=number][data-hole="18"]').forEach(function(inp){{
      inp.addEventListener('keydown',function(ev){{
        if(ev.key!=='Tab' || ev.shiftKey) return;
        var row=inp.closest('tr[data-player-row]');
        if(!row) return;
        // Resolve the player index (pi) from the row's position in the ordered
        // list of unique player rows. This avoids carrying player names into JS.
        var rows=Array.from(document.querySelectorAll('tr[data-player-row]'));
        var seen={{}},ordered=[];
        rows.forEach(function(r){{
          var n=r.getAttribute('data-player-row');
          if(!seen[n]){{seen[n]=true; ordered.push(n);}}
        }});
        var pi=ordered.indexOf(row.getAttribute('data-player-row'));
        if(pi<0) return;
        var btn=document.getElementById('send-btn-p'+pi);
        var sect=document.getElementById('send-scores-section-p'+pi);
        // Only redirect if the send section is visible — otherwise let default
        // Tab take the user wherever the natural tab order points.
        if(btn && sect && sect.style.display!=='none' && sect.offsetParent!==null){{
          ev.preventDefault();
          btn.focus();
        }}
      }});
    }});
  }}

  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',initScorecard);
  else initScorecard();
}})();

// ══════════════════════════════════════════════════════════════════════
// SEND SCORES — per-player completion watcher + mailto builder.
// All send-flow functions take a player INDEX (pi), not a name, so names
// with apostrophes/quotes don't break the JS.
// ══════════════════════════════════════════════════════════════════════
var _feelings={{}};  // pi → feeling

window.selectFeeling=function(pi,val){{
  _feelings[pi]=val;
  var sect=document.getElementById('send-scores-section-p'+pi);
  if(!sect) return;
  var cols={{good:{{bg:'#e8f5e9',border:'#2a7a3e',color:'#2a7a3e'}},
             average:{{bg:'#fff8e1',border:'#c8a030',color:'#c8a030'}},
             struggled:{{bg:'#ffeaea',border:'#c8220e',color:'#c8220e'}}}};
  sect.querySelectorAll('button[data-feel]').forEach(function(b){{
    var k=b.getAttribute('data-feel');
    if(k===val){{
      b.style.background=cols[k].bg;
      b.style.borderColor=cols[k].border;
      b.style.color=cols[k].color;
    }} else {{
      b.style.background='#f9f7f3';
      b.style.borderColor='#e5e3de';
      b.style.color='#333';
    }}
  }});
}};

function getScoresForRow(row){{
  // Find scores for a row element (already scoped to one player)
  var front=[],back=[];
  for(var h=1;h<=9;h++){{
    var inp=row.querySelector('input[data-hole="'+h+'"]');
    if(!inp) return null;
    var v=parseInt(inp.getAttribute('data-score')||inp.value);
    if(isNaN(v)||v<=0) return null;
    front.push(v);
  }}
  for(var h=10;h<=18;h++){{
    var inp=document.querySelector('tr[data-player-row="'+row.getAttribute('data-player-row')+'"] input[data-hole="'+h+'"]');
    if(!inp) return null;
    var v=parseInt(inp.getAttribute('data-score')||inp.value);
    if(isNaN(v)||v<=0) return null;
    back.push(v);
  }}
  return {{front:front,back:back}};
}}

function getScoresFor(pi){{
  // Look up scores for player at index pi using their row element (avoids
  // having to put the raw name into a CSS selector).
  var name=PLAYERS[pi];
  if(!name) return null;
  // Find the front-nine row for this player — there are two rows per player
  // (front + back), but scores are split by hole number so either row lets
  // us find the player's inputs.
  var rows=document.querySelectorAll('tr[data-player-row]');
  var match=null;
  for(var i=0;i<rows.length;i++){{
    if(rows[i].getAttribute('data-player-row')===name){{ match=rows[i]; break; }}
  }}
  if(!match) return null;
  return getScoresForRow(match);
}}

var _sent={{}};  // pi → {{ft, bt, tot, feel, sentAt}}

function checkReadyToSend(){{
  PLAYERS.forEach(function(name,pi){{
    var sect=document.getElementById('send-scores-section-p'+pi);
    if(!sect) return;
    // If this player has already sent, leave the confirmation card alone
    if(_sent[pi]) return;
    var data=getScoresFor(pi);
    if(data){{
      if(sect.style.display==='none'){{
        sect.style.display='block';
      }}
    }} else {{
      sect.style.display='none';
    }}
  }});
}}
window.checkReadyToSend=checkReadyToSend;

window.sendScores=function(pi){{
  var name=PLAYERS[pi];
  if(!name) return;
  var data=getScoresFor(pi);
  if(!data) return;
  var ft=data.front.reduce(function(a,b){{return a+b;}},0);
  var bt=data.back.reduce(function(a,b){{return a+b;}},0);
  var tot=ft+bt;
  var fpar=FRONT_PAR.reduce(function(a,b){{return a+b;}},0);
  var bpar=BACK_PAR.reduce(function(a,b){{return a+b;}},0);
  var tpar=fpar+bpar;
  function df(s,p){{ var d=s-p; return d>0?'(+'+d+')':d<0?'('+d+')':'(E)'; }}
  var body=COURSE+'\\n'+LONG_DATE+' - '+TIME+'\\n\\n';
  body+='FRONT NINE\\nH1  H2  H3  H4  H5  H6  H7  H8  H9   OUT\\n';
  body+=data.front.map(function(s){{return String(s).padStart(3);}}).join(' ')+'  '+String(ft).padStart(3)+' '+df(ft,fpar)+'\\n\\n';
  body+='BACK NINE\\nH10 H11 H12 H13 H14 H15 H16 H17 H18   IN\\n';
  body+=data.back.map(function(s){{return String(s).padStart(3);}}).join(' ')+'  '+String(bt).padStart(3)+' '+df(bt,bpar)+'\\n\\n';
  body+='TOTAL: '+tot+' '+df(tot,tpar)+'\\n';
  var feel=_feelings[pi];
  if(feel){{
    var fl={{good:'Played well',average:'Average',struggled:'Struggled'}}[feel];
    body+='Feeling: '+fl+'\\n';
  }}
  body+='\\n---\\nSCORES|'+COURSE+'|'+DATE+'|'+name;
  if(feel) body+='|feeling:'+feel;
  body+='\\nFRONT|'+data.front.join('|')+'\\nBACK|'+data.back.join('|');
  var subject='Scores - '+COURSE_SHORT+' - '+SUBJECT_DATE+' - '+name;
  window.location.href='mailto:'+SWEEPER+'?subject='+encodeURIComponent(subject)+'&body='+encodeURIComponent(body);

  // Mark sent and replace the section with a confirmation card
  _sent[pi]={{ft:ft,bt:bt,tot:tot,tpar:tpar,feel:feel,sentAt:new Date()}};
  showSentConfirmation(pi);
}};

function showSentConfirmation(pi){{
  var name=PLAYERS[pi];
  var sect=document.getElementById('send-scores-section-p'+pi);
  if(!sect || !name) return;
  var s=_sent[pi];
  if(!s) return;
  // Safely escape name for innerHTML (name may contain <, >, &, etc.)
  var esc=function(x){{return String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}};
  var safeName=esc(name);
  var d=s.tot-s.tpar;
  var diffStr=d>0?'+'+d:d===0?'E':''+d;
  var diffCol=d>0?'#c8220e':d<0?'#1a6e2e':'#888';
  var feelMap={{good:{{label:'Played well',color:'#2a7a3e',dot:'\\ud83d\\udfe2'}},
                 average:{{label:'Average',color:'#c8a030',dot:'\\ud83d\\udfe1'}},
                 struggled:{{label:'Struggled',color:'#c8220e',dot:'\\ud83d\\udd34'}}}};
  var feelHtml='';
  if(s.feel && feelMap[s.feel]){{
    var f=feelMap[s.feel];
    feelHtml='<div style="font-size:12px;color:'+f.color+';font-weight:600;margin-top:4px;">'+f.dot+' '+f.label+'</div>';
  }}
  sect.style.background='#f4faf3';
  sect.style.borderColor='#1a6e2e';
  sect.innerHTML=
    '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">'
    +'<div>'
    +'<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:#1a6e2e;margin-bottom:2px;">\\u2713 Scores sent to Ollie</div>'
    +'<div style="font-size:13px;font-weight:700;color:#1a6e2e;">'+safeName+'</div>'
    +'</div>'
    +'<div style="font-size:24px;">\\u2709</div>'
    +'</div>'
    +'<div style="background:#fff;border-radius:8px;padding:12px 14px;margin-bottom:10px;border:1px solid #d4e3d2;">'
    +'<div style="display:flex;justify-content:space-between;align-items:baseline;">'
    +'<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.08em;">Front / Back / Total</div>'
    +'<div style="font-size:13px;color:'+diffCol+';font-weight:700;">'+diffStr+'</div>'
    +'</div>'
    +'<div style="display:flex;gap:18px;align-items:baseline;margin-top:6px;">'
    +'<div style="font-size:18px;font-weight:700;color:#333;">'+s.ft+'</div>'
    +'<div style="font-size:18px;color:#bbb;">/</div>'
    +'<div style="font-size:18px;font-weight:700;color:#333;">'+s.bt+'</div>'
    +'<div style="font-size:18px;color:#bbb;">/</div>'
    +'<div style="font-size:22px;font-weight:800;color:#1a6e2e;">'+s.tot+'</div>'
    +'</div>'
    +feelHtml
    +'</div>'
    +'<div style="display:flex;gap:8px;">'
    +'<button type="button" onclick="resendScores('+pi+')" '
    +'style="flex:1;padding:9px;border-radius:8px;border:1px solid #b0c8af;background:#fff;color:#1a6e2e;font-size:12px;font-weight:600;cursor:pointer;touch-action:manipulation;">'
    +'\\u21bb Re-send</button>'
    +'<button type="button" onclick="editScores('+pi+')" '
    +'style="flex:1;padding:9px;border-radius:8px;border:1px solid #d4d0c8;background:#fff;color:#666;font-size:12px;font-weight:600;cursor:pointer;touch-action:manipulation;">'
    +'\\u270e Edit & re-send</button>'
    +'</div>';
}}

window.resendScores=function(pi){{
  if(!_sent[pi]) return;
  delete _sent[pi];
  window.sendScores(pi);
}};

window.editScores=function(pi){{
  delete _sent[pi];
  var name=PLAYERS[pi];
  var sect=document.getElementById('send-scores-section-p'+pi);
  if(!sect || !name) return;
  sect.style.background='#fff';
  sect.style.borderColor='#1a1f3a';
  var esc=function(x){{return String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}};
  var safeName=esc(name);
  sect.innerHTML=
    '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">'
    +'<div>'
    +'<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:#1a1f3a;margin-bottom:2px;">\\u2709 Send Your Scores</div>'
    +'<div style="font-size:13px;font-weight:700;color:#c4621a;">'+safeName+'</div>'
    +'</div>'
    +'<div style="font-size:24px;">\\u26f3</div>'
    +'</div>'
    +'<div style="margin-bottom:14px;">'
    +'<div style="font-size:11px;font-weight:600;color:#555;margin-bottom:8px;text-transform:uppercase;letter-spacing:.08em;">How did you play?</div>'
    +'<div style="display:flex;gap:8px;">'
    +'<button type="button" onclick="selectFeeling('+pi+',\\'good\\')" data-feel="good" style="flex:1;padding:10px 6px;border-radius:8px;border:1.5px solid #e5e3de;background:#f9f7f3;font-size:13px;font-weight:600;cursor:pointer;touch-action:manipulation;transition:all .15s;">\\ud83d\\udfe2 Played well</button>'
    +'<button type="button" onclick="selectFeeling('+pi+',\\'average\\')" data-feel="average" style="flex:1;padding:10px 6px;border-radius:8px;border:1.5px solid #e5e3de;background:#f9f7f3;font-size:13px;font-weight:600;cursor:pointer;touch-action:manipulation;transition:all .15s;">\\ud83d\\udfe1 Average</button>'
    +'<button type="button" onclick="selectFeeling('+pi+',\\'struggled\\')" data-feel="struggled" style="flex:1;padding:10px 6px;border-radius:8px;border:1.5px solid #e5e3de;background:#f9f7f3;font-size:13px;font-weight:600;cursor:pointer;touch-action:manipulation;transition:all .15s;">\\ud83d\\udd34 Struggled</button>'
    +'</div>'
    +'</div>'
    +'<div class="scores-status" style="font-size:12px;color:#2a7a3e;font-weight:500;margin-bottom:10px;min-height:16px;text-align:center;">\\u2713 All 18 holes filled \\u2014 ready to send</div>'
    +'<button type="button" class="send-btn" onclick="sendScores('+pi+')" style="width:100%;padding:14px;border-radius:10px;border:none;background:linear-gradient(135deg,#1a2e1a,#2d4a1e);color:#fff;font-size:15px;font-weight:700;cursor:pointer;touch-action:manipulation;letter-spacing:.02em;">\\u2709 Send Scores to Ollie</button>';
  if(_feelings[pi]) selectFeeling(pi,_feelings[pi]);
}};

// ══════════════════════════════════════════════════════════════════════
// WEATHER REFRESH — live fetch on open + manual button
// ══════════════════════════════════════════════════════════════════════
(function(){{
  var WI={{'0':'\\u2600\\ufe0f','1':'\\u26c5\\ufe0f','2':'\\u26c5\\ufe0f','3':'\\u2601\\ufe0f','51':'\\ud83c\\udf27','53':'\\ud83c\\udf27','55':'\\ud83c\\udf27','61':'\\ud83c\\udf27','63':'\\ud83c\\udf27','65':'\\ud83c\\udf27','71':'\\u2744\\ufe0f','73':'\\u2744\\ufe0f','80':'\\ud83c\\udf27','81':'\\ud83c\\udf27','95':'\\u26c8\\ufe0f'}};
  var WL={{'0':'Clear Sky','1':'Mainly Clear','2':'Partly Cloudy','3':'Overcast','51':'Light Drizzle','53':'Drizzle','55':'Dense Drizzle','61':'Rain','63':'Rain','65':'Heavy Rain','71':'Light Snow','73':'Snow','80':'Showers','81':'Showers','95':'Thunderstorm'}};
  function wd(d){{ return ['N','NE','E','SE','S','SW','W','NW'][Math.round(d/45)%8]; }}
  function teeLabel(i){{
    if(i===0) return TIME;
    if(i===1) return '+2 Hours';
    return '+4 Hours';
  }}
  function teeSub(i){{
    // Tee-time card: show real sunrise if we have it, else 'Early tee'.
    // Other cards: no sub-line (kept empty for visual balance with a placeholder div).
    if(i===0){{
      return SUNRISE ? '\\ud83c\\udf05 Sunrise ' + SUNRISE : '\\ud83c\\udf05 Early tee';
    }}
    return '';
  }}
  function card(d,i,isTee){{
    var bdr=isTee?'2px solid #3a6a9a':'1px solid #c0ccd8';
    var pip=isTee?'<div style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#7ecb6a;margin-right:4px;vertical-align:middle;"></div>':'';
    var wp=Math.min(100,Math.round(d.wind/60*100)),wc=d.wind>=30?'#e8735a':d.wind>=20?'#f5c96e':'#7ab648';
    var sub=teeSub(i);
    var subContent = sub || '\\u00a0';  // non-breaking space to preserve height
    var subHtml = '<div style="font-size:10px;color:rgba(255,255,255,.85);margin-bottom:8px;font-weight:500;">'+subContent+'</div>';
    return '<div class="wx-card" style="border-radius:10px;overflow:hidden;border:'+bdr+';">'
      +'<div style="background:linear-gradient(170deg,#3a5878,#6a90b0);padding:12px 10px 10px;text-align:center;">'
      +'<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:rgba(255,255,255,.95);margin-bottom:3px;">'+pip+teeLabel(i)+'</div>'
      +subHtml
      +'<div style="font-size:30px;line-height:1;margin-bottom:6px;">'+(WI[String(d.wc)]||'\\u26c5')+'</div>'
      +'<div style="font-size:24px;font-weight:700;color:#fff;">'+d.temp+'\\u00b0C</div></div>'
      +'<div style="background:#fff;padding:8px 10px;">'
      +'<div style="font-size:10px;font-weight:600;color:#334;margin-bottom:6px;text-align:center;">'+(WL[String(d.wc)]||'Mixed')+'</div>'
      +'<div style="display:flex;align-items:center;gap:5px;margin-bottom:4px;"><span style="font-size:10px;">\\ud83d\\udca8</span>'
      +'<div style="flex:1;height:3px;background:#e8ecf0;border-radius:2px;overflow:hidden;"><div style="width:'+wp+'%;height:100%;background:'+wc+';border-radius:2px;"></div></div>'
      +'<span style="font-size:9px;color:#667;white-space:nowrap;">'+d.wdir+' '+d.wind+' km/h</span></div>'
      +'<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-top:8px;border-top:1px solid #e8ecf0;padding-top:6px;">'
      +'<div style="text-align:center;"><div style="font-size:9px;color:#999;text-transform:uppercase;letter-spacing:.06em;">Feels like</div><div style="font-size:13px;font-weight:700;color:#334;">'+d.feels+'\\u00b0C</div></div>'
      +'<div style="text-align:center;"><div style="font-size:9px;color:#999;text-transform:uppercase;letter-spacing:.06em;">Humidity</div><div style="font-size:13px;font-weight:700;color:#334;">'+d.humidity+'%</div></div>'
      +'<div style="text-align:center;"><div style="font-size:9px;color:#999;text-transform:uppercase;letter-spacing:.06em;">High / Low</div><div style="font-size:12px;font-weight:600;color:#334;">'+d.hi+'\\u00b0C / '+d.lo+'\\u00b0C</div></div>'
      +'<div style="text-align:center;"><div style="font-size:9px;color:#999;text-transform:uppercase;letter-spacing:.06em;">Rain chance</div><div style="font-size:13px;font-weight:700;color:#3a6a9a;">'+d.rain+'%</div></div>'
      +'</div></div></div>';
  }}
  function callout(cards){{
    var r0=cards[0].rain,r2=cards[1].rain,w0=cards[0].wind;
    var hdr=r0>=50?'\\ud83c\\udf27 Wet morning ahead':r0>=25?'\\ud83c\\udf26 Keep an eye on it':'\\u26c5 Conditions look good';
    var b1=r0>=50?'Expect rain at tee time - pack the wet gear':r0>=25?'Chance of a shower at tee time':'Dry at tee time';
    var b2=r2<r0?'Conditions improving through the round':r2>r0?'Rain chance increases later':'Conditions holding steady through the round';
    var b3=w0>=30?'Breezy - '+w0+' km/h '+cards[0].wdir+' wind, club up':w0>=20?'Moderate wind - '+w0+' km/h '+cards[0].wdir:'Light wind - good scoring conditions';
    var dot='<span style="width:5px;height:5px;border-radius:50%;background:#b07820;flex-shrink:0;display:inline-block;"></span>';
    var li=function(t){{return '<div style="display:flex;align-items:center;gap:8px;">'+dot+'<span style="font-size:11px;color:#b07820;font-weight:500;">'+t+'</span></div>';}};
    return '<div style="font-size:11px;font-weight:700;color:#3a6a9a;margin-bottom:6px;">'+hdr+'</div><div style="display:flex;flex-direction:column;gap:5px;">'+li(b1)+li(b2)+li(b3)+'</div>';
  }}
  function updBtn(s){{
    var b=document.getElementById('wx-refresh-btn');if(!b)return;
    if(s==='loading'){{b.innerHTML='\\u21bb Updating...';b.style.opacity='0.6';b.disabled=true;}}
    else if(s==='done'){{b.innerHTML='\\u2713 Updated';b.style.opacity='1';b.style.background='#2a7a3e';b.disabled=false;setTimeout(function(){{b.innerHTML='\\ud83d\\udd04 Refresh Weather';b.style.background='#3a6a9a';}},3000);}}
    else{{b.innerHTML='\\u26a0 Using cached data';b.style.opacity='1';b.style.background='#c4621a';b.disabled=false;setTimeout(function(){{b.innerHTML='\\ud83d\\udd04 Refresh Weather';b.style.background='#3a6a9a';}},4000);}}
  }}
  window.refreshWeather=function(){{
    updBtn('loading');
    fetch('https://api.open-meteo.com/v1/forecast?latitude='+LAT+'&longitude='+LNG+'&hourly=temperature_2m,apparent_temperature,relative_humidity_2m,precipitation_probability,wind_speed_10m,wind_direction_10m,weathercode&daily=temperature_2m_max,temperature_2m_min&timezone=America%2FToronto&forecast_days=16')
    .then(function(r){{return r.json();}})
    .then(function(data){{
      var times=data.hourly.time,daily=data.daily,hi='?',lo='?';
      var di=daily.time.indexOf(DATE);
      if(di>=0){{hi=Math.round(daily.temperature_2m_max[di]);lo=Math.round(daily.temperature_2m_min[di]);}}
      var cards=TEE_HOURS.map(function(h,i){{
        var target=DATE+'T'+String(h).padStart(2,'0')+':00',idx=times.indexOf(target);
        if(idx<0)return null;
        var wc=data.hourly.weathercode[idx];
        return {{temp:Math.round(data.hourly.temperature_2m[idx]),feels:Math.round(data.hourly.apparent_temperature[idx]),humidity:Math.round(data.hourly.relative_humidity_2m[idx]),rain:Math.round(data.hourly.precipitation_probability[idx]),wind:Math.round(data.hourly.wind_speed_10m[idx]),wdir:wd(data.hourly.wind_direction_10m[idx]),hi:hi,lo:lo,wc:wc}};
      }});
      if(cards.some(function(c){{return c===null;}})){{updBtn('error');return;}}
      var grid=document.getElementById('wx-cards-grid');
      if(grid) grid.innerHTML=cards.map(function(c,i){{return card(c,i,i===0);}}).join('');
      var co=document.getElementById('wx-callout');
      if(co) co.innerHTML=callout(cards);
      var ts=document.getElementById('wx-timestamp');
      if(ts){{var n=new Date();ts.innerHTML='Updated '+n.toLocaleTimeString([],{{hour:'2-digit',minute:'2-digit'}});}}
      updBtn('done');
    }}).catch(function(){{updBtn('error');}});
  }};
  document.addEventListener('DOMContentLoaded',function(){{setTimeout(window.refreshWeather,800);}});
}})();
</script>
'''


# ════════════════════════════════════════════════════════════════════════════
# MAIN BUILD FUNCTION
# ════════════════════════════════════════════════════════════════════════════

def build_report(course_name, date_str, time_str, players, output_path):
    """
    Build a complete golf prep report HTML file.
    players: list of names e.g. ['Nick', 'Ollie'] — Ollie always last
    """
    print(f"\n[report_builder] Building: {course_name} | {date_str} {time_str} | Players: {players}")

    if course_name not in COURSES:
        raise ValueError(f"Course '{course_name}' not found in courses.json")

    c       = COURSES[course_name]
    meta    = c.get('meta', {})
    rng     = random.Random(hash(course_name + date_str))
    layout  = c['layout']
    yards   = c['holeYards']
    front   = layout[:9]; back    = layout[9:]
    fy      = yards[:9];  by      = yards[9:]
    fp      = sum(h['par'] for h in front)
    bp      = sum(h['par'] for h in back)
    ft      = sum(fy);    bt      = sum(by)
    tee_nm  = c.get('tee', 'White').split(' ')[0]

    # Date / time
    tee_date     = datetime.strptime(date_str, '%Y-%m-%d')
    day_name     = tee_date.strftime('%A')
    date_display = tee_date.strftime(f'%B {tee_date.day}, %Y')
    date_short   = tee_date.strftime(f'%a %b {tee_date.day}')
    h24, m24     = map(int, time_str.split(':'))
    ampm         = 'AM' if h24 < 12 else 'PM'
    h12          = h24 if h24 <= 12 else h24 - 12
    time_display = f"{h12}:{m24:02d}"
    time_full    = f"{time_display} {ampm}"

    # Short course name for sticky nav
    short_name = (course_name.replace('Golf & Country Club', 'G&CC')
                             .replace('Golf Course', 'GC')
                             .replace('Golf Club', 'GC'))

    # Location string for post-round
    address  = meta.get('address', '')
    location = address.split(',')[1].strip() if ',' in address else course_name.split(' ')[0]

    # Google rating
    g_rating   = meta.get('google_rating', '?')
    g_reviews  = meta.get('google_reviews', '?')
    g_stars    = ''.join(['&#9733;'] * round(g_rating) + ['&#9734;'] * (5 - round(g_rating))) if isinstance(g_rating, (int, float)) else ''

    # Booking system
    booking = meta.get('booking', 'Chronogolf').title()

    # Rounds played text
    rounds = c.get('rounds', 0)
    rounds_text = f'{rounds} round{"s" if rounds != 1 else ""} played'

    print(f"  [weather] Fetching Open-Meteo for {meta.get('lat')}, {meta.get('lng')} on {date_str}...")
    wx_cards, sunrise_str = fetch_weather(meta.get('lat', 45.353), meta.get('lng', -76.030), tee_date, time_str)
    wx_icon, wx_header, wx_bullets = _weather_note(wx_cards)
    print(f"  [weather] Done — tee time: {wx_cards[0]['temp']}, rain: {wx_cards[0]['rain']}"
          + (f", sunrise {sunrise_str}" if sunrise_str else ""))

    # Tee time offsets — for each card:
    #   time_str = the primary label shown in the blue header (big)
    #   sub      = small sub-line under the label (we put the sunrise here for
    #              the tee-time card; leave blank for the others)
    offsets = [f"{time_display} {ampm}", "+2 Hours", "+4 Hours"]
    # Sub-line for each card. Tee-time card gets the real sunrise. Others blank.
    if sunrise_str:
        first_sub = f"&#127749; Sunrise {sunrise_str}"
    else:
        first_sub = "&#127749; Early tee"
    subs = [first_sub, "", ""]
    # `notes` was a second small caption in the middle of the card — set to blank
    # now that the sub-line carries the useful info.
    notes = ["", "", ""]
    wx_html = ''.join(build_wx_card(offsets[i], subs[i], wx_cards[i], notes[i], tee=(i==0))
                      for i in range(3))

    wx_bullets_html = ''.join(
        f'<div style="display:flex;align-items:center;gap:8px;">'
        f'<span style="width:5px;height:5px;border-radius:50%;background:#b07820;flex-shrink:0;display:inline-block;"></span>'
        f'<span style="font-size:11px;color:#b07820;font-weight:500;">{b}</span></div>'
        for b in wx_bullets
    )

    # Scorecard tables — palette is chosen from the tees the group plays at
    # this course (c['tee'] → TEE_PALETTES lookup). Same course always looks
    # the same; different tee colors give visually distinct scorecards.
    palette = pick_palette(tee_nm)
    sc = merge_sc(palette)
    print(f"  [palette] {tee_nm} tees")
    front_table = build_scorecard_table(front, fy, 'OUT', ft, fp, players, tee_name=tee_nm, rng=rng, is_front=True, sc=sc)
    back_table  = build_scorecard_table(back,  by, 'IN',  bt, bp, players, tee_name=tee_nm, use_initials=True, rng=rng, is_front=False, sc=sc)

    # Stat boxes, intel, reviews, stops
    stat_boxes = build_stat_boxes(course_name, c)
    intel      = build_course_intel(course_name, c, short_name)
    reviews    = build_reviews(meta, rng)
    stops      = build_stops(meta, location)
    fun_fact   = meta.get('fun_fact', '')

    # Pull per-course round data
    rd = c.get('roundData') or {}

    # Real elevation curve from FIT altitude samples, if available
    elev_curve = rd.get('meanAltCurve')
    elev_svg   = build_elevation_svg(curve=elev_curve)

    # Round-time LCD: use real avg, fall back to em-dashes
    avg_time_min = rd.get('avgTimeMin')
    if avg_time_min:
        h = int(avg_time_min // 60)
        m = int(round(avg_time_min - h * 60))
        if m == 60: h, m = h + 1, 0
        lcd_time_str = f'{h}:{m:02d}'
    else:
        lcd_time_str = '—:—'

    lcd_svg            = build_lcd(lcd_time_str)
    round_time_ex      = _build_round_time_ex(course_name, c)
    elev_caption       = _build_elev_caption(rd)
    round_time_caption = _build_round_time_caption(rd, time_str, c.get('drive_min_from_kanata'))

    # ── Assemble HTML ──────────────────────────────────────────────────────
    parts = [
        f'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">',
        f'<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">',
        f'<link href="https://fonts.googleapis.com/css2?family=Kalam:wght@300&family=Caveat:wght@600&display=swap" rel="stylesheet">',
        f'<style>{CSS}</style>',
        f'</head>\n<body>',

        # Sticky nav
        f'<div id="sticky-nav" style="position:sticky;top:0;z-index:999;background:#1a2e1a;padding:8px 14px;display:flex;align-items:center;justify-content:space-between;box-shadow:0 2px 8px rgba(0,0,0,.3);margin:-12px -12px 12px -12px;">'
        f'<div><div style="font-size:11px;font-weight:700;color:#fff;letter-spacing:.04em;">{short_name}</div>'
        f'<div style="font-size:10px;color:#7ecb6a;font-weight:600;">{time_full} &nbsp;&middot;&nbsp; {date_short}</div></div>'
        f'<div style="display:flex;gap:6px;">'
        f'<a href="#sec-stats" style="font-size:10px;color:rgba(255,255,255,.75);text-decoration:none;background:rgba(255,255,255,.1);padding:4px 8px;border-radius:10px;font-weight:600;">Stats</a>'
        f'<a href="#sec-scorecard" style="font-size:10px;color:rgba(255,255,255,.75);text-decoration:none;background:rgba(255,255,255,.1);padding:4px 8px;border-radius:10px;font-weight:600;">Card</a>'
        f'<a href="#sec-weather" style="font-size:10px;color:rgba(255,255,255,.75);text-decoration:none;background:rgba(255,255,255,.1);padding:4px 8px;border-radius:10px;font-weight:600;">Weather</a>'
        f'<a href="#sec-postround" style="font-size:10px;color:rgba(255,255,255,.75);text-decoration:none;background:rgba(255,255,255,.1);padding:4px 8px;border-radius:10px;font-weight:600;">After</a>'
        f'</div></div>',

        # Hero
        f'<div class="header" style="margin-bottom:0;">'
        f'<div style="background:linear-gradient(135deg,#1a2e1a 60%,#2d4a1e);border-radius:14px;padding:1.4rem 1.5rem 1.2rem;margin-bottom:10px;position:relative;overflow:hidden;">'
        f'<div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.15em;color:#8fcc7a;margin-bottom:10px;">&#9971; Course Guide</div>'
        f'<div style="font-size:26px;font-weight:800;color:#ffffff;letter-spacing:-.5px;line-height:1.1;margin-bottom:4px;">{course_name}</div>'
        f'<div style="font-size:14px;color:rgba(255,255,255,.7);margin-bottom:16px;">{meta.get("address","")}</div>'
        f'<div style="background:rgba(0,0,0,.25);border-radius:10px;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;">'
        f'<div><div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.1em;color:rgba(255,255,255,.6);margin-bottom:3px;">Tee Time</div>'
        f'<div style="font-size:32px;font-weight:800;color:#ffffff;letter-spacing:-.5px;line-height:1;">{time_display} <span style="font-size:16px;font-weight:600;color:rgba(255,255,255,.8);">{ampm}</span></div>'
        f'<div style="font-size:12px;color:#7ecb6a;font-weight:600;margin-top:2px;">{day_name}, {date_display}</div></div>'
        f'<div style="text-align:right;">'
        f'<div style="font-size:11px;color:rgba(255,255,255,.5);margin-bottom:4px;">{rounds_text}</div>'
        f'<div style="font-size:11px;color:rgba(255,255,255,.5);margin-top:2px;">Par {c["par"]} &middot; {c["yards"]:,} yds</div>'
        f'</div></div></div></div>',

        # Reviews section
        f'<div id="sec-stats"></div>',
        f'<div class="section" style="padding:0.6rem 1rem;">',
        f'<div style="display:flex;align-items:center;gap:0;margin-top:0;margin-bottom:8px;">'
        f'<a href="https://www.google.com/maps/search/{course_name.replace(" ", "+")}" target="_blank" '
        f'style="display:inline-flex;align-items:center;gap:7px;background:#fffbf2;border:1px solid #f5c96e44;border-radius:20px;padding:7px 14px 7px 10px;text-decoration:none;">'
        f'<svg width="18" height="18" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        f'<path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/>'
        f'<path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>'
        f'<path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l3.66-2.84z" fill="#FBBC05"/>'
        f'<path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/></svg>'
        f'<span style="font-size:14px;font-weight:700;color:#333;">{g_rating}</span>'
        f'<span style="font-size:13px;color:#f5c96e;letter-spacing:1px;">{g_stars}</span>'
        f'<span style="font-size:12px;color:#999;">{g_reviews} reviews</span></a></div>',
        reviews,
        f'</div>',

        # Stat boxes
        f'<div class="section" style="background:#f4f1eb;padding:0;border-radius:0;box-shadow:none;">',
        f'<div class="stat-grid" style="gap:8px;">{stat_boxes}</div>',
        f'</div>',

        # Intel + elevation/clock
        intel,
        f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:5px;margin-top:5px;">'
        f'<div class="stat clickable" style="margin-top:5px;" onclick="toggleEx(\'elev-ex\')">'
        f'<div style="display:flex;align-items:center;justify-content:center;gap:6px;margin-bottom:8px;">'
        f'<div class="stat-label" style="color:#c4621a;font-weight:600;letter-spacing:.04em;margin-bottom:0;padding:0;border:none;">Elevation</div>'
        f'</div>'
        f'{elev_svg}'
        f'<div style="font-size:10px;color:#aaa;text-align:center;margin-top:3px;">{elev_caption}</div>'
        f'</div>'
        f'<div class="stat clickable" style="margin-top:5px;text-align:center;" onclick="toggleEx(\'round-time-ex\')">'
        f'<div style="display:flex;align-items:center;justify-content:center;gap:6px;margin-bottom:8px;">'
        f'<div class="stat-label" style="color:#c4621a;font-weight:600;letter-spacing:.04em;margin-bottom:0;padding:0;border:none;">Avg. Round Time</div>'
        f'</div>'
        f'{lcd_svg}'
        f'<div style="font-size:10px;color:#aaa;text-align:center;margin-top:6px;">{round_time_caption}</div>'
        f'</div></div>'
        f'{round_time_ex}'
        f'{_build_elev_ex(course_name, c)}',

        # Scorecard
        f'<div id="sec-scorecard"></div>',
        f'<div class="section" style="margin-top:0.75rem;margin-bottom:0.75rem;padding:0;border:1.5px solid #1a1f3a;border-radius:10px;overflow:hidden;">'
        f'<div style="text-align:center;padding:12px 0 10px;">'
        f'<div style="font-family:Georgia,serif;font-size:9px;font-weight:600;text-transform:uppercase;letter-spacing:.25em;color:#888;margin-bottom:4px;">{course_name}</div>'
        f'<div style="font-family:Georgia,serif;font-size:22px;font-weight:700;color:#1a1f3a;letter-spacing:.05em;">SCORECARD</div>'
        f'</div>'
        f'{front_table}{back_table}{build_hcp_legend()}</div>',

        # Send-scores sections — one per player, hidden until their 18 holes are filled
        build_send_scores_sections(players),

        # Weather
        f'<div id="sec-weather"></div>',
        f'<div class="section">'
        f'<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">'
        f'<div><div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:#3a6a9a;margin-bottom:2px;">&#127780; {day_name} Morning Weather</div>'
        f'<div style="font-size:13px;font-weight:600;color:#1a1a16;">{location} &nbsp;&middot;&nbsp; {date_display}</div></div>'
        f'<div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px;">'
        f'<a href="https://weather.gc.ca/en/location/index.html?coords={meta.get("lat",45.353)},{meta.get("lng",-76.030)}" target="_blank" '
        f'style="font-size:10px;color:#3a6a9a;text-decoration:none;background:#f0f4f8;border:1px solid #c0ccd8;padding:4px 10px;border-radius:10px;font-weight:600;white-space:nowrap;">Environment Canada &#8599;</a>'
        f'<button id="wx-refresh-btn" onclick="refreshWeather()" type="button" '
        f'style="font-size:10px;color:#fff;background:#3a6a9a;border:none;padding:4px 10px;border-radius:10px;font-weight:600;cursor:pointer;white-space:nowrap;touch-action:manipulation;">'
        f'&#128260; Refresh Weather</button>'
        f'<span id="wx-timestamp" style="font-size:9px;color:#aaa;"></span>'
        f'</div>'
        f'</div>'
        f'<div id="wx-cards-grid" style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px;" class="wx-grid">{wx_html}</div>'
        f'<div style="background:#f0f4f8;border-left:3px solid #3a6a9a;border-radius:0 8px 8px 0;padding:10px 14px;">'
        f'<div id="wx-callout">'
        f'<div style="font-size:11px;font-weight:700;color:#3a6a9a;margin-bottom:6px;">{wx_icon} {wx_header}</div>'
        f'<div style="display:flex;flex-direction:column;gap:5px;">{wx_bullets_html}</div>'
        f'</div></div><div style="margin-top:14px;"></div></div>',

        # Fun fact
        f'<div class="fun-fact"><div class="fun-fact-label">&#9971; Did you know?</div>'
        f'<div class="fun-fact-text">{fun_fact}</div></div>' if fun_fact else '',

        # Post-round stops
        stops,

        # Footer
        f'<div class="footer">{course_name} &middot; {meta.get("address","")}<br>'
        f'<span style="color:#c8a85a;">{booking}</span> booking system</div>',

        build_js(course_name, date_str, time_str, players,
                 [h['par'] for h in front], [h['par'] for h in back],
                 meta.get('lat', 45.353), meta.get('lng', -76.030),
                 sunrise_str=sunrise_str),
        f'</body>\n</html>',
    ]

    html = '\n'.join(p for p in parts if p)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"  [done] {output_path} — {len(html):,} bytes")

    # Update the manifest + regenerate the index page in the same directory
    update_manifest_and_index(
        output_path  = output_path,
        course_name  = course_name,
        date_str     = date_str,
        time_str     = time_str,
        time_full    = time_full,
        date_display = date_display,
        day_name     = day_name,
        players      = players,
        meta         = meta,
        wx_cards     = wx_cards,
        short_name   = short_name,
        c            = c,
    )

    return output_path


# ════════════════════════════════════════════════════════════════════════════
# INDEX PAGE  — auto-regenerated on every build_report call
# ════════════════════════════════════════════════════════════════════════════

def update_manifest_and_index(output_path, course_name, date_str, time_str,
                              time_full, date_display, day_name, players,
                              meta, wx_cards, short_name, c):
    """Update reports.json and rebuild index.html in the same directory."""
    import os, json
    out_dir = os.path.dirname(output_path)
    fname   = os.path.basename(output_path)
    manifest_path = os.path.join(out_dir, 'reports.json')
    index_path    = os.path.join(out_dir, 'index.html')

    # Load existing manifest
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
    else:
        manifest = {'reports': []}

    # Build / update this report's entry
    wx0 = wx_cards[0] if wx_cards else {}
    entry = {
        'file':         fname,
        'course':       course_name,
        'short':        short_name,
        'date':         date_str,
        'time_24':      time_str,
        'time_display': time_full,
        'date_display': date_display,
        'day_name':     day_name,
        'players':      players,
        'address':      meta.get('address', ''),
        'tee':          c.get('tee', 'White'),
        'yards':        c.get('yards'),
        'par':          c.get('par'),
        'wx': {
            'temp':      wx0.get('temp'),
            'icon':      wx0.get('icon'),
            'condition': wx0.get('condition'),
            'rain':      wx0.get('rain'),
        }
    }
    # Replace if (date, time, course) already exists; else append
    keep = [r for r in manifest['reports']
            if not (r['date']==date_str and r.get('time_24')==time_str and r['course']==course_name)]
    keep.append(entry)
    manifest['reports'] = keep
    manifest['updated'] = datetime.now().isoformat(timespec='seconds')

    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    # Regenerate index
    write_index(index_path, manifest['reports'])
    print(f"  [index] {index_path} ({len(manifest['reports'])} report{'s' if len(manifest['reports'])!=1 else ''})")


def write_index(index_path, reports):
    """Render index.html — cohesive with the report design."""
    from datetime import date as _date
    import json as _json

    today = _date.today().isoformat()

    # Sort newest first
    sorted_reports = sorted(reports, key=lambda r: (r['date'], r.get('time_24','')), reverse=True)
    upcoming = [r for r in sorted_reports if r['date'] >= today]
    past     = [r for r in sorted_reports if r['date'] <  today]

    def fmt_card(r, is_past):
        wx = r.get('wx') or {}
        temp_chip = ''
        if wx.get('temp') is not None:
            icon = wx.get('icon') or '&#9925;&#65039;'
            cond = wx.get('condition') or ''
            rain = wx.get('rain')
            rain_chip = (f'<span style="font-size:10px;color:#3a6a9a;background:#eef2f6;'
                         f'padding:2px 7px;border-radius:8px;margin-left:5px;">{rain}% rain</span>'
                         if rain not in (None, 0, '0%', '0', 0.0) else '')
            temp_chip = (f'<div style="display:flex;align-items:center;gap:6px;margin-top:6px;">'
                         f'<span style="font-size:16px;">{icon}</span>'
                         f'<span style="font-size:12px;color:#666;">{wx["temp"]} &middot; {cond}</span>'
                         f'{rain_chip}</div>')
        # Players row — just initials in colored chips
        chips = ''
        for i, p in enumerate(r.get('players', [])):
            chips += (f'<span style="display:inline-flex;align-items:center;justify-content:center;'
                      f'width:22px;height:22px;border-radius:50%;background:#1a2e1a;color:#fff;'
                      f'font-size:10px;font-weight:700;font-family:Caveat,cursive;'
                      f'margin-right:-6px;border:2px solid #fff;">{p[0].upper()}</span>')
        players_html = (f'<div style="display:flex;align-items:center;margin-top:8px;">'
                        f'<div style="display:flex;">{chips}</div>'
                        f'<span style="font-size:11px;color:#888;margin-left:14px;">'
                        f'{", ".join(r.get("players", []))}</span></div>')

        # Completed badge for past rounds
        badge = ''
        if is_past:
            badge = (f'<span style="background:#1a6e2e;color:#fff;font-size:9px;font-weight:700;'
                     f'text-transform:uppercase;letter-spacing:.1em;padding:3px 8px;border-radius:8px;'
                     f'margin-left:8px;vertical-align:middle;">&#10003; Played</span>')

        bg = '#fff' if not is_past else '#fafaf6'
        border = '2px solid #1a2e1a' if not is_past else '1px solid #e5e3de'

        # Yards and par sub-line
        meta_chips = ''
        if r.get('par'):  meta_chips += f'<span style="margin-right:10px;">Par {r["par"]}</span>'
        if r.get('yards'): meta_chips += f'<span style="margin-right:10px;">{r["yards"]:,} yds</span>'
        if r.get('tee'):   meta_chips += f'<span>{r["tee"]} tees</span>'

        return (
            f'<a href="{r["file"]}" style="text-decoration:none;color:inherit;display:block;'
            f'background:{bg};border-radius:12px;padding:1rem 1.1rem;margin-bottom:.65rem;'
            f'border:{border};box-shadow:0 1px 4px rgba(0,0,0,.04);transition:transform .1s;">'
            f'<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">'
            f'<div style="flex:1;min-width:0;">'
            f'<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;'
            f'color:#c4621a;margin-bottom:3px;">{r["day_name"]} &middot; {r["time_display"]}</div>'
            f'<div style="font-size:15px;font-weight:700;color:#1a1a16;line-height:1.25;">'
            f'{r["short"] or r["course"]}{badge}</div>'
            f'<div style="font-size:11px;color:#888;margin-top:3px;">{r["date_display"]}</div>'
            f'<div style="font-size:10px;color:#999;margin-top:5px;">{meta_chips}</div>'
            f'{temp_chip}'
            f'{players_html}'
            f'</div>'
            f'<div style="font-size:18px;color:#c4621a;flex-shrink:0;margin-top:4px;">&rsaquo;</div>'
            f'</div></a>'
        )

    upcoming_html = ''.join(fmt_card(r, False) for r in upcoming)
    past_html     = ''.join(fmt_card(r, True)  for r in past)

    upcoming_section = ''
    if upcoming:
        upcoming_section = (
            f'<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;'
            f'color:#1a2e1a;margin:0 0 12px 4px;">&#9971; Upcoming</div>'
            f'{upcoming_html}'
        )
    past_section = ''
    if past:
        past_section = (
            f'<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;'
            f'color:#888;margin:24px 0 12px 4px;">Past Rounds</div>'
            f'{past_html}'
        )
    empty = ''
    if not upcoming and not past:
        empty = ('<div style="background:#fff;border-radius:12px;padding:2rem;text-align:center;'
                 'color:#aaa;font-size:13px;">No reports yet. Run the build script to add one.</div>')

    css = (
        'html{scroll-behavior:smooth;}*{box-sizing:border-box;margin:0;padding:0;}'
        'body{font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;'
        'background:#f4f1eb;color:#1a1a16;padding:14px;max-width:680px;margin:0 auto;padding-bottom:3rem;}'
        'a:active{transform:scale(0.99);}'
    )

    html = (
        f'<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        f'<meta charset="UTF-8">\n'
        f'<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">\n'
        f'<title>NoB Golf Reports</title>\n'
        f'<link href="https://fonts.googleapis.com/css2?family=Caveat&display=swap" rel="stylesheet">\n'
        f'<style>{css}</style>\n'
        f'</head>\n<body>\n'
        # Hero
        f'<div style="background:linear-gradient(135deg,#1a2e1a 60%,#2d4a1e);border-radius:14px;'
        f'padding:1.4rem 1.5rem;margin-bottom:1rem;color:#fff;">'
        f'<div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.15em;'
        f'color:#8fcc7a;margin-bottom:6px;">&#9971; NoB Golf Group</div>'
        f'<div style="font-family:Caveat,cursive;font-size:32px;font-weight:700;line-height:1.1;">'
        f'Round Reports</div>'
        f'<div style="font-size:12px;color:rgba(255,255,255,.7);margin-top:6px;">'
        f'{len(reports)} round{"s" if len(reports)!=1 else ""} on file'
        f'</div>'
        f'</div>'
        f'{upcoming_section}'
        f'{past_section}'
        f'{empty}'
        f'<div style="margin-top:2rem;padding-top:1rem;border-top:1px solid #e5e3de;'
        f'font-size:10px;color:#aaa;text-align:center;">'
        f'Each report includes course intel, weather, and a digital scorecard.'
        f'</div>'
        f'</body>\n</html>'
    )

    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(html)


# ════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Build a golf prep report')
    parser.add_argument('--course',   required=True, help='Exact course name from courses.json')
    parser.add_argument('--date',     required=True, help='Date YYYY-MM-DD')
    parser.add_argument('--time',     required=True, help='Tee time HH:MM (24h)')
    parser.add_argument('--players',  required=True, nargs='+', help='Player names (Ollie last)')
    parser.add_argument('--output',   required=True, help='Output HTML path')
    args = parser.parse_args()

    build_report(
        course_name = args.course,
        date_str    = args.date,
        time_str    = args.time,
        players     = args.players,
        output_path = args.output
    )
