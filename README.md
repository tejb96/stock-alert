# Stock Alert

Serverless Discord alerts for tickers whose Reddit buzz is **accelerating** — no hosting required. A GitHub Actions cron job fetches ApeWisdom mention data and Yahoo Finance price/volume history, labels each pick by whether the price has already reacted, and posts color-coded Discord embeds.

## What you get

Each digest is a header line plus one card per ticker (top 5 by default):

- **Stage tag** — 🟢 **Early** (buzz rising, price hasn't reacted), 🟡 **Moving** (price already in play), 🔴 **Late / chasing** (big run-up already happened)
- **Price context** — price, 1-day and 5-day change, today's move vs the stock's usual daily swing, relative volume, 52-week range
- **Reddit trajectory** — mentions and rank now vs 24h ago
- **Research** (top pick) — recent news headlines and a short AI summary of the likely catalyst, whether it's fresh, and whether price has reacted

```
🟢 1. ACME — Acme Corp · Early
💵 $4.12 · 1d +2.1% · 5d +3.0% · RelVol 4.8×
📊 52w $2.00 – $9.50
📈 Reddit: 6 → 58 mentions (+866.7%) · rank #212 → #31
```

## How it works

```
GitHub Actions (weekdays pre-market / post-open / close, Sunday evening)
        │
        ▼
  scripts/notify.py
        │
        ├── ApeWisdom API   → top ~300 tickers: mentions & rank, now vs 24h ago
        ├── Yahoo Finance   → 2 months of daily bars per candidate
        ├── DuckDuckGo news → headlines for the top pick
        ├── GitHub Models   → brief catalyst summary (CI)
        └── Discord webhook → header + embeds
```

The job is **stateless** — every run ranks the current data from scratch.

### Trend score

The score rewards *acceleration*, not size, so a small ticker going 3 → 40 mentions outranks a mega-cap going 900 → 950:

```
acceleration = log2((mentions + 1) / (mentions_24h_ago + 1))   # doublings in 24h, floored at 0
climb        = log2(rank_24h_ago / rank)                        # doublings in rank, floored at 0
trend_score  = log2(1 + mentions) × (acceleration + 0.5 × climb)
```

Tickers need at least `MIN_MENTIONS` (default 5) and a positive score. The top candidates are then price-checked on Yahoo; ETFs, stocks under `MIN_PRICE`, and symbols Yahoo doesn't recognise (often ordinary words matched as tickers) are dropped.

### Stage

Moves are measured against each stock's own volatility (standard deviation of daily % returns over the last ~2 months), so a 4% day is significant for McDonald's but noise for a volatile small cap:

| Stage | Rule |
|-------|------|
| 🔴 Late | 1d ≥ +15% or 5d ≥ +30%, or an upside move ≥ 3× the usual swing |
| 🟡 Moving | 1d or 5d move (either direction) ≥ 1.5× the usual swing |
| 🟢 Early | otherwise |

Without enough history, fixed thresholds are used instead (±5% 1d / ±10% 5d for Moving).

**Relative volume** is today's volume vs the prior 20-session average. During market hours today's volume is projected to a full session, so a mid-morning run isn't understated.

## Setup

1. **Create a Discord webhook**  
   Server → Channel → Integrations → Webhooks → New Webhook → copy URL.

2. **Add the webhook to GitHub**  
   Repo → **Settings** → **Secrets and variables** → **Actions**  
   New repository secret: `DISCORD_WEBHOOK_URL` = your webhook URL.

3. **Test manually**  
   **Actions** → **Stock alert** → **Run workflow**.

4. **Scheduled runs** (UTC; see [`.github/workflows/stock-alert.yml`](.github/workflows/stock-alert.yml)):

   | UTC | ET (summer) | Purpose |
   |-----|-------------|---------|
   | Mon–Fri 12:37 | 8:37 AM | Pre-market: overnight Reddit growth before the open |
   | Mon–Fri 14:23 | 10:23 AM | Post-open: opening noise settled, relative volume meaningful |
   | Mon–Fri 20:07 | 4:07 PM | Close: final daily volume, what's building for tomorrow |
   | Sun 21:13 | 5:13 PM | Weekend due-diligence threads before Monday |

   Odd minutes avoid the `:00`/`:15`/`:30` rush when GitHub's scheduler is most congested. GitHub cron is fixed to UTC, so ET times shift an hour earlier in winter.

## Local development

```bash
pip install -r scripts/requirements.txt
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
# Optional: AI summary via GitHub Models (PAT with `models` scope)
export GITHUB_TOKEN="ghp_..."
python scripts/notify.py
```

Without `GITHUB_TOKEN`, the top pick still gets its news sources, just without the AI summary.

### Tests

```bash
pip install -r scripts/requirements.txt pytest
python -m pytest scripts/test_notify.py -v
```

Or without a virtualenv, using [uv](https://docs.astral.sh/uv/):

```bash
uv run --no-project --with-requirements scripts/requirements.txt --with pytest python -m pytest scripts/test_notify.py
```

## Configuration

Set these as repository **Variables** in GitHub (or export them locally):

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_WEBHOOK_URL` | — | **Required.** Discord webhook URL (GitHub secret in CI). |
| `MIN_MENTIONS` | `5` | Minimum current mentions for a ticker to be ranked. |
| `APEWISDOM_PAGES` | `3` | ApeWisdom pages fetched (100 tickers each). |
| `TOP_N` | `5` | Tickers shown per digest. |
| `MIN_PRICE` | `1` | Drop stocks priced below this (USD). |
| `YAHOO_USER_AGENT` | `stock_alert-cron/1.0` | User-Agent for Yahoo Finance requests. |
| `ENABLE_RESEARCH` | `1` | Set `0` to disable the research block. |
| `RESEARCH_MODEL` | `openai/gpt-4o-mini` | [GitHub Models](https://docs.github.com/en/github-models) catalog ID for summaries. |
| `RESEARCH_NEWS_COUNT` | `5` | Headlines fetched for model context (top 3 shown). |
| `GITHUB_MODELS_URL` | `https://models.github.ai/inference/chat/completions` | Override inference endpoint (e.g. tests). |

In GitHub Actions, the workflow passes its built-in `GITHUB_TOKEN` to the script; `permissions: models: read` lets it call GitHub Models. Private repos may have different GitHub Models access than public repos — see the [GitHub Models docs](https://docs.github.com/en/github-models).

## Project layout

```
.github/workflows/stock-alert.yml   # Scheduled GitHub Actions workflow
scripts/
  notify.py                         # Fetch data, build message, post to Discord
  test_notify.py                    # Unit tests for scoring, staging and embeds
  requirements.txt                  # Python dependencies (httpx, ddgs)
```

## Requirements

- Python 3.13 (used in CI)
- [httpx](https://www.python-httpx.org/) for async HTTP
- [ddgs](https://pypi.org/project/ddgs/) for news headline search
