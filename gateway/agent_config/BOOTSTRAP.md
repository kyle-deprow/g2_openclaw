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

- **Reddit**: Posts from r/wallstreetbets, r/stocks, r/investing + LLM sentiment scores
- **News**: Articles with sentiment from Massive.com and Polygon.io
- **OHLCV**: 1-minute bars from Massive.com
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

## Research Direction

Generic textbook indicators are EXHAUSTED. The following have been tried and are banned as primary strategy signals:
- SMA crossover, RSI, MACD, Bollinger Bands, OBV

Future experiments MUST pursue novel intraday alpha through:
- **Intraday microstructure**: Volume profile analysis, VWAP deviation patterns, opening range dynamics, bid-ask spread proxies from 1-min bars
- **Intraday sentiment timing**: Reddit/news sentiment spikes timed against intraday price action — when does sentiment lead price within a day?
- **ML with theoretical basis**: LightGBM/XGBoost on intraday engineered features with purged walk-forward CV, not raw LSTM on prices
- **Intraday regime detection**: HMM on intraday volatility states, change-point detection on volume profiles → regime-conditional entry/exit
- **Cross-session patterns**: How does prior day's closing action predict opening patterns? Session-over-session regime persistence.
- **Time-of-day effects**: ML models with hour-of-day features, learned session segmentation, time-conditional strategy switching
- **Unusual asset niches**: Crypto funding rates, FX carry, agricultural futures — anomalies from less-crowded markets
- **Feature engineering over indicator stacking**: Transform raw data into meaningful features (volatility ratios, volume imbalance, momentum decay rates)

## Experiment Notebooks

All experiments produce Jupyter notebooks as primary output.

- **Location:** `notebooks/experiments/<strategy_name>.ipynb`
- **Existing:** `notebooks/llm_comparison_experiment.ipynb` (pre-existing)
- **Deps:** `jupyter` and `nbformat` are NOT yet in pyproject.toml — Copilot must add them when creating the first experiment notebook. Also add `matplotlib` for visualizations if not present.
- **Execution:** `uv run jupyter execute <notebook.ipynb> --timeout=300`
- **Convention:** Notebook imports module code from `src/quantipy/alpha/<strategy_name>/` — it orchestrates the experiment, not duplicates the code.

## Shared Experiment Memory

The file `RESEARCH_LOG.md` in the OpenClaw workspace tracks all experiments tried, rejected ideas, and insights. Read it before every ideation round. Update it after every experiment result. Pass this context to Copilot's researcher agent when delegating ideation.

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
