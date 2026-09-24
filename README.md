# Stock Alert

Serverless Discord alerts for early market signals — no hosting required. Every job is **alert-only**: it posts when something needs your attention and stays silent otherwise. Five GitHub Actions cron jobs:

- **Reddit alerts** (checked each weekday pre-market) — silent unless a ticker with **accelerating** Reddit buzz clears the alert bar: bullish buzz, price hasn't reacted yet, not in a downtrend, and confirmed by unusual volume, insider buying or a fresh un-priced catalyst. Plus a **Sunday recap**: the week's tracked picks, what's heading into the new week, and a scorecard.
- **SEC checker** (every 20 min in market hours) — silent unless company insiders make meaningful open-market purchases, or a ticker you've been alerted on files a notable 8-K.
- **Put scan** (checked each weekday, mid-morning) — silent unless selling cash-secured puts is unusually well paid: a strike ladder for stocks you're happy to own (GME by default) when their implied volatility is high against its past year, and any watchlist put that passes every filter.
- **Congress trades** (once each weekday evening) — silent unless newly filed House trade reports contain a purchase that scores high on the signals that matter: leadership or committee chair, a committee that oversees the company's sector, size, long-dated call options, several members buying the same stock, and how quickly it was disclosed.
- **Market alarms** (hourly in market hours, plus Sunday evening) — silent unless a market-wide **crash alarm** (yen carry unwind, volatility shock, credit/funding/bank stress, rates shock) or a **bottom signal** (capitulation, fear normalizing, credit healing, yen stabilizing) switches on.

None of it is investment advice. The weekly scorecard exists so you can judge which signals actually work before trusting them.

## What you get

### Reddit alert card

```
1. ACME — Acme Corp
✅ Confirmed by: volume 4.8× normal, insiders buying
🐂 Bullish buzz — DoD contract award; buzz is early buyers, not bagholders
🟢 Early (price quiet over the last 5d)
💵 $4.12 · 1d +2.1% · 5d +3.0% · RelVol 4.8×
📈 Uptrend · 1m +12.0% · 3m +31.0% · 57% below 52w high
📈 Reddit: 6 → 58 mentions (+866.7%) · rank #212 → #31
🔥 3rd day on the list, still heating up · mentions by day 12 → 41 → 58
🏛 CFO Jane Doe bought $410k (30d, latest 2d ago)
📅 Earnings in 9d (Fri Oct 2, after close)
📰 Catalyst: DoD contract award announced Tuesday.
   contract/partnership · fresh news · priced in: no
⚠️ Risks: small cap · earnings soon
Sources: …
```

Every weekday run ranks the top 5 accelerating tickers and researches them, but only posts the ones that clear the bar:

| Gate | Why |
|------|-----|
| 🐂 Bullish buzz | Bearish buzz usually follows a drop that has already happened, when puts are already expensive |
| 🟢 Early | The price hasn't reacted over the last 5 days (relative to the stock's usual swings) |
| Not 📉 in a downtrend | Buzz on a stock below its 50- and 200-day averages is mostly bagholders and dip-buyers |
| At least one confirmation | Volume ≥ 1.5× normal, insiders buying, or a fresh catalyst the AI judges not priced in |

The bar depends on the AI sentiment read: if GitHub Models is unavailable, nothing alerts that day.

- **Sentiment** — 🐂 bullish / 🐻 bearish / ⚖️ mixed, judged by the AI from the week's headlines and price action. Mention counts alone can't tell a rally from a crash: ApeWisdom has no sentiment, and Reddit/Stocktwits block bots
- **Stage** — 🟢 **Early** (buzz rising, price quiet over 5 days), 🟡 **Moving** (price already in play), 🔴 **Late / chasing** (big run-up already happened). Only looks back 5 days — check the trend line too
- **Price** — 1-day and 5-day change, today's move vs the stock's usual swing, relative volume
- **Trend** — 📈 up / 📉 down / ↔️ sideways vs the 50- and 200-day averages, 1- and 3-month change, distance from the 52-week high
- **Reddit** — mentions and rank now vs 24h ago, plus the trajectory across recent days
- **SEC** — insider buys/sells in the last 30 days (sales flagged as pre-scheduled 10b5-1 or not) and notable 8-Ks in the last 7
- **Earnings** — if due in the next two weeks
- **Research** (every pick) — AI summary of the likely catalyst, whether it's fresh, whether it's priced in, and risks

