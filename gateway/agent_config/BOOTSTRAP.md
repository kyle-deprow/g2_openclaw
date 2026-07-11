# Bootstrap — Quantipy Context

## Repository

`~/repos/quantipy` — Quantitative finance platform for market intelligence.

## Tech Stack

- Python 3.11+ (managed with **uv**, not pip)
- SQLAlchemy 2.0 + asyncpg (async PostgreSQL)
- Alembic migrations
- pytest + ruff
- Source layout: `src/quantipy/`

## Modules

| Module | Purpose |
|--------|---------|
| `reddit_sentiment/` | Reddit post scraping + LLM sentiment analysis (live + historical) |
| `news_sentiment/` | News article sentiment via Massive.com and Polygon.io |
| `price_data/` | OHLCV 1-min bars via Databento |
| `technical_indicators/` | 13 volume indicators (VWAP, OBV, MFI, CMF, A/D, VROC, PVT, Klinger, EOM, VWMA, Elder Force, NVI, Chaikin Vol) |
| `universe/` | Trading universe builder (ticker selection) |
| `queue/` | PostgreSQL job queue (SKIP LOCKED pattern) |
| `llm/` | LLM provider abstraction for sentiment analysis |
| `common/` | Config, database, enums, HTTP, logging, migrations |
| `cli/` | Typer CLI: reddit, news, ohlc, jobs, reset commands |

## Data Available

- **OHLCV**: Any ticker, any timeframe (down to 1-min bars). `qp.prices()` auto-fetches missing data from Massive.com on first call — no manual fetch step needed. Period: 2021–2026.
- **Reddit sentiment**: 2021–2026 historical posts from r/wallstreetbets, r/stocks, r/investing with LLM sentiment scores
- **News sentiment**: Articles with sentiment from Massive.com and Polygon.io
- **Volume indicators**: VWAP, OBV, MFI, CMF, A/D, VROC, PVT, Klinger, EOM, VWMA, Elder Force, NVI, Chaikin Volatility

## Compute Resources

You are on a machine with a Nvidia graphics card. Feel free to designate true ML
training routines to Codex subagents if your experiments are pushing you in
that direction.

## Intraday Trading Focus

**All research targets INTRADAY strategies.** We have 1-minute OHLCV bars — exploit this granularity.

- **Holding period**: Minutes to hours. No overnight positions. Entry and exit within the same trading day.
- **Data edge**: 1-min bars give us microstructure resolution (volume profiles, VWAP dynamics, opening range patterns, intraday momentum/mean-reversion cycles).
- **Intraday patterns to explore**: Opening range breakout/failure, VWAP reversion, volume-at-price concentration, lunch hour mean reversion, power hour momentum, intraday sentiment spikes (Reddit/news timing vs. price).
- **Time features matter**: Hour-of-day, minutes-since-open, time-to-close, session half. Intraday alpha often has strong time-of-day dependence.
- **Transaction costs are critical**: At intraday frequency, slippage and commissions can destroy alpha. Every backtest MUST model realistic transaction costs.

## Data Range Coverage Rule (NON-NEGOTIABLE)

**Every experiment MUST use at least 95% of available trading days for the chosen ticker(s).** We have 2021–2026 data — use it all.

- **Data is auto-fetched**: `qp.prices()` gap-fills missing dates from Massive.com automatically. Just request the full range.
- **Train/CV period**: At least 3 years of data (e.g., 2021–2024)
- **OOS holdout**: At least 6 months / 120 trading days (e.g., 2025-H1). NEVER touched during training.
- **Walk-forward folds**: With 3+ years of data, use 20+ folds minimum.

## Research Direction

### Trading Universe: Small/Mid-Cap Only (NON-NEGOTIABLE)

**You may ONLY trade small-cap and mid-cap stocks ($500M–$20B market cap).** This is the core thesis: simple indicators work better on less-efficient, higher-volatility names where institutional coverage is thinner and retail flow creates exploitable patterns.

