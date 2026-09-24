# Stock Alert

Serverless Discord alerts for early market signals — no hosting required. Four GitHub Actions cron jobs:

- **Digest** (3× each weekday, plus Sunday evening) — tickers whose Reddit buzz is **accelerating**, labelled by whether the price has already reacted, with insider activity, upcoming earnings and an AI catalyst summary.
- **SEC checker** (every 20 min in market hours) — silent unless company insiders make meaningful open-market purchases, or a ticker you've been alerted on files a notable 8-K.
- **Put scan** (once each weekday, mid-morning) — the best-priced cash-secured puts on a watchlist of stocks you'd hold, plus a strike ladder for stocks you're happy to own outright (GME by default).
- **Market alarms** (hourly in market hours, plus Sunday evening) — silent unless a market-wide **crash alarm** (yen carry unwind, volatility shock, credit/funding/bank stress, rates shock) or a **bottom signal** (capitulation, fear normalizing, credit healing, yen stabilizing) switches on.

None of it is investment advice. The weekly scorecard exists so you can judge which signals actually work before trusting them.

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

### Put scan

```
1. KRE — sell Oct 23 $68.50 put
💵 $71.60 · IV 25% vs realized 17% (1.5×)
🎯 $0.76 mid (bid 0.68 / ask 0.84) · Δ 0.24 · 29 DTE · OI 570
💰 1.1% on $6.8k cash · 14%/yr
🛡 Breakeven $67.74 (5.4% below) · 1.2 typical moves away · ~77% chance of profit
⚠️ Below its 50-day average — puts get assigned in downtrends

⚓ GME — strike ladder (28–60 DTE)
💵 $24.07 · IV 55% vs realized 39% (1.4×)
🟢 Conservative (Δ 0.10–0.18)  Sell Oct 23 $21 put @ $0.28 · 16%/yr · BE $20.73 (−13.9%) · ~85% profit
🟡 Balanced (Δ 0.18–0.25)      Sell Oct 23 $22 put @ $0.49 · 28%/yr · BE $21.50 (−10.7%) · ~78% profit
🟠 Aggressive (Δ 0.25–0.35)    Sell Oct 23 $23 put @ $0.92 · 50%/yr · BE $22.09 (−8.2%)  · ~70% profit
```

### Market alarms