### Sunday recap

```
🗓 Reddit weekly recap · week ending Sun Sep 27 — 8 tickers tracked, 1 alert sent
📋 This week's tracked picks
🔔 ACME 3 days · 🐂 bullish · mentions 12 → 58 ↗️ · price +9.1% since Tue
• ORCL 4 days · 🐻 bearish · mentions 30 → 40 ↗️ · price -3.1% since Mon
• BB 1 day · mentions 53 → 48 ➡️ · price +3.1% since Thu
👀 Heading into the week
1. META · 🐂 Bullish · 🟡 Moving · 📈 Uptrend
📊 Scorecard — picks from the last 14 days
🔔 Alerts sent: 3 picks · avg +6.0% · vs SPY +4.2% · 67% winners
🟢 Early: 9 picks · avg +1.2% · vs SPY -0.3% · 44% winners
```

The recap is where everything that didn't alert shows up — tracked picks that stayed, faded, or were bearish. The scorecard compares **alerts sent** against all tracked picks, so you can tell whether the alert bar is earning its silence.

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

### Congress trades

```
🏛 Rep. Josh Gottheimer (D-NJ05)
 9  🟢 BUY MSFT calls $500k–$1.0M · Aug 14 · joint · stock +0.5% since
  ↳ large position · call options · sits on Intelligence, which oversees tech · cluster: 2 members bought within 30d · stale: filed 31d after
  ↳ Call options; Strike price $330; Expires 10/16/2026
Filed Sep 14 · 31 days after the earliest trade
```

Parsed from the House Clerk's official Periodic Transaction Reports. Members have up to 45 days to report, so **"since"** is the move that had already happened before you could see the trade.

Only purchases are scored (sales happen for taxes, houses, rebalancing). Points, from `congress_signals.py`:

| Signal | Points |
|--------|--------|
| Purchase | +1 |
| Size: over $50k / $250k / $1M | +1 / +2 / +3 |
| Smallest range only ($1k–$15k, usually an advisor-run account) | −2 |
| Call options · long-dated (6+ months to expiry) | +2 · +1 |
| Put options (a bearish bet) | +1 |
| House leadership role (Speaker, leaders, whips, caucus chairs) | +2 |
| Sits on a committee overseeing the company's sector (Armed Services → defense, Financial Services → banks, Energy & Commerce → energy/health/tech, …) · as its chair or ranking member | +3 · +1 more |
| Chairs or is ranking member of an unrelated committee | +1 |
| Other members bought the same stock within 30 days | +2 each, max +4 |
| Filed within 7 / 14 days · filed after 30 days | +2 / +1 · −1 |

