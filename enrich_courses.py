#!/usr/bin/env python3
"""
enrich_courses.py — Populate Google Places data for every course in courses.json.

For each course missing meta data, this script:
  1. Geocodes the course name to lat/lng + address               (Geocoding API)
  2. Calculates drive time from Kanata                          (Distance Matrix)
  3. Fetches the course's own Google rating + 6 reviews         (Places API new)
  4. Finds top 5 sweet/snack stops within ~18 min of the course (Places API new)
  5. Validates each stop's actual drive time                    (Distance Matrix)
  6. Writes everything to course['meta'] in the exact shape that
     report_builder.py's build_stops() and build_reviews() expect.

Usage:
    py enrich_courses.py                      # enrich every course missing meta
    py enrich_courses.py --course "Edelweiss" # enrich one specific course (substring)
    py enrich_courses.py --force              # re-enrich even already-populated courses
    py enrich_courses.py --dry-run            # don't write changes
    py enrich_courses.py --verbose            # print every API call

Requires:
    - GOOGLE_MAPS_API_KEY in .env (or environment variable)
    - APIs enabled on the key: Places API (New), Distance Matrix API,
      Geocoding API
    - Python packages: pip install requests python-dotenv

Output written to course['meta']:
    address          (str)
    lat, lng         (float)
    google_rating    (float)
    google_reviews   (int)
    reviews          (list of {stars, text, source})
    stops            (list of {icon, name, addr, dist, desc, rating, badge,
                               hours, color})
And to course (top level):
    drive_min_from_kanata (int)

================================================================================
COST ESTIMATE (Google Maps Platform free tier: $200/month covers all of this):
  Per course (one-time enrich):
    1  Geocoding API call           ($0.005)
    1  Distance Matrix call         ($0.005) — course → Kanata
    1  Text Search call             ($0.032) — find course's own place_id
    1  Place Details call           ($0.017) — course's reviews + rating
    6  Nearby Search calls          ($0.192) — categorical (bakery, ice cream...)
    7  Text Search calls            ($0.224) — chip wagons, butter tarts, etc.
    1  Distance Matrix call         ($0.005) — batch validate ~10 stops
    ────────────────────────────────────────
    Total per course:               ~$0.48
    All 19 unenriched courses:      ~$9.12 one-time
================================================================================
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

# ── Optional deps with graceful fallback ──────────────────────────────────
try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("WARN: 'python-dotenv' not installed; relying on shell env vars.", file=sys.stderr)
    print("      To install: pip install python-dotenv", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════

API_KEY = os.environ.get('GOOGLE_MAPS_API_KEY', '').strip()
COURSES_JSON = Path(__file__).parent / 'courses.json'

# Kanata centroid — "home base" for drive-time calculations.
# Matches the existing project convention.
KANATA_LAT = 45.30
KANATA_LNG = -75.90

# Stops design constants
STOPS_PER_COURSE     = 5      # top N by score
SEARCH_RADIUS_M      = 18000  # 18km — about 15 min drive in Ontario
MAX_DRIVE_MIN        = 18     # drop anything farther by actual drive time
MAX_DRIVE_MIN_FAR    = 25     # for distant courses (>60min from Kanata) — relax

# Category weighting (higher = preferred). Multiplied into rating-based score.
CATEGORY_WEIGHTS = {
    'bakery':       1.5,    # bread, pastry, donuts
    'ice_cream':    1.4,    # ice cream parlors, gelato, frozen yogurt
    'donut':        1.4,    # specifically donut shops
    'chip_wagon':   1.3,    # poutine/fries trucks (Ontario specialty)
    'dessert':      1.3,    # general dessert restaurants
    'candy':        1.2,    # candy stores, sweet shops, confectionery
    'snack':        1.1,    # general snack/light food
    'cafe':         0.8,    # coffee + pastry shops; some sweet items
    'other':        0.0,    # excluded — full restaurants, bars, etc.
}

# Google Place "types" → our categories.
PLACE_TYPE_MAP = {
    'bakery': 'bakery',
    'ice_cream_shop': 'ice_cream',
    'donut_shop': 'donut',
    'candy_store': 'candy',
    'chocolate_shop': 'candy',
    'confectionery': 'candy',
    'dessert_restaurant': 'dessert',
    'dessert_shop': 'dessert',
    'cafe': 'cafe',
    'coffee_shop': 'cafe',
    'food_court': 'snack',
}

# Text search queries — catch the stuff Google's categories miss.
EXTRA_TEXT_QUERIES = [
    'chip wagon',
    'chip stand',
    'poutine truck',
    'french fry stand',
    'ice cream stand',
    'farm market bakery',
    'butter tarts',     # Ontario classic
]

# Things that get auto-rejected even if they appear.
EXCLUDED_TYPES = {
    'restaurant', 'meal_takeaway', 'meal_delivery',
    'bar', 'pub', 'night_club',
    'gas_station', 'convenience_store',
    'supermarket', 'grocery_or_supermarket', 'grocery_store',
    'department_store', 'shopping_mall',
    'lodging', 'hotel',
    'park', 'tourist_attraction',
    'pharmacy',
}

# Names that suggest chains — slight rank penalty (we want local picks).
CHAIN_NAME_HINTS = [
    "tim hortons", "mcdonald", "subway", "burger king", "wendy",
    "starbucks", "second cup", "country style", "coffee time",
    "a&w", "kfc", "pizza hut", "domino",
    # Note: Dairy Queen NOT included — DQ is iconic for ice cream, leave neutral
]

# Renderer display map — maps each category to {icon, badge, color}
# matching what build_stops() in report_builder.py expects to render.
CATEGORY_DISPLAY = {
    'bakery':     {'icon': '🥐', 'badge': 'Bakery',     'color': '#d4860a'},
    'ice_cream':  {'icon': '🍦', 'badge': 'Ice Cream',  'color': '#e05a8a'},
    'donut':      {'icon': '🍩', 'badge': 'Donuts',     'color': '#c4621a'},
    'chip_wagon': {'icon': '🍟', 'badge': 'Chip Wagon', 'color': '#c4401a'},
    'dessert':    {'icon': '🍰', 'badge': 'Dessert',    'color': '#b8408a'},
    'candy':      {'icon': '🍬', 'badge': 'Sweets',     'color': '#9a3aa8'},
    'snack':      {'icon': '🍿', 'badge': 'Snack',      'color': '#b87a3a'},
    'cafe':       {'icon': '☕', 'badge': 'Café',       'color': '#2a8a6a'},
    'other':      {'icon': '🍴', 'badge': 'Stop',       'color': '#7a5cc4'},
}

# Endpoints
PLACES_BASE         = 'https://places.googleapis.com/v1'
URL_GEOCODE         = 'https://maps.googleapis.com/maps/api/geocode/json'
URL_DISTANCE_MATRIX = 'https://maps.googleapis.com/maps/api/distancematrix/json'

# Places API field mask for nearby/text search — only request what we use.
PLACE_FIELDS = ','.join([
    'places.id',
    'places.displayName',
    'places.formattedAddress',
    'places.location',
    'places.types',
    'places.primaryType',
    'places.rating',
    'places.userRatingCount',
    'places.businessStatus',
    'places.regularOpeningHours.weekdayDescriptions',
    'places.googleMapsUri',
])

# Field mask for Place Details (fetching course's own reviews + summary).
DETAIL_FIELDS = ','.join([
    'id',
    'displayName',
    'formattedAddress',
    'location',
    'rating',
    'userRatingCount',
    'reviews',
    'editorialSummary',
])


# ══════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp/2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def classify_place(place: dict) -> str:
    """Map a Google place to one of our categories. 'other' = exclude.

    Hard rule: if ANY excluded type is present, REJECT — even if a wanted type
    is also present. This kills big-box stores (Costco bakeries, Walmart Tim
    Hortons, supermarket coffee counters, etc.) that Google sometimes tags
    with both their parent type AND a food/drink subtype."""
    types = set(place.get('types', []) or [])
    primary = place.get('primaryType', '')
    if primary:
        types.add(primary)

    # HARD REJECT — if it's any kind of big-box / non-standalone-shop type,
    # excluded types win regardless of what other tags it has
    if types & EXCLUDED_TYPES:
        return 'other'

    wanted_hits = [PLACE_TYPE_MAP[t] for t in types if t in PLACE_TYPE_MAP]
    if wanted_hits:
        return max(wanted_hits, key=lambda c: CATEGORY_WEIGHTS.get(c, 0))

    # Heuristics from the name
    name = (place.get('displayName', {}).get('text', '') or '').lower()

    # Reject by name too — chains and big-box that snuck through without type tags
    NAME_REJECT = [
        # Big-box stores & supermarkets
        'walmart', 'costco', 'loblaws', 'metro grocery', 'sobeys',
        'shoppers drug mart', 'no frills', 'food basics', 'farm boy',
        'real canadian superstore', 'giant tiger', 'home depot', 'canadian tire',
        'foodland', 'independent grocer', 'your independent grocer',
        # Generic grocery / convenience patterns
        'grocery', 'convenience', 'gas bar', 'esso', 'petro-canada', 'shell',
        # Coffee/donut chains (so common they're not a 'stop' worth promoting)
        'tim hortons', "tim's", 'starbucks', 'second cup', 'country style',
        'mcdonald', 'a&w', 'wendy', 'burger king', 'subway', 'kfc',
        'pizza hut', 'domino', 'mary brown',
    ]
    if any(n in name for n in NAME_REJECT):
        return 'other'

    if any(k in name for k in ['chip', 'poutine', 'fries', 'french fry']):
        if 'restaurant' in types or 'meal_takeaway' in types:
            if any(k in name for k in ['wagon', 'stand', 'truck', 'shack']):
                return 'chip_wagon'
        else:
            return 'chip_wagon'
    if any(k in name for k in ['bakery', 'butter tart', 'pastry']):
        return 'bakery'
    if 'ice cream' in name or 'gelato' in name:
        return 'ice_cream'
    if 'donut' in name or 'doughnut' in name:
        return 'donut'

    return 'other'


def score_place(place: dict, category: str, distance_km: float) -> float:
    """Higher = better. rating × log(reviews) × cat_weight × distance_decay × chain."""
    rating = place.get('rating', 0) or 0
    reviews = place.get('userRatingCount', 0) or 0

    if rating < 3.5:
        return 0.0
    if reviews < 5:
        return 0.0

    cat_weight = CATEGORY_WEIGHTS.get(category, 0)
    if cat_weight == 0:
        return 0.0

    review_factor = math.log10(reviews + 1) + 1
    distance_factor = max(0.4, 1 - (distance_km / 30))
    name = (place.get('displayName', {}).get('text', '') or '').lower()
    chain_factor = 0.7 if any(c in name for c in CHAIN_NAME_HINTS) else 1.0

    return rating * review_factor * cat_weight * distance_factor * chain_factor


def format_dist_label(drive_min: float) -> str:
    """Human-friendly: '2 min from the course' / '15 min from the course'."""
    drive_min = round(drive_min)
    if drive_min < 1:
        return 'right at the course'
    if drive_min == 1:
        return '1 min from the course'
    return f'{drive_min} min from the course'


def format_hours(weekday_descriptions: list[str] | None) -> str:
    """Convert Google's per-day hours array into a short human label."""
    if not weekday_descriptions:
        return 'Hours unavailable'
    # Detect "every day same hours" → condense to "Daily ..."
    all_hours = [d.split(': ', 1)[-1] for d in weekday_descriptions]
    if all(h == all_hours[0] for h in all_hours) and all_hours[0] != 'Closed':
        return f'Daily {all_hours[0]}'
    # Otherwise show first 2 days
    label = ' · '.join(weekday_descriptions[:2])
    if len(weekday_descriptions) > 2:
        label += '…'
    return label


