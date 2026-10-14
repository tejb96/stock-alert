# Stock alert run history

Written by the GitHub Actions cron jobs on `main`; do not edit by hand.

- `digest.json` — Reddit alerts: per-run mention snapshots (momentum, streaks) and tracked picks (scorecard)
- `options.json` — put scan: daily 30-day implied volatility per ticker (IV rank), last scan day, last posted ladders
- `congress.json` — congress trades: House filings already processed, and recent purchases (cluster buying)
- `macro.json` — market alarms: which alarms are on, and when each last alerted

Deleting a file just resets that history.