**Universe discovery IS part of the research.** You choose which small/mid-cap tickers to trade. Consider:
- Liquidity (>2M avg daily volume — must be tradeable at intraday frequency)
- Volatility (higher intraday range = more alpha opportunity)
- Sector diversity (don't cluster everything in one sector)
- Data availability (need 2021–2026 coverage for robust walk-forward)

**Examples of valid small/mid-cap tickers** (not exhaustive — discover your own):
- Meme/retail-heavy: PLTR, SOFI, HOOD, RIVN, LCID, MARA, BB, CLOV, WISH, SKLZ, BBBY
- Biotech/pharma: MRNA (was mid-cap pre-2021), DNA, CRSP, BEAM
- Tech mid-cap: RBLX, U, DKNG, OPEN, UPST, AFRM, BILL
- EV/clean energy: CHPT, QS, PLUG, FCEL, BLNK
- Fintech: SQ (was mid-cap), COIN, NU, LMND

**Large-caps (SPY, AAPL, NVDA, TSLA, MSFT, GOOG, META, AMZN, etc.) may be used as SIGNAL SOURCES but NEVER traded.** Cross-asset lead-lag, beta hedging, sector rotation signals — all fine as features. But the positions you take must be in small/mid-caps.

### Data Sources

- **OHLCV**: Any ticker (1-min to daily bars, 2021–2026). Auto-fetched on first `qp.prices()` call.
- **Reddit sentiment**: 2021–2026 posts from r/wallstreetbets, r/stocks, r/investing with LLM sentiment scores. Use for **feature generation and signal conditioning** — NOT to restrict the trading universe. Many tradeable small/mid-caps have zero Reddit coverage, and that's fine.
- **News sentiment**: Articles with sentiment from Massive.com and Polygon.io

### Data Download: Pre-fetch Before Backtesting

When you select tickers for an experiment, **download the full 2021–2026 OHLCV data upfront** before running the backtest. `qp.prices()` auto-fetches missing data, but fetching inside a walk-forward loop is slow. Pre-fetch pattern:

```python
# Pre-fetch all tickers at experiment start (before any backtest logic)
import quantipy as qp
TICKERS = ["PLTR", "SOFI", "DKNG", "SPY"]  # SPY for signals only, not traded
for t in TICKERS:
    df = qp.prices(t, "2021-01-01", "2026-04-01")
    print(f"{t}: {len(df)} bars, {df['timestamp'].dt.date.nunique()} trading days")
```

### Alpha Directions

Pursue novel intraday alpha through any combination of:
- **Intraday microstructure**: Volume profiles, VWAP deviation, opening range dynamics, bid-ask spread proxies
- **Sentiment-gated signals**: Use Reddit/news sentiment as conditioning variables — but don't require tickers to have Reddit coverage
- **Cross-asset signals**: Use SPY/QQQ/sector ETFs as leading indicators for small/mid-cap positions. The signal is from large-caps, the trade is in small/mid-caps.
- **ML with theoretical basis**: LightGBM/XGBoost/HistGradientBoosting on engineered features with purged walk-forward CV
- **Regime detection**: HMM on intraday volatility states, change-point detection → regime-conditional entry/exit
- **Cross-session patterns**: Prior day closing action predicting opening patterns
- **Feature engineering over indicator stacking**: Volatility ratios, volume imbalance, momentum decay, sentiment×volume interactions
- **Universe selection as alpha**: Which small/mid-caps to trade on which days is itself a signal. Rotation, momentum, liquidity screening.

## Experiment Notebooks

All experiments produce Jupyter notebooks as primary output.

- **Location:** `notebooks/experiments/<strategy_name>.ipynb`
- **Existing:** `notebooks/llm_comparison_experiment.ipynb` (pre-existing)
- **Dependency/runtime tooling:** If notebook execution requires missing
  `jupyter`, `nbformat`, `matplotlib`, or any other dependency/runtime tooling,
  fail closed: report/block with the exact missing-dependency evidence and
  await human/Codex operator action. PM and stage agents do not modify
  dependency or runtime tooling.
- **Execution:** `uv run jupyter execute <notebook.ipynb> --timeout=300`
- **Convention:** Notebook imports module code from `src/quantipy/alpha/<strategy_name>/` — it orchestrates the experiment, not duplicates the code.

## Shared Experiment Memory

The file `RESEARCH_LOG.md` in the quantipy repo tracks all experiments tried,
rejected ideas, and insights. Read it before every ideation round. Update it
after every experiment result. Pass this context to `context-curator`, then to
the five debate agents through `consensus-arbiter`. MemPalace is the only
durable research memory layer; use it for prior experiment summaries, metrics,
reviewer objections, and failure patterns.

## Data APIs

You are authorized to instruct Codex subagents to pull OHLC data from an
available API. Make sure that data is persisted on disk in the SQL databases and
you are not making repeated requests. Do not make manual pushes. If features
need to be added to the codebase to handle this, delegate planning and
implementation to the appropriate subagent.

## Commands

```bash
cd ~/repos/quantipy
uv sync                          # install deps
uv run pytest -q                 # run tests
uv run ruff check src/           # lint
uv run python -m quantipy --help # CLI
```
