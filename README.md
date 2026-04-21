# NoB Golf Reports

Per-round prep reports for the NoB golf group, hosted at https://nob-golf-reports.netlify.app

## Folder contents

- `report_builder.py` — generates a report HTML for a given course/date/players
- `fit_parser.py` — parses Garmin .FIT files (referenced by the build process if you re-run aggregation)
- `courses.json` — the course database (course details, scorecards, walk/time/elevation aggregates)
- `index.html` — auto-regenerated landing page that lists all reports
- `reports.json` — manifest of all generated reports (auto-managed by build script)
- `YYYY-MM-DD_HHMM_course-slug.html` — one file per round, the report itself

## Building a new report

```
py report_builder.py --course "Irish Hills Golf & Country Club" --date 2026-05-03 --time 08:15 --players Nick Brett Ollie --output 2026-05-03_0815_irish-hills.html
```

This:
1. Writes the HTML report to the named file
2. Updates `reports.json` (the manifest)
3. Regenerates `index.html` so the landing page lists the new report

Player order: **Ollie always last.** Ollie's row in the scorecard is display-only — no inputs, no send-to-Ollie section (Ollie is the sweeper, not a submitter).

## Deploying

Once the files are committed and pushed to GitHub, Netlify auto-deploys within ~30 seconds:

```
git add .
git commit -m "Add May 3 Irish Hills report"
git push
```

Then visit https://nob-golf-reports.netlify.app to verify, and send the boys the direct report URL:
`https://nob-golf-reports.netlify.app/2026-05-03_0815_irish-hills.html`

## Course list

To see what courses are available, look in `courses.json` — top-level keys are exact course names. New courses get added by extending that JSON manually (or via a future course-builder script).
