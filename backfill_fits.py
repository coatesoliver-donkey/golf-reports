"""
backfill_fits.py — aggregate Garmin FIT data into courses.json roundData.

Walks a directory of .FIT files, parses each one with fit_parser.parse_fit_round,
filters to sport=golf and sane 18-hole rounds, matches each round to a course in
courses.json by date (using the course's existing scorecard dates), and writes
per-course averages into courses[<name>].roundData. Those fields are what the
report's Walk / Elevation / Avg Round Time boxes read.

Usage:
    py backfill_fits.py --fit-dir <path>            # dry-run preview
    py backfill_fits.py --fit-dir <path> --apply    # write courses.json
    py backfill_fits.py --fit-dir <path> --apply --only "Irish Hills" "Madawaska"

Notes:
  - SCORCRDS-style files (G-prefixed, no session record) are skipped naturally —
    parse_fit_round returns None for them.
  - 9-hole rounds and clear outliers are excluded from the 18-hole averages.
  - Existing roundData for a course is overwritten only when at least one FIT
    matches that course. Courses with no matches are left untouched.
"""

import argparse, json, os, sys
from collections import defaultdict

# parse_fit_round lives in fit_parser.py in the same folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fit_parser import parse_fit_round


# 18-hole sanity bounds — exclude 9-hole rounds, abandoned rounds, GPS-error rounds
MIN_DIST_KM, MAX_DIST_KM = 6.0, 14.0
MIN_TIME_MIN, MAX_TIME_MIN = 150, 360


def smooth(arr, window=5):
    """Centered moving average to dampen GPS altitude jitter."""
    if len(arr) < window:
        return list(arr)
    out, half = [], window // 2
    for i in range(len(arr)):
        lo, hi = max(0, i - half), min(len(arr), i + half + 1)
        out.append(sum(arr[lo:hi]) / (hi - lo))
    return out


def resample(arr, target_n):
    """Linear-interpolate down to target_n evenly-spaced points."""
    if len(arr) <= target_n:
        return list(arr)
    out = []
    for i in range(target_n):
        f = i * (len(arr) - 1) / (target_n - 1)
        lo = int(f); hi = min(lo + 1, len(arr) - 1)
        frac = f - lo
        out.append(arr[lo] * (1 - frac) + arr[hi] * frac)
    return out


def round_metrics(parsed):
    """Compute one round's walk/time/altitude metrics from parse_fit_round() output.
    Returns None if the round fails 18-hole sanity bounds or lacks altitude data."""
    if not parsed or parsed.get('sport') != 'golf':
        return None
    dist_m = parsed.get('total_distance_m') or 0
    time_s = parsed.get('total_elapsed_s') or 0
    dist_km = dist_m / 1000.0
    time_min = time_s / 60.0

    if not (MIN_DIST_KM <= dist_km <= MAX_DIST_KM): return None
    if not (MIN_TIME_MIN <= time_min <= MAX_TIME_MIN): return None

    alts = [r['alt_m'] for r in parsed.get('records', []) if r.get('alt_m') is not None]
    if len(alts) < 10:
        # Distance/time only — no altitude
        return {
            'distKm': dist_km, 'timeMin': time_min,
            'smoothAscentM': None, 'altSpanM': None,
            'altMinM': None, 'altMaxM': None, 'curve': None,
        }

    sm = smooth(alts, window=5)
    alt_min, alt_max = min(sm), max(sm)
    ascent = sum(max(0.0, sm[i] - sm[i-1]) for i in range(1, len(sm)))
    curve = [round(v, 1) for v in resample(sm, 48)]

    return {
        'distKm': dist_km, 'timeMin': time_min,
        'smoothAscentM': ascent, 'altSpanM': alt_max - alt_min,
        'altMinM': alt_min, 'altMaxM': alt_max, 'curve': curve,
    }


def aggregate(rounds):
    """Average a list of round_metrics() dicts into a roundData block."""
    if not rounds:
        return None
    n = len(rounds)
    avg = lambda key: sum(r[key] for r in rounds if r.get(key) is not None) / max(1, sum(1 for r in rounds if r.get(key) is not None))
    n_alt = sum(1 for r in rounds if r.get('curve'))

    # Element-wise average of curves (only rounds that have a curve)
    mean_curve = None
    if n_alt:
        curves = [r['curve'] for r in rounds if r.get('curve')]
        length = min(len(c) for c in curves)
        mean_curve = [round(sum(c[i] for c in curves) / len(curves), 1) for i in range(length)]

    return {
        'avgDistKm':        round(avg('distKm'), 2),
        'avgTimeMin':       round(avg('timeMin'), 1),
        'avgSmoothAscentM': round(avg('smoothAscentM'), 1) if n_alt else None,
        'avgAltSpanM':      round(avg('altSpanM'), 1) if n_alt else None,
        'altMinM':          round(avg('altMinM'), 1) if n_alt else None,
        'altMaxM':          round(avg('altMaxM'), 1) if n_alt else None,
        'meanAltCurve':     mean_curve,
        'nDistSamples':     n,
        'nTimeSamples':     n,
    }