def shape_stop_for_renderer(place: dict, category: str, drive_min: float) -> dict:
    """Produce the {icon, name, addr, dist, desc, rating, badge, hours} dict
    that report_builder.py's build_stops() helper expects.

    NOTE: We intentionally do NOT set 'color' — the renderer cycles its own
    5-color palette across stops so consecutive cards visually differ even
    when they share a category (e.g., 5 cafés get 5 different colors)."""
    display = CATEGORY_DISPLAY.get(category, CATEGORY_DISPLAY['other'])
    name = (place.get('displayName', {}) or {}).get('text', '') or 'Unknown'
    addr_full = place.get('formattedAddress', '') or ''
    # Strip ", ON KxX yYy, Canada" suffix for cleaner display
    addr = addr_full.split(', ON')[0]
    hours = format_hours((place.get('regularOpeningHours') or {}).get('weekdayDescriptions'))

    return {
        'icon':   display['icon'],
        'name':   name,
        'addr':   addr,
        'dist':   format_dist_label(drive_min),
        'desc':   '',  # Places API doesn't reliably give descriptions; left blank for human polish later
        'rating': round(place.get('rating', 0), 1),
        'badge':  display['badge'],
        'hours':  hours,
        # 'color' deliberately omitted — see docstring above
    }


def shape_review_for_renderer(google_review: dict) -> dict:
    """Produce {stars, text, source} dict that build_reviews() expects."""
    rating = google_review.get('rating', 3)
    text = (google_review.get('text') or {}).get('text', '') or \
           (google_review.get('originalText') or {}).get('text', '')
    if len(text) > 280:
        text = text[:277] + '…'
    return {
        'stars': rating,
        'text':  text,
        'source': 'Google Reviews',
    }


