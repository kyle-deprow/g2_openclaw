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
- **OHLCV**: 1-minute bars from Databento (equities)
- **Volume indicators**: VWAP, OBV, MFI, CMF, A/D, VROC, PVT, Klinger, EOM, VWMA, Elder Force, NVI, Chaikin Volatility

## Commands

```bash
cd ~/repos/quantipy
uv sync                          # install deps
uv run pytest -q                 # run tests
uv run ruff check src/           # lint
uv run python -m quantipy --help # CLI
```
