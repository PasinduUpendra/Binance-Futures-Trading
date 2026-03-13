---
name: risk-assessment
description: Assess risk for a potential trade
---

# Risk Assessment Skill

Evaluate and size a potential trade.

## Steps
1. Fetch current account balance
2. Check circuit breaker level
3. Count open positions
4. Calculate Half-Kelly position size
5. Determine leverage based on confidence + regime
6. Verify liquidation buffer > 5%
7. Check correlation with existing positions
8. Return APPROVE or REJECT with full math

## Hard Limits (IMMUTABLE)
- Max leverage: 10x
- Max positions: 3
- Max 15% of balance per trade
- Balance < $30 = HALT
- Liquidation buffer must be > 5%