# ══════════════════════════════════════════════════════════════════════════
# GOOGLE API CALLS
# ══════════════════════════════════════════════════════════════════════════

def geocode(query: str, verbose: bool = False) -> dict | None:
    """Geocoding API → {lat, lng, formatted_address} or None on failure.

    Also returns None if the result looks too generic (country/region only),
    so the caller can try a different query strategy."""
    params = {
        'address': query,
        'key': API_KEY,
        'region': 'ca',
        # Bias toward Ottawa region so a course name doesn't match somewhere far.
        'bounds': '44.5,-77.0|46.0,-74.5',
    }
    if verbose:
        print(f"      [api] geocode '{query}'")
    r = requests.get(URL_GEOCODE, params=params, timeout=15)
    if r.status_code != 200:
        print(f"      [api] geocode HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return None
    data = r.json()
    if data.get('status') != 'OK' or not data.get('results'):
        return None
    result = data['results'][0]

    # Reject country/region-only results — these are signs Google fell back
    # because the query was too vague (e.g., 'Canadian Golf' matching all of
    # Canada). Useful results have specific types like establishment, premise,
    # street_address. Pure country/admin-area matches are useless.
    types = set(result.get('types', []))
    GENERIC_TYPES = {'country', 'administrative_area_level_1',
                     'administrative_area_level_2', 'political'}
    SPECIFIC_TYPES = {'establishment', 'street_address', 'premise',
                      'subpremise', 'route', 'point_of_interest'}
    if types & GENERIC_TYPES and not (types & SPECIFIC_TYPES):
        if verbose:
            print(f"      [api] rejected — too generic: '{result.get('formatted_address','')}' types={types}")
        return None

    loc = result['geometry']['location']
    return {
        'lat': round(loc['lat'], 6),
        'lng': round(loc['lng'], 6),
        'formatted_address': result['formatted_address'],
    }


def find_place_id(query: str, near_lat: float | None = None,
                   near_lng: float | None = None, verbose: bool = False) -> str | None:
    """Places API (new) Text Search → first matching place_id."""
    body: dict[str, Any] = {
        'textQuery': query,
        'maxResultCount': 1,
    }
    if near_lat and near_lng:
        body['locationBias'] = {
            'circle': {
                'center': {'latitude': near_lat, 'longitude': near_lng},
                'radius': 10000,
            }
        }
    headers = {
        'Content-Type': 'application/json',
        'X-Goog-Api-Key': API_KEY,
        'X-Goog-FieldMask': 'places.id,places.displayName',
    }
    if verbose:
        print(f"      [api] find_place_id '{query}'")
    r = requests.post(f'{PLACES_BASE}/places:searchText', headers=headers, json=body, timeout=15)
    if r.status_code != 200:
        print(f"      [api] find_place_id HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return None
    places = r.json().get('places', [])
    return places[0]['id'] if places else None


def place_details(place_id: str, verbose: bool = False) -> dict | None:
    """Places API (new) → full details for one place (used for course's own reviews)."""
    headers = {
        'X-Goog-Api-Key': API_KEY,
        'X-Goog-FieldMask': DETAIL_FIELDS,
    }
    if verbose:
        print(f"      [api] place_details {place_id}")
    r = requests.get(f'{PLACES_BASE}/places/{place_id}', headers=headers, timeout=15)
    if r.status_code != 200:
        print(f"      [api] place_details HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return None
    return r.json()


def places_nearby(lat: float, lng: float, included_types: list[str] | None = None,
                  max_results: int = 20, verbose: bool = False) -> list[dict]:
    """Places API (new) Nearby Search."""
    body: dict[str, Any] = {
        'maxResultCount': min(max_results, 20),
        'locationRestriction': {
            'circle': {
                'center': {'latitude': lat, 'longitude': lng},
                'radius': SEARCH_RADIUS_M,
            }
        },
    }
    if included_types:
        body['includedTypes'] = included_types
    headers = {
        'Content-Type': 'application/json',
        'X-Goog-Api-Key': API_KEY,
        'X-Goog-FieldMask': PLACE_FIELDS,
    }
    if verbose:
        print(f"      [api] nearby types={included_types}")
    r = requests.post(f'{PLACES_BASE}/places:searchNearby', headers=headers, json=body, timeout=15)
    if r.status_code != 200:
        print(f"      [api] nearby HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return []
    return r.json().get('places', []) or []


def places_text(query: str, lat: float, lng: float, max_results: int = 10,
                verbose: bool = False) -> list[dict]:
    """Places API (new) Text Search, biased to a location."""
    body = {
        'textQuery': query,
        'maxResultCount': max_results,
        'locationBias': {
            'circle': {
                'center': {'latitude': lat, 'longitude': lng},
                'radius': SEARCH_RADIUS_M,
            }
        },
    }
    headers = {
        'Content-Type': 'application/json',
        'X-Goog-Api-Key': API_KEY,
        'X-Goog-FieldMask': PLACE_FIELDS,
    }
    if verbose:
        print(f"      [api] text '{query}'")
    r = requests.post(f'{PLACES_BASE}/places:searchText', headers=headers, json=body, timeout=15)
    if r.status_code != 200:
        print(f"      [api] text HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return []
    return r.json().get('places', []) or []


def distance_matrix(origin_lat: float, origin_lng: float,
                     dest_pairs: list[tuple[float, float]],
                     verbose: bool = False) -> list[float | None]:
    """Distance Matrix → drive minutes per destination. Up to 25 dests per call."""
    if not dest_pairs:
        return []
    origins = f"{origin_lat},{origin_lng}"
    destinations = '|'.join(f"{lat},{lng}" for lat, lng in dest_pairs)
    params = {
        'origins': origins,
        'destinations': destinations,
        'mode': 'driving',
        'units': 'metric',
        'key': API_KEY,
    }
    if verbose:
        print(f"      [api] distance_matrix × {len(dest_pairs)}")
    r = requests.get(URL_DISTANCE_MATRIX, params=params, timeout=15)
    if r.status_code != 200:
        print(f"      [api] distance_matrix HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return [None] * len(dest_pairs)
    data = r.json()
    rows = data.get('rows', [])
    if not rows:
        return [None] * len(dest_pairs)
    elements = rows[0].get('elements', [])
    minutes_list: list[float | None] = []
    for el in elements:
        if el.get('status') == 'OK' and 'duration' in el:
            minutes_list.append(el['duration']['value'] / 60.0)
        else:
            minutes_list.append(None)
    return minutes_list


# ══════════════════════════════════════════════════════════════════════════
# CORE WORKFLOW
# ══════════════════════════════════════════════════════════════════════════

def find_stops_for_course(course_name: str, lat: float, lng: float,
                           max_drive: int, verbose: bool = False) -> list[dict]:
    """Categorical + text search → classify → score → drive-time validate → top N."""
    raw_places: dict[str, dict] = {}

    # Categorical searches — one per type group
    type_searches = [
        ['bakery'],
        ['ice_cream_shop'],
        ['donut_shop'],
        ['candy_store', 'chocolate_shop', 'confectionery'],
        ['dessert_restaurant', 'dessert_shop'],
        ['cafe', 'coffee_shop'],
    ]
    for types in type_searches:
        for p in places_nearby(lat, lng, included_types=types, verbose=verbose):
            pid = p.get('id')
            if pid and pid not in raw_places:
                raw_places[pid] = p
        time.sleep(0.1)

    # Text searches for chip wagons / butter tarts / etc.
    for q in EXTRA_TEXT_QUERIES:
        for p in places_text(q, lat, lng, verbose=verbose):
            pid = p.get('id')
            if pid and pid not in raw_places:
                raw_places[pid] = p
        time.sleep(0.1)

    print(f"      Found {len(raw_places)} unique places before filtering")

    # Classify, filter, score
    candidates = []
    for p in raw_places.values():
        if p.get('businessStatus') == 'CLOSED_PERMANENTLY':
            continue
        category = classify_place(p)
        if category == 'other':
            continue
        loc = p.get('location') or {}
        plat, plng = loc.get('latitude'), loc.get('longitude')
        if plat is None or plng is None:
            continue
        dist_km = haversine_km(lat, lng, plat, plng)
        score = score_place(p, category, dist_km)
        if score == 0:
            continue
        candidates.append({
            '_place': p,
            '_category': category,
            '_distance_km': dist_km,
            '_score': score,
            '_lat': plat,
            '_lng': plng,
        })

    if not candidates:
        return []

    candidates.sort(key=lambda c: -c['_score'])
    candidates = candidates[:STOPS_PER_COURSE * 2]   # 2x headroom for drive-time culls
    print(f"      {len(candidates)} candidates after categorical filtering")

    # Drive-time validate (batched in one Distance Matrix call)
    dest_pairs = [(c['_lat'], c['_lng']) for c in candidates]
    drive_times = distance_matrix(lat, lng, dest_pairs, verbose=verbose)

    finalists = []
    for c, dm in zip(candidates, drive_times):
        if dm is None or dm > max_drive:
            continue
        c['_drive_min'] = dm
        finalists.append(c)

    finalists.sort(key=lambda c: -c['_score'])
    finalists = finalists[:STOPS_PER_COURSE]
    print(f"      {len(finalists)} stops after drive-time validation (≤ {max_drive} min)")

    return [shape_stop_for_renderer(c['_place'], c['_category'], c['_drive_min'])
            for c in finalists]


def enrich_course(name: str, course: dict, verbose: bool = False) -> bool:
    """Full pipeline for one course. Mutates course in place. Returns True on success."""
    meta = course.setdefault('meta', {})

    # ── STEP 1: Geocode (if no coords, or address looks generic) ──────
    addr_now = (meta.get('address') or '').strip()
    looks_generic = addr_now.lower() in ('ontario, canada', 'canada', 'quebec, canada', '')
    needs_geocode = (
        not (meta.get('lat') and meta.get('lng'))
        or looks_generic
    )
    if needs_geocode:
        if looks_generic and meta.get('lat'):
            print(f"  [1/5] Re-geocoding (existing address '{addr_now}' looks generic)...")
            # Clear old bad coords so we don't keep them on failure
            meta.pop('lat', None)
            meta.pop('lng', None)
        else:
            print(f"  [1/5] Geocoding...")
        # Try multiple query strategies — some course names are ambiguous
        # (e.g. "Canadian Golf & Country Club" matches all of Canada with
        # the wrong query). Stop on first non-generic result.
        candidates = [
            f"{name}",                            # name as-is, sometimes specific enough
            f"{name} Ottawa Ontario",             # bias toward Ottawa region directly
            f"{name} golf course Canada",         # original strategy
            f"{name} Ontario Canada",             # broader Ontario hint
        ]
        geo = None
        for q in candidates:
            geo = geocode(q, verbose=verbose)
            if geo:
                if verbose:
                    print(f"      [geocode] strategy '{q}' succeeded")
                break

        # Last-resort fallback: Places Text Search (returns place_id of best match)
        if not geo:
            print(f"    [geocode] all geocode strategies failed, trying Places Text Search…")
            place_id = find_place_id(f"{name} golf course",
                                      KANATA_LAT, KANATA_LNG, verbose=verbose)
            if place_id:
                details = place_details(place_id, verbose=verbose)
                if details and 'location' in details:
                    loc = details['location']
                    geo = {
                        'lat': round(loc['latitude'], 6),
                        'lng': round(loc['longitude'], 6),
                        'formatted_address': details.get('formattedAddress', ''),
                    }

        if not geo:
            print(f"    ⚠ All geocoding strategies failed — skipping this course.")
            return False
        meta['lat'] = geo['lat']
        meta['lng'] = geo['lng']
        if not meta.get('address') or meta.get('address') in ('Ontario, Canada', 'Canada'):
            meta['address'] = geo['formatted_address']
        print(f"    → {geo['lat']}, {geo['lng']}  |  {geo['formatted_address']}")
    else:
        print(f"  [1/5] Geocode: already have coords ({meta['lat']}, {meta['lng']})")

    # ── STEP 2: Drive time from Kanata ─────────────────────────────────
    if not course.get('drive_min_from_kanata'):
        print(f"  [2/5] Drive time from Kanata...")
        dt_list = distance_matrix(KANATA_LAT, KANATA_LNG,
                                   [(meta['lat'], meta['lng'])], verbose=verbose)
        dt = dt_list[0] if dt_list else None
        if dt is not None:
            course['drive_min_from_kanata'] = round(dt)
            print(f"    → {round(dt)} min")
    else:
        print(f"  [2/5] Drive: already have ({course['drive_min_from_kanata']} min)")

    # Decide tolerance based on how far the course is
    home_drive = course.get('drive_min_from_kanata', 30)
    max_drive = MAX_DRIVE_MIN_FAR if home_drive > 60 else MAX_DRIVE_MIN

    # ── STEP 3: Course's own Google reviews + rating ──────────────────
    print(f"  [3/5] Fetching course's own Google reviews...")
    place_id = find_place_id(name, meta['lat'], meta['lng'], verbose=verbose)
    if place_id:
        details = place_details(place_id, verbose=verbose)
        if details:
            if 'rating' in details:
                meta['google_rating'] = round(details['rating'], 1)
            if 'userRatingCount' in details:
                meta['google_reviews'] = details['userRatingCount']
            # Sort by rating ASCENDING so the lowest-star (funniest/saltiest)
            # reviews land at the top. The report's build_reviews() prefers
            # 1-star reviews — keep them all here so it has the widest pool.
            reviews_raw = sorted(
                details.get('reviews', []),
                key=lambda r: r.get('rating', 5)
            )
            if reviews_raw:
                meta['reviews'] = [shape_review_for_renderer(r) for r in reviews_raw]
                star_breakdown = {}
                for r in meta['reviews']:
                    s = r['stars']
                    star_breakdown[s] = star_breakdown.get(s, 0) + 1
                breakdown_str = ', '.join(f'{c}×{s}★' for s, c in sorted(star_breakdown.items()))
                print(f"    → rating {meta.get('google_rating')}, "
                      f"{meta.get('google_reviews')} total reviews, "
                      f"{len(meta['reviews'])} snippets saved ({breakdown_str})")
            # Save editorial summary as fun_fact if we don't have one
            if not meta.get('fun_fact'):
                summary = (details.get('editorialSummary') or {}).get('text', '')
                if summary:
                    meta['fun_fact'] = summary
        else:
            print(f"    ⚠ Place details fetch failed.")
    else:
        print(f"    ⚠ Could not find place_id for course.")

    # ── STEP 4 & 5: Find + validate stops ─────────────────────────────
    print(f"  [4/5] Finding nearby food/drink stops (≤ {max_drive} min drive)...")
    stops = find_stops_for_course(name, meta['lat'], meta['lng'],
                                   max_drive=max_drive, verbose=verbose)
    meta['stops'] = stops
    print(f"  [5/5] → {len(stops)} stops written to meta.stops")
    for s in stops:
        print(f"        · {s['icon']} {s['name']} ({s['rating']}★, {s['dist']})")

    return True


# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--course', help='Substring match — enrich just this course')
    ap.add_argument('--dry-run', action='store_true',
                    help="Print what would happen but don't write courses.json")
    ap.add_argument('--force', action='store_true',
                    help='Re-enrich courses that already have meta.stops')
    ap.add_argument('--verbose', action='store_true',
                    help='Print every API call')
    ap.add_argument('--courses-json', default=str(COURSES_JSON),
                    help='Path to courses.json (default: ./courses.json)')
    args = ap.parse_args()

    if not API_KEY:
        print("ERROR: GOOGLE_MAPS_API_KEY not set. Add to .env or env vars.", file=sys.stderr)
        return 1

    courses_path = Path(args.courses_json)
    if not courses_path.exists():
        print(f"ERROR: {courses_path} not found.", file=sys.stderr)
        return 1

    with courses_path.open('r', encoding='utf-8') as f:
        courses = json.load(f)

    # Filter to one course if requested
    targets: list[tuple[str, dict]] = []
    for name, course in courses.items():
        if args.course and args.course.lower() not in name.lower():
            continue
        already_enriched = bool(course.get('meta', {}).get('stops'))
        if already_enriched and not args.force:
            print(f"  ⏭  {name} — already enriched (use --force to redo)")
            continue
        targets.append((name, course))

    if not targets:
        print("\nNothing to enrich. Use --force to re-enrich already-populated courses.")
        return 0

    print(f"\n{'='*72}")
    print(f"Enriching {len(targets)} course(s)")
    print(f"{'='*72}\n")

    enriched_count = 0
    error_count = 0

    for i, (name, course) in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] {name}")
        try:
            ok = enrich_course(name, course, verbose=args.verbose)
            if ok:
                enriched_count += 1
            else:
                error_count += 1
        except Exception as e:
            print(f"  ⚠ ERROR: {e}", file=sys.stderr)
            error_count += 1
        print()
        time.sleep(0.5)  # gentle pause between courses

    print(f"\n{'='*72}")
    print(f"Summary: {enriched_count} enriched, {error_count} errors, "
          f"{len(targets) - enriched_count - error_count} skipped")
    print(f"{'='*72}")

    if args.dry_run:
        print("\n[dry-run] courses.json NOT modified.")
        return 0

    if enriched_count > 0:
        backup = courses_path.with_suffix('.json.bak')
        shutil.copy(courses_path, backup)
        print(f"\nBackup → {backup}")

        with courses_path.open('w', encoding='utf-8') as f:
            json.dump(courses, f, indent=2, ensure_ascii=False)
        print(f"Updated  {courses_path}")
    else:
        print("\nNo changes to write.")

    return 0


if __name__ == '__main__':
    sys.exit(main())
