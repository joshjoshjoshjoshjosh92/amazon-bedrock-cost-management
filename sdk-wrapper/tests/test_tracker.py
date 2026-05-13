"""Tests for BedrockCostTracker — context manager, decorator, and wiring."""

from __future__ import annotations

import json
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

from bedrock_cost_tracker.client import TrackedClient
from bedrock_cost_tracker.config import TrackerConfig
from bedrock_cost_tracker.emitter import CloudWatchEmitter, MetricsEmitter, S3Emitter
from bedrock_cost_tracker.models import InvocationRecord
from bedrock_cost_tracker.tracker import BedrockCostTracker


class FakeEmitter(MetricsEmitter):
    """A test emitter that records emitted records and flush calls."""

    def __init__(self) -> None:
        super().__init__(buffer_size=1000, retry_max_attempts=1)
        self.emitted_records: List[InvocationRecord] = []
        self.flush_count: int = 0

    def emit(self, record: InvocationRecord) -> None:
        self.emitted_records.append(record)

    def flush(self) -> None:
        self.flush_count += 1

    def _do_emit(self, records: List[InvocationRecord]) -> None:
        pass


@pytest.fixture
def tracker_config() -> TrackerConfig:
    """Create a TrackerConfig with account_id pre-set to avoid STS calls."""
    with patch("boto3.client"):
        return TrackerConfig(
            team="ml-platform",
            application="chatbot-v2",
            environment="production",
            account_id="123456789012",
        )


@pytest.fixture
def mock_bedrock_client() -> MagicMock:
    """Create a mock boto3 Bedrock Runtime client."""
    client = MagicMock()
    response_body = json.dumps(
        {"usage": {"input_tokens": 100, "output_tokens": 50}, "content": "test"}
    ).encode("utf-8")
    mock_body = MagicMock()
    mock_body.read.return_value = response_body

    client.invoke_model.return_value = {
        "body": mock_body,
        "ResponseMetadata": {"RequestId": "req-test-123"},
    }
    client.converse.return_value = {
        "output": {"message": {"role": "assistant", "content": [{"text": "Hi!"}]}},
        "usage": {"inputTokens": 100, "outputTokens": 50},
        "ResponseMetadata": {"RequestId": "req-conv-test"},
    }
    return client


class TestBedrockCostTrackerInit:
    """Tests for BedrockCostTracker initialization and wiring."""

    def test_creates_cloudwatch_emitter_by_default(
        self, tracker_config: TrackerConfig
    ) -> None:
        """Default emit_to='cloudwatch' creates a CloudWatchEmitter."""
        tracker = BedrockCostTracker(tracker_config)
        assert isinstance(tracker.emitter, CloudWatchEmitter)

    def test_creates_s3_emitter(self) -> None:
        """emit_to='s3' creates an S3Emitter."""
        with patch("boto3.client"):
            config = TrackerConfig(
                team="team",
                application="app",
                environment="prod",
                emit_to="s3",
                account_id="123456789012",
            )
        tracker = BedrockCostTracker(config)
        assert isinstance(tracker.emitter, S3Emitter)

    def test_pricing_lookup_initialized(self, tracker_config: TrackerConfig) -> None:
        """PricingLookup is initialized and accessible."""
        tracker = BedrockCostTracker(tracker_config)
        assert tracker.pricing is not None
        # Should have bundled pricing for known models
        price = tracker.pricing.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "input"
        )
        assert price is not None

    def test_config_stored(self, tracker_config: TrackerConfig) -> None:
        """Config is stored on the tracker."""
        tracker = BedrockCostTracker(tracker_config)
        assert tracker.config is tracker_config


class TestContextManager:
    """Tests for the track() context manager pattern."""

    def test_track_returns_tracked_client(
        self, tracker_config: TrackerConfig, mock_bedrock_client: MagicMock
    ) -> None:
        """track() context manager yields a TrackedClient."""
        tracker = BedrockCostTracker(tracker_config)

        with tracker.track(mock_bedrock_client) as tracked_client:
            assert isinstance(tracked_client, TrackedClient)

    def test_context_manager_flushes_on_exit(
        self, tracker_config: TrackerConfig, mock_bedrock_client: MagicMock
    ) -> None:
        """Context manager flushes emitter on exit."""
        tracker = BedrockCostTracker(tracker_config)
        # Replace emitter with our fake
        fake_emitter = FakeEmitter()
        tracker._emitter = fake_emitter

        with tracker.track(mock_bedrock_client) as tracked_client:
            tracked_client.converse(
                modelId="anthropic.claude-3-sonnet-20240229-v1:0",
                messages=[{"role": "user", "content": [{"text": "Hello"}]}],
            )

        # Flush called on context exit
        assert fake_emitter.flush_count >= 1

    def test_context_manager_flushes_on_exception(
        self, tracker_config: TrackerConfig, mock_bedrock_client: MagicMock
    ) -> None:
        """Context manager flushes even when an exception occurs inside."""
        tracker = BedrockCostTracker(tracker_config)
        fake_emitter = FakeEmitter()
        tracker._emitter = fake_emitter

        with pytest.raises(ValueError):
            with tracker.track(mock_bedrock_client) as tracked_client:
                raise ValueError("Something went wrong")

        # Flush still called
        assert fake_emitter.flush_count >= 1

    def test_invoke_model_through_context_manager(
        self, tracker_config: TrackerConfig, mock_bedrock_client: MagicMock
    ) -> None:
        """Full flow: track → invoke_model → record emitted."""
        tracker = BedrockCostTracker(tracker_config)
        fake_emitter = FakeEmitter()
        tracker._emitter = fake_emitter

        with tracker.track(mock_bedrock_client) as tracked_client:
            response = tracked_client.invoke_model(
                modelId="anthropic.claude-3-sonnet-20240229-v1:0",
                body=json.dumps({"prompt": "Hello"}),
            )

        assert response is not None
        assert len(fake_emitter.emitted_records) == 1
        record = fake_emitter.emitted_records[0]
        assert record.input_tokens == 100
        assert record.output_tokens == 50


