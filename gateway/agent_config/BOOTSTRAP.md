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

- **OHLCV**: Any ticker, any timeframe (down to 1-min bars) via Massive.com subscription. Pull what you need — don't limit to what's on disk. Period: 2021–2026.
- **Reddit sentiment**: 2021–2026 historical posts from r/wallstreetbets, r/stocks, r/investing with LLM sentiment scores
- **News sentiment**: Articles with sentiment from Massive.com and Polygon.io
- **Volume indicators**: VWAP, OBV, MFI, CMF, A/D, VROC, PVT, Klinger, EOM, VWMA, Elder Force, NVI, Chaikin Volatility

## Compute Resources

You are on a machine with a Nvidia graphics card. Feel free to designate true ML training routines to copilot to train complex models if your experiments are pushing you in that direction.

## Intraday Trading Focus

**All research targets INTRADAY strategies.** We have 1-minute OHLCV bars — exploit this granularity.

- **Holding period**: Minutes to hours. No overnight positions. Entry and exit within the same trading day.
- **Data edge**: 1-min bars give us microstructure resolution (volume profiles, VWAP dynamics, opening range patterns, intraday momentum/mean-reversion cycles).
- **Intraday patterns to explore**: Opening range breakout/failure, VWAP reversion, volume-at-price concentration, lunch hour mean reversion, power hour momentum, intraday sentiment spikes (Reddit/news timing vs. price).
- **Time features matter**: Hour-of-day, minutes-since-open, time-to-close, session half. Intraday alpha often has strong time-of-day dependence.
- **Transaction costs are critical**: At intraday frequency, slippage and commissions can destroy alpha. Every backtest MUST model realistic transaction costs.

## Data Range Coverage Rule (NON-NEGOTIABLE)

**Every experiment MUST use at least 95% of available trading days for the chosen ticker(s).** We have 2021–2026 data via Massive.com — use it all.

- **Pull data first**: Before starting an experiment, fetch OHLCV: `uv run quantipy ohlc fetch <TICKER> -s 2021-01-01 -e 2026-03-01`
- **Train/CV period**: At least 3 years of data (e.g., 2021–2024)
- **OOS holdout**: At least 6 months / 120 trading days (e.g., 2025-H1). NEVER touched during training.
- **Do NOT limit to Jan-Jul 2022.** That was the old constraint. The `experiment-data` skill in quantipy may still reference narrow dates — OVERRIDE those with the full range.
- **Walk-forward folds**: With 3+ years of data, use 20+ folds minimum.

## Research Direction

**Fresh start.** No experiments have been run yet. All data channels are available:

- **OHLCV**: Any ticker via Massive.com (1-min to daily bars, 2021–2026). Start with low-to-mid cap equities but expand in any direction.
- **Reddit sentiment**: 2021–2026 posts from r/wallstreetbets, r/stocks, r/investing with LLM sentiment scores
- **News sentiment**: Articles with sentiment from Massive.com and Polygon.io

Pursue novel intraday alpha through any combination of:
- **Intraday microstructure**: Volume profiles, VWAP deviation, opening range dynamics, bid-ask spread proxies
- **Sentiment-gated signals**: Use Reddit/news sentiment as conditioning variables for intraday volume/price features
- **ML with theoretical basis**: LightGBM/XGBoost/HistGradientBoosting on engineered features with purged walk-forward CV
- **Regime detection**: HMM on intraday volatility states, change-point detection → regime-conditional entry/exit
- **Cross-session patterns**: Prior day closing action predicting opening patterns
- **Multi-asset exploration**: Low/mid-cap equities, sector ETFs, volatility products — diverse universe for orthogonal strategies
- **Feature engineering over indicator stacking**: Volatility ratios, volume imbalance, momentum decay, sentiment×volume interactions

## Experiment Notebooks

All experiments produce Jupyter notebooks as primary output.

- **Location:** `notebooks/experiments/<strategy_name>.ipynb`
- **Existing:** `notebooks/llm_comparison_experiment.ipynb` (pre-existing)
- **Deps:** `jupyter` and `nbformat` are NOT yet in pyproject.toml — Copilot must add them when creating the first experiment notebook. Also add `matplotlib` for visualizations if not present.
- **Execution:** `uv run jupyter execute <notebook.ipynb> --timeout=300`
- **Convention:** Notebook imports module code from `src/quantipy/alpha/<strategy_name>/` — it orchestrates the experiment, not duplicates the code.

## Shared Experiment Memory

The file `RESEARCH_LOG.md` in the quantipy repo tracks all experiments tried, rejected ideas, and insights. Read it before every ideation round. Update it after every experiment result. Pass this context to Copilot's researcher agent when delegating ideation. Currently empty — this is a fresh start.

## Data APIs

You are authorized to instruct copilot to pull data (OHLC) from an available api. Make sure that data is persisted on disk in the sql databases and you are not making repeated requests, but this should be done as part of the infrastructure. DO NOT MAKE MANUAL PUSHES OR HAVE COPILOT DO THAT. If features need to be added to the codebase to handle this, delegate to copilot for planning and implementation.

## Commands

```bash
cd ~/repos/quantipy
uv sync                          # install deps
uv run pytest -q                 # run tests
uv run ruff check src/           # lint
uv run python -m quantipy --help # CLI
```