Roles come from the [congress-legislators](https://github.com/unitedstates/congress-legislators) dataset; a company's sector from its SEC industry (SIC) code, which needs `SEC_CONTACT_EMAIL`. Repeated stock buys of one ticker in a filing are merged into one line. The Senate's disclosure site blocks automated access and White House (executive branch) trade reports aren't published as a feed, so only the House is covered.

### Put scan

```
💰 Premiums are rich — GME: IV rank 72, up to 50%/yr

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

Posted only on days worth selling: an anchor's ladder when its IV rank is 50+ (implied ≥ 1.3× realized volatility until 20 days of IV history exist) and some strike pays 30%/yr or more, and watchlist picks only when one passes every filter. Every run still records IV history, so quiet days build the IV rank.

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

Every job keeps JSON history on the **`state` branch** (checked out into `state/` during a run and pushed back afterwards), so `main` isn't cluttered by bot commits:

- `digest.json` — 14 days of per-run mention snapshots and 30 days of alerts (price, stage, SPY at the time)
- `sec.json` — last check time, filings already processed, recent insider-alert tickers
- `options.json` — daily 30-day implied volatility per ticker (a year), for IV rank
- `macro.json` — which market alarms are on, and when each last alerted
- `congress.json` — House filings already processed, and 90 days of purchases (to spot cluster buying)

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
   **Actions** → **Stock alert** → **Run workflow**, and the same for **SEC insider check**, **Put scan** and **Congress trades**.

4. **Scheduled runs** (UTC; GitHub cron doesn't follow daylight saving, so ET times shift an hour earlier in winter). Odd minutes avoid the `:00`/`:15`/`:30` rush when GitHub's scheduler is most congested.

   | Job | UTC | ET (summer) | Purpose |
   |-----|-----|-------------|---------|
   | Reddit alerts | Mon–Fri 11:37 | 7:37 AM | Pre-market: overnight Reddit growth and filings before the open. GitHub often starts cron runs late (hours, on busy days); it never runs twice in one US day |
   | Reddit recap + scorecard | Sun 21:13 | 5:13 PM | Weekend due-diligence threads before Monday |
   | SEC check | Mon–Fri :13/:33/:53, 12–23h | 8 AM – 8 PM | New insider buys and watched 8-Ks |
   | Put scan | Mon–Fri 14:47 | 10:47 AM | After the open, once option spreads have tightened |
   | Congress trades | Mon–Fri 22:41 | 6:41 PM | New House trade disclosures |

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
| `RESEARCH_TOP_N` | `TOP_N` | How many top cards get research (and a sentiment read). |
| `RESEARCH_MODEL` | `openai/gpt-4o-mini` | [GitHub Models](https://docs.github.com/en/github-models) catalog ID. |
| `RESEARCH_NEWS_COUNT` | `5` | Headlines fetched per ticker for model context (2 shown). |
| `GITHUB_MODELS_URL` | `https://models.github.ai/inference/chat/completions` | Override inference endpoint. |
| `FORCE` | `0` | Reddit alerts: run even if today's run already happened (manual workflow runs set this). |
| `DIGEST_MODE` | `auto` | `auto` = alerts on weekdays and the recap on Sundays; `alert` or `recap` to force one. |
| `SCORECARD_DAYS` | `14` | Pick age window for the Sunday scorecard. |
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
| `CSP_ALERT_IV_RANK` | `50` | Post an anchor's ladder only when its IV rank is at least this. |
| `CSP_ALERT_IV_EDGE` | `1.3` | Until 20 days of IV history exist (no IV rank yet): require implied ≥ this × realized volatility instead. |
| `CSP_ALERT_MIN_ANNUALIZED` | `30` | …and a ladder strike paying at least this %/yr. |
| `CONGRESS_MIN_SCORE` | `6` | Purchases scoring at least this are posted. |
| `CONGRESS_WATCH` | — | Optional comma-separated member names whose every stock/option trade is posted regardless of score. |
| `CONGRESS_LOOKBACK_DAYS` | `10` | Ignore filings older than this (keeps a first run from flooding the channel). |
| `DRY_RUN` | `0` | Reddit alerts, put scan, congress trades and market alarms: print the Discord payload instead of posting. |

In GitHub Actions the workflow passes its built-in `GITHUB_TOKEN`; `permissions: models: read` lets it call GitHub Models, and `contents: write` lets it push history to the `state` branch.

## Project layout

```
.github/workflows/
  stock-alert.yml     # Digest schedule
  sec-check.yml       # SEC checker schedule
  put-scan.yml        # Put scan schedule
  macro-check.yml     # Market alarms schedule
  congress-check.yml  # Congress trades schedule
scripts/
  notify.py           # Reddit alerts + Sunday recap: rank, enrich, alert bar, post
  sec_check.py        # SEC checker: scan feeds, filter, post
  put_scan.py         # Put scan: rank puts, anchor ladders, rich-premium gate, post
  macro_check.py      # Market alarms: on/off transitions, post, backtest
  congress_check.py   # Congress trades: new House filings, filter, post
  congress.py         # House Clerk index + PTR PDF parsing
  congress_signals.py # Member roles, committee ↔ sector overlap, trade scoring
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
  requirements.txt    # httpx, ddgs, pypdf
```

## Requirements

- Python 3.13 (used in CI)
- [httpx](https://www.python-httpx.org/) for async HTTP
- [ddgs](https://pypi.org/project/ddgs/) for news headline search