def load_courses(path):
    """Tolerant read — survives legacy cp1252 corruption."""
    raw = open(path, 'rb').read()
    try:
        return json.loads(raw.decode('utf-8'))
    except UnicodeDecodeError:
        return json.loads(raw.decode('cp1252'))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fit-dir', required=True, help='Directory containing .FIT files (recursive)')
    ap.add_argument('--courses', default='courses.json', help='Path to courses.json (default: ./courses.json)')
    ap.add_argument('--apply', action='store_true', help='Write changes (default: dry-run preview)')
    ap.add_argument('--only', nargs='*', metavar='COURSE', help='Substring-match: only process matching courses')
    args = ap.parse_args()

    if not os.path.isdir(args.fit_dir):
        sys.exit(f"--fit-dir not found: {args.fit_dir}")
    if not os.path.exists(args.courses):
        sys.exit(f"--courses not found: {args.courses}")

    courses = load_courses(args.courses)

    # 1. Walk fit-dir
    fits = []
    for dp, _, files in os.walk(args.fit_dir):
        for f in files:
            if f.lower().endswith('.fit'):
                fits.append(os.path.join(dp, f))
    print(f"  Found {len(fits)} .fit file(s) under {args.fit_dir}")

    # 2. Parse each, bucket by date
    by_date = defaultdict(list)  # 'YYYY-MM-DD' -> [(path, metrics)]
    parsed_count = 0; skipped = {'not_golf': 0, 'no_session': 0, 'out_of_bounds': 0, 'no_date': 0}
    for fp in fits:
        try:
            r = parse_fit_round(fp, alt_sample_every=10)
        except Exception as e:
            print(f"  [warn] {os.path.basename(fp)}: parse error: {e}")
            continue
        if not r:
            skipped['no_session'] += 1; continue
        if r.get('sport') != 'golf':
            skipped['not_golf'] += 1; continue
        m = round_metrics(r)
        if not m:
            skipped['out_of_bounds'] += 1; continue
        if not r.get('start_time'):
            skipped['no_date'] += 1; continue
        date_str = r['start_time'].strftime('%Y-%m-%d')
        by_date[date_str].append((fp, m))
        parsed_count += 1
    print(f"  Parsed {parsed_count} valid golf round(s); skipped: {skipped}")

    # 3. For each course, find matching rounds by scorecard date
    selected = lambda cname: (not args.only) or any(s.lower() in cname.lower() for s in args.only)
    course_rounds = defaultdict(list)
    matched_dates_per_course = defaultdict(list)
    for cname, c in courses.items():
        if not (isinstance(c, dict) and 'par' in c): continue
        if not selected(cname): continue
        for sc in (c.get('scorecards') or []):
            date_str = sc.get('date')
            if date_str in by_date:
                for fp, m in by_date[date_str]:
                    course_rounds[cname].append(m)
                    matched_dates_per_course[cname].append(date_str)

    # 4. Aggregate + diff
    print(f"\n  {'Course':<40} {'Rounds':>7} {'Walk':>8} {'Time':>7} {'Ascent':>8}")
    print('  ' + '-' * 76)
    updates = {}
    for cname in sorted(course_rounds):
        rounds = course_rounds[cname]
        rd = aggregate(rounds)
        prior = courses[cname].get('roundData') or {}
        prior_n = prior.get('nDistSamples')
        prior_walk = prior.get('avgDistKm')
        new_walk = rd['avgDistKm']
        delta = f" (was {prior_walk:.2f})" if prior_walk and abs(prior_walk - new_walk) > 0.01 else ''
        print(f"  {cname[:40]:<40} {len(rounds):>5}    {rd['avgDistKm']:>5.2f}km {rd['avgTimeMin']:>5.0f}min "
              f"{(rd['avgSmoothAscentM'] or 0):>5.0f}m{delta}")
        updates[cname] = rd

    if not updates:
        print("\n  No matches. Either the FIT directory is empty of dated golf rounds, or none "
              "of the rounds fall on a date that appears in any course's scorecards.")
        return

    print(f"\n  {len(updates)} course(s) would be updated.")

    if args.apply:
        for cname, rd in updates.items():
            courses[cname]['roundData'] = rd
            # Clean up stale top-level fields from an earlier (mis-wired) backfill
            for stale in ('walk_km', 'avg_time', 'walk_km_rounds'):
                courses[cname].pop(stale, None)
        with open(args.courses, 'w', encoding='utf-8') as f:
            json.dump(courses, f, indent=2, ensure_ascii=False)
        print(f"  Wrote {args.courses}")
    else:
        print("  Dry-run only. Add --apply to write courses.json.")


if __name__ == '__main__':
    main()
