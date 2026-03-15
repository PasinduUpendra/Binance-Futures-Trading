# Error Log

Structured error tracking for API failures, exceptions, and unexpected behavior.
Format: `ERR-YYYYMMDD-XXX | severity | status | area`

---

## ERR-20260313-001 | critical | resolved | exchange-api
**Testnet connection failure**
```
Error: "Invalid API-key, IP, or permissions for action"
Context: ccxt binanceusdm with enable_demo_trading(True)
Keys: Production BINANCE_API_KEY/SECRET
```
Root cause: Production keys cannot access testnet. Need separate testnet keys.
Resolution: User provided testnet API keys (2026-03-14). Bot connected successfully with $5000 paper balance. Testnet keys configured in `.env` as `BINANCE_API_KEY`/`BINANCE_API_SECRET`, production keys renamed to `_PROD` suffix.

## ERR-20260314-002 | medium | resolved | data-pipeline
**Decimal/float type mismatch in RegimeDetector**
```
Error: "unsupported operand type(s) for /: 'decimal.Decimal' and 'float'"
Context: RegimeDetector._volume_ratio() dividing Decimal volume by float average
File: src/strategies/regime_detector.py:236
```
Root cause: Binance testnet API returns `Decimal` for volume values. The `_volume_ratio` method divided `Decimal` current volume by `float` rolling average.
Fix: Added `.astype(float)` cast on volume series before computation.

## ERR-20260314-003 | medium | resolved | orchestrator
**Orchestrator constructor missing required args**
```
Error: PerformanceTracker.__init__() missing 1 required positional argument: 'journal'
Error: PriceValidator.__init__() missing 1 required positional argument: 'market_data_client'
File: src/orchestrator/main.py:151-153
```
Root cause: Orchestrator was instantiating `PerformanceTracker()` and `PriceValidator()` without required constructor arguments.
Fix: `PerformanceTracker(journal=self.trade_journal)`, `PriceValidator(market_data_client=self.market_data)`.

## ERR-20260314-004 | low | resolved | orchestrator
**Orchestrator never called connect() on exchange clients**
```
Error: "MarketDataClient not connected. Call connect() first."
File: src/orchestrator/main.py — start() method
```
Root cause: `Orchestrator.start()` called `get_account_balance()` without first connecting MarketDataClient, PositionTracker, and OrderManager.
Fix: Added explicit `connect()` calls at top of `start()` and `close()` calls in `stop()`.
