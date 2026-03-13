# Claude Quant - Autonomous AI Trading System

## Project Overview
Autonomous trading system targeting aggressive capital compounding on Binance Futures.
Starting capital: $75. Target: ~7.2% daily compound returns via multi-strategy AI agent system.

## Architecture
- **Orchestrator** (Opus): Runs 5-min loop coordinating all agents
- **Sentinel** (Haiku): Circuit breaker enforcement every cycle
- **Market Analyst** (Sonnet): Technical analysis + regime detection
- **Strategy Selector** (Sonnet): Maps regime to optimal strategy
- **Risk Manager** (Sonnet): Kelly sizing, leverage, position approval
- **Execution Agent** (Sonnet): Order placement and management
- **Memory Agent** (Sonnet): TradeMemory Protocol integration
- **Daily Reporter** (Sonnet): P&L dashboard and alerts

## IMMUTABLE RULES (NEVER OVERRIDE)
1. **Hard floor**: Balance < $30 = HALT ALL TRADING. No exceptions.
2. **Max leverage**: 10x absolute maximum. Circuit breaker reduces this.
3. **Max concurrent positions**: 3
4. **Max capital per trade**: 15% of balance
5. **Max daily trades**: 20
6. **Anti-hallucination**: ALL market data from API/MCP tools only. NEVER fabricate prices.
7. **Risk Manager authority**: NEVER place a trade without Risk Manager approval.
8. **Circuit breakers are HARDCODED**: GREEN/YELLOW/RED/DEAD thresholds cannot be changed by any agent.

## Circuit Breaker Levels
- **GREEN**: Balance > $60 — normal trading
- **YELLOW**: $45-$60 — 50% reduced sizes, max 5x leverage
- **RED**: $30-$45 — 1 position max, 3x leverage, need 2/3 win rate in last 10
- **DEAD**: Balance < $30 — HALT ALL TRADING

## Code Standards
- Python 3.11+, type hints on all functions
- Pydantic models for all data structures
- Async where appropriate (data fetching, order management)
- Every module has `__init__.py` with public API exports
- All monetary values as `Decimal` for precision
- UTC timestamps everywhere
- Structured logging with context (agent name, trade ID)

## Testing
- `pytest tests/ -v` to run all tests
- Integration tests marked with `@pytest.mark.integration`
- All risk/circuit breaker code must have edge case tests

## Key Paths
- Agent definitions: `.claude/agents/`
- Skills: `.claude/skills/`
- Freqtrade strategy: `user_data/strategies/ClaudeQuantAdaptive.py`
- Agent state files: `user_data/agent_state/`
- Config: `config/`
- Reports: `docs/reports/`
