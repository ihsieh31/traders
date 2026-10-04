# US Equity Data Sources That Include DELISTED Securities
### Survivorship-bias research report — compiled 2026-09-28

**Legend:** ✅ VERIFIED LIVE (I ran the request/parsed the file) · 📄 VERIFIED IN DOCS (vendor's own page) · ❌ COULD NOT VERIFY (flagged explicitly)

---

## TL;DR — Ranked practical options

| Rank | Source | Serves delisted *price bars*? | Earliest | Cheapest entry | Notes |
|---|---|---|---|---|---|
| 1 | **HistoricalData.net** | ✅ 50k+ delisted tickers, full frozen history | Oct/Nov **2003** | **$299 one-time** (daily CSV) | Best value found. Adjusted+unadjusted side by side. Vendor has internal price inconsistencies — see §B1 |
| 2 | **EODHD** | ✅ `delisted=1` list + EOD for all | ~Jan 2000 (26k US tickers) | **$19.99/mo** | ⚠️ **pre-2018 delistings = EOD ONLY** (no fundamentals/divs) |
| 3 | **Norgate** | ✅ Platinum/Diamond only | **1990** (Platinum) / 1950 (Diamond) | **$630/yr** (Platinum) | Best index-constituent (PIT) data too |
| 4 | **Sharadar SEP/SF1** | ✅ ~12,000 delisted | **1998** | Free DJIA sample; paid price unverified | 99% survivorship-bias-free, has delist *reasons* |
| 5 | **Massive (ex-Polygon)** | ✅ flat files | **2003-09-10** | $29/mo (5yr) … $199/mo (20+yr) | Flat-file history capped by plan tier |
| 6 | **Tiingo** | ✅ (roster ✅ verified; permaTicker for prices) | 30+ yrs claimed (conflict) | **Free roster file** + $30/mo for prices | Free tier capped at **500 unique symbols/month** |
| 7 | **QuantConnect free cloud** | ✅ recently-delisted names confirmed | ~1998 (AlgoSeek/QuantQuote lineage) | **Free** in cloud | No bulk free download anymore; ticker-reuse bug |
| 8 | **CRSP/Compustat via WRDS** | ✅ reference standard | 1926 (CRSP) | Institutional only | No individual tier; students free via university |
| 9 | **Intrinio** | ✅ 50k+ active+delisted securities | **2007** for delisted | $150/mo | ⚠️ Mergers get *spliced* into acquirer — avoid |
| 10 | **FMP** | Delisted roster only (free tier claimed) | — | Free tier | ❌ Could not verify free tier behaviour |
| 11 | **Alpaca** | ✅ bars yes, ❌ roster no | 2016 (SIP) / 2020 (IEX) | Free (IEX + delayed SIP) | `/v2/assets?status=inactive` is CUSIP-keyed, not a roster |

**Free roster files (no API key, verified):**
- Tiingo `supported_tickers.zip` → **10,401 US major-exchange delisted tickers** w/ start+end dates
- SEC EDGAR daily-index `form.*.idx` → complete, back to 1999 verified
- stockanalysis.com per-year HTML → roster exists (**~9,600 names 1998–2026**) but **only 50/year free**

---

## 1. QuantConnect / LEAN — free data library

### 1a. Does the FREE dataset serve delisted price bars? → **YES (partially verifiable)**

📄 **Security Master is delisting-aware and explicitly marketed as the survivorship-bias fix:**
- "The US Equity Security Master dataset by QuantConnect tracks US Equity corporate actions, including splits, dividends, **delistings**, mergers, and ticker changes through history. The data covers approximately **27,500 US Equities, starts in January 1998**."
- "Split, dividend, and **survivorship bias free** US Equities backtesting is enabled by the QuantConnect US Equity Security Master."
- https://www.quantconnect.com/docs/v2/writing-algorithms/datasets/quantconnect/us-equity-security-master
- https://www.quantconnect.com/data/quantconnect-us-equity-security-master

📄 **Universe doc quantifies the delisted population:**
> "The total number of stocks in the US Equity Security Master dataset is **30,000** but your coarse filter function won't receive all of these at one time because the US Equity Security Master dataset is **free of survivorship bias and some of the securities have delisted over time**. ... Currently, there are about **10,000 securities** that LEAN passes into your coarse filter function."
> — https://www.quantconnect.com/docs/v2/writing-algorithms/universes/equity/legacy-fundamental-universes

📄 **QuantConnect staff confirming price history exists for delisted names:**
- 2020 forum, QC staffer "Shile Wen": *"I was able to pull historical data for **LN**, which was recently delisted."* — https://www.quantconnect.com/forum/discussion/9987/delisted-historical-data-in-research-environment/
- Louis Szeto: *"LEAN will include past-delisted securities in universe selection, while removing the securities and liquidating your positions of them when they got delisted, so there is no survivorship bias."* — https://www.quantconnect.com/forum/discussion/14753/backtests-don-039-t-include-delisted-stocks/

📄 **Delisting events are queryable:**
```python
history = self.history[Delisting](self.add_equity("BBBY").symbol, timedelta(10*365))
# history.type == DelistingType.WARNING (0) or DelistingType.DELISTED (1)
```
— https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/asset-classes/us-equities

### 1b. How far back / how much
📄 Free cloud dataset history window by resolution:
| Resolution | Available history |
|---|---|
| Daily | **All historical data** |
| Hour | **All historical data** |
| Minute | 1 year |
| Second | 2 months |
| Tick | 1 month |
— https://www.quantconnect.com/docs/v2/cloud-platform/datasets/quantconnect/us-equities

❌ **COULD NOT VERIFY:** the number of delisted tickers in the free dataset, nor whether pre-2000 delisted names are present. The GitHub sample data is only 21 tickers (✅ verified via GitHub API: `aaa, aapl, aig, bac, bno, eem, fb, foxa, gooav, goocv, goog, googl, ibm, iwm, nwsa, qqq, spy, uso, uw, wm, wmi`) — i.e. **the open-source repo is NOT the full dataset**.

### 1c. Cost
📄 **Free in the cloud:** "The QuantConnect data provider serves US Equities data for free." (https://www.quantconnect.com/docs/v2/cloud-platform/datasets/quantconnect/us-equities)

### 1d. Download method
📄 **Bulk local download of the FREE dataset is no longer documented.** The LEAN CLI `US Equities` entry now points to the **paid AlgoSeek** dataset:
- `lean data download --dataset "US Equity Security Master"`
- `lean data download --dataset "US Equities" --data-type "Bulk" --resolution "Daily" --start ... --end ...`
- Requires "a member in an organization on a **paid tier**" — https://www.lean.io/docs/v2/lean-cli/datasets/quantconnect/us-equity
- "To use the CLI, you must be a member in an organization on a paid tier." — https://www.quantconnect.com/docs/v2/lean-cli/datasets/quantconnect/key-concepts
- Interactive wizard shows `US Equities | **AlgoSeek** | ...` — https://github.com/QuantConnect/Documentation/blob/master/05%20Lean%20CLI/05%20Datasets/05%20QuantConnect/01%20Key%20Concepts/03%20Using%20the%20CLI.html

**So the free path is: QuantConnect Cloud free tier (backtest / Research notebook), pull bars via `History()`.** There is also `lean backtest "X" --download-data` which auto-pulls via `ApiDataProvider` (still billed in QCC).

### 1e. Caveats
- **Ticker reuse breaks lookups.** "EAGL" = 4 different SPACs (2011, 2013, 2015, 2018); `AddEquity("EAGL")` returns only the 2018 one. — https://www.quantconnect.com/forum/discussion/11630/how-to-find-symbols-for-delisted-securities/
- **DELL 2000 is unrecoverable** by ticker: *"testing DELL in the year 2000 I receive an error message that says that the first trading date for DELL is 2018, the year it was re-listed."* — https://www.quantconnect.com/forum/discussion/12181/backtest-not-including-delisted-symbols/
- **Known bug:** daily History returns one extra bar past the delisting date. https://github.com/QuantConnect/Lean/issues/7740
- **Fundamentals are missing for many delisted names** (confirmed by user in the 2020 thread).
- The **Algo Framework** removes delisted holdings from `Securities` (see `Algorithm/QCAlgorithm.Framework.cs`); iterate `Securities.Total` to see them.
- ❌ No zip URL is published for the free dataset.

### 1f. Paid alternative (AlgoSeek) — the "complete dataset" if you pay
📄 "The US Equities dataset by AlgoSeek is **survivorship bias-free daily coverage of every stock traded in the US SIP CTA/UTP feed since 1998**. The dataset covers approximately **27,500 securities**, starts in **January 1998**... Over-the-Counter (OTC) trades are not included."
— https://www.quantconnect.com/data/algoseek-us-equities

| Item | Quant Researcher | Team | Trading Firm | Institution |
|---|---|---|---|---|
| Security Master (required) | $600/yr | $900/yr | $1,200/yr | $1,800/yr |
| US Equities **Daily** bulk | $2,136/yr | $3,480/yr | $3,480/yr | $3,480/yr |
| US Equities Hour bulk | $2,136/yr | $3,480/yr | $3,480/yr | $3,480/yr |
| US Equities Minute bulk | $11,760/yr | $16,800/yr | $31,200/yr | $43,200/yr |
| Updates | $600/yr | $840/yr | $1,440/yr | $2,640/yr |

By-ticker (QCC, $0.01/QCC): **Daily $1.00/file**, Hour $3.00/file, Minute $0.05/file, Second $0.05, Tick $0.06.
→ **~$10,400 buys you a full, genuinely survivorship-free daily US panel 1998–present.** Daily files are 1 per ticker, 2 GB total. — https://www.lean.io/docs/v2/lean-cli/datasets/quantconnect/us-equity

---

## 2. Tiingo

### 2a. Does it serve delisted bars? → Roster: ✅ YES, verified live. Prices: 📄 likely, ❌ not verified.

**✅ VERIFIED — free, no-auth, 796 KB file:**
```
GET https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip
```
CSV columns: `ticker, exchange, assetType, priceCurrency, startDate, endDate`

My measurements (2026-09-28):
- **108,846 rows total**
- Exchange mix: NMFQS 49,827 · PINK 20,609 · NASDAQ 11,037 · NYSE 9,302 · SHE 3,805 · SHG 3,446 · OTCMKTS 2,138 · BATS 2,051 · NYSE ARCA 1,565 · OTCGREY 1,553 · EXPM 927 · OTCBB 602 · OTCQB 455 · AMEX 383 · NYSE MKT 231 · OTCQX 204
- Asset types: Mutual Fund 49,878 · Stock 49,226 · ETF 9,742
- **42,858 rows have `endDate` < 2026-09-01** (i.e. delisted/inactive)
- **10,401 of those are on major US exchanges** (NYSE / NASDAQ / NYSE ARCA / AMEX / BATS)
- Delisted US by `endDate` decade: **2020s 6,569 · 2010s 3,783 · 2000s 48 · 1990s 1**
- Earliest `startDate` among US delisted: 1970-01-02, 1970-01-02, 1972-06-01 … (but very few pre-2000)

Spot checks I ran:
| Ticker | Result |
|---|---|
| `ATVI` | ✅ 1993-10-25 → **2023-10-13** (correct, acquired by Microsoft Oct 2023) |
| `WFM` | ✅ 1992-01-23 → **2017-08-29** (Whole Foods/Amazon) |
| `PVTB` | ✅ 1999-06-30 → 2017-06-30 |
| `FRCB` | ⚠️ present but as `PINK` (OTC continuation), startDate 2010-12-09 |
| `SIRI` | ⚠️ **two rows** — a 1-day stub 2024-09-09→2024-09-09, and a long 1994-09-13→2026-09-25 |
| `S` | ⚠️ **two rows** — 1984-11-08→2020-04-01 and 2021-06-30→2026-09-25 |
| `SPLS` | ⚠️ two rows (BATS ETF + NASDAQ Stock) |
| `AAC` | ⚠️ two rows (2014-2021, 2021-2023) |
| `LEHM`, `LEHKQ`, `ENRNQ`, `MYLN` | ❌ **absent** — the marquee pre-2005 blowups are missing |

📄 **The `permaTicker` mechanism for recycled/delisted symbols:**
- `GET /tiingo/utilities/search/<query>` returns `ticker`, `name`, `assetType`, **`isActive`** (boolean, "false if the ticker is no longer actively quoted (delisted)"), **`permaTicker`**, `openFIGI`
- `GET /tiingo/fundamentals/meta` returns `permaTicker`, `ticker`, `name`, `isActive`, `isADR`
- Fundamentals endpoints: *"Requests historical daily fundamental data for **active symbols using the ticker, or for delisted/recycled symbols using the Tiingo permaTicker**"*
- https://context7.com/websites/tiingo/llms.txt (mirror of tiingo.com docs)

❌ **COULD NOT VERIFY:** whether `GET /tiingo/daily/{ticker}/prices` returns bars for a delisted ticker by plain ticker vs requiring `permaTicker`. Tiingo's doc pages are JS-rendered and I have no API key. The permaTicker language above is stated for *fundamentals*; do not assume it for EOD prices.

### 2b. History depth
📄 Tiingo's own blog: *"end-of-day history back to 1962"*, *"30+ years of price history"*. — https://www.tiingo.com/blog/best-stock-price-api/
⚠️ **CONFLICT:** the pricing page's feature matrix shows `History | 5 Years | 15+ Years` for the two columns. — https://www.tiingo.com/about/pricing
❌ Unresolved.

### 2c. Cost (📄 https://www.tiingo.com/about/pricing + blog)
- **Free / Starter: $0** — 500 unique symbols/**month**, 50 req/hr, 1,000 req/day, 1 GB bandwidth, 30+ yrs price history, 5 yrs fundamentals, 500 req/min
- **Power (individual): $30/mo or $300/yr** — *"Flat-rate, no per-symbol metering"*; "unlimited unique symbol API calls, unlimited saved screens, 15 custom indicators"
- **Commercial: $50/mo or $499/yr**
- **Fundamental Data API = separate add-on** via a third-party provider (contact sales)
- ⚠️ The 500-symbols/month free cap is the binding constraint: 10,401 delisted US names ≈ **21 months** of free-tier crawling. Paid tiers remove it.

### 2d. Endpoints
```
https://api.tiingo.com/tiingo/daily/{ticker}/prices?startDate=YYYY-1-1&format=csv   # fastest bulk path
https://api.tiingo.com/tiingo/daily/prices                                          # latest-day delta for updates
https://api.tiingo.com/tiingo/utilities/search/{query}
https://api.tiingo.com/tiingo/fundamentals/meta
https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip                 # free roster
```
✅ Verified the `&format=csv` trick and the two-step "re-download whole history if splitFactor≠1" workflow from Tiingo's own KB: https://www.tiingo.com/kb/article/the-fastest-method-to-ingest-tiingo-end-of-day-stock-api-data/

### 2e. Caveats
- **IEX vs "eod" tier:** the *free/paid* distinction is about symbols-per-month and fundamentals, **not** about delisted access. IEX is a *real-time* feed, unrelated to delisting. There is no "IEX excludes delisted" effect.
- Reused tickers produce **duplicate rows**; the file has no permaticker column, so you cannot unambiguously bind a row to a security. Cross-reference with a permaticker source.
- The file has **165 rows with an empty `exchange`** — filter these out.
- Pre-2005 mega-failures (LEHM, ENRNQ, MYLN) are **absent**.
- No "delisted flag endpoint" per se — `isActive` exists on the *search* and *fundamentals/meta* endpoints, not on a bulk listing.

---

## 3. EOD Historical Data (EODHD)

### 3a. Serves delisted price bars? → ✅ **YES, explicitly designed for this**
📄 "When a company is acquired, goes bankrupt, or otherwise leaves an exchange, its ticker is delisted, but its historical data does not disappear from EODHD. You can still list delisted symbols and pull their end-of-day prices, fundamentals, dividends, and splits. **This is essential for survivorship-bias-free backtesting** and long-horizon research."
— https://eodhd.com/financial-apis/delisted-stock-companies-data-2

### 3b. History depth
📄 "We have a solution that covers **26,000+ US stock tickers (mostly from Jan 2000)** and 42,000+ non-US tickers." — https://eodhd.com/financial-academy/financial-faq/historical-stock-prices-for-delisted-companies
📄 "the US list alone returns **tens of thousands** of symbols across stocks, ETFs, and funds."

### 3c. Cost (📄 https://eodhd.com/pricing, https://eodhd.com/llms.txt)

| Plan | Monthly | Annual | Calls/day | Data range |
|---|---|---|---|---|
| Free | $0 | $0 | **20/day**, 50 req/min | **Past year only** |
| EOD Historical Data — All World | $19.99 | $199.00 | 100,000 | 30+ years |
| EOD+Intraday — All World Extended | $29.99 | $299.90 | 100,000 | 30+ years |
| Fundamentals Data Feed | $59.99 | $599.90 | 100,000 | 30+ years |
| **All-In-One** | $99.99 | $999.90 | 100,000 | 30+ years |
| All-In-One Extended | $119.99 | — | 100,000 | 30+ years |

- 500 welcome bonus API calls on all paid plans. **50% academic discount.**
- Add-ons: Corporate Events Calendar + News $19.99/mo · US Ticks $9.99/mo · US Options $29.99/mo · **Indices Historical Constituents $29.99/mo** (useful!) · Stock logos $299/yr

⚠️ **The free plan's "Past year" data range is a hard blocker**: a company delisted in 2019 will return nothing on the free plan. And the docs warn (see 3e).

### 3d. Endpoints (📄 all verified in docs)
```bash
# STEP 1 — the delisted roster (call TWICE and merge on Code: delisted=1 REPLACES, not extends)
GET https://eodhd.com/api/exchange-symbol-list/US?delisted=1&type=common_stock&api_token=KEY&fmt=json
GET https://eodhd.com/api/exchange-symbol-list/US?delisted=0&...&fmt=json

# STEP 2 — the data
GET https://eodhd.com/api/eod/{TICKER}?from=YYYY-MM-DD&to=YYYY-MM-DD&api_token=KEY&fmt=json
GET https://eodhd.com/api/fundamentals/{TICKER}   # General.IsDelisted, General.DelistedDate
GET https://eodhd.com/api/div/{TICKER}
GET https://eodhd.com/api/splits/{TICKER}
GET https://eodhd.com/api/intraday/{TICKER}
GET https://eodhd.com/api/symbol-change-history?from=YYYY-MM-DD&to=YYYY-MM-DD&ex=US   # US only, renames not delistings
```

### 3e. Coverage gaps / caveats — **READ THIS**
1. ⚠️ **Availability is time-gated by delisting date:**
   | Delisted | Available data |
   |---|---|
   | After **2018** | EOD, Fundamentals, Dividends, Splits |
   | After **2021** | all of the above, **plus Intraday** |
   | **Before 2018** | **EOD ONLY** |
   — https://eodhd.com/financial-apis/delisted-stock-companies-data-2
   → If you need dividends/fundamentals for the 2008–2018 delisting cohort, EODHD alone is not enough.
2. ⚠️ **Default window is 120 days:** *"without the '&from=' and '&to=' parameters specified, the length of the data obtained includes the previous 120 days, API requests for tickers that were delisted earlier won't return the data."* → **You must pass explicit from/to.**
3. ⚠️ **`_old` ticker convention for reused symbols:** "EODHD system marks tickers with the index 'old'. For example **ACR_old.US** was traded as ACR before delisting and now another company trades under ACR.US." — https://eodhd.com/financial-academy/financial-faq/survivorship-bias-free-financial-analysis
4. `delisted=1` and `delisted=0` sets **do not overlap**; you must call twice and merge.
5. **No pagination** — whole exchange in one response. US JSON ≈ **7.7 MB**; EUFUND considerably more. Stream/buffer accordingly.
6. Filters combine with **AND**; unknown `symbols` are **silently skipped** with HTTP 200 (no error).
7. `symbols` can be used as a cheap coverage probe: `&symbols=LEHM,MYLN`.
8. Bad exchange code → HTTP 404 "Exchange Not Found." Unsupported `type` → HTTP 422.
9. Omitting `fmt` returns **CSV**, not JSON.
10. ❌ **I could not test with a token** — `api_token=demo` returns HTTP `Forbidden` on `/exchange-symbol-list` and `/eod`.

---

## 4. Polygon.io → **Massive** (rebranded)

📄 "All Polygon.io endpoints continue to work as-is. Massive is the same platform under a new name." — https://massive.com/stocks

### 4a. Flat files include delisted? → 📄 **YES, explicitly**
- "across **32,345+ active and delisted tickers**" — https://massive.com/stocks
- "Our archive goes back over two decades, **free of survivorship bias — delisted tickers keep their full history**, so backtests see the market as it actually was."
- "Our market data is **point-in-time**, meaning the data is returned as it happened on a specific date." — https://massive.com/knowledge-base/categories/faq
- "What does Massive do with delisted tickers?" — https://massive.com/knowledge-base/categories/faq
- Corporate actions (splits, dividends, IPOs) back to **2008**.
- Independent corroboration: *"All Polygon (Massive) data is point-in-time by ticker... Polygon does not have this [ticker-reuse data loss] problem."* — https://github.com/shinathan/polygon.io-stock-database

### 4b. History depth (📄 https://massive.com/knowledge-base/article/how-much-historical-stock-data-does-massive-have)
> "Everything starts on **10 September 2003**: aggregate bars, tick trades and NBBO quotes all begin that day for listed US stocks. OTC history is much shorter, starting **31 December 2021**."

| Data | Earliest | Endpoint |
|---|---|---|
| Aggregate bars (1s and up) | 2003-09-10 | `/v2/aggs/ticker/{ticker}/range/...` |
| Tick trades | 2003-09-10 | `/v3/trades/{ticker}` |
| Tick NBBO quotes | 2003-09-10 | `/v3/quotes/{ticker}` |
| Any OTC ticker | 2021-12-31 | same endpoints |

### 4c. Pricing (✅ verified from https://massive.com/pricing)

| Plan | $/mo | History | Flat Files | Other |
|---|---|---|---|---|
| **Stocks Basic** | **$0** | **2 years** | ❌ Not included | 5 calls/min |
| **Stocks Starter** | **$29** | **5 years** | ✅ Included | 15-min delayed, min aggs, WS, snapshots, 2nd aggs |
| **Stocks Developer** | **$79** | **10 years** | ✅ Included | + Trades |
| **Stocks Advanced** | **$199** | **20+ years** | ✅ Included | + Quotes, Financials & Ratios |

⚠️ All individual-use, non-pros only. ⚠️ **CONFLICT:** the comparison table on https://massive.com/stocks says "S3-compatible bulk files on **Advanced**" and "Add-on priced per GB" for older history, while the pricing page lists Flat Files on Starter/Developer too. **Treat flat-file history as capped by your plan's window** and confirm with Massive before budgeting.

### 4d. Exact URL structure (📄 https://massive.com/knowledge-base/article/how-to-get-started-with-s3)
```
Endpoint: https://files.massive.com
Bucket:   flatfiles
Prefixes: us_stocks_sip · us_options_opra · us_indices · global_forex · global_crypto
```
```bash
aws s3 ls s3://flatfiles/ --endpoint-url https://files.massive.com
aws s3 cp s3://flatfiles/us_stocks_sip/trades_v1/2024/03/2024-03-07.csv.gz . --endpoint-url https://files.massive.com
```
Stocks datasets (from https://massive.com/docs/flat-files/stocks/overview):
```
us_stocks_sip/trades_v1/{YYYY}/{MM}/{YYYY-MM-DD}.csv.gz
us_stocks_sip/quotes_v1/{YYYY}/{MM}/{YYYY-MM-DD}.csv.gz
us_stocks_sip/minute_aggs_v1/{YYYY}/{MM}/{YYYY-MM-DD}.csv.gz
us_stocks_sip/day_aggs_v1/{YYYY}/{MM}/{YYYY-MM-DD}.csv.gz   # ← what you want
```
Day-agg columns: `close, high, low, open, ticker, transactions, volume, window_start` (ns unix).
rclone / `mc` / boto3 examples all in the KB article. **One file per session containing every ticker** → delisted names appear naturally on the days they traded.

**Delisted roster (API, not flat file):**
```
GET https://api.massive.com/v3/reference/tickers?market=stocks&active=false&date=YYYY-MM-DD&order=asc&limit=1000&sort=ticker&apiKey=KEY
```
Response fields: `active` ("False means the asset has been **delisted**"), **`delisted_utc`** ("The last date that the asset was traded"), **`cik`**, `composite_figi`, `share_class_figi`, `primary_exchange`, `type`, `locale`, `last_updated_utc`. The `date` param gives you a **point-in-time roster**. (📄 https://massive.com/docs/rest/stocks/tickers/all-tickers)

### 4e. Caveats
- ⚠️ "**All stock Flat Files contain unadjusted data.** Prices and volumes are not adjusted for stock splits, dividends... via REST API by setting `adjusted` to `true`, **or apply adjustments manually**." Splits-only; **no dividend adjustment**.
- ⚠️ Their FAQ: "We support historical market data that is adjusted for **splits, but not dividends**."
- Ticker-reuse: point-in-time by ticker means you must handle renames yourself; the `delisted_utc` + CIK crosswalk is your tool.
- CUSIP is **not returned** "due to legal reasons."
- Flat file data available ~11:00 AM ET the following day.

---

## 5. Norgate Data

### 5a. Serves delisted bars? → ✅ **YES, but only Platinum and above**
📄 "We specialize in **survivorship bias-free data** for US, Australian and Canadian stock markets." — https://norgatedata.com/
📄 "Delisted Securities" is a row in the US package comparison table, checked only for Platinum and Diamond. — https://norgatedata.com/stockmarketpackages.php

### 5b. Coverage
📄 "For the security types included in our US Listed and Delisted data set, coverage is considered to be **essentially complete back to late 1992**. We are not aware of any material coverage gaps from late 1992 onward." — https://norgatedata.com/data-package-faq.php
📄 "containing **25,222 delisted securities** from the start of 1950 to Sep 2022" — https://norgatedata.com/data-content-tables.php
📄 Daily-snapshot: NYSE Composite index starts Jan 1995; Russell Top 200 July 1995; "delisted stocks replaced at reconstitution". — https://norgatedata.com/data-content-tables.php

### 5c. Cost (📄 https://norgatedata.com/stockmarketpackages.php)

| US Package | History (listed) | **Delisted?** | OTC? | PIT index constituents? | 6-mo | 12-mo |
|---|---|---|---|---|---|---|
| Silver | last 10 yrs | ❌ | ❌ | ❌ | $148.50 | **$270** |
| Gold | last 20 yrs | ❌ | ❌ | ❌ | $198.00 | **$360** |
| **Platinum** | **back to 1990** | ✅ | ✅ | ✅ | $346.50 | **$630** |
| **Diamond** | **back to 1950** | ✅ | ✅ | ✅ | $433.13 | **$787.50** |

- US OTC add-on: $49.50 / 6mo, **$90 / 12mo**
- **Free trial: 3 weeks at Platinum level** (includes delisted + historical index constituents) **but history is limited to 2 years** and they will not customize. — https://norgatedata.com/data-package-faq.php
- World Indices included with all stock packages.

### 5d. Access method (📄)
- **Norgate Data Updater (NDU)** desktop app syncs a **local database**; then the free `norgatedata` Python package reads it locally.
- NDU databases: `US Equities` and `US Equities Delisted` (must both be marked Active).
- Plugin integrations: AmiBroker, RealTest, Wealth-Lab, **Zipline**, and other supported environments.
- `norgatedata.price_timeseries(symbol, stock_price_adjustment_setting=StockPriceAdjustmentType.TOTALRETURN, padding_setting=PaddingType.NONE, start_date=, end_date=, timeseriesformat='pandas-dataframe')`
- `norgatedata.database_symbols('US Equities')` / `('US Equities Delisted')`
- ASCII / MetaStock "Legacy" export of **price data only**.
- Free Python walkthrough: https://concretumgroup.com/how-to-construct-a-survivorship-bias-free-database-in-norgate-using-python/

### 5e. Caveats — this is the most thoughtfully-designed source, read carefully
- ✅ **Symbol convention for delisted:** "The symbol for a delisted security has a year & month suffix indicating when the security last traded — e.g. `ALOG-201806`, `NAV-202106`, `ENRNQ-200411`." Prevents overlap.
- ✅ **Norgate's definition of delisted is stricter than most vendors:** delisted = *no longer tradeable at all*. **Downlisting from NYSE to OTC is NOT a delisting** — they keep covering the OTC period. "Beware of inferior (non-Norgate) source data that determines a 'delisting' to mean that it is no longer listed on a major exchange."
  - Enron: `ENE` on NYSE to 2002-01-11, then `ENRNQ` on OTC, then `ENRNQ-200411`. Navistar: NYSE → OTC early 2007 → back to NYSE mid-2008 → delisted 2021 as `NAV-202106`.
- ✅ **`Major Exchange Listed` indicator** (Platinum+, back to 1990) gives a daily 1/0 for major-exchange-listed vs OTC — the cleanest way to build a "listed-only" universe.
- ✅ **Historical index constituents are daily and intra-period** — "Beware - some other providers only provide an inferior month-end or quarter-end variety, which has their own lagging/look-ahead issues." (Platinum+)
- ⚠️ **Excluded from the Delisted database:** OTC-only common stocks, liquidation trusts, litigation trusts, special investment products, ETDs, **preference shares**, convertible preference, corporate units, **warrants**, **rights**, "when-issued" securities, parallel-trading short-life securities.
- ⚠️ Included in Delisted: operating/holding companies, CEFs, BDCs, MLPs, royalty trusts, **blank-check companies/SPACs**, ETFs, ETNs.
- ⚠️ "A stock exists in our delisted database under its **last known company name and ticker** and not under any previous names or tickers. A stock only has one history." → the America Online/AOL lifecycle is a documented case where this bites.
- ❌ Subscription-only; historical data is not sold stand-alone. 6- or 12-month terms, no monthly, no auto-renew.
- ❌ No intraday/tick data at all.

---

## 6. Sharadar (Nasdaq Data Link) — SEP + SF1

### 6a. Serves delisted bars? → ✅ **YES**
📄 **SEP (Sharadar Equity Prices)** — https://sharadar.com/prices
> "The prices dataset corresponds to our fundamentals dataset and provides historical **End-of-Day (EOD) prices for 21,000 stock tickers and 10,000 fund tickers** in the United States... **The data includes active and delisted securities, minimizing survivorship bias**."
> "End-of-day (EOD) Open, High, Low, Close and Volume (OHLCV) price data for **active and delisted US public stocks**. Provided with **3 different adjustment methods**: (1) unadjusted; (2) stock-split adjusted; (3) split + cash dividend + spinoff adjusted. **Current count of 21,000 tickers. Deep history to 1998.**"
> Dataset code: https://data.nasdaq.com/databases/SEP

📄 **SF1 (Core US Fundamentals)** — https://sharadar.com/fundamentals
> "**Nearly 18,000 tickers, including 12,000 delisted tickers** · **99% survivorship bias free** · History back to **January 1998** · Point-in-time dimension to data with time-indexing to the filing date"
> "150+ financial indicators · Includes both as-reported and restated values in continuous time series"
> Dataset code: https://data.nasdaq.com/databases/SF1

### 6b. **The securities master has an explicit delist flag** — this is the field you want
📄 The Sharadar reference/securities-master table includes:
| Field | Code | Type |
|---|---|---|
| Permanent Ticker Symbol | `permaticker` | text |
| Ticker Symbol | `ticker` | text |
| Is Delisted? | **`isdelisted`** | **Y/N** |
| First Price Date | `firstpricedate` | date |
| Last Price Date | **`lastpricedate`** | date |
| SEC Filings URL | `secfilings` | text |
| CUSIPs | `cusips` | text |
| Composite FIGI | `figi` | text |
| Category / SIC / Sector / Industry | | |
Also 📄 **corporate actions** table: *"Includes ticker changes, stock splits, cash dividends, spinoffs, ADR ratio changes, **listing dates, delisting dates, delist reasons, acquisition counterparties**, and relations between different securities from the same issuer. Deep history to 1998."*
Daily fundamentals table: "Deep history to **1996**."

### 6c. Cost
📄 **Free sample:** *"Our entry level dataset is completely **FREE** and includes company fundamentals and market data for all **30 Dow Jones Industrial Average** companies."* + *"You can make the following queries below to get 5 years of history for AAPL (no sign-up required)."* — https://sharadar.com/
📄 Old fact sheet: *"Free data sample available. Contact us to find out more."* — https://resources.quandl.com/a/res-hub/Sharadar_Datasheet_final.pdf
❌ **COULD NOT VERIFY:** the current paid price for SEP or SF1. I found no public price list on the pages fetched. Nasdaq Data Link sells SEP/SF1 as per-product subscriptions; **request a quote from Sharadar or check data.nasdaq.com/databases/SEP "Premium"/"Individual" tabs directly.**

### 6d. Endpoints
```bash
# Sharadar direct API (auth = api_key)
GET https://api.sharadar.com/v1.0/data/{table}?api_key=KEY&ticker=AAPL&format=csv
GET https://api.sharadar.com/v1.0/data/daily?api_key=KEY&ticker=AAPL&format=csv
GET https://api.sharadar.com/v1.0/data/daily?api_key=KEY&years=full   # 5 | 10 | full → 302 to time-limited ZIP
# query params: fields, ticker (comma-sep), tablename, fromdate, todate, lastupdated.gte, limit (10000), skip, sort
# 📄 docs use api_key=test-api-key in public examples
```
— 📄 https://sharadar.com/docs/daily

**Nasdaq Data Link / Quandl (legacy but live) format:**
```python
import quandl
quandl.ApiConfig.api_key = KEY
quandl.get_table('SHARADAR/SEP', ticker='AAPL')     # OHLCV, active + delisted
quandl.get_table('SHARADAR/SF1', ticker='AAPL')     # 150+ indicators
quandl.get_table('SHARADAR/SEP', ... )['isdelisted'] # securities master flag
```
`SEP` / `SF1` / `DAILY` / `CORPActions` / `DIM_SECURITY` are the tables. 14 tables total.

### 6e. Caveats
- ⚠️ **"99% survivorship bias free"** — 1% is missing; Sharadar does not enumerate which.
- ⚠️ Coverage is **primary class of common stock** in SF1; SEP additionally covers secondary classes, preferred, and listed commons that never filed with the SEC ("primarily a historical foreign filer phenomenon").
- ⚠️ SEP/SF1 cover **Nasdaq, NYSE, NYSEMKT** (funds also NYSEARCA, BATS). **No OTC.**
- ⚠️ "(1) and (3) require full imputation for full OHLCV" — see their FAQs.
- ⚠️ Historical numbers in Sharadar's own marketing have drifted: fact sheet says "more than 5,000 active and 9,000 delisted" / "more than 16,000 active and delisted tickers" and "History from 1997"; the current site says 18,000/12,000 and 1998. Use the current numbers.

---

## 7. Intrinio

### 7a. Serves delisted bars? → ✅ **YES, with a documented 2007 floor**
📄 "**Delisted securities are available back to 2007.**" — https://intrinio.com/docs/market-data/security-reference-data
📄 Starter plan history line: **"Stock Prices: 1967 to Present for Actively Trading Securities | 2007 to Present for Delisted Securities"** — https://intrinio.com/guides/starter-plan
📄 Coverage: **"10,000+ Active and Delisted Companies and 50,000+ Active and Delisted Securities"** — https://intrinio.com/guides/starter-plan

### 7b. Endpoints (📄 https://docs.intrinio.com/documentation/web_api/get_all_securities_v2)
```bash
# Roster — `active` AND `delisted` are BOTH required params
GET https://api-v2.intrinio.com/securities?active=true&delisted=true&api_key=KEY
GET https://api-v2.intrinio.com/securities?active=false&delisted=true&api_key=KEY   # delisted only
#   optional: stock_prices_after=YYYY-MM-DD, stock_prices_before=YYYY-MM-DD
#   paginate with the `next_page` token

# Per-security
GET https://api-v2.intrinio.com/securities/{identifier}?api_key=KEY
#   → active, etf, delisted, primary_listing, primary_security,
#     first_stock_price, last_stock_price, last_corporate_action, previous_tickers[],
#     figi, composite_figi, share_class_figi, cik, ticker, composite_ticker

# Prices
GET https://api-v2.intrinio.com/securities/{identifier}/prices?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
GET https://api-v2.intrinio.com/stock_exchanges/{MIC}/prices?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
```
✅ **The by-exchange endpoint is the best PIT-universe tool I found anywhere:**
> "How can I filter out delisted securities when searching EOD prices for the market? Use the prices by exchange endpoint. **This endpoint returns only those securities that traded on the specific day requested.** For example, if you pull prices for the universe on October 15, 2013, the endpoint would return only those securities that actually traded that day."
> — https://help.intrinio.com/eod-market-data-faqs

### 7c. Cost (📄 https://intrinio.com/pricing)
| Plan | Price | Notes |
|---|---|---|
| **Individual** | **$150/mo** | AI-ready, personal use, 1 seat |
| **Startup** | **$333/mo → $666 → $999** (billed quarterly) | Commercial use + display rights |
| **Enterprise** | $1,250/mo+ | Custom feeds, SLA |
| **Free trial** | 2 weeks, no card | **Includes "10,000+ Active & Delisted Securities", US & OTC EOD, 10 yrs history, IEX real-time** |
- ❌ No permanent free tier.

### 7d. Caveats — ⚠️ **the most important one**
> "**If a company undergoes a merger, we use the security master to connect the full price history. We take the old, delisted security and add that pricing history to the new security. You shouldn't notice any separation when querying historical data from our system.**"
> — https://help.intrinio.com/stock-price-adjustments-ipos-and-mergers

**This is bad for your use case.** Intrinio *splices* the delisted security's history onto the acquirer. For a survivorship-bias study you want the delisted security to die on its own last trading day. Use the securities master to detect mergers and re-split the series yourself, or use a different vendor.
- ⚠️ "Our API does not allow calls for multiple tickers" — you must loop per security (slow for 50k names).
- ⚠️ EOD feed is "official, EOD pricing data through our data partner EDI. **It does not include any pre- or post-market trading.**"
- ⚠️ IPO-ing companies may be `active` but have no fundamentals until their first 10-Q.

---

## 8. Financial Modeling Prep (FMP)

### 8a. Delisted flag / stable symbol list? → ✅ **Yes, a dedicated endpoint**
📄 **Delisted Companies API**
```
GET https://financialmodelingprep.com/stable/delisted-companies?page=0&limit=100
```
> "The output lists the **Symbol**, the **Company Name**, and the specific **Exchange** it traded on. Crucially, it provides both the **Ipo Date** and the **Delisted Date** ... giving you the exact window of time the asset was publicly available."
> — https://site.financialmodelingprep.com/how-to/how-to-handle-delisted-companies-and-historical-symbols-with-a-free-api

Related endpoints: `/stable/stock-list` (80,000+ symbols), `/stable/search-symbol`, `/stable/search-name`, and a **Stock Symbol Change History** endpoint (mergers, acquisitions, splits, name changes).

### 8b. Cost
📄 **"Yes, both the active Company Symbols List and the Delisted Companies endpoints are accessible via the free plan, making it easy to audit your databases without a paid subscription."** — https://site.financialmodelingprep.com/how-to/how-to-handle-delisted-companies-and-historical-symbols-with-a-free-api
❌ **COULD NOT VERIFY.** My requests both failed:
```
GET https://financialmodelingprep.com/stable/delisted-companies?page=0&limit=5
→ {"Error Message":"Invalid API KEY. Feel free to create a Free API Key ..."}
GET https://financialmodelingprep.com/api/v3/delisted-companies?apikey=demo
→ {"Error Message":"Invalid API KEY..."}
```
Both a registered free key and a paid tier are required. ❌ FMP's current price list also unverified.

### 8c. Caveats
- ⚠️ "The standard endpoint provides the dates and the exchange, but **not the specific narrative reason** (e.g. bankruptcy vs. merger)."
- ⚠️ Global (ticker suffixes `.DE` XETRA, `.HK` Hong Kong) — filter to US.
- ⚠️ No stable/permanent ticker — same ticker-reuse ambiguity as everyone else except Norgate/Sharadar/Tiingo.
- ⚠️ FMP has been migrating v3 → `stable` routes; both appear documented.

---

## 9. CRSP / Compustat via WRDS

### 9a. Serves delisted bars? → ✅ **Yes — this is the academic reference standard**
📄 NYU library guide: *"CRSP ... maintains the most comprehensive collection of security price, return, and volume data for the NYSE, AMEX and NASDAQ stock markets."*
📄 DU library guide: *"**Compustat** — 'Provides global market information on **active and inactive** publicly held companies.'"* — https://libguides.du.edu/c.php?g=90412&p=583151

### 9b. Cost
📄 **"WRDS Subscription Fees — Please contact wrds@wharton.upenn.edu for pricing information."** — https://wrds-www.wharton.upenn.edu/pages/about/what-wrds/
📄 *"There is **no individual free tier**"*; institutional subscription only.
❌ No public price list. (A 2015-era EconJobRumors thread claims ~$100k/yr for CRSP+Compustat+TR Mutual Funds at a large school — **10 years old, anecdotal, unverified**; treat as rough order of magnitude only.)

### 9c. Student access — 📄 **free, via your university**
- **"Master's/Undergrad accounts are available to all full-time master's and undergraduate students of subscribing institutions."** Unlimited via web, SSH, FTP. **No disk storage.** Default expiry **2 years**. Accounts are **disabled between semesters** and auto-re-enabled when classes resume.
- **Class accounts:** set up by a faculty member + approved by the institution's WRDS Rep; students get a unique "Class Code."
- Faculty/PhD: personal accounts at **no cost to the researcher** (institution pays). https://guides.nyu.edu/wrds/faqs
- ⚠️ **Undergraduates are often restricted** — e.g. NYU: *"Almost all undergraduates must use the WRDS 'Day Pass' access option."*
- ⚠️ DU: *"DU's subscription does not allow you to use the data downloaded from this database for any non-academic or commercial endeavor."* — **check your own institution's terms before commercial use.**

### 9d. Notes
- CRSP data is a **point-in-time** CRSP-returns panel (delisting returns encoded), i.e. the standard against which everything else is measured.
- QuantRocket's framing: *"What the free set does not replace: survivorship-bias-free long CRSP daily returns, full Compustat history with point-in-time discipline, IBES detail, and OptionMetrics."* — https://instituteforautomatedresearch.org/wiki/licensed/wrds/

---

## 10. Alpaca

### 10a. `/v2/assets?status=inactive` — what it actually returns (📄 + verified-in-forum evidence)
📄 **Schema** (https://docs.alpaca.markets/us/reference/get-v2-assets-1):
> `status`: *"active or inactive"* · `tradable`: bool · `id`: UUID · `class`, `exchange`, `symbol`, `name`, `fractionable`, `marginable`, `shortable`, `maintenance_margin_requirement`, `attributes[]`
> *"By default, all statuses are included."* Param: `?status=inactive`

📄 **The critical caveat — it is a CUSIP-lifetime list, not a delisted roster.** Alpaca staff, forum thread "GetAssets not returning distinct values" (2024-05-31):
> *"**There may be multiple instances of a symbol but only one (or none) will have an active status.** ... **Every time there is a CUSIP change a new asset is created.** Many corporate actions such as stock splits will create a new CUSIP but retain the same symbol."
> — https://forum.alpaca.markets/t/getassets-not-returning-distinct-values/14330

Verified examples in that thread (✅ read the actual JSON):
- `MSP` → **two rows, both `status: "inactive"`**, different UUIDs, one `fractionable: false` / one `true`
- `ARRY` → **one `inactive` + one `active`**, same name

📄 **And it is INCOMPLETE as a roster** (forum, 2022-01-20):
> *"I see that `https://api.alpaca.markets/v2/assets` exists, **but it does not contain WFM**."*
> — https://forum.alpaca.markets/t/is-there-a-way-to-get-a-list-of-supported-symbols-by-the-historical-api-also-how-are-mergers-handled-in-the-historical-data/8082

### 10b. Do the SIP/IEX bar endpoints return history for delisted symbols? → ✅ **YES**
📄 Same forum thread, the author's own working example:
> *"However, `https://data.alpaca.markets/v2/stocks/WFM/bars` **returns historical data for WFM with 2017-08-25T04:00:00Z as the last date.**"*

(WFM = Whole Foods, acquired by Amazon 28 Aug 2017 — the returned last bar matches.) Alpaca's own answer in that thread was to use Polygon for a delisted asset list.

**So: use Alpaca for *prices* of a known delisted ticker; do NOT use `/v2/assets` for the *roster*.**

### 10c. Endpoints
```bash
GET https://api.alpaca.markets/v2/assets?status=inactive&asset_class=us_equity
GET https://api.alpaca.markets/v2/assets/{symbol}

# Data host:
GET https://data.alpaca.markets/v2/stocks/{symbol}/bars?start=&end=&timeframe=1Day&limit=10000&feed=sip&adjustment=all
GET https://data.alpaca.markets/v2/stocks/bars?symbols=A,B,C&start=&end=&timeframe=1Day&feed=sip&page_token=
GET https://data.alpaca.markets/v2/stocks/trades/{symbol}   · /quotes/{symbol}
GET https://data.alpaca.markets/v1beta1/historical/corporate-actions/{symbol}   # dividends/splits
GET https://data.alpaca.markets/v2/stocks/{symbol}/auctions
```

### 10d. History depth & cost
📄 https://docs.alpaca.markets/us/docs/about-market-data-api
| | Basic (free) | Algo Trader Plus |
|---|---|---|
| Pricing | **$0** | **$99/mo** |
| Real-time coverage | IEX only | All US exchanges |
| **Historical data timeframe** | **Since 2016** | **Since 2016** |
| Historical API calls | 200/min | 10,000/min |
| WebSocket symbols | 30 | Unlimited |
| SIP historical | ✅ if `end` is ≥15 min old | ✅ unrestricted |

📄 **Free tier CAN get SIP historical data** — "if you are using the free market data then only historical SIP data can be fetched and only IEX 'real time' data is available. 'Real time' in this case is anything more current than the past 15 minutes." So set `feed=sip` **and** an `end` at least 15 minutes in the past. (Forum: https://forum.alpaca.markets/t/paper-account-data-query-denied/13822)
📄 **IEX bar data only from 2020; SIP from 2016** (forum: https://forum.alpaca.markets/t/cannot-get-historical-bar-data-before-2020/12415).
⚠️ 2016 floor means Alpaca alone cannot cover 2000–2015.

### 10e. Caveats
- ⚠️ **No merger/delisting list.** Alpaca's forum answer to the survivorship question was "use Polygon." Corporate Actions is oriented to *your positions*, not to a historical delisting census.
- ⚠️ CUSIP-keyed assets → same ticker appears many times. Use the `id`, not the `symbol`, as your key.
- ⚠️ Paging: bars/trades/quotes return ≤10,000 items per call; ~3,500 symbols/call practical URL limit.
- ⚠️ `tradable` param on `getAssets` is silently ignored (reported in the 2024 thread).

---

## 11. stockanalysis.com

### 11a. **A delisted roster DOES exist** — ✅ VERIFIED LIVE, and it is **paywalled at 50 rows/year**

✅ **Confirmed pages exist for 1998 → 2026** (29 year-archives, all year links present in the index page HTML):
```
https://stockanalysis.com/actions/delisted/
https://stockanalysis.com/actions/delisted/2020/
...
https://stockanalysis.com/actions/delisted/1998/
```
Meta description of `/actions/delisted/2020/`: *"A list of stocks that were delisted in the year 2020. The list includes public companies trading on the main US exchanges (NYSE and NASDAQ)."*
Columns: **Date | Symbol | Company Name**. Server-rendered (SvelteKit), data inlined in `__sveltekit` payload as `[{date:"Dec 31, 2020",symbol:"PACDQ",name:"Pacific Drilling SA"}, ...]`.

✅ **THE PAYWALL** — raw HTML from `/actions/delisted/2020/`:
```html
<h4 ...>Showing 50 of 252</h4>
<div class="text-xl">Subscribe to see the full list</div>
<a href="/pro/" class="button mt-4 w-44">Activate Now</a>
```

### 11b. ✅ VERIFIED — the true size of the roster (parsed "Showing 50 of N" from every year page)

| Year | Delistings | Year | Delistings | Year | Delistings |
|---|---|---|---|---|---|
| 1998 | 245 | 2007 | 339 | 2016 | 335 |
| 1999 | 591 | 2008 | 316 | 2017 | 321 |
| 2000 | 608 | 2009 | 280 | 2018 | 289 |
| 2001 | *(fetch failed, ~300 est.)* | 2010 | 291 | 2019 | 278 |
| 2002 | 385 | 2011 | 273 | 2020 | 252 |
| 2003 | 306 | 2012 | 278 | 2021 | 364 |
| 2004 | 316 | 2013 | 244 | 2022 | 374 |
| 2005 | 284 | 2014 | 234 | 2023 | 486 |
| 2006 | 315 | 2015 | 308 | 2024 | 411 |
| | | | | 2025 | 373 |
| | | | | 2026 | 267 |

**Measured sum: 9,363. With 2001 included: ≈ 9,650 US delistings, 1998–2026.**

### 11c. **This explains your 27%**
- Free HTML = **50 rows/year × 29 years ≈ 1,450 names** = **~15% of the roster**
- And the free `/api/symbol/s/<T>/history` route is **Cloudflare-challenged** for naive clients: my `curl` with `User-Agent: Mozilla/5.0` on `https://stockanalysis.com/api/symbol/s/BBY/history?...` returned `Just a moment...` (HTTP 403 interstitial), while the plain HTML pages returned 200 with a realistic browser UA + `Accept-Language`. (The desktop browser tool was unavailable in my session, so **I could not test in a real browser**.)
- Only ~1,450 of ~9,650 delisted names have a free price-history page, and only for the ones stockanalysis.com chooses to keep a page for.

### 11d. Is there a JSON API? → ❌ **No public one found**
| Endpoint tried | Result |
|---|---|
| `GET /api/actions/delisted` | ❌ `404 {"status":404}` |
| `GET /api/screener/d/f?...` | ❌ `404 {"status":404}` |
| `GET /api/screener/s/f?m=marketCap&s=desc&c=no,s,n,marketCap&cn=5000&i=stocks` | ❌ Cloudflare challenge (403) |
| `GET /actions/delisted/2020/` | ✅ 200, 50 rows in HTML |

❌ **I could not find a delisted filter in the stockanalysis screener.** The regular screener JSON is `/api/screener/s/f?...` but I could not confirm a delisted flag exists, and I could not enumerate its parameters.

### 11e. Free alternatives for the full roster (in preference order)
1. **Tiingo `supported_tickers.zip`** — ✅ free, no key, **10,401 US major-exchange delisted** with start/end dates. Best free option.
2. **SEC EDGAR daily-index** — ✅ free, complete, verified working back to 1999.
3. **Massive `GET /v3/reference/tickers?active=false`** — free Basic plan includes Reference Data.
4. **EODHD `exchange-symbol-list/US?delisted=1`** — needs a token; free plan's "past year" window is useless for old delistings, but the *list* endpoint may work.
5. **stockanalysis.com Pro** (`/pro/`) — unlocks the full ~9,650 roster.

---

## 12. Alpha Vantage `LISTING_STATUS`

### 12a. ✅ CONFIRMED: `state=defunct` is **not a valid value**. The correct value is **`state=delisted`**.

📄 From the live Alpha Vantage documentation (https://www.alphavantage.co/documentation/):
> **`state`** *(Optional)* — *"By default, **`state=active`** and the API will return a list of actively traded stocks and ETFs. Set **`state=delisted`** to query a list of delisted assets."*
>
> **`date`** *(Optional)* — *"If no date is set, the API endpoint will return a list of active or delisted symbols as of the latest trading day. If a date is set, the API endpoint will **'travel back' in time** and return a list of active or delisted symbols on that particular date in history. **Any date later than 2010-01-01 is supported.** For example, `date=2013-08-03`"*
>
> *"To ensure optimal API response time, this endpoint uses the **CSV format** which is more memory-efficient than JSON."*

> **The endpoint is positioned to facilitate equity research on asset lifecycle and survivorship.**

**There is no `defunct` value in the API.** Your query `state=defunct` is silently ignored, the default `state=active` applies, and you get the active list back with 0 rows having a `delistingDate`. **That is exactly the symptom you described.** The fix is the parameter value, not the endpoint.

### 12b. Correct usage
```bash
# Active universe
GET https://www.alphavantage.co/query?function=LISTING_STATUS&apikey=KEY

# Delisted universe
GET https://www.alphavantage.co/query?function=LISTING_STATUS&state=delisted&apikey=KEY

# Point-in-time (any date after 2010-01-01)
GET https://www.alphavantage.co/query?function=LISTING_STATUS&date=2014-07-10&state=delisted&apikey=KEY
```
Output columns (📄 corroborated by https://www.macroption.com/alpha-vantage-delisted-stocks/ and https://github.com/ellisvalentiner/AlphaVantage.jl):
`symbol, name, exchange, assetType, ipoDate, delistingDate, status`
`assetType` ∈ {`Stock`, `ETF`}; `exchange` ∈ {`NASDAQ`, `NYSE`, `NYSE ARCA`, `BATS`}; `status` is always `Delisted` when `state=delisted`.

### 12c. Is it "known/deprecated"? → **NO — it is not deprecated.** It is a live, documented, actively-promoted endpoint.
❌ **I could not find any deprecation notice.** The docs page currently features it under "Status 🔧 Utility" with the survivorship-bias rationale quoted above.

### 12d. My live test — and why it is inconclusive
```bash
$ curl "https://www.alphavantage.co/query?function=LISTING_STATUS&state=delisted&apikey=demo"
{}
$ curl "https://www.alphavantage.co/query?function=LISTING_STATUS&state=defunct&apikey=demo"
{}
$ curl "https://www.alphavantage.co/query?function=LISTING_STATUS&state=active&apikey=demo" | wc -l
0
```
**The `demo` key returns `{}` for LISTING_STATUS in every state.** So the demo key simply lacks entitlement — this is *not* evidence about the delisted path.
📄 **Third-party confirmation that `demo` does not work:** *"Add your Alpha Vantage API key at the end. **This url does not work with `apikey=demo`.**"* — https://www.macroption.com/alpha-vantage-delisted-stocks/

❌ **COULD NOT VERIFY (requires a real key):** whether `delistingDate` is actually populated in the delisted rows, and the true size of AV's delisted list.

### 12e. Independent evidence that AV's delisted list is real and substantial
Two third parties have used `LISTING_STATUS` as their delisting ground truth:
1. **HuggingFace `YL95/new_experiment_1-data`** (https://huggingface.co/datasets/YL95/new_experiment_1-data) — used *"listing/delisting records from Alpha Vantage's `LISTING_STATUS` endpoint"* and reports: **"Of 9,446 delisted US securities identified from listing records, 472 of those symbols appear somewhere in this data, but only 243 contain any history from before the delisting date."** And: *"Delisting records begin in 1997 but are skewed towards recent years: **550 delistings are recorded before 2015 against 8,896 from 2015 onward.** Figures for earlier periods are lower bounds."*
2. **paperswithbacktest** (https://paperswithbacktest.com/datasets/stocks-daily-price) — cross-checked against `AlphaVantage-Stocks-Daily-ListingStatus`: *"which lists **16,675 US symbols of which 7,276 are delisted**... On the listing record, **44% of the US common stocks that ever traded are.**"*

⚠️ **The "550 before 2015 vs 8,896 from 2015 onward" skew is the single most important caveat about AV's roster** — it is essentially a 2015-forward delisting census with a thin pre-2015 tail. Not sufficient alone for 2000–2014 survivorship work.

### 12f. Cost
📄 Alpha Vantage free tier is heavily rate-limited; paid tiers unlock realtime/extended intraday. ❌ **I did not verify the current free-tier daily request cap or the paid tier prices.** The LEAN CLI enumerates the plan names: `Free | Plan30 | Plan75 | Plan150 | Plan300 | Plan600 | Plan1200` — https://github.com/QuantConnect/lean-cli

### 12g. Workarounds / alternatives if AV disappoints
| Need | Better option |
|---|---|
| Delisted roster, free | Tiingo `supported_tickers.zip` (10,401 US, verified) |
| Delisted roster, complete & free | SEC EDGAR daily-index Form 25 / 25-NSE / 15-12G |
| Delisted roster with `delisted_utc` + CIK | Massive `/v3/reference/tickers?active=false` |
| Delisted roster, US exchange | EODHD `exchange-symbol-list/US?delisted=1` |
| PIT roster with permanent IDs | Norgate (`SYM-YYYYMM`), Sharadar (`permaticker`), Tiingo (`permaTicker`) |
| Delist **reasons** | Sharadar corporate actions, EODHD `symbol-change-history` |
| Delist **effective date** | SEC Form 25 + 10 business days |

---

## 13. SEC EDGAR as a delisting-event source — ✅ **the best free, complete option**

### 13a. ✅ The right tool: **daily index files**, NOT full-text search

```bash
# By form type, one file per day
GET https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{N}/form.{YYYYMMDD}.idx

# By company/CIK, one file per day
GET https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{N}/company.{YYYYMMDD}.idx

# Directory listing
GET https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{N}/
```
✅ **VERIFIED LIVE** (HTTP 200, correct content) for `form.19990104.idx`, `form.20010102.idx`, `form.20020102.idx`, `form.20030102.idx`, `form.20040102.idx`. Directory listings confirmed at `/2003/QTR1/` and `/1999/QTR1/`. (`/2004/QTR1/form.20040102.idx` = 835,638 bytes.)

✅ **Format** (real output from `form.20240102.idx`):
```
Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    Jan  2, 2024
Form Type   Company Name                       CIK        Date Filed    File Name
----------- -------------------------------- --------- ------------ ----------------------------
25          Constellation Acquisition Corp I   1834032    20240102      edgar/data/1834032/0001213900-24-000306.txt
25-NSE      FREYR Battery                      1844224    20240102      edgar/data/1844224/0000876661-24-000006.txt
25-NSE      GRIID Infrastructure Inc.          1830029    20240102      edgar/data/1830029/0001143313-24-000006.txt
25-NSE      NEW YORK STOCK EXCHANGE LLC        876661     20240102      edgar/data/876661/0000876661-24-000002.txt
25-NSE      NYSE AMERICAN LLC                  1143313    20240102      edgar/data/1143313/0001143313-24-000002.txt
```
Parse: `awk -F'|' '$1 ~ /^25/'` — 12 header lines to skip.
⚠️ Rate limit: **10 requests/second max, and you must send a descriptive `User-Agent`** (I used `Research research@example.com`). Expect HTTP 403 when throttled — this is what I hit initially.

### 13b. ⚠️ Full-text search is **NOT** a form index — verified counterexample
```bash
GET https://efts.sec.gov/LATEST/search-index?q=%22a%22&forms=25-NSE&startdt=2024-01-01&enddt=2024-01-31
```
✅ Worked, returned rich JSON: `hits.total.value`, per-hit `ciks[]`, `display_names[]`, `file_date`, `form`, `adsh`, `file_num`, `biz_states`, `sics`, `items`, `file_type`, `sequence`.
❌ **But it undercounts.** Measured Jan 2024 hits: `25-NSE` → 84, `25` → 13, `15-12G` → 49, `15-12B` → 0, `8-K` → 10,000 (capped).
Compare `form.20240102.idx` for the *same day*: **1 × `25`** and **7 × `25-NSE`**. FTS only returns documents containing your search term. **Use `form.idx` for enumeration; use FTS only to inspect the documents you already found.**

### 13c. Other verified endpoints
```bash
# Atom feed, per-company browse by form type
GET https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=25-NSE&dateb=&owner=include&count=10&output=atom   # ✅ 200

# Company submissions (all filings, recent + pointers to older JSON)
GET https://data.sec.gov/submissions/CIK##########.json

# Ticker -> CIK  (✅ 200, 798,244 bytes)
GET https://www.sec.gov/files/company_tickers.json
# ⚠️ only CURRENTLY listed companies — no delisted

# Full submission text
GET https://www.sec.gov/Archives/edgar/data/{cik}/{accession-without-dashes}.txt
```

### 13d. Which forms matter
| Form | Meaning | Verified? |
|---|---|---|
| **25** | Notification of removal from listing (Rule 12d2-4, 1934 Act) | ✅ 1 filing on 2024-01-02 |
| **25-NSE** | NYSE's delisting notification, filed **by NYSE** (CIK `0000876661`) with the issuer as co-registrant | ✅ 7 on 2024-01-02; FTS shows `ciks: ["0001793229","0000876661"]` |
| **25-NASSE** | Nasdaq equivalent | ❌ **not tested** — string unconfirmed |
| **15-12G** | Certification/notice of termination of registration under §12(g) — **deregistration** | ✅ 49 FTS hits Jan 2024 |
| **15-12B** | Termination of registration under §12(b) | ✅ **0 FTS hits Jan 2024 — this form is effectively unused** |
| **15-15D** | Suspension of duty to file reports | ⚠️ my test returned unparseable; untested |
| **8-K Item 3.01** | Notice of delisting or failure to satisfy listing standards | 📄 the FTS JSON has an `items` field you can filter on |
| **8-K Item 2.05** | Costs associated with exit or disposal of assets | 📄 same |

Full 8-K code: `https://efts.sec.gov/LATEST/search-index?q=...&forms=8-K&startdt=&enddt=` then filter `_source.items`.

### 13e. Caveats (important)
1. ⚠️ **EDGAR only covers registrants.** Non-registered securities, and companies that deregistered long ago, are absent.
2. ⚠️ **Many issuers file Form 15 and NEVER file a Form 25** — especially post-2005 reverse mergers where the *acquirer* is the registrant and the target is never separately delisted. **You need both 25\* and 15-12G/15-12B.**
3. ⚠️ **A Form 25 is a removal notice, not the last trading day.** The BlackFalconData project computes `effectiveDelistingDate` as **10 business days after filing** for Form 25 — https://github.com/BlackFalconData-org/delisted-stocks-list
4. ⚠️ **Form 25-NSE is filed by the exchange**, so `companyName` in `form.idx` is "NEW YORK STOCK EXCHANGE LLC", not the issuer. You must parse the document or use the FTS `ciks[]` array (the issuer is the *other* CIK).
5. ⚠️ **EDGAR full-text search covers 2001+ only.**
6. ⚠️ Rate-limit hard; batch by day and sleep.

### 13f. Bonus — a ready-made EDGAR-derived roster
📄 **BlackFalconData-org/delisted-stocks-list** — *"Complete list of **36,000+ delisted stocks** from NYSE, NASDAQ, and OTC exchanges - updated daily from SEC EDGAR since 2002. Every stock delisting (Form 25) and voluntary deregistration (Form 15)"*. Fields: `ticker, issuerName, cik, formType, fileDate, effectiveDelistingDate, exchange, securityType, filingUrl, confidence`. Incremental pulls via `updatedSince`. **Coverage: 2002→present, "most complete from 2004 onwards."** Distributed via Apify (actor) — **the paid/hosted access is Apify; the GitHub repo is a description, not a free CSV I verified.** ❌ I did not verify that a free download exists.
— https://github.com/BlackFalconData-org/delisted-stocks-list

---

## 14. Wayback Machine — ❌ **NOT viable at scale**

### What I verified (partially — the IA was mostly offline)
- `archive.org/wayback/available` → **HTTP 429 Too Many Requests**
- `web.archive.org/cdx/search/cdx` → **"Internet Archive: Temporarily Offline"** on most attempts

✅ **Two successful observations before the outage:**
1. `?url=query1.finance.yahoo.com/v7/finance/download/LEHKQ*` → **`[]`** (zero captures for the Lehman OTC ticker)
2. Wildcard on `query1.finance.yahoo.com/v7/finance/download/*` with `collapse=urlkey` returned only **template/placeholder URLs captured from code snippets**:
```
https://query1.finance.yahoo.com/v7/finance/download/                                  [401]
http://query1.finance.yahoo.com/v7/finance/download/$code.IS?&period1=$start&...        [400]
https://query1.finance.yahoo.com/v7/finance/download/%1-%2?events=history&...          
https://query1.finance.yahoo.com/v7/finance/download/%25s
https://query1.finance.yahoo.com/v7/finance/download/%22                              [404]
http://query1.finance.yahoo.com/v7/finance/download.$sym
```
**Every capture is a broken placeholder or a 4xx. Not one real per-ticker CSV.**

### Structural argument (independently verifiable)
- Yahoo's `/v7/finance/download` **requires a session cookie + crumb**. Unauthenticated requests return **401** — which is exactly the status code the CDX returned for the bare `download/` path.
- Yahoo has deprecated/restricted the endpoint, so post-~2019 archives are mostly 401.
- The archive's own capture policy would never have crawled millions of parameterized URLs at depth.

### Verdict
❌ **Not viable at scale, and not viable at all for breadth.** Even if the IA comes back, a wildcard CDX would need to be counted to prove the negative — and the sample of 40 distinct URLs contained zero real CSVs. **Budget 0% coverage.** Use EODHD/HistoricalData.net/Norgate/Tiingo instead.

---

## 15. Free, publicly downloadable survivorship-bias-free panels — **I did not find a complete one**

### 15a. What exists (partial, unverified quality)

| # | Artifact | URL | What it is | Verified? |
|---|---|---|---|---|
| 1 | **Arandkei: Historical Delisted Assets Archive** (Kaggle) | https://www.kaggle.com/datasets/rodas86/arandkei-historical-delisted-assets-archive | **Delisted-only** daily OHLCV, one CSV per ticker named `TICKER_StartDate_EndDate_ARANDKEI.csv`, **CC BY 4.0**, v2 = 22.04 MB, ~760 files. Filenames show start dates 1987–2026 (`TEF_19870612`, `ZEUS_19940310`, `MEL_19900102`, `AVDL_19960607`) | 📄 page read; ❌ provenance/completeness unverified. ⚠️ Their own note: *"In cases where primary data sources did not provide an adjusted value, the standard Close price has been maintained"* → Adj Close may be a fake. **Sample it before trusting.** |
| 2 | **Point-in-Time S&P 500 Membership Dataset** (Zenodo) | published 2026-06-04 (via exa.ai mirror) | **Membership only, NO prices.** 756 tickers. PIT removal tracking from 2015-01-01; "some interval start dates are preserved from public sources back to 1976-07-01 and are **not PIT-accurate before 2015**" | 📄 abstract read. ❌ Could not resolve the Zenodo DOI directly. |
| 3 | **FINSABER-reproduce** (HuggingFace) | https://huggingface.co/datasets/finsaber-team/FINSABER-reproduce | `data/price/all_sp500_prices_2000_2024_delisted_include.csv` (~253 MB) — "CSV price-only data for S&P500 (**including delisted**)". Also an 11 GB pickle with price+news+filings. | 📄 file listing read. ❌ contents unverified. **S&P 500 only.** |
| 4 | **sp500-quantitative-dataset** (GitHub) | https://github.com/K0D1Z/sp500-quantitative-dataset | Full reproducible pipeline + released CSV/parquet. "Survivorship-Bias-Free, and Point-in-Time (PIT) daily financial dataset of the S&P 500... **Includes delisted, acquired, and bankrupt companies (e.g., First Republic Bank, SVB)**... Everything available for free: the pipeline uses public APIs and the free plans of data-providing institutions." Uses **Tiingo free tier** for delisted price backfill + SEC EDGAR XBRL + Wikipedia. 1.6M rows, 45+ features. PIT membership 2015→present. | 📄 README read. ❌ outputs unverified. **S&P 500 only, membership PIT from 2015.** |
| 5 | **pystock-data** (GitHub) | https://github.com/lynsueforever/pystock-data | Daily prices from 2009 via yfinance crawler, gzip tarballs by date. **UNMAINTAINED**, API frozen at 2017-03-31. yfinance-sourced ⇒ survivors-biased. | 📄 README; ❌ not used |

### 15b. ✅ VERIFIED NEGATIVES — popular "free panels" that are survivors-only
- **HuggingFace `YL95/new_experiment_1-data`** (39,260 series, 135M obs, 13 markets, 1990→2026) — **self-documents its own bias**: *"This dataset contains survivors only... **Of 9,446 delisted US securities identified from listing records, 472 of those symbols appear somewhere in this data, but only 243 contain any history from before the delisting date.** ... A model trained on this data sees a world where companies essentially never fail."* ⚠️ Useful byproduct: it ships `metadata/listings_history.parquet` (listing + delisting records) and `metadata/tickers.parquet` (per-series first/last date) — **a free roster even if the prices are useless.**
- **paperswithbacktest "Stocks-Daily-Price"** — *"only **3.5%** of the symbols in this file are companies that have since been delisted. On the listing record, **44%** of the US common stocks that ever traded are... a cross-sectional sort run on this file is a sort over the survivors, and its Sharpe ratio is flattered by that."*
- **Quandl/Wiki US equities (the ML-for-Trading `wiki` dataset)** — 3,199 symbols, 1962–2018; 777 delisted (24.3%). Survivorship-biased.

### 15c. ❌ CONCLUSION
**There is no single free, publicly downloadable, full-market US daily OHLCV panel for 2000–present that includes delisted names.** The closest free artifacts are:
- the Kaggle **Arandkei** delisted-only archive (CC BY 4.0),
- the **stockanalysis.com** 50-rows/year HTML (≈1,450 of ~9,650),
- the **Tiingo** free roster ZIP (10,401 US names, no prices),
- **SEC EDGAR** daily-index (complete event list, no prices).

Everything full-market requires either a build (EDGAR + Tiingo/EODHD/HistoricalData.net) or a purchase.

---

## 16. BONUS — three sources you did not ask about but should know

### B1. **HistoricalData.net** — likely the single best $/delisted-name in this report
📄 https://historicaldata.net/
- "Daily and 1-minute OHLCV for **15,000+ active and 50,000+ delisted US tickers**, from **November 2003** to the latest trading day — aggregated across 20+ venues (all major exchanges plus FINRA/TRF and **OTC**), rebuilt every Saturday."
- "Delisted companies **keep their full history, frozen as of the delisting date**. This makes the dataset suitable for **survivorship-bias-free backtesting**."
- **Filenames encode the delist date:** `delisted_{TYPE}_{TICKER}_{delisting-date}_....csv` — 📄 https://historicaldata.net/methodology.html
- **16 columns per row:** unadjusted OHLCV, **VWAP, transaction count**, split/dividend-adjusted OHLCV, plus in-line `Dividend` and `Split` event columns. Regular-session minute file (09:30–16:00) *and* an extended-hours file from 04:00 ET.
- "Corrections applied retroactively: when a split or dividend occurs, the ticker's entire history is recomputed and replaced."
- **Flat files, plain CSV, one-time purchase, no API.** Delivery link valid 21 days.
- **Free metadata API** (no account): `GET /api/status`, `/api/catalog`, `/api/symbol/{ticker}` — 30 req/min/IP, **metadata only, never data rows**. "Single-symbol lookups only — this interface is not a bulk symbol-master database and may not be scraped to build one." Free sample ZIPs (Jul–Dec 2022) at `/file/`.

**⚠️ Price inconsistency I could not resolve — verify before buying:**
| | products page / ai.html | FAQ page |
|---|---|---|
| Stock **daily** archive | **$299** | **$399** |
| Stock 1-minute archive | **$399** (daily included) | **$699** |
| Options full archive | $590 | $590 ✅ |
| Daily updates | $39/mo | $39/mo ✅ |
| Minute updates | $69/mo | $69/mo ✅ |
| Options updates | $59/mo | $79/mo (conflict) |

**⚠️ Coverage-count inconsistency too:** `stocks.html` / homepage say "15,770 active and 23,259 delisted as of 2026-09-18" and "50,000+ delisted"; the FAQ says "15,784 active and 23,259 delisted as of 2026-09-22". The "50,000+" figure is not reproducible from the other pages. **Ask them directly.**

### B2. **QuantRocket Data Library**
📄 *"All of QuantRocket's datasets (excluding broker data feeds) **include delisted stocks and are survivorship-bias-free**."* Uses **EDI** global equities. *"In North America, by the time you go back 10 years, a dataset with survivorship bias will be missing **75%** of the stocks that were actually trading at that time. In Europe ~50%. In Asia ~25%."* — https://www.quantrocket.com/blog/survivorship-bias/
- Some datasets go back to 2007, one to the 1990s. CIK + FIGI included.
- ❌ Pricing unverified.

### B3. **Tickerbot** (unvetted, small)
📄 Claims `GET /v2/tickers` catalog = *"Every symbol we track, active or delisted, as one identity row each"* with `ticker, name, asset_class, asset_type, exchange, exchange_mic, currency_name, country, cik, composite_figi, list_date, delisted_utc, active`, plus `/v2/scan?universe=` and `?asof=` for historical tenure. — https://tickerbot.io/docs/endpoints/tickers/catalog/
- ❌ **Completely unverified**: no coverage confirmation, no pricing found, unknown operator. Note their changelog says the `?tickers=`/`?asof=` params changed on 2026-09-08.

---

## 17. Recommended build recipes

### Recipe A — Cheapest defensible 2000–present daily panel (~$20–300 one-time + $0/mo)
1. **Roster** (union, dedupe on CIK where available):
   - ✅ `https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip` (free) → 10,401 US major-exchange delisted
   - ✅ EDGAR `form.*.idx` for Form `25`, `25-NSE`, `15-12G` (free, complete, 1999→)
   - ✅ Massive `GET /v3/reference/tickers?market=stocks&active=false&limit=1000` (free Basic plan) → adds `delisted_utc` + `cik`
2. **Prices:** EODHD **EOD Historical Data — All World, $19.99/mo** (`/api/eod/{T}.US?from=2000-01-01&to=2026-09-27`)
   - ⚠️ Gives you **EOD only** for pre-2018 delistings. Acceptable if you only need returns.
   - Free plan is useless here (20 calls/day + "past year" window).
3. **Delist reasons & renames:** Sharadar corporate actions, or EODHD `/api/symbol-change-history?ex=US`.

### Recipe B — Deepest panel with fundamentals (~$630/yr)
**Norgate Platinum ($630/yr)** — 1990 start, delisted + OTC + **PIT historical index constituents (daily, intra-period)** + `Major Exchange Listed` flag + `{SYM}-YYYYMM` disambiguation. Use `norgatedata.database_symbols('US Equities Delisted')` + `price_timeseries()`. This is the only source I found that gives you a *clean PIT index-membership universe* at a sub-$1k/yr price.

### Recipe C — Best price-per-name (~$300 one-time)
**HistoricalData.net daily archive ($299)** — Nov 2003+, 23k–50k delisted tickers, adjusted + unadjusted side by side, VWAP, inline dividend/split, delist date in the filename, no API, no subscription. Confirm the price and the delisted count by email first.

### Recipe D — Reference-grade (institutional)
**CRSP via WRDS** if your institution licenses it (free for you). Use **Sharadar SF1** (99% SBB-free, 12k delisted, 1998, PIT, delist reasons) as a cross-check/point-in-time fundamentals layer on top.

---

## 18. Things I could NOT verify — explicit list

| # | Claim | Why |
|---|---|---|
| 1 | Number of delisted tickers in the **QuantConnect free** dataset | No free bulk download exists; LEAN GitHub has only 21 sample tickers |
| 2 | Whether QC free dataset covers pre-2000 delisted names | Same |
| 3 | Whether **Tiingo `/tiingo/daily/{ticker}/prices`** returns bars for a delisted ticker by plain ticker vs `permaTicker` | No API key; Tiingo doc pages are JS-rendered |
| 4 | Tiingo **history depth** (30+ yrs vs 5 vs 15+) | Tiingo's own pricing page and blog contradict each other |
| 5 | EODHD behaviour with a real token | `api_token=demo` → HTTP `Forbidden` on every endpoint |
| 6 | Whether EODHD's **free** plan includes the `delisted=1` symbol list | Demo token blocked; feature matrix text is column-mangled |
| 7 | Massive flat-file **history add-on** pricing / whether Starter really gets 5 yrs of flat files | Pricing page and comparison table contradict |
| 8 | **Alpha Vantage** whether `delistingDate` is populated in `state=delisted` rows | `apikey=demo` returns `{}` for all LISTING_STATUS calls |
| 9 | Alpha Vantage current **free-tier request cap** and paid prices | Not read from their pricing page |
| 10 | **FMP** free-tier access to `/delisted-companies` and `/stock-list` | Both endpoints require a key; unauthenticated → "Invalid API KEY" |
| 11 | **FMP** current price list | Not read |
| 12 | **Sharadar SEP/SF1 paid pricing** | No public price list found |
| 13 | **CRSP/Compustat WRDS pricing** | "Contact wrds@wharton.upenn.edu" — not public |
| 14 | **Intrinio** whether the free 2-week trial actually includes delisted securities in the API responses (docs say the plan "includes" them) | No account |
| 15 | **Form 25-NASSE** (Nasdaq's delisting form) string validity | Not tested on EDGAR |
| 16 | **Form 15-15D** FTS hit counts | My test returned unparseable output |
| 17 | **Wayback total** capture count for Yahoo `/v7/finance/download` | Internet Archive was offline for most of the session |
| 18 | **stockanalysis.com** JSON API for the delisted list, and whether its screener has a delisted filter | `/api/actions/delisted` and `/api/screener/d/f` both 404; `/api/screener/s/f` Cloudflare-challenged; desktop browser tool disconnected so I could not test in a real browser |
| 19 | **Kaggle Arandkei** provenance, completeness, and Adj Close integrity | Page read only; not downloaded (22 MB) |
| 20 | **HF FINSABER / GitHub K0D1Z / Zenodo** dataset contents | Listings read; files not downloaded |
| 21 | **BlackFalconData** free (non-Apify) access | GitHub repo is a description; actor is paid/hosted |
| 22 | **Tickerbot** coverage and pricing | Unvetted |
| 23 | **QuantRocket Data Library** pricing | Not read |
| 24 | **HistoricalData.net** which of $299/$399 (daily) and $399/$699 (minute) is current, and whether delisted count is 23,259 or "50,000+" | Their own pages conflict |
| 25 | Whether the free **stockanalysis.com** 50/year pages are rate-limited | Only 29 requests made, no throttling observed, but Cloudflare is clearly present |

---

*Every URL in this report was fetched on 2026-09-28. "✅ VERIFIED LIVE" means I actually executed the request and parsed the result on that date.*