class TestDecoratorPattern:
    """Tests for the @tracker.instrument decorator pattern."""

    def test_decorator_wraps_function(
        self, tracker_config: TrackerConfig, mock_bedrock_client: MagicMock
    ) -> None:
        """@instrument decorator wraps the function's client argument."""
        tracker = BedrockCostTracker(tracker_config)
        fake_emitter = FakeEmitter()
        tracker._emitter = fake_emitter

        @tracker.instrument
        def my_bedrock_call(client: Any) -> Any:
            return client.converse(
                modelId="anthropic.claude-3-sonnet-20240229-v1:0",
                messages=[{"role": "user", "content": [{"text": "Hello"}]}],
            )

        result = my_bedrock_call(mock_bedrock_client)

        assert result is not None
        assert result["usage"]["inputTokens"] == 100
        assert len(fake_emitter.emitted_records) == 1

    def test_decorator_flushes_after_call(
        self, tracker_config: TrackerConfig, mock_bedrock_client: MagicMock
    ) -> None:
        """@instrument decorator flushes emitter after function returns."""
        tracker = BedrockCostTracker(tracker_config)
        fake_emitter = FakeEmitter()
        tracker._emitter = fake_emitter

        @tracker.instrument
        def my_call(client: Any) -> Any:
            return client.converse(
                modelId="anthropic.claude-3-sonnet-20240229-v1:0",
                messages=[{"role": "user", "content": [{"text": "Hello"}]}],
            )

        my_call(mock_bedrock_client)
        assert fake_emitter.flush_count >= 1

    def test_decorator_flushes_on_exception(
        self, tracker_config: TrackerConfig, mock_bedrock_client: MagicMock
    ) -> None:
        """@instrument decorator flushes even when function raises."""
        tracker = BedrockCostTracker(tracker_config)
        fake_emitter = FakeEmitter()
        tracker._emitter = fake_emitter

        @tracker.instrument
        def failing_call(client: Any) -> Any:
            client.converse(
                modelId="anthropic.claude-3-sonnet-20240229-v1:0",
                messages=[{"role": "user", "content": [{"text": "Hello"}]}],
            )
            raise RuntimeError("Application error")

        with pytest.raises(RuntimeError):
            failing_call(mock_bedrock_client)

        # Flush still called
        assert fake_emitter.flush_count >= 1

    def test_decorator_preserves_function_name(
        self, tracker_config: TrackerConfig
    ) -> None:
        """@instrument preserves the original function's name and docstring."""
        tracker = BedrockCostTracker(tracker_config)

        @tracker.instrument
        def my_special_function(client: Any) -> str:
            """My docstring."""
            return "result"

        assert my_special_function.__name__ == "my_special_function"
        assert my_special_function.__doc__ == "My docstring."

    def test_decorator_with_no_args_calls_original(
        self, tracker_config: TrackerConfig
    ) -> None:
        """@instrument with no arguments still calls the original function."""
        tracker = BedrockCostTracker(tracker_config)

        @tracker.instrument
        def no_args_func() -> str:
            return "no client"

        result = no_args_func()
        assert result == "no client"


class TestWiring:
    """Tests that components are wired together correctly."""

    def test_buffer_size_passed_to_emitter(self) -> None:
        """TrackerConfig.buffer_size is passed to the emitter."""
        with patch("boto3.client"):
            config = TrackerConfig(
                team="team",
                application="app",
                environment="prod",
                buffer_size=50,
                account_id="123456789012",
            )
        tracker = BedrockCostTracker(config)
        assert tracker.emitter._buffer_size == 50

    def test_retry_max_attempts_passed_to_emitter(self) -> None:
        """TrackerConfig.retry_max_attempts is passed to the emitter."""
        with patch("boto3.client"):
            config = TrackerConfig(
                team="team",
                application="app",
                environment="prod",
                retry_max_attempts=5,
                account_id="123456789012",
            )
        tracker = BedrockCostTracker(config)
        assert tracker.emitter._retry_max_attempts == 5
