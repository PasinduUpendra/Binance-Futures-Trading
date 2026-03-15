# GitHub Copilot Instructions — Claude Quant

> This file configures GitHub Copilot (and compatible agents) for the Claude Quant trading system.
> It registers skills, agents, and conventions so AI assistants produce code that matches the project.

## Project Overview

Claude Quant is an autonomous AI trading bot for **Binance USDT-M Perpetual Futures**.
Stack: Python 3.11+, ccxt async, Claude API, Binance WebSocket.
Phase: Paper Trading on Testnet ($5000 balance).

## Skills Registry

The following skills are available in `.github/skills/` and `.claude/skills/`:

| Skill | File | Use When |
|-------|------|----------|
| **Binance Futures Trading** | `.github/skills/binance-futures-trading/SKILL.md` | Orders, positions, funding, liquidation, WebSocket, testnet/mainnet |
| **CCXT Integration** | `.github/skills/ccxt-crypto-integration/SKILL.md` | Exchange API calls, ccxt errors, rate limits, async patterns |
| **Backtest Expert** | `.github/skills/backtest-expert/SKILL.md` | Running/analyzing backtests, strategy validation, walk-forward |
| **Quant Finance** | `.github/skills/quant-finance-strategy-risk/SKILL.md` | Risk metrics, position sizing, volatility, regime detection |
| **Advanced Tool Use** | `.github/skills/advanced-tool-use/SKILL.md` | Tool search, MCP integration, multi-tool workflows |

## Agent Registry

9 agents in `.claude/agents/`:

| Agent | Role |
|-------|------|
| `orchestrator` | Master coordinator — 7-step trading loop |
| `sentinel` | Health monitor, circuit breaker enforcement |
| `market-analyst` | Technical analysis, regime detection |
| `strategy-selector` | Maps regime → optimal strategy |
| `risk-manager` | Position sizing, leverage, trade approval |
| `execution-agent` | Order placement on Binance |
| `memory-agent` | Trade memory, learning from outcomes |
| `daily-reporter` | Daily P&L reports |
| `watchdog` | Real-time monitoring, mistake detection |

## Code Conventions

### Python Standards
- Python 3.11+, type hints on ALL functions
- Pydantic models with `frozen=True` for data structures
- `Decimal` for ALL monetary values
- `async`/`await` for exchange I/O
- UTC timestamps everywhere
- Structured logging with context (agent name, trade ID)

### Symbol Format
Always use ccxt format: `ETH/USDT:USDT` (never `ETHUSDT` or `ETH/USDT`)

### Exchange Initialization
```python
exchange.enable_demo_trading(True)   # ✅ Correct
# exchange.set_sandbox_mode(True)    # ❌ NEVER — routes to dead endpoints
```

### Testing
- 228+ tests in `tests/` via pytest
- Run: `python -m pytest tests/ -v`
- Every strategy change needs backtest evidence via `scripts/backtest_v4.py`

### Safety (IMMUTABLE)
1. $30 hard floor — balance < $30 = HALT
2. 10× max leverage
3. 3 max concurrent positions
4. 15% max capital per trade
5. 20 max daily trades
6. All data from API — never fabricate
7. Risk manager approval for every trade
8. Circuit breaker thresholds hardcoded
9. Min 2.0 R/R ratio (SL=3×ATR, TP=6×ATR)
10. 5% liquidation buffer minimum

## Key Paths
| Path | Purpose |
|------|---------|
| `CLAUDE.md` | Project constitution |
| `docs/SINGLE_SOURCE_OF_TRUTH.md` | Complete reference |
| `CHANGELOG.md` | Change history |
| `src/orchestrator/main.py` | 7-step trading loop |
| `src/risk/circuit_breaker.py` | Safety-critical CB levels |
| `src/strategies/supertrend_trend.py` | Active strategy |
| `src/execution/order_manager.py` | Order execution |
| `scripts/backtest_v4.py` | Production-code backtest |
| `config/` | YAML configuration files |
