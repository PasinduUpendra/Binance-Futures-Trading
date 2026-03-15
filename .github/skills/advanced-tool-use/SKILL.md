---
name: advanced-tool-use
description: >
  Anthropic advanced tool use patterns for Claude agents. Use when: implementing tool search + programmatic
  calling, designing agent tool chains, building MCP server integrations, structuring multi-tool workflows,
  deferred tool loading, error handling for tool calls, and optimizing agent-tool interaction patterns.
applyTo: "src/mcp_tools/**,src/orchestrator/**,.claude/agents/**,.github/skills/**"
---

# Anthropic Advanced Tool Use — Tool Search + Programmatic Calling Skill

## Scope

This skill covers advanced patterns for using Claude's tool infrastructure in the Claude Quant
trading system: tool search and discovery, programmatic tool calling, MCP server integration,
multi-step agent workflows, and error-resilient tool chains.

---

## 1. Tool Search & Discovery Pattern

### Deferred Tool Loading

Many tools in the Claude ecosystem are "deferred" — not loaded until explicitly discovered via `tool_search_tool_regex`.

```
RULE: Before calling any deferred tool, MUST use tool_search_tool_regex to load it.
Calling a deferred tool without loading it WILL FAIL.
```

### Regex Pattern Syntax for Tool Search

```python
# Python re.search() syntax, case-insensitive
# Examples:
"^mcp_github_"           # Tools starting with "mcp_github_"
"issue|pull_request"     # Tools containing "issue" OR "pull_request"
"create.*branch"         # Tools with "create" followed by "branch"
"mcp_.*list"             # MCP tools with "list" in name
"binance|exchange|trade" # Any trading-related tool
"git.*commit|git.*push"  # Git operations
```

### Available Deferred Tools (Claude Quant Context)

Key tools that must be loaded before use:

| Tool Pattern | Purpose |
|-------------|---------|
| `mcp_gitkraken_git_*` | Git operations (add, commit, push, log, diff, status) |
| `github-pull-request_*` | PR management (create, review, status checks) |
| `fetch_webpage` | Fetch external documentation/APIs |
| `get_changed_files` | Review changed files before commit |
| `create_and_run_task` | Task automation |
| `terminal_*` | Terminal control and history |
| `vscode_*` | VS Code integration (rename, search, extensions) |

### Tool Loading Workflow

```
Step 1: Identify needed tool by name/category
Step 2: tool_search_tool_regex(pattern="<regex>")
Step 3: Tool is now loaded and available for immediate use
Step 4: Call the tool with proper parameters
        ↳ Never search for a tool that was already loaded
        ↳ Never retry tool_search if it returns no results — tool is unavailable
```

---

## 2. MCP Server Integration

### What is MCP (Model Context Protocol)?

MCP allows Claude to connect to external tool servers that provide domain-specific capabilities.
Each MCP server exposes tools that Claude can discover and call programmatically.

### MCP Tools for Claude Quant

```python
# Available MCP tool categories:
# 1. Analysis Tools (src/mcp_tools/analysis_tools.py)
#    - Technical indicator computation
#    - Regime classification
#    - Signal generation

# 2. Binance Tools (src/mcp_tools/binance_tools.py)
#    - Market data fetching
#    - Order placement
#    - Position management

# 3. Risk Tools (src/mcp_tools/risk_tools.py)
#    - Position sizing
#    - Leverage calculation
#    - Circuit breaker checks

# 4. Reporting Tools (src/mcp_tools/reporting_tools.py)
#    - Daily P&L reports
#    - Performance dashboards
#    - Alert generation
```

### MCP Tool Definition Pattern

```python
# Standard MCP tool structure:
from typing import Any

def tool_definition() -> dict:
    """Return JSON Schema for tool parameters."""
    return {
        "name": "analyze_market",
        "description": "Analyze a trading pair's market state",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Trading pair (e.g., ETH/USDT:USDT)"
                },
                "timeframe": {
                    "type": "string",
                    "enum": ["1h", "4h", "1d"],
                    "default": "4h"
                }
            },
            "required": ["symbol"]
        }
    }

async def execute(params: dict) -> dict:
    """Execute the tool with validated parameters."""
    symbol = params["symbol"]
    timeframe = params.get("timeframe", "4h")
    # ... implementation
    return {"regime": "trending", "adx": 28.5, ...}
```

---

## 3. Multi-Tool Workflow Patterns

### Sequential Chain (Dependent Tools)

```
# When output of tool A is input to tool B:
Step 1: Call tool A → get result
Step 2: Extract needed values from result
Step 3: Call tool B with extracted values
Step 4: Continue chain...

# Example: Market Analysis → Risk Assessment → Execution
1. analyze_market(ETH/USDT:USDT) → regime, confidence, signal
2. assess_risk(signal, balance, confidence) → approved, size, leverage
3. execute_trade(signal, size, leverage) → order_id, fill_price
```

### Parallel Batch (Independent Tools)

```
# When tools don't depend on each other:
# Call ALL independent tools simultaneously

# Example: Multi-pair analysis
parallel_call([
    analyze_market("ETH/USDT:USDT"),
    analyze_market("SOL/USDT:USDT"),
    analyze_market("DOGE/USDT:USDT"),
])
# Wait for all → process results together
```

