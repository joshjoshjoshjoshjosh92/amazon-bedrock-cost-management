"""Bedrock Cost Tracker — SDK wrapper for per-request token usage and cost tracking."""

from __future__ import annotations

__version__ = "0.1.0"

# Public API — these will be implemented in subsequent tasks.
# Importing here establishes the public surface for the package.
__all__ = [
    "BedrockCostTracker",
    "TrackedClient",
    "TrackerConfig",
    "InvocationRecord",
]


def __getattr__(name: str):
    """Lazy imports for public API classes (avoids circular imports during development)."""
    if name == "BedrockCostTracker":
        from bedrock_cost_tracker.tracker import BedrockCostTracker

        return BedrockCostTracker
    if name == "TrackedClient":
        from bedrock_cost_tracker.client import TrackedClient

        return TrackedClient
    if name == "TrackerConfig":
        from bedrock_cost_tracker.config import TrackerConfig

        return TrackerConfig
    if name == "InvocationRecord":
        from bedrock_cost_tracker.models import InvocationRecord

        return InvocationRecord
    raise AttributeError(f"module 'bedrock_cost_tracker' has no attribute {name!r}")
