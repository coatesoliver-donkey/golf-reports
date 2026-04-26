# enrich_courses.py — Quick start

Populates `courses.json` with Google Places data: each course gets coords, address, drive time from Kanata, the course's own Google rating + 6 reviews, and a top 5 sweet/snack stops on the way home.

## One-time setup (Windows)

From `D:\Ollie\NOBReports\golf-reports\`:

```
py -m pip install requests python-dotenv
```

Make sure `.env` is in the same folder and contains:

```
GOOGLE_MAPS_API_KEY=AIzaSy...your-key-here...
```

And that `.gitignore` includes `.env` (so the key never reaches GitHub).

## Run

```
py enrich_courses.py                      # enrich every course missing meta.stops
py enrich_courses.py --course "Edelweiss" # one course only (substring match)
py enrich_courses.py --dry-run            # preview without writing
py enrich_courses.py --force              # re-enrich already-populated courses
py enrich_courses.py --verbose            # print every API call (debugging)
```

The script auto-backs up `courses.json` to `courses.json.bak` before writing.

## What it writes per course

- `course['meta']['lat']`, `course['meta']['lng']` — geocoded coords
- `course['meta']['address']` — formatted street address
- `course['meta']['google_rating']` — float, e.g. 4.2
- `course['meta']['google_reviews']` — int, total review count
- `course['meta']['reviews']` — list of 6 review snippets in `{stars, text, source}` shape
- `course['meta']['stops']` — list of up to 5 stops in `{icon, name, addr, dist, desc, rating, badge, hours, color}` shape — exactly what `report_builder.py`'s `build_stops()` already consumes
- `course['meta']['fun_fact']` — Google's editorial summary if present (otherwise leaves any existing fun_fact alone)
- `course['drive_min_from_kanata']` — int, minutes

## After running

Rebuild reports as usual:

```
py report_builder.py --course "..." --date 2026-MM-DD --time HH:MM --players Nick Brett Ollie --output ...html
```

The reviews and stops sections will now populate automatically for every enriched course.

## Cost

Roughly $0.48 per course one-time. All 19 unenriched courses ≈ $9.12, well under the $200/month free tier.

## If something breaks

- **"GOOGLE_MAPS_API_KEY not set"** — `.env` is missing or in the wrong folder. Must be in the same folder as the script.
- **"Geocoding failed — skipping"** — course name is ambiguous. Edit the function call in `enrich_course()` to pass a more specific query, or manually add `meta.lat` and `meta.lng` to that course in `courses.json` and rerun.
- **HTTP 403 / "API key not authorized"** — the key doesn't have Places API (New), Distance Matrix, or Geocoding enabled. Check Google Cloud Console → Credentials → Edit API key → API restrictions.
- **HTTP 429** — rate limited. The script has small sleeps between calls but Google's free tier is generous; very unlikely. Wait a minute and rerun with `--force` to redo just the failures.
