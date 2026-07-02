# Codex Project Guide

## Project Shape
- This workspace contains two small applications:
  - A dependency-free Node.js classic snake app served by `server.js`.
  - A Python wafer defect analysis tool in `defect_analyzer.py`.
- The Python tool uses only the standard library.
- The Node app uses native ES modules and Node's built-in test runner.

## Common Commands
- Run the web app: `npm run dev`
- Run Node tests: `npm test`
- Run Python tests: `python3 -m unittest test_defect_analyzer.py`
- Generate sample defect data and dashboard:
  `python3 defect_analyzer.py --generate-sample-data sample_defects --output defect_report.csv --summary-json lot_summary.json --dashboard-html dashboard.html`

## Working Notes
- Keep edits scoped to the requested app area. The snake app and defect analyzer are independent.
- Do not add third-party dependencies unless they clearly reduce meaningful complexity.
- Generated reports and dashboards may be overwritten by analyzer runs.
- Prefer preserving Korean dashboard/user-facing text already present in the analyzer.

## Verification
- For snake changes, run `npm test`.
- For defect analyzer changes, run `python3 -m unittest test_defect_analyzer.py`.
- For shared or uncertain changes, run both test commands.
