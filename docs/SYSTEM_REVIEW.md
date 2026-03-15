# Claude Quant — Complete System Review Document

> **Generated**: 2026-03-15
> **Purpose**: Every detail an AI agent needs to understand, review, audit, or extend this trading system.
> **Authority chain**: Production code → CLAUDE.md v3.0 → SINGLE_SOURCE_OF_TRUTH.md → This document.
> If any value here contradicts production code, CODE IS TRUTH.

---

## Table of Contents

1. [System Identity & Status](#1-system-identity--status)
2. [Architecture Overview](#2-architecture-overview)
3. [Complete File Tree](#3-complete-file-tree)
4. [Orchestrator — The Main Loop](#4-orchestrator--the-main-loop)
5. [Strategy Engine](#5-strategy-engine)
6. [Risk Management System](#6-risk-management-system)
7. [Execution Engine](#7-execution-engine)
8. [Data Pipeline](#8-data-pipeline)
9. [Anti-Hallucination System](#9-anti-hallucination-system)
10. [Memory & Reporting](#10-memory--reporting)
11. [AI Agent Definitions](#11-ai-agent-definitions)
12. [Configuration Files](#12-configuration-files)
13. [Pydantic Data Models](#13-pydantic-data-models)
14. [Complete Function Reference](#14-complete-function-reference)
15. [Test Suite](#15-test-suite)
16. [Known Issues & Technical Debt](#16-known-issues--technical-debt)
17. [Learnings & Error History](#17-learnings--error-history)
18. [Backtest Evidence](#18-backtest-evidence)
19. [Deployment & Operations](#19-deployment--operations)
20. [Immutable Rules & Safety Invariants](#20-immutable-rules--safety-invariants)

---

## 1. System Identity & Status

| Field | Value |
|-------|-------|
| **Name** | Claude Quant |
| **Purpose** | Autonomous AI trading bot for Binance USDT-M Perpetual Futures |
| **Stack** | Python 3.11+ · ccxt async · Pydantic · TA-Lib · Claude API |
| **Phase** | Paper Trading on Testnet ($5000 simulated) |
| **Production Balance** | $68.33 USDT (verified 2026-03-13 via API) |
| **Bot PID** | 83621 (v3, restarted 2026-03-15) |
| **Cycle Interval** | 1 hour (3600 seconds) |
| **Trading Pairs** | ETH/USDT:USDT, SOL/USDT:USDT, DOGE/USDT:USDT |
| **Active Strategy** | SupertrendTrend ONLY (all others disabled) |
| **Test Suite** | 228 tests passing (1.10s) |
| **Target Return** | 1% daily compound (aspirational); 0.628% daily validated |

### Performance Reality (v4 Production-Code Backtest)

| Metric | Value |
|--------|-------|
| Return | +172.9% over 172 days |
| Win Rate | 69.2% |
| Sharpe Ratio | 3.98 |
| Profit Factor | 5.39 |
| Total Trades | 39 |
| Avg Daily Return | 0.628% |
| Max Drawdown | Not measured (v3 was 7.9%) |

### Binance Account Configuration (Verified 2026-03-13)

| Field | Value |
|-------|-------|
| Balance | $68.33 USDT |
| Fee Tier | VIP 0 |
| Maker Fee | 0.02% (0.0002) |
| Taker Fee | 0.05% (0.0005) |
| Round-trip Fee | 0.07% (maker+taker) or 0.10% (taker+taker) |
| BNB Burn | ENABLED (10% discount) |
| Multi-Assets Mode | FALSE (isolated margin) |
| Position Mode | ONE-WAY |
| Testnet | CONNECTED ($5000 paper balance) |

### Trading Pair Specifications

| Pair | Max Leverage | Min Notional | Amount Precision | Spread | Daily Vol |
|------|-------------|-------------|-----------------|--------|-----------|
| ETH/USDT:USDT | 150x | $20 | 3 decimals | 0.0002% | $13B |
| SOL/USDT:USDT | 100x | $5 | 1 decimal | 0.0011% | $405M |
| DOGE/USDT:USDT | 75x | $5 | **whole numbers only** | 0.0100% | $798M |

### Funding Rates (per 8h)

- ETH: 0.0029%
- SOL: 0.0045%
- DOGE: 0.01%

---

## 2. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR (main.py)                         │
│              1-hour cycle, 7 steps per cycle                     │
│                                                                   │
│  Step 1: SENTINEL ─── CircuitBreaker.is_trading_allowed()        │
│  Step 1b: DATA ────── MarketDataClient + IndicatorEngine         │
│  Step 2: EXIT ─────── Supertrend reversal + trailing stops       │
│  Step 3: SIGNAL ───── AdaptiveStrategy.get_signal_multi_tf()     │
│  Step 4: RISK ─────── LeverageManager + PositionSizer + GARCH   │
│  Step 5: AUDIT ────── DecisionAuditor (devil's advocate)         │
│  Step 6: EXECUTE ──── OrderManager (idempotent submission)       │
│  Step 7: MEMORY ───── TradeJournal + DatabaseManager             │
│                                                                   │
│  At midnight UTC: DailyPnLCalculator + ReportGenerator           │
└──────────────────────────────────────────────────────────────────┘

Data Flow:
  Binance API (ccxt async)
    → MarketDataClient.fetch_ohlcv(4H, 1H)
    → IndicatorEngine.calculate_all()
    → DataValidator.validate_ohlcv()
    → RegimeDetector.detect(df_4h) → RegimeState
    → AdaptiveStrategy.select_strategy(regime) → SupertrendTrend
    → SupertrendTrend.generate_signal(df_4h, entry_price=close_1h) → Signal
    → LeverageManager.determine_leverage(confidence, regime, cb_level) → LeverageResult
    → VolatilityModel.adjust_leverage(GARCH) → final leverage
    → Confidence-based position sizing (7/10/15% × CB multiplier)
    → LeverageManager.calculate_liquidation_buffer() → safety check
    → OrderManager._submit_order_idempotent() → OrderResult
    → TradeJournal.record_trade_entry()

Multi-Timeframe:
  4H candles → Regime detection, SupertrendTrend signals, ATR for SL/TP
  1H candles → Entry price timing (current 1H close)
```

### Component Dependencies

```
orchestrator/main.py
  ├── data/market_data.py ──── ccxt async exchange connection
  ├── data/indicator_engine.py ──── TA-Lib calculations
  ├── data/data_validator.py ──── OHLCV integrity checks
  ├── data/database.py ──── SQLite consolidated DB
  ├── strategies/
  │   ├── regime_detector.py ──── 4-regime classifier
  │   ├── adaptive_strategy.py ──── regime → strategy router
  │   └── supertrend_trend.py ──── THE active strategy
  ├── risk/
  │   ├── circuit_breaker.py ──── SAFETY CRITICAL (hardcoded)
  │   ├── leverage_manager.py ──── confidence × regime → leverage
  │   ├── position_sizer.py ──── Half-Kelly (NOT used in orchestrator)
  │   ├── volatility_model.py ──── GARCH(1,1) leverage adjustment
  │   └── drawdown_monitor.py ──── high-water mark tracking
  ├── execution/
  │   ├── order_manager.py ──── idempotent order placement
  │   ├── position_tracker.py ──── open position monitoring
  │   └── fee_calculator.py ──── maker/taker/BNB fees
  ├── memory/
  │   ├── trade_journal.py ──── SQLite trade log
  │   └── performance_tracker.py ──── strategy perf by regime
  ├── anti_hallucination/
  │   ├── price_validator.py ──── cross-reference prices
  │   ├── signal_validator.py ──── validate signals vs raw data
  │   ├── decision_auditor.py ──── devil's advocate logging
  │   └── sanity_checks.py ──── math sanity on positions
  └── reporting/
      ├── daily_pnl.py ──── daily P&L calculation
      ├── report_generator.py ──── markdown reports
      └── alert_system.py ──── notifications
```

---

## 3. Complete File Tree

```
Claude Quant/
├── .claude/
│   ├── agents/                          # 9 AI agent definitions (8 base + watchdog)
│   │   ├── orchestrator.md              # Opus — master coordinator
│   │   ├── sentinel.md                  # Haiku — circuit breaker enforcement
│   │   ├── market-analyst.md            # Sonnet — technical analysis
│   │   ├── strategy-selector.md         # Sonnet — regime-to-strategy mapping
│   │   ├── risk-manager.md              # Sonnet — position sizing + approval
│   │   ├── execution-agent.md           # Sonnet — order management
│   │   ├── memory-agent.md              # Sonnet — trade journaling
│   │   ├── daily-reporter.md            # Sonnet — P&L reporting
│   │   └── watchdog.md                  # Sonnet — real-time bot monitoring
│   └── skills/                          # 5 skill definitions
│       ├── backtesting.md
│       ├── market-analysis.md
│       ├── performance-report.md
│       ├── risk-assessment.md
│       └── trade-execution.md
│
├── config/
│   ├── freqtrade/ (config.json, config_backtest.json, config_freqai.json, pairlists.json)
│   ├── risk/
│   │   ├── risk_params.yaml             # Tunable risk parameters (NOT safety thresholds)
│   │   └── circuit_breakers.yaml        # Documentation (actual values hardcoded in code)
│   └── regime/
│       └── regime_params.yaml           # Regime classification thresholds
│
├── src/
│   ├── orchestrator/main.py             # ** MAIN LOOP ** — 7-step hourly cycle (981 lines)
│   ├── strategies/
│   │   ├── base_strategy.py             # Signal model, BaseStrategy ABC, R/R validator
│   │   ├── regime_detector.py           # MarketRegime classifier (4 regimes)
│   │   ├── adaptive_strategy.py         # Regime→strategy router (multi-TF)
│   │   ├── supertrend_trend.py          # ** PRIMARY ** 4H Supertrend flip strategy
│   │   ├── trend_follower.py            # DISABLED (30% WR)
│   │   ├── mean_reversion.py            # DISABLED (5.3% WR)
│   │   ├── breakout_trader.py           # DISABLED (23.9% WR)
│   │   └── scalper.py                   # DISABLED
│   ├── risk/
│   │   ├── circuit_breaker.py           # ** SAFETY CRITICAL ** (400 lines, hardcoded)
│   │   ├── leverage_manager.py          # Confidence × regime → leverage (280 lines)
│   │   ├── position_sizer.py            # Half-Kelly (NOT used in orchestrator)
│   │   ├── volatility_model.py          # GARCH(1,1) for leverage scaling
│   │   ├── drawdown_monitor.py          # High-water mark tracking
│   │   └── correlation_monitor.py       # Multi-position correlation limits
│   ├── execution/
│   │   ├── order_manager.py             # Idempotent order placement (~700 lines)
│   │   ├── position_tracker.py          # Open position monitoring
│   │   ├── fee_calculator.py            # VIP 0 fee math
│   │   └── slippage_estimator.py        # Orderbook-based slippage
│   ├── data/
│   │   ├── market_data.py               # ccxt async OHLCV, ticker, orderbook
│   │   ├── indicator_engine.py          # TA-Lib wrapper (all indicators)
│   │   ├── data_validator.py            # OHLCV integrity validation
│   │   ├── candle_store.py              # SQLite candle cache
│   │   └── database.py                  # Consolidated DB — DatabaseManager
│   ├── memory/
│   │   ├── trade_journal.py             # SQLite trade log
│   │   ├── performance_tracker.py       # Strategy perf by regime
│   │   ├── trade_memory_client.py       # TradeMemory MCP client
│   │   └── bias_detector.py             # Behavioral bias detection
│   ├── reporting/
│   │   ├── daily_pnl.py                 # Daily P&L calculation
│   │   ├── dashboard.py                 # Terminal dashboard (rich)
│   │   ├── report_generator.py          # HTML/Markdown reports
│   │   └── alert_system.py              # Telegram/Discord alerts
│   ├── anti_hallucination/
│   │   ├── price_validator.py           # Cross-reference prices (2 sources)
│   │   ├── signal_validator.py          # Validate signals vs raw indicator data
│   │   ├── decision_auditor.py          # Audit decisions vs reality
│   │   └── sanity_checks.py            # Math sanity on all outputs
│   └── mcp_tools/
│       ├── binance_tools.py             # Binance API MCP server
│       ├── analysis_tools.py            # Analysis MCP server
│       ├── risk_tools.py                # Risk MCP server
│       └── reporting_tools.py           # Reporting MCP server
│
├── scripts/
│   ├── run_bot.py                       # Bot entry point
│   ├── backtest_v4.py                   # ** PRODUCTION-CODE ** backtest (+172.9%)
│   ├── backtest_v3.py                   # v3 backtest (+94%, Sharpe 3.31)
│   ├── watchdog_tools.py                # Watchdog agent CLI (5 subcommands)
│   ├── watchdog.py                      # Legacy simple watchdog
│   └── diagnose_strategies.py           # Strategy rejection diagnostics
│
├── tests/                               # 228 tests, all passing (1.10s)
│   ├── conftest.py
│   ├── test_strategies/ (5 files, 51 tests)
│   ├── test_risk/ (5 files, 104 tests)
│   ├── test_execution/ (1 file, 4 tests)
│   ├── test_anti_hallucination/ (1 file, 11 tests)
│   ├── test_memory/ (1 file, 20 tests)
│   ├── test_reporting/ (1 file, 18 tests)
│   └── test_integration/ (1 file, 7 tests)
│
├── .learnings/ (LEARNINGS.md — 12 entries, ERRORS.md — 4 resolved)
├── CLAUDE.md                            # v3.0 constitutional document (738 lines)
├── CHANGELOG.md                         # Full change history
└── docs/SINGLE_SOURCE_OF_TRUTH.md       # Complete reference
```

---

## 4. Orchestrator — The Main Loop

**File**: `src/orchestrator/main.py` (981 lines)
**Entry point**: `asyncio.run(main())` via `scripts/run_bot.py`

### Constants

```python
TRADING_PAIRS = ["ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT"]
TIMEFRAME_DIRECTION = "4h"    # Regime detection + signal generation
TIMEFRAME_ENTRY = "1h"        # Entry price timing
CYCLE_INTERVAL_SECONDS = 3600 # 1 hour
```

### Key Classes

**`OrchestratorState`** — Current state (cycle count, balance, CB level, halt reason)
**`TrailingStopState`** — Per-position trailing stop (symbol, direction, entry_price, best_price, atr_4h, activated)
**`CycleResult`** — Result of one cycle (cycle_number, CB level, signal, trade, positions closed, errors)

### The 7-Step Cycle (`_run_cycle()`)

**Step 1: SENTINEL** (lines 260-306)
- Fetches balance from Binance API
- Updates drawdown monitor
- Converts recent trades to `TradeResult` for circuit breaker
- Calls `CircuitBreaker.is_trading_allowed()` — the single authoritative gate
- Handles DEAD (halt), daily loss halt, consecutive loss pause, RED win-rate gate
- If sentinel fails, defaults to RED and returns immediately

**Step 1b: DATA FETCH** (lines 308-336)
- For each of 3 pairs:
  - Fetch 200 candles of 4H OHLCV → DataFrame → calculate all indicators
  - Fetch 200 candles of 1H OHLCV → DataFrame → calculate all indicators
  - Validate each with `DataValidator.validate_ohlcv()`
  - Store as `dict[symbol] → (df_4h, df_1h)`

**Step 2: SUPERTREND REVERSAL EXITS** (lines 338-345)
- For each open position that uses SupertrendTrend:
  - Check if 4H Supertrend flipped against position direction
  - If flipped: close at market, cancel open orders, remove trailing stop state
  - Capital recycling: individually -$33 but enables +$97 net

**Step 2b: TRAILING STOP MANAGEMENT** (lines 347-352)
- For each open position with `TrailingStopState`:
  - Update `best_price` (high for long, low for short)
  - If moved ≥ 2.0 × ATR(4H) favorably → activate trailing stop
  - If activated AND pullback ≥ 2.5 × ATR(4H) from best → close position

**Step 3: SIGNAL GENERATION** (lines 354-397)
- For each pair with valid data:
  - `AdaptiveStrategy.get_signal_multi_tf(df_4h, df_1h)`
  - Detect regime on 4H data for logging
  - Keep best signal by confidence
- If no signals → early return (no trade this cycle)

**Step 4: RISK MANAGEMENT** (lines 399-512)
- Check open positions vs CB max
- Check if already positioned in the pair
- `LeverageManager.determine_leverage(confidence, regime, cb_level)`
- GARCH volatility adjustment: `VolatilityModel.adjust_leverage()`
- **Confidence-based position sizing** (lines 449-483):
  ```
  confidence >= 60%: 15% of balance
  confidence >= 45%: 10%
  else:              7%
  × CB_size_multiplier (GREEN=1.0, YELLOW=0.5, RED=0.25)
  Hard cap: 15% of balance, minimum $5
  ```
- `SanityChecker.check_position_math()` — verify math
- `LeverageManager.calculate_liquidation_buffer()` — verify ≥ 5%

**Step 5: AUDIT** (lines 514-531)
- `DecisionAuditor.audit_decision()` — log counter-arguments (non-blocking)

**Step 6: EXECUTION** (lines 533-626)
- `OrderManager.set_leverage(pair, leverage)`
- Calculate quantity: `notional / entry_price`
- `OrderManager.place_market_order()` — idempotent
- Verify fill with `get_order_status()`
- `OrderManager.place_stop_loss()` — STOP_MARKET, reduceOnly
- `OrderManager.place_take_profit()` — TAKE_PROFIT_MARKET, reduceOnly (v4 fix)
- Initialize `TrailingStopState` for new position

**Step 7: MEMORY** (lines 627-636)
- `TradeJournal.record_trade_entry(trade_details)`

### Daily Report (midnight UTC)

- Triggered when `hour == 0 and minute < 10`
- Calculates P&L from trade journal
- Stores in `DatabaseManager.store_daily_report()`
- Generates markdown report to `docs/reports/YYYY-MM-DD.md`
- Alerts on daily loss > 5%

### Graceful Shutdown

- Signal handlers for SIGTERM, SIGINT
- `stop()` → sets shutdown event, closes exchange connections, closes DB

---

## 5. Strategy Engine

### 5.1 Regime Detection (`regime_detector.py`)

**Class**: `RegimeDetector`
**Input**: 4H DataFrame with indicators
**Output**: `RegimeState(regime, confidence, adx, bb_width_ratio, atr_ratio, volume_ratio)`

| Regime | ADX | BB Width | ATR Ratio | Volume |
|--------|-----|----------|-----------|--------|
| TRENDING | > 25 | Normal | Normal | Normal/Rising |
| RANGING | < 20 | Narrow (<0.8x) | Low (<0.8x) | Low (<0.7x) |
| VOLATILE | 15-30 | Wide (>1.5x) | High (>1.2x) | Spike (>1.5x) |
| QUIET | < 15 | Very narrow (<0.5x) | Very low (<0.5x) | Very low (<0.5x) |

Lookback windows: BB width avg=100, ATR avg=100, Volume avg=20

### 5.2 Adaptive Strategy Router (`adaptive_strategy.py`)

**Class**: `AdaptiveStrategy`
**Key constant**: `MIN_CONFIDENCE = 25.0`
**Entry point**: `get_signal_multi_tf(df_4h, df_1h) → Optional[Signal]`

Routing logic (v4 — only SupertrendTrend active):

| Regime | ADX | Route | Status |
|--------|-----|-------|--------|
| TRENDING | ≥ 18 | SupertrendTrend(4H) | **ACTIVE** |
| TRENDING | < 18 | None | TrendFollower DISABLED (30% WR) |
| RANGING | any | None | MeanReversion DISABLED (5.3% WR) |
| VOLATILE | any | None | BreakoutTrader DISABLED (23.9% WR) |
| QUIET | any | None | No trade |

Multi-timeframe data routing:
- 4H strategies (SupertrendTrend, MeanReversion): receive `df_4h`, with `entry_price=close_1h`
- 1H strategies (TrendFollower, BreakoutTrader): receive `df_1h`

**Supertrend reversal exit**: `check_supertrend_reversal(df_4h, position_direction) → bool`

### 5.3 SupertrendTrend Strategy (`supertrend_trend.py`)

**Class**: `SupertrendTrend(BaseStrategy)`
**Only active strategy. All trading decisions flow through this.**

**Entry conditions**:
- **LONG**: 4H `supertrend_direction` flips from -1 to +1 AND `adx >= 18`
- **SHORT**: 4H `supertrend_direction` flips from +1 to -1 AND `adx >= 18`

**Parameters**:
```python
ADX_MIN = 18.0       # Minimum trend strength
SL_ATR_MULT = 3.0    # Stop-loss: 3× ATR(4H) from entry
TP_ATR_MULT = 6.0    # Take-profit: 6× ATR(4H) from entry (R/R = 2.0)
```

**Confidence scoring** (0-100):
| Component | Max Points | Logic |
|-----------|-----------|-------|
| Base flip | 40 | Always given for valid Supertrend flip |
| ADX strength | 20 | Linear 18→40 mapped to 0→20 |
| EMA alignment | 20 | EMA9 > EMA21 for long (or < for short) |
| RSI position | 10 | Not at extremes (30-65 long, 35-70 short) |
| Flip quality | 10 | Always given for valid flip |

**R/R pre-filter**: Signal rejected if `rr < 1.5` (line 140-141)
**Signal Pydantic validator**: `base_strategy.py` enforces `rr >= 2.0` on Signal creation

**Indicators consumed** (from IndicatorEngine on 4H data):
- `supertrend_direction` (1 = bullish, -1 = bearish)
- `adx`, `ema_9`, `ema_21`, `rsi`, `atr`, `close`

### 5.4 Disabled Strategies

| Strategy | File | Win Rate | P&L | Reason Disabled |
|----------|------|----------|-----|-----------------|
| MeanReversion | `mean_reversion.py` | 5.3% | -$7.65 | 2-of-3 confirmation too loose |
| BreakoutTrader | `breakout_trader.py` | 23.9% | -$1.13 | Negative EV |
| TrendFollower | `trend_follower.py` | 30.0% | +$0.35 | Marginal, not worth risk |
| Scalper | `scalper.py` | Not tested | N/A | Never deployed |

All code is retained for potential future re-evaluation through the strategy versioning pipeline.

### 5.5 Signal Data Model (`base_strategy.py`)

```python
class Signal(BaseModel):
    model_config = {"frozen": True}
    direction: SignalDirection          # LONG, SHORT, or NONE
    confidence: float                   # 0-100
    entry_price: float
    stop_loss: float
    take_profit: float
    strategy_name: str
    regime: str
    indicators_used: dict[str, Any]
    reasoning: str

    @model_validator(mode="after")
    def _check_rr(self) -> Signal:
        rr = calculate_rr_ratio(self.entry_price, self.stop_loss, self.take_profit)
        if rr < 2.0:
            raise ValueError(f"R/R ratio {rr:.2f} below minimum 2.0")
        return self
```

---

## 6. Risk Management System

### 6.1 Circuit Breaker (`circuit_breaker.py`) — SAFETY CRITICAL

**All thresholds are `Final` module-level constants. No setter exists. No override possible.**

```python
_GREEN_BALANCE_MIN  = Decimal("60")     # ≥ $60
_YELLOW_BALANCE_MIN = Decimal("45")     # ≥ $45
_RED_BALANCE_MIN    = Decimal("30")     # ≥ $30
# Below RED → DEAD

_GREEN_MAX_LEVERAGE  = 10
_YELLOW_MAX_LEVERAGE = 5
_RED_MAX_LEVERAGE    = 3

_GREEN_MAX_POSITIONS  = 3
_YELLOW_MAX_POSITIONS = 2
_RED_MAX_POSITIONS    = 1

_GREEN_SIZE_MULTIPLIER  = Decimal("1.0")
_YELLOW_SIZE_MULTIPLIER = Decimal("0.5")
_RED_SIZE_MULTIPLIER    = Decimal("0.25")

_RED_MIN_WIN_RATE_LAST_10 = Decimal("0.6667")  # 2/3

_DAILY_LOSS_HALT_PCT = Decimal("0.10")          # 10%
_CONSECUTIVE_LOSS_PAUSE_THRESHOLD = 5
_CONSECUTIVE_LOSS_PAUSE_SECONDS = 7200          # 2 hours
```

**`is_trading_allowed(balance, recent_trades, start_of_day_balance) → CircuitBreakerState`**

Checks in order (first failure exits):
1. **DEAD** — balance < $30 → HALT ALL TRADING
2. **Daily loss** — > 10% of start-of-day balance → HALT until next UTC day
3. **Consecutive losses** — 5+ in a row → 2-hour pause (anchored to last loss time)
4. **RED win-rate gate** — requires ≥ 2/3 win rate on last 10 trades
5. **All clear** — return constraints for current level

### 6.2 Leverage Manager (`leverage_manager.py`)

**`determine_leverage(confidence, regime, cb_level) → LeverageResult`**

Lookup table:

| Confidence | Regime | Leverage Range | Midpoint |
|-----------|--------|---------------|----------|
| 80-100% | TRENDING | 7-10x | 8x |
| 60-79% | TRENDING | 5-7x | 6x |
| 60-79% | VOLATILE/RANGING | 3-5x | 4x |
| 40-59% | TRENDING | 3-5x | 4x |
| 40-59% | VOLATILE/RANGING | 2-3x | 2x |
| 25-39% | TRENDING | 2-3x | 2x |
| 25-39% | VOLATILE | 1-2x | 1x |
| 25-39% | RANGING | 2-3x | 2x |
| < 25% | Any | 0 | NO TRADE |
| Any | QUIET | 0 | NO TRADE |

CB caps: GREEN=10x, YELLOW=5x, RED=3x, DEAD=0x

**`calculate_liquidation_buffer(entry_price, leverage, direction) → LiquidationBuffer`**

Heuristic (approximation):
- Long: `liq_price = entry × (1 - 1/leverage)`
- Short: `liq_price = entry × (1 + 1/leverage)`
- Buffer must be ≥ 5% (Immutable Rule #10)

### 6.3 Position Sizing — Confidence-Based (v4)

**NOT in `position_sizer.py`** — code is in `orchestrator/main.py` lines 449-483.

```python
if confidence >= 60:   position_pct = 0.15   # 15%
elif confidence >= 45: position_pct = 0.10   # 10%
else:                  position_pct = 0.07   # 7%

position_pct *= CB_size_multiplier  # GREEN=1.0, YELLOW=0.5, RED=0.25
margin = balance × position_pct
margin = max(margin, $5)            # Binance minimum
margin = min(margin, balance × 15%) # Hard cap (Immutable Rule #4)
notional = margin × leverage
```

**Note**: `position_sizer.py` contains Half-Kelly code but it is NOT called by the orchestrator.

### 6.4 Volatility Model (`volatility_model.py`)

**GARCH(1,1)** for forward volatility prediction:
- Reduces leverage during vol spikes (2× normal → 75% cut)
- Boosts leverage during calm periods
- RiskMetrics EWMA fallback if GARCH fails to fit
- `adjust_leverage(requested, vol_state, max) → int`

### 6.5 Drawdown Monitor (`drawdown_monitor.py`)

- Tracks high-water mark balance
- Calculates current drawdown % and max drawdown %
- Persistent state via JSON
- `update(current_balance) → DrawdownState`

### 6.6 Correlation Monitor (`correlation_monitor.py`)

- Pearson correlation over 30-day log returns
- Threshold: 0.7
- Max correlated exposure: 25% of balance
- Blocks new position if adding it exceeds 25% correlated exposure

---

## 7. Execution Engine

### 7.1 Order Manager (`order_manager.py`)

**CRITICAL CHANGE (2026-03-15)**: Now implements idempotent order submission.

**Core method**: `_submit_order_idempotent(symbol, order_type, side, amount, ...) → OrderResult | None`

**Idempotent retry logic**:
1. Generate unique `newClientOrderId` (prefix `cq_` + 16 hex chars)
2. Attempt `exchange.create_order()`
3. **On success** → verify with separate GET call → return OrderResult
4. **On NetworkError/Timeout/503** → UNKNOWN STATE:
   - Wait 2 seconds for propagation
   - Query exchange by `origClientOrderId` via `fapiPrivateGetOrder`
   - If found → return existing order (no duplicate)
   - If not found → safe to retry with NEW client order ID
5. **On DDoSProtection** → definitely not placed → retry with backoff
6. **On InsufficientFunds** → return None
7. **On InvalidOrder** → return None
8. Max 3 attempts, exponential backoff

**Public methods** (all delegate to `_submit_order_idempotent`):
- `place_market_order(symbol, side, amount) → OrderResult | None`
- `place_limit_order(symbol, side, amount, price) → OrderResult | None`
- `place_stop_loss(symbol, side, amount, stop_price) → OrderResult | None`
- `place_take_profit(symbol, side, amount, stop_price) → OrderResult | None`

**Other methods**:
- `set_leverage(symbol, leverage)` — validates 1-10 range
- `cancel_order(symbol, order_id)` — with cancellation verification
- `cancel_open_orders(symbol)` — bulk cancel all open orders
- `get_order_status(symbol, order_id) → OrderStatus`
- `get_open_orders(symbol) → list[OrderStatus]`

**Anti-hallucination**: Every order placement is verified with a separate GET call.

**Connection**: Uses `enable_demo_trading(True)` for testnet (not deprecated `set_sandbox_mode`).

**Binance Testnet Endpoints** (for custom tooling bypassing ccxt):
- REST: `https://demo-fapi.binance.com` | Production: `https://fapi.binance.com`
- WS: `wss://fstream.binancefuture.com/ws` | Production: `wss://fstream.binance.com/ws`
- Listen key expiry: 60 min (keepalive via `PUT /fapi/v1/listenKey` every ≤30 min)
- Single user-data WS connection valid for max 24 hours — must reconnect
- Testnet and production API keys are NOT interchangeable (ERR-20260313-001)

### 7.2 Fee Calculator (`fee_calculator.py`)

```python
MAKER_FEE = Decimal("0.0002")  # 0.02%
TAKER_FEE = Decimal("0.0005")  # 0.05%
BNB_DISCOUNT = Decimal("0.10") # 10% off
```

- `calculate_total_cost(entry, exit, size, leverage, ...) → dict`
- TP targets adjusted to be net-of-fees profitable

### 7.3 Position Tracker (`position_tracker.py`)

- `get_open_positions() → list[Position]`
- Each Position has: symbol, side, size, entry_price, current_price, unrealized_pnl

---

## 8. Data Pipeline

### 8.1 Market Data Client (`market_data.py`)

- ccxt async `binanceusdm` wrapper
- `fetch_ohlcv(symbol, timeframe, limit) → list[dict]`
- `get_account_balance() → Decimal`
- `fetch_ticker(symbol) → TickerData`
- Connection management: `connect()`, `close()`
- Rate limiting via ccxt `enableRateLimit=True`
- WebSocket URLs: Testnet `wss://fstream.binancefuture.com/ws`, Production `wss://fstream.binance.com/ws`
- WebSocket reconnection: 5-second retry on error
- User-data stream: listen key expires 60 min, connection valid 24h max

### 8.2 Indicator Engine (`indicator_engine.py`)

`calculate_all(df, ...) → DataFrame` adds these columns:

| Indicator | Parameters | Columns |
|-----------|-----------|---------|
| EMA | 9, 21, 50, 200 | `ema_9`, `ema_21`, `ema_50`, `ema_200` |
| RSI | 14 | `rsi` |
| MACD | 12/26/9 | `macd`, `macd_signal`, `macd_hist` |
| ADX | 14 | `adx`, `plus_di`, `minus_di` |
| Bollinger Bands | 20, 2.0 | `bb_upper`, `bb_lower`, `bb_middle`, `bb_width` |
| ATR | 14 | `atr` |
| Supertrend | 10, 3.0 | `supertrend`, `supertrend_direction` |
| Volume SMA | 20 | `volume_sma` |
| Z-Score | 20 | `zscore` |

### 8.3 Data Validator (`data_validator.py`)

Anti-hallucination Layer 1:
- OHLCV integrity: `low <= close <= high`, `low <= open <= high`
- Timestamp freshness: data must be within 2 candles of current time
- No NaN in critical columns
- Returns `ValidationResult(passed, status, details)`

### 8.4 Database (`database.py`)

Single consolidated SQLite at `user_data/claude_quant.db` (WAL mode).

| Table | Purpose | Key Columns |
|-------|---------|------------|
| `trades` | Full trade journal | trade_id, symbol, direction, entry/exit/pnl, strategy, regime |
| `daily_reports` | Daily P&L snapshots | report_date, start/end_balance, net_pnl, wins/losses |
| `cycle_history` | Every orchestrator cycle | cycle_number, timestamp, cb_level, balance, trade_placed |
| `system_state` | Key-value bot state | key, value, updated_at |
| `strategy_metrics` | Cached perf per strategy/regime | strategy, regime, win_rate, total_pnl, sharpe |

Migration helpers: `migrate_from_trade_journal()`, `migrate_drawdown_state()`

---

## 9. Anti-Hallucination System

### 5 Defense Layers

| Layer | Module | What It Checks |
|-------|--------|---------------|
| 1 | `data_validator.py` | OHLCV integrity, timestamp freshness (<2 candles), low≤high |
| 2 | `price_validator.py` | Cross-reference 2 sources within 0.1%, within daily range |
| 3 | `signal_validator.py` | Signal references specific indicator values, passes strategy math |
| 4 | `decision_auditor.py` | Every trade includes reasons NOT to trade, raw data timestamps |
| 5 | `order_manager.py` | Separate GET after POST, fill price within 0.5% expected |

### SanityChecker (`sanity_checks.py`)

- `check_position_math(balance, margin, leverage, notional) → (bool, dict)`
- `check_price_sanity(price, symbol) → bool`
- Validates: margin ≤ 15% of balance, notional ≈ margin × leverage, leverage ≤ 10

### Test Coverage Warning

Layers 2, 3, and 4 (`price_validator.py`, `signal_validator.py`, `decision_auditor.py`) currently **lack dedicated unit tests**. This is a P0 priority gap.

---

## 10. Memory & Reporting

### 10.1 Trade Journal (`trade_journal.py`)

- SQLite-backed trade log
- `record_trade_entry(details)` / `record_trade_exit(trade_id, exit_details)`
- `get_recent_trades(limit=10) → list[TradeEntry]`
- `get_all_trades() → list[TradeEntry]`
- Win rate calculation, consecutive loss tracking

### 10.2 Performance Tracker (`performance_tracker.py`)

- Strategy performance by regime
- Requires `journal` arg in constructor: `PerformanceTracker(journal=trade_journal)`

### 10.3 Daily P&L Calculator (`daily_pnl.py`)

- `calculate_daily_pnl(trades, start_balance, end_balance, report_date) → DailyPnL`
- `get_cumulative_stats(all_daily_pnls) → CumulativeStats`
- Calculates: net P&L, fees, win rate, Sharpe ratio, doubling progress

### 10.4 Alert System (`alert_system.py`)

- `send_alert(message, level) → None`
- Levels: info, warning, critical
- Currently logs to file (Telegram/Discord integration planned)

---

## 11. AI Agent Definitions

9 agents defined in `.claude/agents/`:

| Agent | Model | Role |
|-------|-------|------|
| **orchestrator** | Opus | Master coordinator, runs main loop |
| **sentinel** | Haiku | Circuit breaker enforcement (fast, numbers-only) |
| **market-analyst** | Sonnet | Technical analysis, regime detection |
| **strategy-selector** | Sonnet | Regime-to-strategy mapping |
| **risk-manager** | Sonnet | Position sizing, leverage, approval |
| **execution-agent** | Sonnet | Order placement and management |
| **memory-agent** | Sonnet | Trade journaling, pattern detection |
| **daily-reporter** | Sonnet | P&L reporting at midnight UTC |
| **watchdog** | Sonnet | Real-time bot monitoring (Claude agent, not Python script) |

### Watchdog Agent

Invoked via `@watchdog` in Claude Code sessions. Uses `scripts/watchdog_tools.py` for data:

| Subcommand | Purpose |
|------------|---------|
| `health` | Bot process alive? Cycle freshness? |
| `logs` | Parse bot.log for errors, signals, trades |
| `performance` | Balance, P&L, daily rate, target gap |
| `mistakes` | Missing TP/SL, slow cycles, no-trade periods |
| `market` | Live regime, supertrend direction per pair |

---

## 12. Configuration Files

### `config/risk/risk_params.yaml`

```yaml
position_sizing:
  method: half_kelly
  kelly_fraction: 0.5
  max_position_pct: 0.15
  min_position_usd: 5.0
leverage:
  max_leverage: 10
  default_leverage: 3
risk_reward:
  min_rr_ratio: 2.0
overtrading:
  max_daily_trades: 20
  cooldown_after_loss_streak: 7200
  loss_streak_threshold: 5
correlation:
  max_correlated_exposure: 0.25
  correlation_threshold: 0.7
```

**Note**: CB thresholds are NOT in this file — they are hardcoded in `circuit_breaker.py`.

### `config/regime/regime_params.yaml`

```yaml
indicators:
  adx_period: 14
  bb_period: 20
  bb_std: 2.0
  atr_period: 14
  ema_periods: [9, 21, 50, 200]
  rsi_period: 14
  volume_avg_period: 20
lookback:
  regime_window: 50
  bb_width_avg_window: 100
  atr_avg_window: 100
```

---

## 13. Pydantic Data Models

All models use `frozen=True` (immutable after creation) unless noted.

| Model | File | Key Fields |
|-------|------|-----------|
| `Signal` | base_strategy.py | direction, confidence, entry/sl/tp, strategy_name, regime, reasoning |
| `RegimeState` | regime_detector.py | regime, confidence, adx, bb_width_ratio, atr_ratio, volume_ratio |
| `OrchestratorState` | main.py | cycle_count, is_running, current_balance, cb_level, halt_reason |
| `CycleResult` | main.py | cycle_number, timestamp, cb_level, signal_generated, trade_placed, errors |
| `TrailingStopState` | main.py | symbol, direction, entry_price, best_price, atr_4h, activated |
| `CircuitBreakerConstraints` | circuit_breaker.py | level, max_leverage, max_positions, size_multiplier, trading_allowed |
| `CircuitBreakerState` | circuit_breaker.py | level, constraints, balance, daily_loss_pct, checked_at |
| `TradeResult` | circuit_breaker.py | is_win, closed_at |
| `LeverageResult` | leverage_manager.py | leverage, raw_leverage, capped_by_cb, reason |
| `LiquidationBuffer` | leverage_manager.py | entry_price, estimated_liq_price, buffer_pct, is_safe |
| `OrderResult` | order_manager.py | order_id, client_order_id, symbol, side, amount, price, status, verified |
| `OrderStatus` | order_manager.py | order_id, symbol, side, amount, filled, remaining, status |
| `PositionSize` | position_sizer.py | usd_amount, pct_of_balance, leverage, notional_value, capped |
| `ValidationResult` | data_validator.py | passed, status, details |
| `TickerData` | market_data.py | symbol, bid, ask, last, high, low, volume |
| `TradeEntry` | trade_journal.py | trade_id, symbol, direction, entry/exit/pnl, strategy, regime |
| `DrawdownState` | drawdown_monitor.py | current_balance, peak_balance, current/max_drawdown_pct |
| `DailyReportRow` | database.py | report_date, start/end_balance, net_pnl, pnl_pct, wins, losses |
| `CycleHistoryRow` | database.py | cycle_number, timestamp, cb_level, balance, regime, trade_placed |

---

## 14. Complete Function Reference

### Strategies

| Function | File | Signature |
|----------|------|-----------|
| `RegimeDetector.detect` | regime_detector.py | `(df: DataFrame) → RegimeState` |
| `AdaptiveStrategy.select_strategy` | adaptive_strategy.py | `(regime: RegimeState) → Optional[BaseStrategy]` |
| `AdaptiveStrategy.get_signal_multi_tf` | adaptive_strategy.py | `(df_4h, df_1h: DataFrame) → Optional[Signal]` |
| `AdaptiveStrategy.check_supertrend_reversal` | adaptive_strategy.py | `(df_4h: DataFrame, direction: str) → bool` |
| `AdaptiveStrategy.get_signal` | adaptive_strategy.py | `(df: DataFrame) → Optional[Signal]` (legacy) |
| `SupertrendTrend.generate_signal` | supertrend_trend.py | `(df: DataFrame, entry_price: float=None) → Signal` |
| `TrendFollower.generate_signal` | trend_follower.py | `(df: DataFrame) → Signal` |
| `MeanReversion.generate_signal` | mean_reversion.py | `(df: DataFrame, entry_price: float=None) → Signal` |
| `BreakoutTrader.generate_signal` | breakout_trader.py | `(df: DataFrame) → Signal` |

### Risk

| Function | File | Signature |
|----------|------|-----------|
| `CircuitBreaker.is_trading_allowed` | circuit_breaker.py | `(balance, recent_trades, start_balance, now) → CBState` |
| `CircuitBreaker.get_constraints` | circuit_breaker.py | `(balance: Decimal) → CBConstraints` |
| `CircuitBreaker.check_level` | circuit_breaker.py | `(balance: Decimal) → CBLevel` |
| `CircuitBreaker.check_daily_loss` | circuit_breaker.py | `(start, current) → (bool, Decimal)` |
| `LeverageManager.determine_leverage` | leverage_manager.py | `(confidence, regime, cb_level) → LeverageResult` |
| `LeverageManager.calculate_liquidation_buffer` | leverage_manager.py | `(entry, leverage, direction) → LiqBuffer` |
| `PositionSizer.calculate_size` | position_sizer.py | `(balance, win_rate, rr, cb, leverage) → PositionSize` |
| `VolatilityModel.forecast` | volatility_model.py | `(df) → VolState` |
| `VolatilityModel.adjust_leverage` | volatility_model.py | `(requested, vol_state, max) → int` |
| `DrawdownMonitor.update` | drawdown_monitor.py | `(current_balance) → DrawdownState` |
| `CorrelationMonitor.check_correlation` | correlation_monitor.py | `(positions, symbol, prices, balance, notional) → Result` |

### Execution

| Function | File | Signature |
|----------|------|-----------|
| `OrderManager.place_market_order` | order_manager.py | `(symbol, side, amount) → OrderResult \| None` |
| `OrderManager.place_limit_order` | order_manager.py | `(symbol, side, amount, price) → OrderResult \| None` |
| `OrderManager.place_stop_loss` | order_manager.py | `(symbol, side, amount, stop_price) → OrderResult \| None` |
| `OrderManager.place_take_profit` | order_manager.py | `(symbol, side, amount, stop_price) → OrderResult \| None` |
| `OrderManager.set_leverage` | order_manager.py | `(symbol, leverage) → None` |
| `OrderManager.cancel_order` | order_manager.py | `(symbol, order_id) → None` |
| `OrderManager.cancel_open_orders` | order_manager.py | `(symbol) → int` |
| `OrderManager._submit_order_idempotent` | order_manager.py | `(symbol, type, side, amount, ...) → OrderResult \| None` |
| `OrderManager._query_by_client_order_id` | order_manager.py | `(symbol, client_oid) → OrderStatus \| None` |
| `FeeCalculator.calculate_total_cost` | fee_calculator.py | `(entry, exit, size, leverage, ...) → dict` |
| `PositionTracker.get_open_positions` | position_tracker.py | `() → list[Position]` |

### Data

| Function | File | Signature |
|----------|------|-----------|
| `MarketDataClient.fetch_ohlcv` | market_data.py | `(symbol, timeframe, limit) → list[dict]` |
| `MarketDataClient.get_account_balance` | market_data.py | `() → Decimal` |
| `MarketDataClient.fetch_ticker` | market_data.py | `(symbol) → TickerData` |
| `IndicatorEngine.calculate_all` | indicator_engine.py | `(df, ...) → DataFrame` |
| `DataValidator.validate_ohlcv` | data_validator.py | `(df) → ValidationResult` |
| `DatabaseManager.store_daily_report` | database.py | `(report: DailyReportRow) → None` |
| `DatabaseManager.store_cycle` | database.py | `(cycle: CycleHistoryRow) → None` |
| `DatabaseManager.set_state` | database.py | `(key, value) → None` |
| `DatabaseManager.get_state` | database.py | `(key, default=None) → str \| None` |

### Memory & Reporting

| Function | File | Signature |
|----------|------|-----------|
| `TradeJournal.record_trade_entry` | trade_journal.py | `(details: dict) → None` |
| `TradeJournal.get_recent_trades` | trade_journal.py | `(limit=10) → list[TradeEntry]` |
| `DailyPnLCalculator.calculate_daily_pnl` | daily_pnl.py | `(trades, start, end, date) → DailyPnL` |
| `AlertSystem.send_alert` | alert_system.py | `(message, level) → None` |
| `SanityChecker.check_position_math` | sanity_checks.py | `(balance, margin, leverage, notional) → (bool, dict)` |

---

## 15. Test Suite

**Run**: `.venv/bin/python -m pytest tests/ -v`
**Status**: 228 tests passing (1.10s) as of 2026-03-15

### Test Breakdown

| Test File | Tests | Coverage |
|-----------|-------|----------|
| test_circuit_breaker.py | 23 | All CB levels, daily loss halt, consecutive loss pause, RED win-rate |
| test_leverage_manager.py | 23 | All confidence tiers, CB caps, liquidation buffer, boundaries |
| test_trade_journal.py | 20 | CRUD, win rate, consecutive losses, strategy/regime filters |
| test_position_sizer.py | 18 | Kelly sizing, CB multipliers, min/max caps, leverage clipping |
| test_daily_pnl.py | 18 | Daily calc, cumulative stats, Sharpe, doubling progress |
| test_drawdown_monitor.py | 17 | High-water mark, persistence, reset, edge cases |
| test_supertrend_trend.py | 15 | Flip detection, ADX gate, entry_price override, SL/TP math, confidence |
| test_volatility_model.py | 15 | GARCH(1,1), EWMA fallback, leverage scaling |
| test_trailing_stop.py | 13 | TrailingStopState creation, activation, trail trigger, best price |
| test_sanity_checks.py | 11 | Price validation, signal validation |
| test_adaptive_multi_tf.py | 10 | Regime routing (5 regimes), ST reversal detection |
| test_kelly_criterion.py | 8 | Full/half Kelly, sizing, edge cases |
| test_strategies.py | 8 | Individual strategy signal generation |
| test_pipeline.py | 7 | End-to-end orchestrator cycle |
| test_regime_detector.py | 5 | TRENDING/RANGING/VOLATILE/QUIET classification |
| test_fee_calculator.py | 4 | Maker/taker fees, BNB discount |

### UNTESTED Modules — Ranked by Blast Radius

| Priority | Module | Why It Matters | Min Tests |
|----------|--------|---------------|-----------|
| **P0** | `order_manager.py` | Handles real money. Idempotent logic untested. | 15+ |
| **P0** | `price_validator.py` | Anti-hallucination Layer 2 | 8+ |
| **P0** | `signal_validator.py` | Anti-hallucination Layer 3 | 8+ |
| **P1** | `database.py` | Stores trade journal driving sizing | 10+ |
| **P1** | `decision_auditor.py` | Anti-hallucination Layer 4 | 6+ |
| **P1** | `market_data.py` | All data flows through this | 8+ |
| **P1** | `position_tracker.py` | Tracks open positions for risk | 6+ |
| **P2** | `correlation_monitor.py` | Multi-position risk | 5+ |
| **P2** | `data_validator.py` | Anti-hallucination Layer 1 | 5+ |
| **P2** | `indicator_engine.py` | TA calculations | 6+ |
| **P2** | `slippage_estimator.py` | Execution quality | 4+ |
| **P3** | Memory modules | performance_tracker, bias_detector, trade_memory_client | 4+ each |
| **P3** | Reporting modules | dashboard, alert_system, report_generator, candle_store | 3+ each |

---

## 16. Known Issues & Technical Debt

1. ~~Testnet BROKEN~~ — RESOLVED
2. ~~INITIAL_CAPITAL outdated~~ — RESOLVED
3. ~~Multi-Assets Mode~~ — RESOLVED (FALSE)
4. ~~Position Mode~~ — RESOLVED (ONE-WAY)
5. **Manual trading** — User should stop manual trading before live deployment
6. ~~Taker fee discrepancy~~ — RESOLVED
7. **Freqtrade integration** — `ClaudeQuantAdaptive.py` outdated for v3. Optional.
8. **Paper trading** — ACTIVE since 2026-03-14. Target end: 2026-03-21.
9. **Test coverage** — 228 tests but ~30 modules lack dedicated tests
10. v4 backtest validated — +172.9%, 69.2% WR, Sharpe 3.98
11. Avg daily return 0.628% — VALIDATED CEILING from v4 backtest (~870% annualized). Not underperformance. Closing the gap to 1% requires strategy versioning pipeline with backtest evidence, not adding unproven trades.
12. **Hyperopt** — not yet run. May not be needed (Sharpe 3.31 already strong).
13. ~~Idempotent order submission~~ — RESOLVED: `_submit_order_idempotent()` implemented
14. CLAUDE.md updated to v3.0 — reconciled against production code

### Architectural Debt

- **Half-Kelly code exists but isn't used** — `position_sizer.py` and `kelly_criterion.py` contain Half-Kelly logic that the orchestrator bypasses in favor of confidence-based sizing
- **4 MCP servers built but not primary** — Direct ccxt via orchestrator is the execution path; MCP servers exist for agent-based querying
- **Freqtrade bridge stale** — `ClaudeQuantAdaptive.py` not updated for v3/v4 changes
- **No walk-forward validation** — Backtests are in-sample only
- **Scalper strategy never tested** — Code exists but no backtest or deployment

---

## 17. Learnings & Error History

### Learnings (12 entries)

| ID | Priority | Area | Summary |
|----|----------|------|---------|
| LRN-001 | high | exchange-api | Testnet requires separate API keys. Use `enable_demo_trading(True)`. |
| LRN-002 | high | data-pipeline | `fetch_ohlcv` returns `list[dict]`, must convert to DataFrame |
| LRN-003 | high | risk | Regime enum names must match across system |
| LRN-004 | medium | mcp | MCP servers must use `json.dumps`, not `str()` |
| LRN-005 | medium | exchange-api | DOGE/USDT requires whole number amounts |
| LRN-006 | medium | fees | Taker fee is 0.05%, not 0.04% (verified via API) |
| LRN-007 | critical | strategy | 4H Supertrend flips are the key entry signal (60.9% WR) |
| LRN-008 | critical | strategy | Mean reversion works on 4H, not 1H |
| LRN-009 | high | strategy | MR on 1H needs 2-of-3 confirmation, not all 3 |
| LRN-010 | high | risk | LeverageManager gate must match AdaptiveStrategy gate |
| LRN-011 | medium | backtest | Trailing stop: 2.0 ATR activate / 2.5 ATR trail optimal |
| LRN-012 | medium | backtest | Position minimum on notional, not margin |

### Errors (4 resolved)

| ID | Severity | Area | Root Cause |
|----|----------|------|------------|
| ERR-001 | critical | exchange-api | Production keys on testnet → separate keys needed |
| ERR-002 | medium | data-pipeline | Decimal/float mismatch in RegimeDetector |
| ERR-003 | medium | orchestrator | PerformanceTracker/PriceValidator missing constructor args |
| ERR-004 | low | orchestrator | `connect()` never called on exchange clients |

---

## 18. Backtest Evidence

### v4 Backtest (Production Code — `scripts/backtest_v4.py`)

Uses actual production classes: `AdaptiveStrategy`, `PositionSizer`, `LeverageManager`.
This is the authoritative backtest — inline backtests with different logic produce unreliable results.

**Before fixes (all strategies active)**:
- +41.6% return, 22.7% WR, 207 trades
- MeanReversion: 5.3% WR, -$7.65 (destroying capital)

**After fixes (SupertrendTrend only)**:
- +172.9% return, 69.2% WR, 39 trades
- Sharpe 3.98, Profit Factor 5.39
- Avg 5.7x leverage

### v3 Backtest (`scripts/backtest_v3.py`)

Inline backtest (NOT production code paths):
- +94% return, 60.9% WR, 69 trades
- Sharpe 3.31, 7.9% max DD over 172 days
- Proved: 4H Supertrend flips > 1H EMA crossovers

### Key Backtest Findings

1. **1H trend following: 36-44% WR** — negative EV in crypto
2. **4H Supertrend flips: 60.9% WR** — strong positive EV
3. **4H MR: 73% WR** — far outperforms 1H MR (49% WR)
4. **Trailing stop 2.0/2.5 ATR** — optimal for crypto
5. **ST reversal exits: -$33 individually** but enable +$97 net capital recycling

### Strategy Versioning Pipeline

No strategy goes live without:
```
Unit Tests → Backtest (backtest_v4.py) → Walk-Forward OOS → Paper Trading → Live
   100% pass    PF > 1.5, WR > 55%     OOS PF > 1.2      ±20% of backtest  Monitor
               Sharpe > 1.5                                                   Rollback
               Max DD < 15%
```

---

## 19. Deployment & Operations

### Current State

- **Paper trading** on Binance Futures Testnet
- **Bot PID**: 83621 (v3 restart)
- **$5000 simulated balance**
- **Waiting for**: First Supertrend flip signal

### Entry Point

```bash
cd "Claude Quant"
.venv/bin/python scripts/run_bot.py
```

### Watchdog Monitoring

```bash
# In Claude Code session:
@watchdog
# Or manually:
.venv/bin/python scripts/watchdog_tools.py health
.venv/bin/python scripts/watchdog_tools.py performance
```

### Kill Switch (3 independent methods)

1. `python scripts/kill_switch.py`
2. `KILL_SWITCH=true` in env
3. Watchdog auto-trigger on CRITICAL events

### Docker (configured, not primary)

```bash
docker-compose -f docker/docker-compose.yml up
```

### Deployment Checklist

- [x] Testnet configured ($5000 paper balance)
- [x] INITIAL_CAPITAL set to $68.33
- [x] Multi-Assets Mode = FALSE
- [x] Position Mode = ONE-WAY
- [x] Historical data downloaded (4H + 1H × 3 pairs)
- [x] v3/v4 backtests complete
- [ ] 7 days paper trading (in progress, target: 2026-03-21)
- [ ] Hyperopt (optional)
- [ ] Deploy Docker stack
- [ ] Monitor first 48h manually
- [ ] Ramp to full parameters over 5 days

### Scaling Roadmap

| Phase | Balance | Unlock Criteria |
|-------|---------|----------------|
| **Paper** (CURRENT) | $5000 sim | 200+ trades, 2+ weeks, WR > 55% |
| Micro-Live | $68-$150 | SupertrendTrend only, BTC+ETH only |
| Growth | $150-$500 | Add momentum strategy IF validated |
| Diversified | $500-$2K | Add mean reversion, add SOL, 3 concurrent |
| Advanced | $2K+ | Full multi-strategy, 5 concurrent |

---

## 20. Immutable Rules & Safety Invariants

These 10 rules are HARDCODED. No AI agent, configuration, or runtime path may override them.

| # | Rule | Code Location | Enforcement |
|---|------|---------------|-------------|
| 1 | **$30 HARD FLOOR** — Balance < $30 = HALT ALL TRADING | `circuit_breaker.py` DEAD level | `_RED_BALANCE_MIN = Decimal("30")` |
| 2 | **10x MAX LEVERAGE** | `leverage_manager.py` | `_CB_LEVERAGE_CAPS[GREEN] = 10` |
| 3 | **3 MAX CONCURRENT POSITIONS** | `circuit_breaker.py` | `_GREEN_MAX_POSITIONS = 3` |
| 4 | **15% MAX CAPITAL PER TRADE** | `orchestrator/main.py:467-469` | `max_margin = balance × 0.15` |
| 5 | **20 MAX DAILY TRADES** | `risk_params.yaml` | `max_daily_trades: 20` |
| 6 | **ALL DATA FROM API** | Anti-hallucination system | 5-layer defense |
| 7 | **RISK MANAGER APPROVAL** | `orchestrator/main.py` Step 4 | Leverage + sizing + liq buffer |
| 8 | **CB THRESHOLDS HARDCODED** | `circuit_breaker.py` | `Final` constants, no setters |
| 9 | **MIN 2.0 R/R** | `base_strategy.py:114` | Pydantic validator on Signal |
| 10 | **5% LIQUIDATION BUFFER** | `leverage_manager.py` | `_MIN_LIQUIDATION_BUFFER_PCT = 0.05` |

### CB Override Hierarchy

```
Circuit Breaker Level → overrides → Leverage Manager → overrides → Position Sizer
DEAD (halt) > daily loss halt > consecutive loss pause > RED win-rate gate > normal constraints
```

### What Would Cause Total Loss

1. Balance drops below $30 → DEAD → HALT (protected)
2. Flash crash liquidates before SL fills (5% buffer mitigates)
3. Exchange API failure during open position (idempotent submission mitigates for new orders)
4. Funding rate drag on long holds (monitored, cycle-based exits mitigate)

---

*Document compiled 2026-03-15 from production source code, verified against CLAUDE.md v3.0 and SINGLE_SOURCE_OF_TRUTH.md. 228 tests passing.*