### Conditional Branch (Decision-Based)

```
# Tool selection depends on prior results:
result = check_circuit_breaker()
if result.level == "DEAD":
    return halt_trading()
elif result.level == "GREEN":
    signal = generate_signal(full_params)
elif result.level == "YELLOW":
    signal = generate_signal(conservative_params)
```

---

## 4. Error Handling for Tool Calls

### Retry Strategy

```python
# Tool-specific retry logic:
MAX_RETRIES = 3
BACKOFF_BASE = 2  # seconds

for attempt in range(MAX_RETRIES):
    try:
        result = await call_tool(params)
        break
    except ToolTimeout:
        # Transient — retry with backoff
        await asyncio.sleep(BACKOFF_BASE ** attempt)
    except ToolNotFound:
        # Permanent — tool unavailable, stop
        raise
    except ToolError as e:
        if "rate_limit" in str(e):
            await asyncio.sleep(BACKOFF_BASE ** attempt)
        else:
            raise  # Unknown error, don't retry
```

### Graceful Degradation

```python
# If a non-critical tool fails, continue with reduced functionality:
try:
    correlation = await check_correlation(positions)
except ToolError:
    correlation = None  # Non-critical, proceed with caution
    logger.warning("Correlation check unavailable — using conservative sizing")
```

---

## 5. Agent-Tool Interaction Patterns

### Claude Quant Agent Architecture

```
Orchestrator Agent
├── calls: sentinel tools (health, circuit breaker)
├── calls: market analysis tools (OHLCV, indicators, regime)
├── calls: strategy tools (signal generation, confidence)
├── calls: risk tools (sizing, leverage, approval)
├── calls: execution tools (orders, verification)
└── calls: memory tools (journal, learning)
```

### Agent Delegation Pattern

```
# Main agent delegates to specialized sub-agents:
# Each sub-agent has access to specific tool sets

orchestrator:
  tools: [all_tools]
  delegates_to: [sentinel, market-analyst, strategy-selector,
                 risk-manager, execution-agent, memory-agent]

sentinel:
  tools: [health_check, circuit_breaker, balance_fetch]

market-analyst:
  tools: [fetch_ohlcv, calculate_indicators, detect_regime]

execution-agent:
  tools: [set_leverage, create_order, fetch_order, cancel_order]
```

### Subagent Invocation

```
# Use runSubagent for complex multi-step tasks:
runSubagent(
    agentName="risk-manager",
    prompt="Assess this signal: LONG ETH/USDT at 3450, confidence 72%, trending regime",
    description="Risk assessment for ETH long"
)

# Use search_subagent for codebase exploration:
search_subagent(
    query="How does the trailing stop activation work?",
    description="Find trailing stop logic",
    details="Look for TrailingStopState, activation threshold, and trail distance computation"
)
```

---

## 6. GitHub Agent Integration

### Git Operations via MCP Tools

```
# Load git tools first:
tool_search_tool_regex(pattern="mcp_gitkraken_git_status")
tool_search_tool_regex(pattern="mcp_gitkraken_git_add_or_commit")
tool_search_tool_regex(pattern="mcp_gitkraken_git_push")

# Workflow for code changes:
1. Make code changes via file edit tools
2. mcp_gitkraken_git_status() → review changes
3. get_changed_files() → verify diff
4. mcp_gitkraken_git_add_or_commit(files, message) → commit
5. mcp_gitkraken_git_push() → push (requires user confirmation)
```

### PR Management via GitHub Tools

```
# Load PR tools:
tool_search_tool_regex(pattern="github-pull-request")

# Create PR:
github-pull-request_openPullRequest(title, body, base, head)

# Check PR status:
github-pull-request_pullRequestStatusChecks()
```

---

## 7. Tool Composition for Trading Operations

### Full Trade Lifecycle (Tool Chain)

```
1. SENTINEL CHECK
   tools: fetch_balance, check_circuit_breaker, check_daily_loss

2. DATA COLLECTION
   tools: fetch_ohlcv(4h), fetch_ohlcv(1h), fetch_ticker [parallel per pair]

3. SIGNAL GENERATION
   tools: calculate_indicators, detect_regime, generate_signal [sequential per pair]

4. RISK ASSESSMENT
   tools: count_positions, calculate_size, determine_leverage,
          check_liquidation_buffer, check_correlation

5. EXECUTION
   tools: set_leverage, place_order, verify_fill,
          place_stop_loss, place_take_profit, verify_orders

6. RECORDING
   tools: write_journal, update_tracker, log_decision
```

---

## 8. Key Files

| File | Purpose |
|------|---------|
| `src/mcp_tools/__init__.py` | MCP tool registration |
| `src/mcp_tools/analysis_tools.py` | Market analysis tools |
| `src/mcp_tools/binance_tools.py` | Exchange interaction tools |
| `src/mcp_tools/risk_tools.py` | Risk assessment tools |
| `src/mcp_tools/reporting_tools.py` | Reporting tools |
| `.claude/agents/*.md` | Agent definitions (9 agents) |
| `.claude/skills/*.md` | Skill definitions |
| `.github/skills/*/SKILL.md` | GitHub-compatible skill files |
| `src/orchestrator/main.py` | Tool orchestration in 7-step cycle |
| `src/orchestrator/agent_runner.py` | Agent execution framework |
