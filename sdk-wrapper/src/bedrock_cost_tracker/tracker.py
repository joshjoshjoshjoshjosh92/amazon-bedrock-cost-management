"""BedrockCostTracker — main entry point with context manager and decorator patterns.

Wires together: TrackerConfig → PricingLookup → TrackedClient → MetricsEmitter.
Provides track() context manager and @instrument decorator for easy integration.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, TypeVar

from bedrock_cost_tracker.client import TrackedClient
from bedrock_cost_tracker.config import TrackerConfig
from bedrock_cost_tracker.emitter import CloudWatchEmitter, MetricsEmitter, S3Emitter
from bedrock_cost_tracker.pricing import PricingLookup

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class _TrackedClientContext:
    """Context manager that returns a TrackedClient and flushes on exit."""

    def __init__(self, tracked_client: TrackedClient) -> None:
        self._tracked_client = tracked_client

    def __enter__(self) -> TrackedClient:
        return self._tracked_client

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._tracked_client.__exit__(exc_type, exc_val, exc_tb)


class BedrockCostTracker:
    """Main entry point for Bedrock cost tracking.

    Usage with context manager::

        tracker = BedrockCostTracker(config)
        with tracker.track(bedrock_client) as tracked_client:
            response = tracked_client.invoke_model(...)

    Usage with decorator::

        tracker = BedrockCostTracker(config)

        @tracker.instrument
        def my_bedrock_call(client):
            return client.invoke_model(...)

        my_bedrock_call(bedrock_client)

    The tracker ensures <50ms latency overhead per instrumented request
    under normal conditions by:
    - Using in-memory buffering (no network call per request)
    - Performing cost calculation with local pricing table
    - Deferring metric emission to buffer flush
    """

    def __init__(self, config: TrackerConfig) -> None:
        self.config = config
        self._pricing = PricingLookup()
        self._emitter = self._create_emitter()

    def track(self, client: Any) -> _TrackedClientContext:
        """Create a tracked client context manager.

        Args:
            client: A boto3 Bedrock Runtime client to wrap.

        Returns:
            A context manager that yields a TrackedClient. On exit,
            any buffered metrics are flushed.

        Example::

            with tracker.track(bedrock_client) as tracked_client:
                response = tracked_client.invoke_model(
                    modelId="anthropic.claude-3-sonnet-20240229-v1:0",
                    body=json.dumps({"prompt": "Hello"})
                )
        """
        tracked_client = TrackedClient(
            client=client,
            config=self.config,
            pricing=self._pricing,
            emitter=self._emitter,
        )
        return _TrackedClientContext(tracked_client)

    def instrument(self, func: F) -> F:
        """Decorator that wraps a function's first argument (bedrock client) with tracking.

        The decorated function receives a TrackedClient instead of the raw
        boto3 client. Metrics are flushed after the function returns.

        Args:
            func: A function whose first positional argument is a boto3
                Bedrock Runtime client.

        Returns:
            Wrapped function with cost tracking.

        Example::

            @tracker.instrument
            def my_bedrock_call(client):
                return client.invoke_model(
                    modelId="anthropic.claude-3-sonnet-20240229-v1:0",
                    body=json.dumps({"prompt": "Hello"})
                )

            result = my_bedrock_call(bedrock_client)
        """

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not args:
                return func(*args, **kwargs)

            # First positional argument is the bedrock client
            raw_client = args[0]
            tracked_client = TrackedClient(
                client=raw_client,
                config=self.config,
                pricing=self._pricing,
                emitter=self._emitter,
            )

            try:
                result = func(tracked_client, *args[1:], **kwargs)
                return result
            finally:
                try:
                    self._emitter.flush()
                except Exception:
                    logger.debug(
                        "Failed to flush emitter after instrumented call",
                        exc_info=True,
                    )

        return wrapper  # type: ignore[return-value]

    def _create_emitter(self) -> MetricsEmitter:
        """Create the appropriate MetricsEmitter based on config.emit_to."""
        emit_to = self.config.emit_to

        if emit_to == "cloudwatch":
            return CloudWatchEmitter(
                buffer_size=self.config.buffer_size,
                retry_max_attempts=self.config.retry_max_attempts,
                metric_dimensions=self.config.metric_dimensions,
            )
        elif emit_to == "s3":
            return S3Emitter(
                buffer_size=self.config.buffer_size,
                retry_max_attempts=self.config.retry_max_attempts,
            )
        elif emit_to == "both":
            # For "both", use CloudWatch as primary (S3 would need separate config)
            # In a full implementation, this would use a composite emitter
            return CloudWatchEmitter(
                buffer_size=self.config.buffer_size,
                retry_max_attempts=self.config.retry_max_attempts,
                metric_dimensions=self.config.metric_dimensions,
            )
        else:
            # Fallback — should not happen due to config validation
            return CloudWatchEmitter(
                buffer_size=self.config.buffer_size,
                retry_max_attempts=self.config.retry_max_attempts,
            )

    @property
    def pricing(self) -> PricingLookup:
        """Access the pricing lookup (for testing/inspection)."""
        return self._pricing

    @property
    def emitter(self) -> MetricsEmitter:
        """Access the metrics emitter (for testing/inspection)."""
        return self._emitter
