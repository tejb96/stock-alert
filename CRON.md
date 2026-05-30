# GitHub Actions cron (Discord digest)

Serverless notifications — no hosting required. Runs twice daily and posts one Discord message with:

- Gold/silver ratio (Yahoo Finance futures)
- Top 3 ApeWisdom stocks by trend score
- Yahoo Finance quotes for those tickers

## Setup

1. Create a Discord webhook: Server → Channel → Integrations → Webhooks → New Webhook → copy URL.

2. Add the webhook to GitHub:
   - Repo → **Settings** → **Secrets and variables** → **Actions**
   - **New repository secret**: `DISCORD_WEBHOOK_URL` = your webhook URL

3. Test manually:
   - **Actions** → **Stock alert** → **Run workflow**

4. Scheduled runs: **08:00** and **20:00 UTC** every day.

## Optional environment variables

Set these as repository **Variables** (or edit `.github/workflows/stock-alert.yml`):

| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_MENTIONS` | `20` | Minimum mentions to include a ticker |
| `APEWISDOM_TOP_N` | `50` | Rows fetched from ApeWisdom before ranking |
| `YAHOO_USER_AGENT` | `stock_alert-cron/1.0` | User-Agent for Yahoo Finance requests |

## Local test

```bash
pip install -r scripts/requirements.txt
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python scripts/notify.py
```

## Tests

```bash
pip install -r scripts/requirements.txt pytest
python -m pytest scripts/test_notify.py -v
```

## Trend score

Same formula as the FastAPI backend:

```
change_24h = ((mentions - mentions_24h_ago) / mentions_24h_ago) * 100
trend_score = mentions * (1 + max(change_24h, 0) / 100)
```

Each run is stateless — no cooldown or alert thresholds. The top 3 by trend score are sent every time.
