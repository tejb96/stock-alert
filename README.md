# Stock Alert

Serverless Discord alerts for early market signals — no hosting required. Two GitHub Actions cron jobs:

- **Digest** (3× each weekday, plus Sunday evening) — tickers whose Reddit buzz is **accelerating**, labelled by whether the price has already reacted, with insider activity, upcoming earnings and an AI catalyst summary.
- **SEC checker** (every 20 min in market hours) — silent unless company insiders make meaningful open-market purchases, or a ticker you've been alerted on files a notable 8-K.

Neither is investment advice. The weekly scorecard exists so you can judge which signals actually work before trusting them.

## What you get

### Digest card

```
🟢 1. ACME — Acme Corp · Early
💵 $4.12 · 1d +2.1% · 5d +3.0% · RelVol 4.8×
📊 52w $2.00 – $9.50
📈 Reddit: 6 → 58 mentions (+866.7%) · rank #212 → #31
🔥 3rd digest in a row · mentions by run 12 → 41 → 58
🏛 CFO Jane Doe bought $410k (30d, latest 2d ago)
📅 Earnings in 9d (Fri Oct 2, after close)
📰 Catalyst: DoD contract award announced Tuesday.
   contract/partnership · fresh news · priced in: no
⚠️ Risks: small cap · earnings soon
Sources: …
```

