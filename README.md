# Paper Trader

A Windows 11 desktop app for a paper-trading experiment: you against an AI strategy, each with
the same virtual cash and the same rules.

**Simulation only.** Every trade is a row in a local database. The app has no brokerage
integration and cannot place a real order. Simulated results do not predict real results, and
nothing here claims the AI will be profitable.

## Quick start

No install is needed beyond Python with Tkinter. The app itself uses only the standard library.

1. **Try it with fake data:** double-click `run_demo.bat`. No API keys are needed. Demo data is
   clearly labelled and lives in its own database file.
2. **Run the real experiment:** double-click `run.bat`. The default price source needs no key.

## Closing the app

The window's X button only **minimizes** the app to the taskbar, so a stray click cannot stop the AI.
Alt+F4 does the same. To really close it, click **Exit app** at the bottom right and confirm. While
the app is closed, AI trading, price updates and snapshots stop.

**Dark mode** is on by default. Use the checkbox at the bottom right to switch; your choice is saved.

## Versions

Versions are numbered `A.B.C`: **A** for a major change, **B** for a change to a function, method or
calculation, and **C** for UX/UI and quality work.

- **1.3.2:** press and hold any table row to read all of it, including long reasons, in a pop-up. Let go to close it.
- **1.3.1:** a "?" next to every setting. Hover over it (or click it) for a short note on what the setting does.
- **1.3.0:** sectors, market links and gain by ticker.
  - Every stock has a sector, shown in both accounts and the watchlist and changeable there.
  - 30 market links connect news in one area to the sectors it tends to move. They can veto or
    support an AI buy, never start one. See the Market links tab.
  - The trade log has a Gain by ticker page: each ticker's all-time result, per account.
  - Tabs are grouped: Accounts, Market and trades, AI and news, Setup.
- **1.2.0:** the AI sells sooner in a falling trend and shows a plan for each holding.
  - Its stop now rises with the price, and a slow slide triggers a sell.
  - Each holding shows "Hold" or "Sell soon" with a rough number of days, a sell target, the
    stop and a buy-more price.
  - The ticker list in My account shows owned tickers in yellow, with the amount on the right.
  - Reference prices are bold in every table.
- **1.1.1:** coloured rows. Holdings are green in profit and red at a loss. In the trade log, sales
  are green or red by their gain, with a new Realized column. In the watchlist, tickers the AI
  account holds are yellow, with an "AI holds" column.
- **1.1.0:** API keys moved to the `source API` folder, and a `PUBLIC` folder for GitHub.
- **1.0.1:** dark mode, and the X button minimizes instead of closing, with a new Exit app button.
- **1.0.0:** first release.

## Setup on Windows 11

### Python

Python 3.10 or newer with Tkinter. Development and testing used Python 3.14.

```powershell
python --version
python -c "import tkinter; print('Tkinter OK')"
```

