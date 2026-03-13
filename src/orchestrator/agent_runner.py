"""
Claude Agent SDK wrapper for running specialized agents.

Provides programmatic access to Claude agents for each role in the system.
"""

import json
import logging
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.agent_runner")


class AgentRole(str, Enum):
    """Available agent roles."""
    ORCHESTRATOR = "orchestrator"
    MARKET_ANALYST = "market-analyst"
    RISK_MANAGER = "risk-manager"
    STRATEGY_SELECTOR = "strategy-selector"
    EXECUTION_AGENT = "execution-agent"
    MEMORY_AGENT = "memory-agent"
    SENTINEL = "sentinel"
    DAILY_REPORTER = "daily-reporter"


class AgentConfig(BaseModel):
    """Configuration for an agent."""
    role: AgentRole
    model: str = "sonnet"
    max_tokens: int = 4096
    temperature: float = 0.0
    mcp_servers: list[str] = Field(default_factory=list)


# Default configurations for each agent
AGENT_CONFIGS: dict[AgentRole, AgentConfig] = {
    AgentRole.ORCHESTRATOR: AgentConfig(
        role=AgentRole.ORCHESTRATOR,
        model="opus",
        max_tokens=8192,
        mcp_servers=["tradememory", "binance"],
    ),
    AgentRole.MARKET_ANALYST: AgentConfig(
        role=AgentRole.MARKET_ANALYST,
        model="sonnet",
        mcp_servers=["binance", "analysis"],
    ),
    AgentRole.RISK_MANAGER: AgentConfig(
        role=AgentRole.RISK_MANAGER,
        model="sonnet",
        mcp_servers=["binance", "risk", "tradememory"],
    ),
    AgentRole.STRATEGY_SELECTOR: AgentConfig(
        role=AgentRole.STRATEGY_SELECTOR,
        model="sonnet",
        mcp_servers=["tradememory"],
    ),
    AgentRole.EXECUTION_AGENT: AgentConfig(
        role=AgentRole.EXECUTION_AGENT,
        model="sonnet",
        mcp_servers=["binance"],
    ),
    AgentRole.MEMORY_AGENT: AgentConfig(
        role=AgentRole.MEMORY_AGENT,
        model="sonnet",
        mcp_servers=["tradememory"],
    ),
    AgentRole.SENTINEL: AgentConfig(
        role=AgentRole.SENTINEL,
        model="haiku",
        max_tokens=1024,
        mcp_servers=["binance"],
    ),
    AgentRole.DAILY_REPORTER: AgentConfig(
        role=AgentRole.DAILY_REPORTER,
        model="sonnet",
        mcp_servers=["tradememory", "binance", "reporting"],
    ),
}


class AgentResult(BaseModel):
    """Result from an agent execution."""
    role: AgentRole
    success: bool
    output: Any = None
    error: Optional[str] = None
    tokens_used: int = 0


class AgentRunner:
    """
    Wrapper for running Claude agents via the Agent SDK.

    In the current implementation, agents are Python classes that use
    the Claude API programmatically. The agent definitions in .claude/agents/
    are used when running via Claude Code CLI.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the agent runner with API credentials."""
        if self._initialized:
            return

        import os
        self.api_key = self.api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")

        self._initialized = True
        logger.info("AgentRunner initialized")

    async def run_agent(
        self,
        role: AgentRole,
        prompt: str,
        context: Optional[dict] = None,
    ) -> AgentResult:
        """
        Run a specialized agent with the given prompt and context.

        For production use with Claude Agent SDK, this would create a
        conversation with the appropriate model and MCP servers.
        Currently implements direct function dispatch.
        """
        config = AGENT_CONFIGS.get(role)
        if config is None:
            return AgentResult(
                role=role,
                success=False,
                error=f"No config for role: {role}",
            )

        logger.info(f"Running agent: {role.value} (model={config.model})")

        try:
            # Build the full prompt with context
            full_prompt = prompt
            if context:
                full_prompt = (
                    f"Context:\n```json\n{json.dumps(context, indent=2, default=str)}\n```\n\n"
                    f"{prompt}"
                )

            # In production, this would use the Claude Agent SDK:
            # from anthropic import Anthropic
            # client = Anthropic(api_key=self.api_key)
            # response = client.messages.create(
            #     model=self._resolve_model(config.model),
            #     max_tokens=config.max_tokens,
            #     temperature=config.temperature,
            #     messages=[{"role": "user", "content": full_prompt}],
            # )

            # For now, return a placeholder indicating SDK integration point
            return AgentResult(
                role=role,
                success=True,
                output={"message": "Agent SDK integration point", "prompt": full_prompt[:200]},
            )

        except Exception as e:
            logger.error(f"Agent {role.value} failed: {e}", exc_info=True)
            return AgentResult(
                role=role,
                success=False,
                error=str(e),
            )

    def _resolve_model(self, model_name: str) -> str:
        """Resolve short model name to full model ID."""
        model_map = {
            "opus": "claude-opus-4-6",
            "sonnet": "claude-sonnet-4-6",
            "haiku": "claude-haiku-4-5-20251001",
        }
        return model_map.get(model_name, model_name)
