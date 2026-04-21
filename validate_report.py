"""
validate_report.py — Run before presenting any report HTML.
Usage: python3 validate_report.py <path>
Exit 0 = valid, Exit 1 = errors found.

Adapts to multi-player reports built by report_builder.py. Player count,
scorecard structure, send-sections, and tabindex ranges are all inferred
from the HTML rather than hardcoded.
"""
import re, sys


def validate_report(path):
    with open(path, encoding='utf-8') as f:
        html = f.read()
    errors = []

    # ── Basic structure ────────────────────────────────────────────────
    if html.count('<body>') != 1:
        errors.append("Body tag count off")
    sc_count = html.count('<script>')
    if sc_count != 1:
        errors.append(f"Script tags: {sc_count} (should be 1)")

    # Div balance (whole doc)
    opens  = len(re.findall(r'<div[\s>]', html))
    closes = html.count('</div>')
    if opens != closes:
        errors.append(f"Div mismatch: {opens}/{closes}")

    # ── Scorecard ──────────────────────────────────────────────────────
    sct = len(re.findall(r'overflow-x:auto;-webkit-overflow-scrolling:touch', html))
    if sct != 2:
        errors.append(f"SC tables: {sct} (should be 2)")

    # Discover players from data-player-row attrs on actual <tr> tags.
    # Restrict the regex to the HTML tag context (preceded by <tr ... ) so it
    # doesn't match JS-string substrings like
    #   document.querySelectorAll('tr[data-player-row="'+name+'"]')
    player_rows = re.findall(r'<tr[^>]*data-player-row="([^"]+)"', html)
    players = []
    for p in player_rows:
        if p not in players:
            players.append(p)
    if not players:
        errors.append("No player rows detected (expected <tr data-player-row=...>)")

    # Inputs — 18 per non-Ollie player; Ollie has 0 (display-only)
    actual_inputs = re.findall(r'<input[^>]+data-player="([^"]+)"', html)
    from collections import Counter
    input_counts = Counter(actual_inputs)
    submitters = [p for p in players if p != 'Ollie']
    expected_total = 18 * len(submitters)
    if sum(input_counts.values()) != expected_total:
        errors.append(f"Score inputs: {sum(input_counts.values())} "
                      f"(expected {expected_total} = 18 x {len(submitters)} submitter{'s' if len(submitters)!=1 else ''})")
    for p in submitters:
        if input_counts.get(p) != 18:
            errors.append(f"Player '{p}': {input_counts.get(p, 0)} inputs (should be 18)")
    if input_counts.get('Ollie', 0) != 0:
        errors.append(f"Ollie has {input_counts['Ollie']} inputs (should be 0 - display-only row)")

    # Tabindex sequence — should be contiguous 1..18*len(submitters), no -1
    tabs = sorted(set(int(x) for x in re.findall(r'tabindex="(-?\d+)"', html)))
    expected_tabs = list(range(1, 18 * len(submitters) + 1))
    if tabs != expected_tabs:
        if len(expected_tabs) < 10:
            errors.append(f"Tabindexes: {tabs} (expected {expected_tabs})")
        else:
            errors.append(f"Tabindexes: {len(tabs)} values (expected contiguous 1..{expected_tabs[-1]})")

    # ── Required anchors ───────────────────────────────────────────────
    for a in ['sec-stats', 'sec-scorecard', 'sec-weather', 'sec-postround',
              'wx-cards-grid', 'wx-callout', 'wx-refresh-btn', 'wx-timestamp']:
        if f'id="{a}"' not in html:
            errors.append(f"Missing: #{a}")

    # Send-scores sections — one per submitter, indexed p0..pN
    for i, p in enumerate(players):
        sid = f'send-scores-section-p{i}'
        is_submitter = p != 'Ollie'
        present = f'id="{sid}"' in html
        if is_submitter and not present:
            errors.append(f"Missing send section for {p} (#{sid})")
        if not is_submitter and present:
            errors.append(f"Unexpected send section for Ollie (#{sid})")

    # Each submitter's send section should be wired to its JS functions
    for i, p in enumerate(players):
        if p == 'Ollie':
            continue
        if f'sendScores({i})' not in html:
            errors.append(f"No sendScores({i}) onclick for {p}")
        if f"selectFeeling({i},'good')" not in html:
            errors.append(f"No selectFeeling({i},'good') for {p}")

    # ── JS sanity ──────────────────────────────────────────────────────
    # Note: BACK_PAR may be chain-declared (e.g. "var FRONT_PAR=[...], BACK_PAR=[...]")
    # so we don't require its own `var` keyword.
    js_required = ['var PLAYERS=', 'FRONT_PAR=', 'BACK_PAR=',
                   'window.sendScores=', 'window.selectFeeling=',
                   'window.refreshWeather=', 'function checkReadyToSend',
                   'function showSentConfirmation']
    for snippet in js_required:
        if snippet not in html:
            errors.append(f"Missing JS: {snippet}")

    # ── Scorecard inner integrity ──────────────────────────────────────
    sc_start = html.find('id="sec-scorecard"')
    sc_end_candidates = [html.find(f'id="send-scores-section-p{i}"') for i in range(len(players))]
    sc_end_candidates = [x for x in sc_end_candidates if x > 0]
    if sc_end_candidates:
        sc_end = min(sc_end_candidates)
    else:
        sc_end = html.find('id="sec-weather"')
    if sc_start > 0 and sc_end > sc_start:
        section = html[sc_start:sc_end]
        so  = len(re.findall(r'<div[\s>]', section)); sc2 = section.count('</div>')
        td_o = section.count('<td');  td_c = section.count('</td>')
        tr_o = section.count('<tr');  tr_c = section.count('</tr>')
        if so  != sc2:   errors.append(f"SC div mismatch: {so}/{sc2}")
        if td_o != td_c: errors.append(f"SC TD mismatch: {td_o}/{td_c}")
        if tr_o != tr_c: errors.append(f"SC TR mismatch: {tr_o}/{tr_c}")
        if section.count('<table') != 2:
            errors.append(f"Tables in SC: {section.count('<table')}")

    # ── Report ─────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"VALIDATION: {path.split('/')[-1]}")
    print(f"{'='*60}")
    print(f"  Size: {len(html):,}  |  Divs: {opens}/{closes}  |  "
          f"Scripts: {sc_count}  |  SC tables: {sct}")
    print(f"  Players: {players}  |  Submitters: {submitters}")
    print(f"  Inputs: {dict(input_counts)}")
    if errors:
        print(f"\n  ERRORS ({len(errors)}):")
        for e in errors:
            print(f"    X {e}")
    else:
        print(f"\n  PASS - All checks passed, safe to present")
    return errors


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else '/mnt/user-data/outputs/2026-04-26_0758_irish-hills.html'
    sys.exit(0 if not validate_report(path) else 1)