If you need Python, install it from [python.org](https://www.python.org/downloads/windows/) and
tick "Add python.exe to PATH". A python.org install is recommended, and it is **required** for the
optional LLM review: the `anthropic` package could not be installed on a MinGW Python, because a
dependency has no prebuilt package for it. The core app runs fine on MinGW Python.

### API keys (optional)

Keys stay on your computer and are never written in the code. In the `source API` folder, copy
`api_keys.example.env` to `api_keys.env` and fill in what you want. `api_keys.env` is excluded from
version control. Real environment variables take priority over it.

| Key | Used for | Get one |
|---|---|---|
| `ALPHAVANTAGE_API_KEY` | Prices (backup, free, closing prices) | [alphavantage.co](https://www.alphavantage.co/support/#api-key) |
| `TWELVEDATA_API_KEY` | Prices (intraday, needs a paid add-on for Canada) | [twelvedata.com](https://twelvedata.com/pricing) |
| `FINNHUB_API_KEY` | Company news | [finnhub.io](https://finnhub.io/register) |
| `ANTHROPIC_API_KEY` | Optional LLM review | [console.anthropic.com](https://console.anthropic.com/) |

The default RSS feeds need no key.

### Choosing a price source

The experiment is CAD only, so the source must carry Canadian listings.

| Source | Canadian prices | Cost | Price timing |
|---|---|---|---|
| **Yahoo Finance** (default, unofficial) | TSX, TSX Venture, Cboe Canada | Free, no key | During the trading day; the delay is not stated |
| **Alpha Vantage** (backup) | TSX and TSX Venture documented; no Cboe Canada format | 25 calls a day |  |
| **Twelve Data + Cboe Canada add-on** | TSX, TSX Venture and Cboe Canada |

### Check your data sources

Do this before starting the experiment, and whenever you want to confirm a new ticker.

```powershell
python -m paper_trader --check-sources RY.TO GOOG.TO SHOP.TO
```

### First run

1. In **Watchlist**, add your tickers: `RY.TO` for TSX, `GOOG.TO` for the Canadian version of a US
   company, `ABC.V` for TSX Venture, `GOOG.NE` for Cboe Canada. Add the company name too, because
   that is how RSS news gets matched to a ticker.
2. In **Settings**, check the starting balance and limit rules. The starting balance locks after
   the first trade.
3. In **AI account**, turn on AI trading. It is off by default.
4. Leave the app open during market hours. See [Known limitations](#known-limitations).

## Trading rules

Both accounts follow these rules. All of them are enforced in one place, `paper_trader/engine.py`,
inside a single database transaction.

### Cash and positions

- Each account starts with the same balance (default CAD 10,000) in **one CAD cash balance**.
- A buy cannot cost more than the cash available. After spending 4,000 of 10,000, 6,000 remains.
- A sell cannot exceed the shares held. Proceeds return to cash and can be used again.
- Cash can never go negative, so leverage is impossible.
- Cash and positions are never stored. They are recalculated from the append-only trade log, and
  database triggers block edits to that log.
- This always holds: `cash + book cost of holdings = starting balance + realized gain/loss`.
- Average cost is a weighted average. Selling does not change the average cost of what remains.
- Rounding always goes against the trader: costs round up to the cent, proceeds round down.

### The daily or weekly limit

A reset **never adds cash**. If each period brought a fresh 10,000, you could hold 10,000 of stock
and spend another 10,000, creating money through a loophole. Instead, the limit caps how much can
be *newly deployed* in a period:

```
maximum buy = the smaller of (cash available) and (limit - used this period)
```

- **Limit:** the "Period buy limit" setting. If blank, it equals the starting balance.
- **Used:** starts at 0 each period. A buy adds its cost. A sale subtracts its proceeds, but `used`
  never drops below 0, so selling older shares cannot lift the allowance above the limit.
- **Daily reset:** 00:00 in your computer's local time.
- **Weekly reset:** Monday 00:00 in your computer's local time.
- The next reset time is shown on each account tab.

With the default (limit equal to the starting balance) cash is usually what binds, and the limit
only matters once an account has grown past its starting balance. To make the limit bite, set it
lower: for example, 2,000 a day.

| Scenario, limit 2,000 per day, cash 8,000 | Result |
|---|---|
| Buy 2,000 | Allowed, used = 2,000 |
| Buy 1 more | Blocked by the limit |
| Sell 1,000 of today's shares | Used = 1,000, so 1,000 can be bought |
| Next day | Used = 0, allowance is 2,000, cash is unchanged |

Changing the limit or reset type takes effect immediately for both accounts and is recorded in the
settings audit log, which is included in exports.

### Whole and fractional shares

Whole shares by default. Settings can allow up to 4 decimal places for both accounts. Quantities are
exact decimals, so the accounting handles them reliably, but real brokers differ on fractional fills.

### Imported holdings

You can add existing holdings to My account (ticker, quantity, average price). They are kept in a
**separate group**, because otherwise importing shares and selling them would create money:

- They use no cash and add nothing to account value, returns or buying power.
- Selling them records the gain in their own currency, and the proceeds stay outside trading cash.
- They are excluded from the My-vs-AI comparison, which stays a same-start, same-rules contest.
- They are marked `IMPORT` / `Imported` in the trade log. To sell one, tick "Sell from imported
  holdings" on the trade ticket.

### Duplicates and corrections

Each trade has a unique ID, so a double-click cannot record it twice. An identical manual trade
within 10 seconds asks for confirmation. Trades cannot be edited or deleted; to correct a mistake,
record the opposite trade, which will cost the simulated spread.

## Price simulation

Each trade records both the **reference price** (the latest quote) and the **simulated fill price**.
A buy fills above the reference and a sell fills below it. This models spread and slippage. It is a
modelling assumption, **not a broker fee**.

| Category | Default | Meaning |
|---|---|---|
| `liquid` | 0.02 per share | For heavily traded stocks |
| `normal` | 0.03 per share | Default for any ticker you have not categorised |
| `illiquid` | 0.25% of price | For thinly traded stocks |

The defaults are starting assumptions, not measurements. Edit or add categories in **Settings**,
and assign tickers in **Watchlist**. Per-share amounts are in the ticker's own currency.

## Currency: CAD only

The **CAD tickers only** setting is on by default, for both accounts. A US listing cannot be added
to the watchlist, bought, imported or traded by the AI. The app skips FX entirely in this mode.

For US companies, use the Canadian version, called a **CDR** (Canadian Depositary Receipt), such as
`GOOG.TO`. Things to know about CDRs:

- Twelve Data's reference data listed 122 on the TSX and 223 on Cboe Canada (`.NE`) in November
  2026, with 117 on both. Prefer the `.TO` listing: Yahoo returned only 1 day of history for
  `GOOG.NE`, and the AI needs 50 days, while Alpha Vantage has no `.NE` format at all.
- **Symbols can differ from the US one:** Visa is `VISA.TO`, Bank of America is `BOFA.TO`, and
  Coca-Cola is `COLA.TO`. Check the name before trusting a symbol, because the same letters can
  mean a different company on the TSX.
- A CDR price is not the US share price converted to CAD. Each CDR is a fraction of a share and is
  currency hedged, so it will not match the US price.

Whatever the setting, a quote whose currency does not match the listing is treated as unavailable.

### If you turn US tickers on later

Untick **CAD tickers only** in Settings. US tickers then settle from the CAD balance at the Bank of
Canada **daily average for the previous business day**, which is not a live rate. No FX spread is
simulated. Each USD trade stores its rate, date and source, and it is blocked if no rate from the
last 5 days is available. If you turn the setting back on, existing US positions can still be sold.

## Missing data, closed markets and holidays

- The app never invents a price. A ticker with no data shows `n/a` and cannot be traded.
- If a source fails, the last known price stays visible, labelled as unavailable, but it cannot be
  traded, and the account shows a "partial valuation" warning.
- An intraday quote older than the limit (default 60 minutes) is **stale**. The AI is paused for
  that ticker. This covers nights, weekends and holidays, with no holiday calendar to maintain.
- An end-of-day source works differently. Its price is the last close, shown as `end of day`, and
  the app fetches each ticker once a day. That price is usable for up to 4 days (configurable),
  which covers a long weekend. Both accounts then trade at the same last close.
- You can still record a manual trade on a stale quote, after confirming. It is flagged `STALE` in
  the trade log, so you can see any time you traded on an old price.
- Each ticker is saved separately, so an interrupted update loses nothing already fetched.
- An update runs on startup, so the app recovers on its own after a restart.

## AI strategy

The AI account runs `momentum+news rules v2` (since 1.2.0), a transparent rules-based strategy in
`paper_trader/ai/strategy.py`. It is not a prediction engine.

1. **Stop:** sell if the price falls to the stop. The stop starts at the stop-loss % below average
   cost, then rises to that % below the highest price since buying. Example with 8%: bought at
   100, rose to 120, the stop is now 110.40 instead of 92.
2. **Target:** sell when the price reaches cost plus twice the stop-loss % (cost 100 → 116).
3. **Trend exit:** sell when the price is below its 20-day average and either
   - it fell over 2% in 5 days, or
   - the 20-day average is itself falling and the price is down over 20 days (a slow slide).
4. **Entry:** consider a buy only if the price is above its 20-day average, the 20-day is above the
   50-day, and 5-day and 20-day momentum are positive. No new buy of a ticker within 3 days of
   selling it.
5. **Buy more:** once per holding, when the price dips to within 1% of its 20-day average while
   the trend holds (above the 20- and 50-day averages, up over 20 days). Never below its own
   average cost, so it never adds to a loser.
6. **News:** negative signals veto a buy, and positive signals raise confidence. News can never
   start a trade by itself.

### The AI plan

The AI account tab lists a plan for each holding, updated at every price refresh:

| Column | Meaning |
|---|---|
| Plan | **Hold (N-M days)** while the trend holds, or **Sell soon (1-3 days)** when the price is below its 20-day average or within 2% of the stop |
| Sell target | the take-profit price from rule 2 |
| Stop | the price from rule 1; "(raised)" once it has moved up |
| Buy more at | the price from rule 5, or "not now" |

The days are trading days, estimated from the last 20 days' pace to the target. They are
rough, rule-based estimates, not forecasts. The plan is also saved with every AI buy (in the
trade reason) and every decision (in its data sources). While prices are paused, the last plan
stays with its time.

Each buy is sized to fit every limit, then goes through the same engine as a manual trade.
Configurable limits: maximum trade size, buys per day, positions, exposure, stop-loss and minimum
confidence. "Suggest only" logs what it would do without trading.

The **AI decisions** tab shows the timestamp, ticker, action, quantity, reference and simulated
fill price, confidence, explanation and data sources. Select a row for the detail. Repeated
identical HOLD or PAUSED outcomes are logged once, not every cycle.

The AI can only ever write to the AI account. The engine refuses anything else, and a database
constraint refuses it again.

To swap the strategy, subclass `Strategy` and set it in `paper_trader/context.py`.

### Optional LLM review

With this switched on in Settings, Claude reviews each BUY candidate. It can only approve or veto.
It cannot add tickers, change quantities or trigger sells, and any failure means no buy. Each review
is a paid API call, so it is limited to one per ticker per day and a configurable daily maximum.
The request enables Anthropic's server-side refusal fallback. To use it:

```powershell
pip install -r requirements-optional.txt
```

### AI lessons

A learning log for the **AI account only**. It covers the whole experiment, because it is worked out
from the saved trades and news.

- **Finished trades:** each result with its gain or loss, days held, how it ended, why the AI bought
  and sold, and the news around the buy. A trade finishes when the position returns to zero, so
  positions still held are not scored.
- **Keywords and events:** for each label and keyword, the number of trades, win rate and average
  return, compared with all finished trades.
- **Trends over time:** results by month and by how trades ended.
- **The text file:** `data\ai_lessons.txt` is rewritten after every update. Exports also include it,
  plus `ai_trade_results.csv`.

News is matched by time: anything with an event label published in the lookback window (48 hours by
default) before the buy, unless it names only other tickers. That includes market-wide war, gold and
politics stories, but only if a news source you have added supplies them.

Read it with care. It shows patterns, not causes. Anything under 20 trades is marked "not enough
data", and even a "possible pattern" can be luck. The AI does not read this log or change its rules
from it. Any change to the strategy is a decision for you to make.

## Sectors and market links

Every stock has a sector, such as "Oil & gas producers" or "AI chips & semiconductors". Known
tickers get one from a built-in list in `paper_trader/network.py`; anything else is
"Unclassified". Change a sector on the Watchlist tab: select the row, pick a sector, click Set
sector. Your choice is saved and overrides the built-in list.

The **Market links** tab lists 30 links, each one a well-known tendency such as "oil price ↑:
oil producers ↑, airlines ↓" or "major new AI model launched: AI chips ↑, software ↓". They are
tendencies, not tested rules.

How they are used:

1. Headlines from the news lookback window (48 hours by default) are matched to each link by
   keywords in the **headline only**. Summaries matched too loosely in testing.
2. Price links (marked ↑/↓) read the headline's verbs: "surges" counts as up and "slumps" as
   down, and a fall swaps the up and down lists. Unclear headlines are skipped.
3. Each stock gets a **net push**: +1 for each link pointing up and -1 for each pointing down.
   Each link counts once, so twenty oil headlines are one vote.
4. For the AI, the winning side becomes news signals: a negative push can veto a buy, and a
   positive push raises confidence. Links never start a trade. Turn them off in Settings
   ("Use market links").

The lower table shows every stock you and the AI own, with its net push and the links behind it.
Links 2 and 30 are information only. CDRs are currency-hedged, and an election's effect depends
on the winner.

## Gain by ticker

Trade log → **Gain by ticker** shows, for each account side by side, every ticker ever traded:
buys, sells, shares still held, realized gain (all finished sales added up), the open gain on
shares still held, and the total. For example, a GOOG sale at a 100 loss followed by one at a
60 gain gives -40. Green rows are ahead overall, and red rows are behind. Imported holdings are
not included.

## News and signals

Sources are managed in **News > Sources**, and keyword categories in **News > Signal categories**.
Both can be changed without touching code. A new source *type* is one class in `paper_trader/news/`
plus one line in `registry.py`; market providers work the same way in `paper_trader/market/`.

- Only documented APIs and feeds are used. The app does not scrape web pages. Check a feed's terms
  before adding it.
- Each stored item has its title, source, publication time, URL, relevant tickers, keywords, event
  labels and summary.
- News is treated as an **unverified signal**. To count as evidence, an item needs a source, a time
  within the lookback window, stated relevance to the ticker, and an event label.
- Keyword labels are crude: "beats estimates" is matched as text, not understood.
- Bare symbols are not matched, because words like "IT" would cause false hits. RSS items match on
  the company name you enter or an explicit form such as `(TSX: RY)` or `$AAPL`.
- The two default RSS feeds are central-bank press releases. They rarely mention a ticker, so they
  are stored for your reference but seldom affect decisions. Finnhub gives per-ticker news.
- No social media source is included, because none was verified. The category exists but is off.
- Unusual volume and momentum come from price data. Economic, geopolitical and commodity items are
  labelled and stored but have no effect on the rules strategy unless tied to a watchlist ticker.

## The five-month experiment

- **Compare** shows both accounts on matching dates: account value, return, realized and unrealized
  gains, maximum drawdown, number of trades and cash held.
- Snapshots are saved hourly while the app is open, so performance is tracked with or without trades.
- Daily, weekly, monthly and yearly figures use rolling 1, 7, 30 and 365 days. They show `n/a` until
  enough history exists.
- The day counter is a guide (about 152 days). Nothing stops at five months.

## Export, backup and reset

All in **About and data**:

- **Export everything to CSV:** trades, snapshots, daily comparison, AI decisions and lessons, news,
  settings changes and watchlist, in a new timestamped folder. The **Trade log** can export a filtered view.
- **Back up now:** a verified copy of the database. A daily automatic backup also runs, keeping the
  last 14.
- **Reset experiment:** offers a backup and export first, and needs `RESET` typed exactly. It clears
  trades (including imported holdings), snapshots and AI decisions. Settings, watchlist, news and
  sources stay. This is also how to change the starting balance once trades exist.

Everything lives in the `data` folder, which is excluded from version control. To restore a backup,
close the app, then copy the backup over `data\paper_trading.sqlite3`. Set `PAPER_TRADER_DATA_DIR`
to keep data elsewhere. Copy `data` to another drive now and then, because automatic backups sit on
the same disk.

## Verification status

What was actually run, as of 2026-11-04. Yahoo needs no key, and Alpha Vantage was tested with a
personal free key. Other providers were tested with the demo keys they publish, which only cover a
few symbols.

| Feature | Status |
|---|---|
| Trading rules, accounting, AI limits, reset, export, backup | Automated tests pass |
| Windows interface | Tested by driving the real window, and reviewed by screenshot |
| Demo mode | Working |
| Yahoo Finance, Canadian tickers | **Verified live after market hours:** `RY.TO` and CDR `GOOG.TO` through a full update, and a manual trade. The AI correctly paused on the stale prices, so an AI trade on a live Yahoo price is still unseen, and the delay is unmeasured. Long-run reliability is unknown. |
| Alpha Vantage, Canadian tickers | **Verified live** on a free key: `RY.TO`, `SHOP.TO`, CDRs `GOOG.TO` and `VISA.TO`, and a `.V` ticker |
| Twelve Data, Canadian tickers | **Not verified.** Needs the paid add-on. |
| Twelve Data, US ticker | **Verified live** (AAPL, demo key): quote, history, AI decision, manual trade |
| Bank of Canada USD/CAD (only used if US tickers are on) | **Verified live** |
| Bank of Canada and Federal Reserve RSS | **Verified live** |
| Finnhub company news | **Not verified live.** Tested only against a sample in the documented format. |
| Claude LLM review | **Not verified with the live API.** Run through the real SDK against a local stub, including refusals and failures. |

If a provider cannot serve a ticker, the ticker shows as unavailable, and the app keeps working
without guessing a price.

## Known limitations

- **The AI, price updates and snapshots only run while the app is open.** If your computer is off
  or asleep, nothing is traded or recorded, and that stretch is a gap in the history.
- **Yahoo is unofficial.** It can break, change or block the app without notice, and a long outage
  would leave a gap in a five-month run. Its data carries no guarantee.
- **Prices may be delayed.** Yahoo does not state its delay, so a fill may not match the live market.
  The Watchlist shows each quote's time and source.
- **The AI is not a day trader.** Even with intraday prices, it uses 20-day and 50-day trends and
  may hold for days or weeks. It checks at each refresh, every 30 minutes by default.
- **After the close, quotes are stale.** The AI pauses, and a manual trade needs confirmation and is
  flagged. Trading outside market hours at the last price would flatter your results.
- **With Alpha Vantage, every trade fills at the last close.** You could see today's real price and
  trade at yesterday's close, which the AI cannot do. If you switch to it, decide using closing
  prices only. Its stop-loss only sees closes, and its 25 calls a day cover about 25 tickers.
- **Twelve Data limits.** At the 30-minute refresh, 800 credits a day cover about 16 tickers.
- **A stop-loss needs a usable quote.** With an intraday source it cannot fire while the market is
  closed, so a gap down at the open sells at the first fresh price, after the move has happened.
- **Finnhub company news may not cover `.TO` symbols,** and it is untested. RSS items match a CDR
  only through the company name you enter.
- **The fill model is simple.** There are no partial fills, order types, market-impact effects,
  dividends, stock splits, taxes or fees. A split would distort a position's value, and CDR ratios
  can change. Spreads on CDRs may differ from the defaults, so review their slippage category.
- **Keyword news labels are crude** and will mislabel some headlines.
- **Fairness is approximate.** You can trade at any moment, while the AI acts only at refreshes. Your
  stale-quote trades are flagged so you can judge their effect.
- **Five months is a short, single sample.** A difference between the accounts may just be luck.

## Project layout

```
paper_trader/
  engine.py        The only code that writes trades; every rule is checked here
  ledger.py        Rebuilds cash and positions from the trade log
  valuation.py     Marks accounts to market; limit budget
  performance.py   Snapshots, returns, drawdown, comparison
  service.py       Background updates
  dataops.py       Export, backup, reset
  market/          Price and FX providers (replaceable)
  news/            News sources and signal labelling (replaceable)
  ai/              Strategy, decision cycle, lessons report, optional LLM review
  ui/              Tkinter interface
tests/             Run with: python -m unittest discover -s tests -t .
source API/        api_keys.env (yours, private) and a blank template
```

