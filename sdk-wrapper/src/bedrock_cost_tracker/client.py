"""TrackedClient — wraps a boto3 Bedrock Runtime client with cost tracking.

Intercepts invoke_model, invoke_model_with_response_stream, converse, and
converse_stream calls to capture token usage, calculate cost, and emit metrics.

Bedrock errors always pass through unchanged. Tracking errors are logged but
never raised to the caller.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterator, Optional

from bedrock_cost_tracker.config import TrackerConfig
from bedrock_cost_tracker.emitter import MetricsEmitter
from bedrock_cost_tracker.models import InvocationRecord
from bedrock_cost_tracker.pricing import PricingLookup

logger = logging.getLogger(__name__)


class TrackedStreamIterator:
    """Wraps a Bedrock streaming response body to intercept token count events.

    Accumulates token usage from stream events. If the stream is interrupted
    (exception during iteration), emits a partial InvocationRecord with
    stream_interrupted=True.
    """

    def __init__(
        self,
        stream_body: Any,
        model_id: str,
        request_id: str,
        input_tokens: int,
        start_time: float,
        config: TrackerConfig,
        pricing: PricingLookup,
        emitter: MetricsEmitter,
        api_method: str,
    ) -> None:
        self._stream_body = stream_body
        self._model_id = model_id
        self._request_id = request_id
        self._input_tokens = input_tokens
        self._output_tokens = 0
        self._start_time = start_time
        self._config = config
        self._pricing = pricing
        self._emitter = emitter
        self._api_method = api_method
        self._completed = False

    def __iter__(self) -> "TrackedStreamIterator":
        return self

    def __next__(self) -> Any:
        try:
            event = next(self._stream_body)
            self._extract_token_counts(event)
            return event
        except StopIteration:
            # Stream completed normally
            if not self._completed:
                self._completed = True
                self._emit_record(stream_interrupted=False)
            raise
        except Exception:
            # Stream interrupted
            if not self._completed:
                self._completed = True
                self._emit_record(stream_interrupted=True)
            raise

    def _extract_token_counts(self, event: Any) -> None:
        """Extract token counts from a stream event."""
        try:
            # invoke_model_with_response_stream: events are dicts with 'chunk' key
            if isinstance(event, dict):
                # Check for Amazon Bedrock usage metadata in the event
                chunk = event.get("chunk", {})
                if isinstance(chunk, dict):
                    bytes_data = chunk.get("bytes")
                    if bytes_data:
                        try:
                            parsed = json.loads(bytes_data)
                            usage = parsed.get("amazon-bedrock-invocationMetrics", {})
                            if usage:
                                self._output_tokens = usage.get(
                                    "outputTokenCount", self._output_tokens
                                )
                                self._input_tokens = usage.get(
                                    "inputTokenCount", self._input_tokens
                                )
                        except (json.JSONDecodeError, TypeError):
                            pass
        except Exception:
            logger.debug("Failed to extract token counts from stream event", exc_info=True)

    def _emit_record(self, stream_interrupted: bool) -> None:
        """Emit an InvocationRecord after stream completes or is interrupted."""
        try:
            latency_ms = int((time.monotonic() - self._start_time) * 1000)
            cost = _calculate_cost(
                self._pricing, self._model_id, self._input_tokens, self._output_tokens
            )
            tags = _build_tags(self._config)
            region = _get_region(self._config)

            record = InvocationRecord(
                request_id=self._request_id,
                model_id=self._model_id,
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                estimated_cost_usd=cost,
                timestamp=datetime.now(timezone.utc),
                latency_ms=latency_ms,
                account_id=self._config.account_id or "unknown",
                region=region,
                tags=tags,
                stream=True,
                stream_interrupted=stream_interrupted,
                api_method=self._api_method,
            )
            self._emitter.emit(record)
        except Exception:
            logger.debug(
                "Failed to emit stream record for request %s",
                self._request_id,
                exc_info=True,
            )


class ConverseStreamIterator:
    """Wraps a Bedrock converse_stream response to intercept token usage.

    The converse_stream API returns events in a 'stream' key. The final
    event typically contains usage metadata.
    """

    def __init__(
        self,
        stream_body: Any,
        model_id: str,
        request_id: str,
        start_time: float,
        config: TrackerConfig,
        pricing: PricingLookup,
        emitter: MetricsEmitter,
    ) -> None:
        self._stream_body = stream_body
        self._model_id = model_id
        self._request_id = request_id
        self._input_tokens = 0
        self._output_tokens = 0
        self._start_time = start_time
        self._config = config
        self._pricing = pricing
        self._emitter = emitter
        self._completed = False

    def __iter__(self) -> "ConverseStreamIterator":
        return self

    def __next__(self) -> Any:
        try:
            event = next(self._stream_body)
            self._extract_token_counts(event)
            return event
        except StopIteration:
            if not self._completed:
                self._completed = True
                self._emit_record(stream_interrupted=False)
            raise
        except Exception:
            if not self._completed:
                self._completed = True
                self._emit_record(stream_interrupted=True)
            raise

    def _extract_token_counts(self, event: Any) -> None:
        """Extract token counts from converse stream events."""
        try:
            if isinstance(event, dict):
                # converse_stream metadata event contains usage
                metadata = event.get("metadata", {})
                if metadata:
                    usage = metadata.get("usage", {})
                    if usage:
                        self._input_tokens = usage.get(
                            "inputTokens", self._input_tokens
                        )
                        self._output_tokens = usage.get(
                            "outputTokens", self._output_tokens
                        )
        except Exception:
            logger.debug(
                "Failed to extract token counts from converse stream event",
                exc_info=True,
            )

    def _emit_record(self, stream_interrupted: bool) -> None:
        """Emit an InvocationRecord after stream completes or is interrupted."""
        try:
            latency_ms = int((time.monotonic() - self._start_time) * 1000)
            cost = _calculate_cost(
                self._pricing, self._model_id, self._input_tokens, self._output_tokens
            )
            tags = _build_tags(self._config)
            region = _get_region(self._config)

            record = InvocationRecord(
                request_id=self._request_id,
                model_id=self._model_id,
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                estimated_cost_usd=cost,
                timestamp=datetime.now(timezone.utc),
                latency_ms=latency_ms,
                account_id=self._config.account_id or "unknown",
                region=region,
                tags=tags,
                stream=True,
                stream_interrupted=stream_interrupted,
                api_method="converse_stream",
            )
            self._emitter.emit(record)
        except Exception:
            logger.debug(
                "Failed to emit converse stream record for request %s",
                self._request_id,
                exc_info=True,
            )


class TrackedClient:
    """Wraps a boto3 Bedrock Runtime client with cost tracking.

    For each API call:
    1. Records start time
    2. Calls the underlying Bedrock API
    3. Extracts token counts from the response
    4. Calculates cost via PricingLookup
    5. Creates InvocationRecord
    6. Emits via MetricsEmitter (never blocks the response)
    7. Returns the original Bedrock response unchanged

    Bedrock errors pass through unchanged. Tracking errors are logged but
    never raised to the caller.
    """

    def __init__(
        self,
        client: Any,
        config: TrackerConfig,
        pricing: PricingLookup,
        emitter: MetricsEmitter,
    ) -> None:
        self._client = client
        self._config = config
        self._pricing = pricing
        self._emitter = emitter

    def __enter__(self) -> "TrackedClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            self._emitter.flush()
        except Exception:
            logger.debug("Failed to flush emitter on context exit", exc_info=True)

    def invoke_model(self, **kwargs: Any) -> Dict[str, Any]:
        """Call invoke_model on the underlying client with tracking.

        Captures request_id, model_id, token counts from response body,
        calculates cost, creates InvocationRecord, and emits metrics.

        Returns the original Bedrock response unchanged.
        """
        start_time = time.monotonic()
        response = self._client.invoke_model(**kwargs)

        try:
            self._track_invoke_model(response, kwargs, start_time)
        except Exception:
            logger.debug("Tracking failed for invoke_model", exc_info=True)

        return response

    def invoke_model_with_response_stream(self, **kwargs: Any) -> Dict[str, Any]:
        """Call invoke_model_with_response_stream with tracking.

        Wraps the streaming response body iterator to capture token counts
        after the stream completes. Handles interrupted streams with partial
        InvocationRecord.

        Returns the original Bedrock response with a wrapped body iterator.
        """
        start_time = time.monotonic()
        response = self._client.invoke_model_with_response_stream(**kwargs)

        try:
            model_id = kwargs.get("modelId", "unknown")
            request_id = response.get("ResponseMetadata", {}).get("RequestId", "unknown")
            # input_tokens may be available from response headers/metadata
            input_tokens = 0

            original_body = response.get("body")
            if original_body is not None:
                wrapped_body = TrackedStreamIterator(
                    stream_body=iter(original_body),
                    model_id=model_id,
                    request_id=request_id,
                    input_tokens=input_tokens,
                    start_time=start_time,
                    config=self._config,
                    pricing=self._pricing,
                    emitter=self._emitter,
                    api_method="invoke_model_with_response_stream",
                )
                response["body"] = wrapped_body
        except Exception:
            logger.debug(
                "Failed to wrap streaming response for tracking", exc_info=True
            )

        return response

    def converse(self, **kwargs: Any) -> Dict[str, Any]:
        """Call converse on the underlying client with tracking.

        The converse API response has usage.inputTokens and usage.outputTokens
        directly in the response structure.

        Returns the original Bedrock response unchanged.
        """
        start_time = time.monotonic()
        response = self._client.converse(**kwargs)

        try:
            self._track_converse(response, kwargs, start_time)
        except Exception:
            logger.debug("Tracking failed for converse", exc_info=True)

        return response

    def converse_stream(self, **kwargs: Any) -> Dict[str, Any]:
        """Call converse_stream on the underlying client with tracking.

        Wraps the stream iterator to capture token counts from metadata events.
        Handles interrupted streams with partial InvocationRecord.

        Returns the original Bedrock response with a wrapped stream iterator.
        """
        start_time = time.monotonic()
        response = self._client.converse_stream(**kwargs)

        try:
            model_id = kwargs.get("modelId", "unknown")
            request_id = response.get("ResponseMetadata", {}).get("RequestId", "unknown")

            original_stream = response.get("stream")
            if original_stream is not None:
                wrapped_stream = ConverseStreamIterator(
                    stream_body=iter(original_stream),
                    model_id=model_id,
                    request_id=request_id,
                    start_time=start_time,
                    config=self._config,
                    pricing=self._pricing,
                    emitter=self._emitter,
                )
                response["stream"] = wrapped_stream
        except Exception:
            logger.debug(
                "Failed to wrap converse stream response for tracking", exc_info=True
            )

        return response

    def _track_invoke_model(
        self, response: Dict[str, Any], kwargs: Dict[str, Any], start_time: float
    ) -> None:
        """Extract tracking data from invoke_model response and emit record."""
        latency_ms = int((time.monotonic() - start_time) * 1000)
        model_id = kwargs.get("modelId", "unknown")
        request_id = response.get("ResponseMetadata", {}).get("RequestId", "unknown")

        # Parse response body for token counts
        input_tokens = 0
        output_tokens = 0
        body = response.get("body")
        if body is not None:
            try:
                # body is a StreamingBody — read and parse
                body_bytes = body.read() if hasattr(body, "read") else body
                if isinstance(body_bytes, (bytes, bytearray)):
                    body_str = body_bytes.decode("utf-8")
                elif isinstance(body_bytes, str):
                    body_str = body_bytes
                else:
                    body_str = str(body_bytes)

                parsed_body = json.loads(body_str)

                # Extract usage from response body
                usage = parsed_body.get("usage", {})
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)

                # Also check amazon-bedrock-invocationMetrics
                metrics = parsed_body.get("amazon-bedrock-invocationMetrics", {})
                if metrics:
                    input_tokens = metrics.get("inputTokenCount", input_tokens)
                    output_tokens = metrics.get("outputTokenCount", output_tokens)

                # Re-wrap body so caller can still read it
                response["body"] = _RereadableBody(body_bytes)
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                logger.debug("Could not parse invoke_model response body for token counts")

        cost = _calculate_cost(self._pricing, model_id, input_tokens, output_tokens)
        tags = _build_tags(self._config)
        region = _get_region(self._config)

        record = InvocationRecord(
            request_id=request_id,
            model_id=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
            timestamp=datetime.now(timezone.utc),
            latency_ms=latency_ms,
            account_id=self._config.account_id or "unknown",
            region=region,
            tags=tags,
            stream=False,
            stream_interrupted=False,
            api_method="invoke_model",
        )
        self._emitter.emit(record)

    def _track_converse(
        self, response: Dict[str, Any], kwargs: Dict[str, Any], start_time: float
    ) -> None:
        """Extract tracking data from converse response and emit record."""
        latency_ms = int((time.monotonic() - start_time) * 1000)
        model_id = kwargs.get("modelId", "unknown")
        request_id = response.get("ResponseMetadata", {}).get("RequestId", "unknown")

        # converse response has usage directly
        usage = response.get("usage", {})
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)

        cost = _calculate_cost(self._pricing, model_id, input_tokens, output_tokens)
        tags = _build_tags(self._config)
        region = _get_region(self._config)

        record = InvocationRecord(
            request_id=request_id,
            model_id=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
            timestamp=datetime.now(timezone.utc),
            latency_ms=latency_ms,
            account_id=self._config.account_id or "unknown",
            region=region,
            tags=tags,
            stream=False,
            stream_interrupted=False,
            api_method="converse",
        )
        self._emitter.emit(record)


class _RereadableBody:
    """A simple wrapper that allows re-reading response body bytes.

    After we read the body to extract token counts, we need to provide
    the same bytes back to the caller.
    """

    def __init__(self, data: Any) -> None:
        if isinstance(data, (bytes, bytearray)):
            self._data = bytes(data)
        elif isinstance(data, str):
            self._data = data.encode("utf-8")
        else:
            self._data = bytes(data) if data else b""

    def read(self) -> bytes:
        return self._data

    def __str__(self) -> str:
        return self._data.decode("utf-8")


def _calculate_cost(
    pricing: PricingLookup,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
) -> Optional[Decimal]:
    """Calculate estimated cost for a model invocation.

    Returns None if model pricing is unknown.
    """
    input_price = pricing.get_price(model_id, "input")
    output_price = pricing.get_price(model_id, "output")

    if input_price is None or output_price is None:
        return None

    cost = (Decimal(input_tokens) / Decimal(1000) * input_price) + (
        Decimal(output_tokens) / Decimal(1000) * output_price
    )
    return cost


def _build_tags(config: TrackerConfig) -> Dict[str, str]:
    """Build cost allocation tags from TrackerConfig."""
    tags = {
        "team": config.team,
        "application": config.application,
        "environment": config.environment,
    }
    if config.custom_tags:
        tags.update(config.custom_tags)
    return tags


def _get_region(config: TrackerConfig) -> str:
    """Get the AWS region from config or default."""
    # Region is typically available from the boto3 client's meta
    # but we don't have direct access here. Use a sensible default.
    return "us-east-1"