- **Stage** — 🟢 **Early** (buzz rising, price hasn't reacted), 🟡 **Moving** (price already in play), 🔴 **Late / chasing** (big run-up already happened)
- **Price** — 1-day and 5-day change, today's move vs the stock's usual swing, relative volume, 52-week range
- **Reddit** — mentions and rank now vs 24h ago, plus the trajectory across recent digests
- **SEC** — insider buys/sells in the last 30 days and notable 8-Ks in the last 7
- **Earnings** — if due in the next two weeks
- **Research** (top 3) — AI summary of the likely catalyst, whether it's fresh, whether it's priced in, and risks

On Sundays a **📊 Scorecard** follows the digest: how every alert from the last 14 days has done since it was sent, by stage, against SPY.

### SEC alerts

```
🏛 Insider buying — INBX (Inhibrx Biosciences, Inc.)
Kayyem Jon Faiz (Director) bought $500k · 5,000 sh @ $100.00 · 2026-09-23
👥 Cluster: 3 insiders bought $4.5M in the last 30 days
💵 $100.33 · 1d -9.3% (2.2× usual) · 5d -5.0% · RelVol 1.0×
↔️ Now +0.3% vs insider price
```

```
📄 8-K — MCD (MCDONALDS CORP)
Reg FD disclosure (7.01)
Watching because: in the Reddit digest Sep 24
```

## How it works

```
Digest (scripts/notify.py)                    SEC checker (scripts/sec_check.py)
  ApeWisdom  → ~300 tickers, now vs 24h          EDGAR latest-filings feed (Form 4, 8-K)
  state      → mentions across past runs            since the previous check
  Yahoo      → 2 months of daily bars           Form 4 XML → open-market buys (code P)
  EDGAR      → insider trades, 8-Ks per ticker  EDGAR     → 30-day cluster per ticker
  Nasdaq     → earnings calendar                Yahoo     → price context
  DuckDuckGo → headlines                        state     → skip filings already processed,
  GitHub Models → catalyst JSON                              watch tickers from the digest
        │                                                │
        └───────────── Discord webhook ──────────────────┘
                 run history → `state` branch
```

### Trend score

Rewards *acceleration*, not size, so a small ticker going 3 → 40 mentions outranks a mega-cap going 900 → 950:

```
acceleration = log2((mentions + 1) / (mentions_24h_ago + 1))   # doublings in 24h, floored at 0
climb        = log2(rank_24h_ago / rank)                        # doublings in rank, floored at 0
trend_score  = log2(1 + mentions) × (acceleration + 0.5 × climb) × momentum
```

**Momentum** comes from earlier digests: +15% per consecutive run of rising mentions (up to +45%), or ×0.8 if mentions fell since the last run. A ticker that keeps building across runs climbs; one that spiked and faded drops.

Tickers need at least `MIN_MENTIONS` (default 5). Candidates are then price-checked on Yahoo; ETFs, stocks under `MIN_PRICE`, and symbols Yahoo doesn't recognise (often ordinary words matched as tickers) are dropped.

### Stage

Moves are measured against each stock's own volatility (standard deviation of daily % returns over ~2 months), so a 4% day is significant for McDonald's but noise for a volatile small cap:

| Stage | Rule |
|-------|------|
| 🔴 Late | 1d ≥ +15% or 5d ≥ +30%, or an upside move ≥ 3× the usual swing |
| 🟡 Moving | 1d or 5d move (either direction) ≥ 1.5× the usual swing |
| 🟢 Early | otherwise |

Without enough history, fixed thresholds are used instead (±5% 1d / ±10% 5d for Moving).

**Relative volume** is today's volume vs the prior 20-session average. During market hours it's projected to a full session, so a mid-morning run isn't understated.

### Insider alerts

Form 4s are filed within two business days of an insider trade. Only **open-market purchases** (transaction code P) by **officers or directors** count — grants, option exercises, gifts and funds acting as 10% owners are ignored. A ticker is posted when either:

- the new purchases total at least `SEC_ALERT_USD` (default $100k), or
- at least two insiders have bought in the last 30 days (a *cluster*) and the new purchase is at least `SEC_MIN_BUY_USD` (default $25k).

Skipped: stocks under `MIN_PRICE`, ETFs and funds, placeholder symbols like `NONE`, and stocks with under a week of trading history (IPO allocations at the offering price, not market buys). Trades reported more than 5 days late are flagged ⚠️.

**8-K alerts** only cover tickers from the digest in the last 7 days or with an insider alert in the last 30, and skip routine items (exhibits, shareholder votes, bylaw changes).

### Run history

Both jobs keep JSON history on the **`state` branch** (checked out into `state/` during a run and pushed back afterwards), so `main` isn't cluttered by bot commits:

- `digest.json` — 14 days of per-run mention snapshots and 30 days of alerts (price, stage, SPY at the time)
- `sec.json` — last check time, filings already processed, recent insider-alert tickers

Deleting a file just resets that history.

## Setup

1. **Create a Discord webhook**
   Server → Channel → Integrations → Webhooks → New Webhook → copy URL.

2. **Add repository secrets**
   Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

   | Secret | Value |
   |--------|-------|
   | `DISCORD_WEBHOOK_URL` | your webhook URL |
   | `SEC_CONTACT_EMAIL` | an email address SEC can contact — [EDGAR requires it](https://www.sec.gov/os/accessing-edgar-data) in every request's User-Agent. Without it the SEC features are skipped. |

3. **Test manually**
   **Actions** → **Stock alert** → **Run workflow**, and the same for **SEC insider check**.

4. **Scheduled runs** (UTC; GitHub cron doesn't follow daylight saving, so ET times shift an hour earlier in winter). Odd minutes avoid the `:00`/`:15`/`:30` rush when GitHub's scheduler is most congested.

   | Job | UTC | ET (summer) | Purpose |
   |-----|-----|-------------|---------|
   | Digest | Mon–Fri 12:37 | 8:37 AM | Pre-market: overnight Reddit growth and filings before the open |
   | Digest | Mon–Fri 14:23 | 10:23 AM | Post-open: opening noise settled, relative volume meaningful |
   | Digest | Mon–Fri 20:07 | 4:07 PM | Close: final daily volume, what's building for tomorrow |
   | Digest + scorecard | Sun 21:13 | 5:13 PM | Weekend due-diligence threads before Monday |
   | SEC check | Mon–Fri :13/:33/:53, 12–23h | 8 AM – 8 PM | New insider buys and watched 8-Ks |

## Local development

```bash
pip install -r scripts/requirements.txt
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export SEC_CONTACT_EMAIL="you@example.com"   # optional: SEC data
export GITHUB_TOKEN="ghp_..."                # optional: AI research (PAT with `models` scope)
export STATE_DIR="state"                     # optional: where history is kept (default ./state)
python scripts/notify.py
python scripts/sec_check.py
```

Without `GITHUB_TOKEN`, researched cards still get news sources, just no AI summary. Without `SEC_CONTACT_EMAIL`, cards omit the SEC line and `sec_check.py` exits immediately.

### Tests

```bash
pip install -r scripts/requirements.txt pytest
python -m pytest scripts -v
```

Or without a virtualenv, using [uv](https://docs.astral.sh/uv/):

```bash
uv run --no-project --with-requirements scripts/requirements.txt --with pytest python -m pytest scripts
```

## Configuration

Set in the workflow `env:` blocks (or export locally):

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_WEBHOOK_URL` | — | **Required.** Discord webhook URL (secret). |
| `SEC_CONTACT_EMAIL` | — | Contact email declared to SEC EDGAR (secret). Enables SEC features. |
| `STATE_DIR` | `state` | Directory holding `digest.json` / `sec.json`. |
| `MIN_MENTIONS` | `5` | Minimum current mentions for a ticker to be ranked. |
| `APEWISDOM_PAGES` | `3` | ApeWisdom pages fetched (100 tickers each). |
| `TOP_N` | `5` | Tickers shown per digest. |
| `MIN_PRICE` | `1` | Drop stocks priced below this (USD). |
| `YAHOO_USER_AGENT` | `stock_alert-cron/1.0` | User-Agent for Yahoo Finance and ApeWisdom. |
| `ENABLE_RESEARCH` | `1` | Set `0` to disable news + AI research. |
| `RESEARCH_TOP_N` | `3` | How many top cards get research. |
| `RESEARCH_MODEL` | `openai/gpt-4o-mini` | [GitHub Models](https://docs.github.com/en/github-models) catalog ID. |
| `RESEARCH_NEWS_COUNT` | `5` | Headlines fetched per ticker for model context (2 shown). |
| `GITHUB_MODELS_URL` | `https://models.github.ai/inference/chat/completions` | Override inference endpoint. |
| `SCORECARD` | `auto` | `auto` = Sundays, `always`, or `never`. |
| `SCORECARD_DAYS` | `14` | Alert age window for the scorecard. |
| `SEC_ALERT_USD` | `100000` | New insider buying that alerts on its own. |
| `SEC_MIN_BUY_USD` | `25000` | Smallest purchase considered (alerts only as part of a cluster). |

In GitHub Actions the workflow passes its built-in `GITHUB_TOKEN`; `permissions: models: read` lets it call GitHub Models, and `contents: write` lets it push history to the `state` branch.

## Project layout

```
.github/workflows/
  stock-alert.yml     # Digest schedule
  sec-check.yml       # SEC checker schedule
scripts/
  notify.py           # Digest: rank, enrich, build embeds, post
  sec_check.py        # SEC checker: scan feeds, filter, post
  reddit.py           # ApeWisdom fetch + acceleration score
  market.py           # Yahoo quotes, stage, relative volume, Nasdaq earnings
  sec.py              # EDGAR client, Form 4 / 8-K parsing
  research.py         # DuckDuckGo news + GitHub Models catalyst JSON
  state.py            # Run history, streaks, momentum, scorecard
  common.py           # Env helpers, formatting, Discord delivery
  save_state.sh       # Commit + push the state checkout
  test_*.py           # Unit tests (factories.py, conftest.py helpers)
  requirements.txt    # httpx, ddgs
```

## Requirements

- Python 3.13 (used in CI)
- [httpx](https://www.python-httpx.org/) for async HTTP
- [ddgs](https://pypi.org/project/ddgs/) for news headline search
