# Stock Alert

Serverless Discord digest for market signals — no hosting required. A GitHub Actions cron job runs twice daily, fetches live data from Yahoo Finance and ApeWisdom, and posts a formatted summary to Discord.

## What you get

Each digest includes:

- **Gold/silver ratio** — COMEX futures (`GC=F` / `SI=F`) from Yahoo Finance
- **Top 3 trending tickers** — ranked by a custom trend score from [ApeWisdom](https://apewisdom.io/) Reddit mention data
- **Yahoo quotes** — price, daily change, and 52-week range for those tickers
- **Research** (optional) — for the #1 trend ticker: recent news headlines and a short AI summary linking Reddit buzz to likely catalysts

## How it works

```
GitHub Actions (08:00 & 20:00 UTC)
        │
        ▼
  scripts/notify.py
        │
        ├── Yahoo Finance  → gold/silver ratio + stock quotes
        ├── ApeWisdom API  → mention counts & 24h change
        ├── DuckDuckGo news → headlines for top trend ticker
        ├── GitHub Models  → brief research summary (CI)
        └── Discord webhook → formatted digest message
```

The job is **stateless** — every run sends the current top 3 by trend score. There are no alert thresholds, cooldowns, or database.

### Trend score

```
change_24h  = ((mentions - mentions_24h_ago) / mentions_24h_ago) × 100
trend_score = mentions × (1 + max(change_24h, 0) / 100)
```

Tickers need at least `MIN_MENTIONS` (default 20) to qualify. Negative 24h change does not reduce the score.

## Setup

1. **Create a Discord webhook**  
   Server → Channel → Integrations → Webhooks → New Webhook → copy URL.

2. **Add the webhook to GitHub**  
   Repo → **Settings** → **Secrets and variables** → **Actions**  
   New repository secret: `DISCORD_WEBHOOK_URL` = your webhook URL.

3. **Test manually**  
   **Actions** → **Stock alert** → **Run workflow**.

4. **Scheduled runs** — **08:00** and **20:00 UTC** every day (see [`.github/workflows/stock-alert.yml`](.github/workflows/stock-alert.yml)).

## Local development

```bash
pip install -r scripts/requirements.txt
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
# Optional: AI summary via GitHub Models (PAT with `models` scope)
export GITHUB_TOKEN="ghp_..."
python scripts/notify.py
```

Without `GITHUB_TOKEN`, research still appears when news is found, using headline bullets instead of an AI summary.

### Tests

```bash
pip install -r scripts/requirements.txt pytest
python -m pytest scripts/test_notify.py -v
```

## Configuration

Set these as repository **Variables** in GitHub (or export them locally):

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_WEBHOOK_URL` | — | **Required.** Discord webhook URL (GitHub secret in CI). |
| `MIN_MENTIONS` | `20` | Minimum mentions for a ticker to be ranked. |
| `APEWISDOM_TOP_N` | `50` | Rows fetched from ApeWisdom before ranking. |
| `YAHOO_USER_AGENT` | `stock_alert-cron/1.0` | User-Agent for Yahoo Finance requests. |
| `ENABLE_RESEARCH` | `1` | Set `0` to disable the research block. |
| `RESEARCH_MODEL` | `openai/gpt-4o-mini` | [GitHub Models](https://docs.github.com/en/github-models) catalog ID for summaries. |
| `RESEARCH_NEWS_COUNT` | `5` | Headlines fetched for model context. |
| `GITHUB_MODELS_URL` | `https://models.github.ai/inference/chat/completions` | Override inference endpoint (e.g. tests). |

In GitHub Actions, `GITHUB_TOKEN` is provided automatically when the workflow has `permissions: models: read`. Private repos may have different GitHub Models access than public repos — see the [GitHub Models docs](https://docs.github.com/en/github-models).

## Project layout

```
.github/workflows/stock-alert.yml   # Scheduled GitHub Actions workflow
scripts/
  notify.py                         # Fetch data, build message, post to Discord
  test_notify.py                    # Unit tests for scoring and formatting
  requirements.txt                  # Python dependencies (httpx, ddgs)
CRON.md                             # Additional cron setup notes
```

## Requirements

- Python 3.13 (used in CI)
- [httpx](https://www.python-httpx.org/) for async HTTP
- [ddgs](https://pypi.org/project/ddgs/) for news headline search
