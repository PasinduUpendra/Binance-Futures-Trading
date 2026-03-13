---
name: trade-execution
description: Execute an approved trade on Binance Futures
---

# Trade Execution Skill

Place and manage a trade on Binance Futures.

## Prerequisites
- Risk Manager MUST have approved the trade
- Circuit breaker must allow trading

## Steps
1. Verify Risk Manager approval exists
2. Set leverage for the pair
3. Place entry order
4. Wait for fill confirmation
5. Place stop-loss order
6. Place take-profit order (if applicable)
7. Verify all orders active
8. Write signal file for Freqtrade integration
9. Log execution details

## Safety
- NEVER skip step 1 (Risk Manager approval)
- ALWAYS verify orders with separate GET call
- ALWAYS set stop-loss before reporting success