```
🔴 Crash alarm — Yen carry trade unwinding
USD/JPY -4.1% in 5d (now 146.52) — yen surging
AUD/JPY -6.3% in 5d — carry pairs being dumped
📜 Aug 2024: USD/JPY 161→142 in 4 weeks, Nikkei −12% in a day, VIX 65 intraday.

🟢 Bottom signal — Volatility capitulation
VIX spiked to 65.7 then fell back to 38.6 (-41% off the high)
📜 Panic got sold into — Aug 5 2024 (65→38) and Mar 2020 lows. Often the washout day, not yet the all-clear.
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

### Put scan strategy

Selling a cash-secured put means agreeing to buy 100 shares at the strike, and being paid the premium up front for it. The worst case is the same as owning the stock (it can go to zero), minus the premium; the best case is keeping the premium. It is **not** low risk — it's stock risk with the upside swapped for income. Historically the trade pays because puts are, on average, priced for bigger moves than stocks actually make (implied volatility above realized, the *volatility risk premium*): Cboe's PutWrite index (S&P 500 cash-secured puts) has earned roughly stock-like returns with lower volatility, but it gives back years of premium in crashes like 2008 and March 2020.

The scan only tries to tilt the odds; the rules come from that research and common premium-selling practice:

| Rule | Default | Why |
|------|---------|-----|
| Only stocks you'd own | `CSP_WATCHLIST` | Assignment is the realistic bad outcome. If you wouldn't hold it at the breakeven, don't sell the put. |
| Put must be priced above the stock's real moves | IV ÷ realized vol ≥ `CSP_MIN_EDGE` (1.1) | That gap is the only source of edge. Realized vol is the larger of the 20- and 60-day measures, so a calm spell doesn't flatter the premium. |
| 28–60 days to expiry | `CSP_MIN_DTE` / `CSP_MAX_DTE` | Time decay is fastest over the last ~45 days, while there's still time for a dip to recover. Weeklies pay more per day but give no room. |
| Delta 0.15–0.25 | `CSP_MIN_DELTA` / `CSP_MAX_DELTA` | Roughly a 75–85% chance of expiring worthless. Further out pays too little; closer in is mostly directional risk. |
| Breakeven ≥ 0.8 typical moves away | `CSP_MIN_CUSHION_SIGMAS` | Distance to breakeven in realized standard deviations over the option's life. |
| No earnings before expiry | Nasdaq earnings date | Earnings premium is priced for a gap, and a gap is what breaks put sellers. ETFs are exempt. |
| Liquid contracts | OI ≥ 50, bid ≥ $0.10, spread ≤ 25% of mid | Wide quotes eat the edge when you open and when you close early. |

**Score** = annualized yield × min(IV ÷ realized vol, 2), for the best contract per ticker. A ⚠️ flags stocks trading below their 50-day average — the same put is more likely to be assigned in a downtrend. **IV rank** (where today's 30-day IV sits in its past year) appears once 20 days of history have built up in `state/options.json`; selling when it's high is better than when it's low.

**Anchors** (`CSP_ANCHORS`, default `GME`) are stocks you've decided you're happy to own. They always get a three-band ladder, with the cushion rule dropped and wider quotes allowed (flagged) so there's always a choice.

**Managing the trade** — the scan only finds entries:

- Place a **limit order at the mid** and walk it down a cent or two; don't sell at the bid.
- **Buy it back at ~50% of the premium.** Most of the profit arrives early; holding to expiry for the last half adds most of the risk.
- **Decide by 21 DTE**: close, or roll out to a later expiry for a net credit. Don't roll down and out repeatedly to avoid a loss.
- If assigned, sell **covered calls** at or above your breakeven and repeat (the *wheel*). Size so assignment is affordable: one contract ties up strike × 100 in cash.

### Market alarms

Free daily data from Yahoo (FX, VIX curve, MOVE, ETFs) and FRED (high-yield spread, SOFR, interest on reserves, 10y yield). Each signal alerts when it switches on, then stays quiet while it stays on and for 10 days after, so an intraday flicker doesn't spam.

| Signal | Fires when | Past examples |
|--------|------------|---------------|
| 🔴 Yen carry unwind | 2 of: USD/JPY −3% in 5d, AUD/JPY −4% in 5d, USD/JPY 10d realized vol ≥ 14% | Jan 2019, Mar 2020, Dec 2022, Aug 2024, Apr 2025 |
| 🔴 Volatility shock | VIX ≥ 30, or VIX 9-day ÷ 3-month ≥ 1.05 with VIX ≥ 22 | Feb 2018, Mar 2020, Aug 2024, Apr 2025 |
| 🔴 Credit stress | HYG ÷ IEF −3.5% in 10d, or high-yield spread +75bp in 20 days | Dec 2018, Feb 2020, Mar 2023 |
| 🔴 Repo funding stress | SOFR ≥ 10bp above interest on reserves (3-day average) | Oct–Dec 2025 |
| 🔴 Regional bank stress | KRE −10% vs SPY in 5d | Mar 2020, Mar 2023 |
| 🔴 Rates shock | 10y yield +40bp in 5 days, or MOVE ≥ 150 | Mar 2020, 2022, Mar 2023, Apr 2025 |
| 🟢 Volatility capitulation | VIX hits ≥ 30 intraday and closes ≥ 15% below its high | Dec 26 2018, Mar 23 2020, Aug 5 2024, Apr 7 2025 |
| 🟢 Fear normalizing | After a volatility shock: VIX curve back below 0.95, VIX ≥ 30% off its 20-day peak | |
| 🟢 Credit healing | After credit stress: HYG ÷ IEF +2% off its 20-day low | |
| 🟢 Yen carry stabilizing | After a carry unwind: USD/JPY +1% in 5d | |

**What the backtest says** (`python scripts/macro_check.py --backtest 2018-01-01`, 2018 – Sep 2026, ~18 alerts a year, clustered in real stress). SPY averaged +1.05% over any 20 trading days. After bottom signals it averaged **+3.4% (capitulation), +3.1% (credit healing), +5.6% (yen stabilizing), +2.0% (fear normalizing)**. After crash alarms it averaged +1.5–4.5% — by the time these stresses show up, most of the drop has usually happened. So read a crash alarm as *cut leverage, hedge, don't chase*, and the bottom signals as the time to take profit on shorts and start buying. The sample is small (a handful of real crises), so treat the thresholds as a starting point, not a proven edge.

### Run history

Both jobs keep JSON history on the **`state` branch** (checked out into `state/` during a run and pushed back afterwards), so `main` isn't cluttered by bot commits:

- `digest.json` — 14 days of per-run mention snapshots and 30 days of alerts (price, stage, SPY at the time)
- `sec.json` — last check time, filings already processed, recent insider-alert tickers
- `options.json` — daily 30-day implied volatility per ticker (a year), for IV rank
- `macro.json` — which market alarms are on, and when each last alerted

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
   **Actions** → **Stock alert** → **Run workflow**, and the same for **SEC insider check** and **Put scan**.

4. **Scheduled runs** (UTC; GitHub cron doesn't follow daylight saving, so ET times shift an hour earlier in winter). Odd minutes avoid the `:00`/`:15`/`:30` rush when GitHub's scheduler is most congested.

   | Job | UTC | ET (summer) | Purpose |
   |-----|-----|-------------|---------|
   | Digest | Mon–Fri 12:37 | 8:37 AM | Pre-market: overnight Reddit growth and filings before the open |
   | Digest | Mon–Fri 14:23 | 10:23 AM | Post-open: opening noise settled, relative volume meaningful |
   | Digest | Mon–Fri 20:07 | 4:07 PM | Close: final daily volume, what's building for tomorrow |
   | Digest + scorecard | Sun 21:13 | 5:13 PM | Weekend due-diligence threads before Monday |
   | SEC check | Mon–Fri :13/:33/:53, 12–23h | 8 AM – 8 PM | New insider buys and watched 8-Ks |
   | Put scan | Mon–Fri 14:47 | 10:47 AM | After the open, once option spreads have tightened |

## Local development

```bash
pip install -r scripts/requirements.txt
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export SEC_CONTACT_EMAIL="you@example.com"   # optional: SEC data
export GITHUB_TOKEN="ghp_..."                # optional: AI research (PAT with `models` scope)
export STATE_DIR="state"                     # optional: where history is kept (default ./state)
python scripts/notify.py
python scripts/sec_check.py
DRY_RUN=1 python scripts/put_scan.py         # print the put scan instead of posting it
DRY_RUN=1 python scripts/macro_check.py      # print market alarms that would fire now
python scripts/macro_check.py --backtest 2018-01-01   # every alert since then, with SPY's return after
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
| `CSP_WATCHLIST` | see `put_scan.py` | Comma-separated tickers to scan for cash-secured puts. Only list stocks you'd own. The workflow sets it empty, so only the anchors are posted. |
| `CSP_ANCHORS` | `GME` | Tickers you're happy to own; always shown with a strike ladder. |
| `CSP_TOP_N` | `5` | Put picks shown per scan. |
| `CSP_MAX_CAPITAL` | `10000` | Largest cash secured per contract (strike × 100). |
| `CSP_MIN_EDGE` | `1.1` | Minimum implied ÷ realized volatility for a pick. |
| `CSP_MIN_DTE` / `CSP_MAX_DTE` | `28` / `60` | Days-to-expiry window. |
| `CSP_MIN_DELTA` / `CSP_MAX_DELTA` | `0.15` / `0.25` | Put delta window (absolute) for picks. |
| `CSP_MIN_CUSHION_SIGMAS` | `0.8` | Minimum distance to breakeven in realized standard deviations. |
| `CSP_MIN_OI` / `CSP_MIN_BID` / `CSP_MAX_SPREAD_PCT` | `50` / `0.10` / `25` | Liquidity filters. |
| `DRY_RUN` | `0` | Put scan and market alarms: print the Discord payload instead of posting. |

In GitHub Actions the workflow passes its built-in `GITHUB_TOKEN`; `permissions: models: read` lets it call GitHub Models, and `contents: write` lets it push history to the `state` branch.

## Project layout

```
.github/workflows/
  stock-alert.yml     # Digest schedule
  sec-check.yml       # SEC checker schedule
  put-scan.yml        # Put scan schedule
  macro-check.yml     # Market alarms schedule
scripts/
  notify.py           # Digest: rank, enrich, build embeds, post
  sec_check.py        # SEC checker: scan feeds, filter, post
  put_scan.py         # Put scan: rank puts, anchor ladders, post
  macro_check.py      # Market alarms: on/off transitions, post, backtest
  macro.py            # Yahoo/FRED series, crash and bottom signal rules
  options.py          # CBOE option chains, realized vol, put metrics and filters
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
