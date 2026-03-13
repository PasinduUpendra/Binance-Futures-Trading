"""
Anti-hallucination layer for Claude Quant.

Ensures every trading decision is backed by verifiable exchange data, concrete
indicator values, sound arithmetic, and a full audit trail.
"""

from src.anti_hallucination.decision_auditor import AuditReport, DecisionAuditor
from src.anti_hallucination.price_validator import PriceValidator, ValidationResult
from src.anti_hallucination.sanity_checks import SanityChecker, SanityResult
from src.anti_hallucination.signal_validator import (
    SignalValidation,
    SignalValidator,
    TradingSignal,
)

__all__ = [
    # price_validator
    "PriceValidator",
    "ValidationResult",
    # signal_validator
    "SignalValidator",
    "SignalValidation",
    "TradingSignal",
    # decision_auditor
    "DecisionAuditor",
    "AuditReport",
    # sanity_checks
    "SanityChecker",
    "SanityResult",
]
