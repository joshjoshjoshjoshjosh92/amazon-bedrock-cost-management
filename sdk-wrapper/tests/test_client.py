"""Tests for TrackedClient — invoke_model, converse, streaming, and error handling."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from bedrock_cost_tracker.client import (
    ConverseStreamIterator,
    TrackedClient,
    TrackedStreamIterator,
    _calculate_cost,
)
from bedrock_cost_tracker.config import TrackerConfig
from bedrock_cost_tracker.emitter import MetricsEmitter
from bedrock_cost_tracker.models import InvocationRecord
from bedrock_cost_tracker.pricing import PricingLookup


class FakeEmitter(MetricsEmitter):
    """A test emitter that records emitted records without side effects."""

    def __init__(self) -> None:
        super().__init__(buffer_size=1000, retry_max_attempts=1)
        self.emitted_records: List[InvocationRecord] = []

    def emit(self, record: InvocationRecord) -> None:
        self.emitted_records.append(record)

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
def pricing() -> PricingLookup:
    """Create a PricingLookup with bundled defaults."""
    return PricingLookup()


@pytest.fixture
def emitter() -> FakeEmitter:
    """Create a FakeEmitter for testing."""
    return FakeEmitter()


@pytest.fixture
def tracked_client(
    tracker_config: TrackerConfig, pricing: PricingLookup, emitter: FakeEmitter
) -> TrackedClient:
    """Create a TrackedClient with a mocked boto3 client."""
    mock_client = MagicMock()
    return TrackedClient(
        client=mock_client,
        config=tracker_config,
        pricing=pricing,
        emitter=emitter,
    )


class TestInvokeModel:
    """Tests for TrackedClient.invoke_model."""

    def test_captures_correct_token_counts(
        self, tracker_config: TrackerConfig, pricing: PricingLookup, emitter: FakeEmitter
    ) -> None:
        """invoke_model extracts input_tokens and output_tokens from response body."""
        mock_client = MagicMock()
        response_body = json.dumps(
            {
                "usage": {"input_tokens": 42, "output_tokens": 128},
                "content": [{"text": "Hello!"}],
            }
        ).encode("utf-8")

        mock_body = MagicMock()
        mock_body.read.return_value = response_body

        mock_client.invoke_model.return_value = {
            "body": mock_body,
            "ResponseMetadata": {"RequestId": "req-123"},
        }

        client = TrackedClient(
            client=mock_client, config=tracker_config, pricing=pricing, emitter=emitter
        )
        response = client.invoke_model(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            body=json.dumps({"prompt": "Hello"}),
        )

        # Response is returned to caller
        assert response is not None
        assert response["ResponseMetadata"]["RequestId"] == "req-123"

        # Token counts captured
        assert len(emitter.emitted_records) == 1
        record = emitter.emitted_records[0]
        assert record.input_tokens == 42
        assert record.output_tokens == 128
        assert record.model_id == "anthropic.claude-3-sonnet-20240229-v1:0"
        assert record.request_id == "req-123"
        assert record.api_method == "invoke_model"
        assert record.stream is False

    def test_captures_bedrock_invocation_metrics(
        self, tracker_config: TrackerConfig, pricing: PricingLookup, emitter: FakeEmitter
    ) -> None:
        """invoke_model extracts token counts from amazon-bedrock-invocationMetrics."""
        mock_client = MagicMock()
        response_body = json.dumps(
            {
                "amazon-bedrock-invocationMetrics": {
                    "inputTokenCount": 50,
                    "outputTokenCount": 200,
                },
                "content": [{"text": "Hello!"}],
            }
        ).encode("utf-8")

        mock_body = MagicMock()
        mock_body.read.return_value = response_body

        mock_client.invoke_model.return_value = {
            "body": mock_body,
            "ResponseMetadata": {"RequestId": "req-456"},
        }

        client = TrackedClient(
            client=mock_client, config=tracker_config, pricing=pricing, emitter=emitter
        )
        client.invoke_model(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            body=json.dumps({"prompt": "Hello"}),
        )

        assert len(emitter.emitted_records) == 1
        record = emitter.emitted_records[0]
        assert record.input_tokens == 50
        assert record.output_tokens == 200

    def test_calculates_cost_correctly(
        self, tracker_config: TrackerConfig, pricing: PricingLookup, emitter: FakeEmitter
    ) -> None:
        """invoke_model calculates cost using PricingLookup."""
        mock_client = MagicMock()
        response_body = json.dumps(
            {"usage": {"input_tokens": 1000, "output_tokens": 500}}
        ).encode("utf-8")

        mock_body = MagicMock()
        mock_body.read.return_value = response_body

        mock_client.invoke_model.return_value = {
            "body": mock_body,
            "ResponseMetadata": {"RequestId": "req-789"},
        }

        client = TrackedClient(
            client=mock_client, config=tracker_config, pricing=pricing, emitter=emitter
        )
        client.invoke_model(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            body=json.dumps({"prompt": "Hello"}),
        )

        record = emitter.emitted_records[0]
        # Claude 3 Sonnet: input $0.003/1K, output $0.015/1K
        # Cost = (1000/1000 * 0.003) + (500/1000 * 0.015) = 0.003 + 0.0075 = 0.0105
        expected_cost = Decimal("1000") / Decimal("1000") * Decimal("0.003") + \
                        Decimal("500") / Decimal("1000") * Decimal("0.015")
        assert record.estimated_cost_usd == expected_cost

    def test_unknown_model_returns_none_cost(
        self, tracker_config: TrackerConfig, pricing: PricingLookup, emitter: FakeEmitter
    ) -> None:
        """invoke_model returns None cost for unknown models."""
        mock_client = MagicMock()
        response_body = json.dumps(
            {"usage": {"input_tokens": 100, "output_tokens": 50}}
        ).encode("utf-8")

        mock_body = MagicMock()
        mock_body.read.return_value = response_body

        mock_client.invoke_model.return_value = {
            "body": mock_body,
            "ResponseMetadata": {"RequestId": "req-unknown"},
        }

        client = TrackedClient(
            client=mock_client, config=tracker_config, pricing=pricing, emitter=emitter
        )
        client.invoke_model(
            modelId="unknown.model-v1",
            body=json.dumps({"prompt": "Hello"}),
        )

        record = emitter.emitted_records[0]
        assert record.estimated_cost_usd is None

    def test_body_still_readable_after_tracking(
        self, tracker_config: TrackerConfig, pricing: PricingLookup, emitter: FakeEmitter
    ) -> None:
        """After tracking reads the body, caller can still read it."""
        mock_client = MagicMock()
        original_body = json.dumps(
            {"usage": {"input_tokens": 10, "output_tokens": 20}, "content": "test"}
        ).encode("utf-8")

        mock_body = MagicMock()
        mock_body.read.return_value = original_body

        mock_client.invoke_model.return_value = {
            "body": mock_body,
            "ResponseMetadata": {"RequestId": "req-read"},
        }

        client = TrackedClient(
            client=mock_client, config=tracker_config, pricing=pricing, emitter=emitter
        )
        response = client.invoke_model(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            body=json.dumps({"prompt": "Hello"}),
        )

        # Caller can still read the body
        body_data = response["body"].read()
        assert body_data == original_body


class TestConverse:
    """Tests for TrackedClient.converse."""

    def test_captures_correct_token_counts(
        self, tracker_config: TrackerConfig, pricing: PricingLookup, emitter: FakeEmitter
    ) -> None:
        """converse extracts inputTokens and outputTokens from response."""
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"role": "assistant", "content": [{"text": "Hi!"}]}},
            "usage": {"inputTokens": 75, "outputTokens": 150},
            "ResponseMetadata": {"RequestId": "req-conv-123"},
        }

        client = TrackedClient(
            client=mock_client, config=tracker_config, pricing=pricing, emitter=emitter
        )
        response = client.converse(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            messages=[{"role": "user", "content": [{"text": "Hello"}]}],
        )

        # Response returned unchanged
        assert response["usage"]["inputTokens"] == 75
        assert response["usage"]["outputTokens"] == 150

        # Record captured
        assert len(emitter.emitted_records) == 1
        record = emitter.emitted_records[0]
        assert record.input_tokens == 75
        assert record.output_tokens == 150
        assert record.model_id == "anthropic.claude-3-sonnet-20240229-v1:0"
        assert record.api_method == "converse"
        assert record.stream is False

    def test_includes_tags_from_config(
        self, tracker_config: TrackerConfig, pricing: PricingLookup, emitter: FakeEmitter
    ) -> None:
        """converse includes team, application, environment tags."""
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"role": "assistant", "content": [{"text": "Hi!"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 20},
            "ResponseMetadata": {"RequestId": "req-tags"},
        }

        client = TrackedClient(
            client=mock_client, config=tracker_config, pricing=pricing, emitter=emitter
        )
        client.converse(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            messages=[{"role": "user", "content": [{"text": "Hello"}]}],
        )

        record = emitter.emitted_records[0]
        assert record.tags["team"] == "ml-platform"
        assert record.tags["application"] == "chatbot-v2"
        assert record.tags["environment"] == "production"
        assert record.account_id == "123456789012"


class TestBedrockErrorPassthrough:
    """Tests that Bedrock errors pass through unchanged to the caller."""

    def test_invoke_model_bedrock_error_passes_through(
        self, tracker_config: TrackerConfig, pricing: PricingLookup, emitter: FakeEmitter
    ) -> None:
        """Bedrock ClientError from invoke_model is raised to caller."""
        from botocore.exceptions import ClientError

        mock_client = MagicMock()
        mock_client.invoke_model.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "InvokeModel",
        )

        client = TrackedClient(
            client=mock_client, config=tracker_config, pricing=pricing, emitter=emitter
        )

        with pytest.raises(ClientError) as exc_info:
            client.invoke_model(
                modelId="anthropic.claude-3-sonnet-20240229-v1:0",
                body=json.dumps({"prompt": "Hello"}),
            )

        assert "ThrottlingException" in str(exc_info.value)
        # No records emitted on error
        assert len(emitter.emitted_records) == 0

    def test_converse_bedrock_error_passes_through(
        self, tracker_config: TrackerConfig, pricing: PricingLookup, emitter: FakeEmitter
    ) -> None:
        """Bedrock ClientError from converse is raised to caller."""
        from botocore.exceptions import ClientError

        mock_client = MagicMock()
        mock_client.converse.side_effect = ClientError(
            {"Error": {"Code": "ModelNotReadyException", "Message": "Model loading"}},
            "Converse",
        )

        client = TrackedClient(
            client=mock_client, config=tracker_config, pricing=pricing, emitter=emitter
        )

        with pytest.raises(ClientError) as exc_info:
            client.converse(
                modelId="anthropic.claude-3-sonnet-20240229-v1:0",
                messages=[{"role": "user", "content": [{"text": "Hello"}]}],
            )

        assert "ModelNotReadyException" in str(exc_info.value)


class TestTrackingErrorsNeverRaise:
    """Tests that tracking errors are logged but never raised to the caller."""

    def test_emitter_error_does_not_affect_response(
        self, tracker_config: TrackerConfig, pricing: PricingLookup
    ) -> None:
        """If the emitter raises, the response is still returned."""
        failing_emitter = FakeEmitter()
        failing_emitter.emit = MagicMock(side_effect=RuntimeError("Emit failed"))

        mock_client = MagicMock()
        response_body = json.dumps(
            {"usage": {"input_tokens": 10, "output_tokens": 20}}
        ).encode("utf-8")
        mock_body = MagicMock()
        mock_body.read.return_value = response_body

        mock_client.invoke_model.return_value = {
            "body": mock_body,
            "ResponseMetadata": {"RequestId": "req-fail"},
        }

        client = TrackedClient(
            client=mock_client,
            config=tracker_config,
            pricing=pricing,
            emitter=failing_emitter,
        )

        # Should not raise despite emitter failure
        response = client.invoke_model(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            body=json.dumps({"prompt": "Hello"}),
        )
        assert response["ResponseMetadata"]["RequestId"] == "req-fail"

    def test_pricing_error_does_not_affect_response(
        self, tracker_config: TrackerConfig, emitter: FakeEmitter
    ) -> None:
        """If pricing lookup raises, the response is still returned."""
        broken_pricing = MagicMock()
        broken_pricing.get_price.side_effect = RuntimeError("Pricing broken")

        mock_client = MagicMock()
        response_body = json.dumps(
            {"usage": {"input_tokens": 10, "output_tokens": 20}}
        ).encode("utf-8")
        mock_body = MagicMock()
        mock_body.read.return_value = response_body

        mock_client.invoke_model.return_value = {
            "body": mock_body,
            "ResponseMetadata": {"RequestId": "req-price-fail"},
        }

        client = TrackedClient(
            client=mock_client,
            config=tracker_config,
            pricing=broken_pricing,
            emitter=emitter,
        )

        # Should not raise despite pricing failure
        response = client.invoke_model(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            body=json.dumps({"prompt": "Hello"}),
        )
        assert response["ResponseMetadata"]["RequestId"] == "req-price-fail"


class TestStreaming:
    """Tests for invoke_model_with_response_stream."""

    def test_stream_captures_token_counts_on_completion(
        self, tracker_config: TrackerConfig, pricing: PricingLookup, emitter: FakeEmitter
    ) -> None:
        """Streaming response emits record with token counts after stream completes."""
        mock_client = MagicMock()

        # Simulate stream events
        stream_events = [
            {"chunk": {"bytes": json.dumps({"content": "Hello"}).encode()}},
            {"chunk": {"bytes": json.dumps({"content": " world"}).encode()}},
            {
                "chunk": {
                    "bytes": json.dumps(
                        {
                            "amazon-bedrock-invocationMetrics": {
                                "inputTokenCount": 30,
                                "outputTokenCount": 60,
                            }
                        }
                    ).encode()
                }
            },
        ]

        mock_client.invoke_model_with_response_stream.return_value = {
            "body": iter(stream_events),
            "ResponseMetadata": {"RequestId": "req-stream-123"},
        }

        client = TrackedClient(
            client=mock_client, config=tracker_config, pricing=pricing, emitter=emitter
        )
        response = client.invoke_model_with_response_stream(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            body=json.dumps({"prompt": "Hello"}),
        )

        # Consume the stream
        events = list(response["body"])
        assert len(events) == 3

        # Record emitted after stream completes
        assert len(emitter.emitted_records) == 1
        record = emitter.emitted_records[0]
        assert record.input_tokens == 30
        assert record.output_tokens == 60
        assert record.stream is True
        assert record.stream_interrupted is False
        assert record.api_method == "invoke_model_with_response_stream"

    def test_interrupted_stream_emits_partial_record(
        self, tracker_config: TrackerConfig, pricing: PricingLookup, emitter: FakeEmitter
    ) -> None:
        """Interrupted stream emits partial record with stream_interrupted=True."""
        mock_client = MagicMock()

        def interrupted_stream():
            yield {"chunk": {"bytes": json.dumps({"content": "Hello"}).encode()}}
            raise ConnectionError("Stream interrupted")

        mock_client.invoke_model_with_response_stream.return_value = {
            "body": interrupted_stream(),
            "ResponseMetadata": {"RequestId": "req-interrupted"},
        }

        client = TrackedClient(
            client=mock_client, config=tracker_config, pricing=pricing, emitter=emitter
        )
        response = client.invoke_model_with_response_stream(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            body=json.dumps({"prompt": "Hello"}),
        )

        # Consume stream until interruption
        with pytest.raises(ConnectionError):
            for _ in response["body"]:
                pass

        # Partial record emitted
        assert len(emitter.emitted_records) == 1
        record = emitter.emitted_records[0]
        assert record.stream is True
        assert record.stream_interrupted is True


class TestConverseStream:
    """Tests for converse_stream."""

    def test_converse_stream_captures_token_counts(
        self, tracker_config: TrackerConfig, pricing: PricingLookup, emitter: FakeEmitter
    ) -> None:
        """converse_stream captures token counts from metadata event."""
        mock_client = MagicMock()

        stream_events = [
            {"contentBlockDelta": {"delta": {"text": "Hello"}}},
            {"contentBlockDelta": {"delta": {"text": " world"}}},
            {"metadata": {"usage": {"inputTokens": 45, "outputTokens": 90}}},
        ]

        mock_client.converse_stream.return_value = {
            "stream": iter(stream_events),
            "ResponseMetadata": {"RequestId": "req-conv-stream"},
        }

        client = TrackedClient(
            client=mock_client, config=tracker_config, pricing=pricing, emitter=emitter
        )
        response = client.converse_stream(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            messages=[{"role": "user", "content": [{"text": "Hello"}]}],
        )

        # Consume the stream
        events = list(response["stream"])
        assert len(events) == 3

        # Record emitted
        assert len(emitter.emitted_records) == 1
        record = emitter.emitted_records[0]
        assert record.input_tokens == 45
        assert record.output_tokens == 90
        assert record.stream is True
        assert record.stream_interrupted is False
        assert record.api_method == "converse_stream"


class TestCostCalculation:
    """Tests for the _calculate_cost helper."""

    def test_known_model_cost(self) -> None:
        """Cost calculation for a known model returns correct Decimal."""
        pricing = PricingLookup()
        cost = _calculate_cost(
            pricing, "anthropic.claude-3-sonnet-20240229-v1:0", 1000, 500
        )
        # input: 1000/1000 * 0.003 = 0.003
        # output: 500/1000 * 0.015 = 0.0075
        # total: 0.0105
        expected = Decimal("1000") / Decimal("1000") * Decimal("0.003") + \
                   Decimal("500") / Decimal("1000") * Decimal("0.015")
        assert cost == expected

    def test_unknown_model_returns_none(self) -> None:
        """Cost calculation for unknown model returns None."""
        pricing = PricingLookup()
        cost = _calculate_cost(pricing, "unknown.model-v1", 100, 50)
        assert cost is None

    def test_zero_tokens_returns_zero_cost(self) -> None:
        """Zero tokens results in zero cost."""
        pricing = PricingLookup()
        cost = _calculate_cost(
            pricing, "anthropic.claude-3-sonnet-20240229-v1:0", 0, 0
        )
        assert cost == Decimal("0")
