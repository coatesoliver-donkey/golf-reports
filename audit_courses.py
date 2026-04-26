#!/usr/bin/env python3
"""
audit_courses.py — Inspect courses.json and report problems per course.

No API calls — purely local analysis. Tells you which courses will
produce empty or broken reviews/stops sections in their reports BEFORE
you ever build them.

Usage:
    py audit_courses.py                  # full table for all courses
    py audit_courses.py --problems       # only show courses with problems
    py audit_courses.py --course "Oaks"  # one course only (substring match)

Exit code: 0 if all clean, 1 if any problems found.

What it checks per course:

  meta.lat / meta.lng         missing → can't enrich without coords
  meta.address                missing or generic (e.g., "Ontario, Canada")
  meta.stops                  empty / all same category / contains chains
  meta.reviews                empty / no low-star reviews available
  drive_min_from_kanata       missing
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

COURSES_JSON = Path(__file__).parent / 'courses.json'

# Patterns that suggest a chain or excluded type slipped through
CHAIN_NAME_PATTERN = re.compile(
    r'tim hortons|mcdonald|starbucks|subway|wendy|burger king|kfc|domino|pizza hut|'
    r'a&w|mary brown|second cup|country style|coffee time|'
    # 'dairy queen' deliberately excluded — DQ stays as a valid post-round
    # ice cream stop (especially useful for rural courses with no independents)
    r'walmart|costco|loblaws|metro grocery|sobeys|shoppers drug mart|no frills|'
    r'food basics|farm boy|real canadian superstore|giant tiger|home depot|'
    r'canadian tire|foodland|grocery|convenience|gas bar|esso|petro-canada|shell',
    re.IGNORECASE
)

# Generic addresses that suggest geocoding fell back to a country/region match
GENERIC_ADDRESS_PATTERN = re.compile(
    r'^(Ontario|Quebec|Canada|United States|USA|US),?\s*(Canada|US|USA)?\s*$',
    re.IGNORECASE
)


def audit_course(name: str, course: dict) -> dict:
    """Return a dict of {issue_type: list of problem messages} for this course.
    Empty dict = course is clean."""
    meta = course.get('meta', {}) or {}
    issues = {}

    # ── Coords / address ─────────────────────────────────────────────────
    if not meta.get('lat') or not meta.get('lng'):
        issues.setdefault('coords', []).append('missing meta.lat or meta.lng')

    addr = meta.get('address', '') or ''
    if not addr:
        issues.setdefault('address', []).append('meta.address is empty')
    elif GENERIC_ADDRESS_PATTERN.match(addr.strip()):
        issues.setdefault('address', []).append(
            f"meta.address looks generic — '{addr}' suggests geocoding fell back to a country match"
        )

    # ── Drive time ────────────────────────────────────────────────────
    if not course.get('drive_min_from_kanata'):
        issues.setdefault('drive', []).append('drive_min_from_kanata not set')

    # ── Stops ────────────────────────────────────────────────────────
    stops = meta.get('stops') or []
    stop_names = [s.get('name', '') for s in stops]

    if len(stops) == 0:
        issues.setdefault('stops', []).append('meta.stops is empty (no Post-Round Stops section will render)')
    else:
        # All same category? Informational only — the renderer's color cycling
        # picks 5 different colors regardless of category, so this is purely
        # an FYI about variety, not a hard problem.
        cats = {s.get('badge', '') for s in stops}
        if len(cats) == 1 and len(stops) > 1:
            issues.setdefault('stops_info', []).append(
                f"all {len(stops)} stops are the same category ('{next(iter(cats))}') — colors will cycle but type variety is low"
            )

        # Chain / grocery slipped through?
        chain_hits = [n for n in stop_names if CHAIN_NAME_PATTERN.search(n)]
        if chain_hits:
            issues.setdefault('stops', []).append(
                f"chain/grocery names present: {', '.join(chain_hits)}"
            )

        # Color field present? (script no longer writes it; if old data is there we should re-enrich)
        if any(s.get('color') for s in stops):
            issues.setdefault('stops', []).append(
                "stops still have 'color' field — re-run enrichment with --force to use renderer's color cycling"
            )

    # ── Reviews ─────────────────────────────────────────────────────
    reviews = meta.get('reviews') or []
    if len(reviews) == 0:
        issues.setdefault('reviews', []).append('meta.reviews is empty (no reviews section will render)')
    else:
        stars = [r.get('stars', 5) for r in reviews]
        lowest = min(stars)
        if lowest >= 4:
            # With the new renderer fallback this is OK (will pick lowest available),
            # so this is informational, not a hard error.
            issues.setdefault('reviews_info', []).append(
                f"all reviews are {lowest}+ stars (renderer will pick lowest available, but no comedic 1-star)"
            )

    # ── Google rating / review count ───────────────────────────────
    if 'google_rating' not in meta:
        issues.setdefault('google_data', []).append('meta.google_rating missing')
    if 'google_reviews' not in meta:
        issues.setdefault('google_data', []).append('meta.google_reviews missing')

    return issues


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--course', help='Substring match — audit just this course')
    ap.add_argument('--problems', action='store_true', help='Only show courses with problems')
    ap.add_argument('--courses-json', default=str(COURSES_JSON))
    args = ap.parse_args()

    courses_path = Path(args.courses_json)
    if not courses_path.exists():
        print(f"ERROR: {courses_path} not found.", file=sys.stderr)
        return 1

    with courses_path.open('r', encoding='utf-8') as f:
        courses = json.load(f)

    targets = [(name, c) for name, c in courses.items()
               if not args.course or args.course.lower() in name.lower()]

    if not targets:
        print("No matching courses.")
        return 0

    print(f"\n{'='*78}")
    print(f"Auditing {len(targets)} course(s)")
    print(f"{'='*78}\n")

    clean_count = 0
    problem_count = 0
    info_only_count = 0

    for name, course in targets:
        issues = audit_course(name, course)
        # Separate hard problems from informational ones
        hard_keys = [k for k in issues if not k.endswith('_info')]
        info_keys = [k for k in issues if k.endswith('_info')]
        is_clean = not hard_keys
        is_info_only = not hard_keys and info_keys

        if args.problems and is_clean and not is_info_only:
            continue

        if is_clean and not is_info_only:
            clean_count += 1
            print(f"✓ {name}")
        elif is_info_only:
            info_only_count += 1
            print(f"○ {name}  [info]")
            for k in info_keys:
                for msg in issues[k]:
                    print(f"    - {msg}")
        else:
            problem_count += 1
            print(f"✗ {name}")
            for k in hard_keys:
                for msg in issues[k]:
                    print(f"    {k}: {msg}")
            for k in info_keys:
                for msg in issues[k]:
                    print(f"    {k}: {msg}")
        print()

    print(f"\n{'='*78}")
    print(f"Summary: {clean_count} clean | {info_only_count} info-only | {problem_count} need fixing")
    print(f"{'='*78}\n")

    if problem_count > 0:
        print("To fix problems:")
        print("  - 'no stops' / 'generic address': course was likely geocoded wrong.")
        print("    Re-run: py enrich_courses.py --course \"<name>\" --force")
        print("  - 'chain/grocery names': old enrichment ran before new filters.")
        print("    Re-run with --force to apply current rejection rules.")
        print("  - 'all same category' / 'color field present': re-run with --force.")
        print("  - 'no reviews available': the course has no 1-3 star reviews on Google.")
        print("    The renderer will fall back to lowest available — usually fine.")
        print("    If empty even with fallback, course may not be in Google Places.")

    return 1 if problem_count > 0 else 0


if __name__ == '__main__':
    sys.exit(main())
