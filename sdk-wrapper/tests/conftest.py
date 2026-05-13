"""Shared test fixtures for bedrock_cost_tracker tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_tracker_config() -> dict:
    """Return a minimal valid TrackerConfig dictionary for testing."""
    return {
        "team": "ml-platform",
        "application": "chatbot-v2",
        "environment": "production",
        "custom_tags": {"cost_center": "CC-1234"},
        "emit_to": "cloudwatch",
        "buffer_size": 100,
        "retry_max_attempts": 3,
    }
