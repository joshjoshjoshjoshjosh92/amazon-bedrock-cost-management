"""MetricsEmitter — buffered, retry-capable metrics emission with circuit breaker.

Provides base MetricsEmitter class and concrete CloudWatchEmitter / S3Emitter subclasses.
All emitters guarantee that Bedrock API calls are never blocked by tracking failures.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from bedrock_cost_tracker.models import InvocationRecord

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Circuit breaker to prevent flooding a recovering service.

    - Opens after `failure_threshold` consecutive failures.
    - Stays open for `cooldown_seconds` before transitioning to half-open.
    - In half-open state, allows a single probe attempt.
    - Closes again on a successful probe; re-opens on failure.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._last_failure_time: Optional[float] = None
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        """Current circuit breaker state (evaluates cooldown for open→half-open)."""
        with self._lock:
            if self._state == CircuitState.OPEN and self._last_failure_time is not None:
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self._cooldown_seconds:
                    self._state = CircuitState.HALF_OPEN
            return self._state

    def allow_request(self) -> bool:
        """Return True if a request is allowed through the circuit breaker."""
        current_state = self.state
        return current_state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def record_success(self) -> None:
        """Record a successful operation — resets the breaker to closed."""
        with self._lock:
            self._consecutive_failures = 0
            self._state = CircuitState.CLOSED
            self._last_failure_time = None

    def record_failure(self) -> None:
        """Record a failed operation — may trip the breaker to open."""
        with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.monotonic()
            if self._consecutive_failures >= self._failure_threshold:
                self._state = CircuitState.OPEN


class MetricsEmitter(ABC):
    """Base class for metrics emission with buffering, retry, and circuit breaker.

    Thread-safe via threading.Lock. Uses lazy async detection on first emit.
    Bedrock API calls are NEVER blocked by tracking failures.
    """

    def __init__(
        self,
        buffer_size: int = 100,
        retry_max_attempts: int = 3,
        retry_base_ms: float = 100.0,
        circuit_failure_threshold: int = 5,
        circuit_cooldown_seconds: float = 60.0,
    ) -> None:
        self._buffer: List[InvocationRecord] = []
        self._buffer_size = buffer_size
        self._retry_max_attempts = retry_max_attempts
        self._retry_base_ms = retry_base_ms
        self._lock = threading.Lock()
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=circuit_failure_threshold,
            cooldown_seconds=circuit_cooldown_seconds,
        )
        self._async_detected: Optional[bool] = None

    @property
    def buffer(self) -> List[InvocationRecord]:
        """Current buffer contents (for testing/inspection)."""
        with self._lock:
            return list(self._buffer)

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Access the circuit breaker (for testing/inspection)."""
        return self._circuit_breaker

    def _detect_async(self) -> bool:
        """Lazy async detection on first emit. Caches result."""
        if self._async_detected is not None:
            return self._async_detected
        try:
            import asyncio

            asyncio.get_running_loop()
            self._async_detected = True
        except RuntimeError:
            self._async_detected = False
        return self._async_detected

    def emit(self, record: InvocationRecord) -> None:
        """Buffer an invocation record and flush if buffer is full.

        Never raises — all errors are logged and swallowed to avoid
        blocking Bedrock API calls.
        """
        try:
            self._detect_async()
            self._append_to_buffer(record)
            if self._should_flush():
                self.flush()
        except Exception:
            logger.exception("Unexpected error in MetricsEmitter.emit")

    def _append_to_buffer(self, record: InvocationRecord) -> None:
        """Append record to buffer, dropping oldest on overflow."""
        with self._lock:
            if len(self._buffer) >= self._buffer_size:
                # Drop oldest events on overflow
                dropped_count = len(self._buffer) - self._buffer_size + 1
                self._buffer = self._buffer[dropped_count:]
                logger.warning(
                    "Buffer overflow: dropped %d oldest event(s). "
                    "Consider increasing buffer_size or reducing emit frequency.",
                    dropped_count,
                )
                self._emit_buffer_overflow_warning()
            self._buffer.append(record)

    def _should_flush(self) -> bool:
        """Determine if the buffer should be flushed."""
        with self._lock:
            return len(self._buffer) >= self._buffer_size

    def flush(self) -> None:
        """Flush buffered records to the backend with retry and circuit breaker.

        Never raises — all errors are logged and swallowed.
        """
        try:
            if not self._circuit_breaker.allow_request():
                logger.warning(
                    "Circuit breaker is open — skipping flush. "
                    "Will probe after cooldown period."
                )
                return

            with self._lock:
                if not self._buffer:
                    return
                records_to_flush = list(self._buffer)
                self._buffer.clear()

            success = self._retry_with_backoff(records_to_flush)

            if success:
                self._circuit_breaker.record_success()
            else:
                self._circuit_breaker.record_failure()
                # Put records back in buffer for next attempt
                with self._lock:
                    # Prepend failed records, respecting buffer size
                    combined = records_to_flush + self._buffer
                    if len(combined) > self._buffer_size:
                        combined = combined[len(combined) - self._buffer_size :]
                    self._buffer = combined

        except Exception:
            logger.exception("Unexpected error in MetricsEmitter.flush")

    def _retry_with_backoff(self, records: List[InvocationRecord]) -> bool:
        """Retry emission with exponential backoff and jitter.

        Returns True on success, False if all attempts failed.
        Base interval: retry_base_ms, doubles each attempt, with random jitter.
        """
        for attempt in range(self._retry_max_attempts):
            try:
                self._do_emit(records)
                return True
            except Exception:
                if attempt < self._retry_max_attempts - 1:
                    # Exponential backoff: base_ms * 2^attempt, with jitter
                    base_delay_s = (self._retry_base_ms / 1000.0) * (2**attempt)
                    jitter = random.uniform(0, base_delay_s * 0.5)
                    delay = base_delay_s + jitter
                    logger.warning(
                        "Emit attempt %d/%d failed, retrying in %.3fs",
                        attempt + 1,
                        self._retry_max_attempts,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "All %d emit attempts failed. Records will be re-buffered.",
                        self._retry_max_attempts,
                    )
        return False

    @abstractmethod
    def _do_emit(self, records: List[InvocationRecord]) -> None:
        """Subclass-implemented emission logic. Raises on failure."""
        ...

    def _emit_buffer_overflow_warning(self) -> None:
        """Emit a BufferOverflow warning metric. Override in subclasses for real emission."""
        logger.warning("BufferOverflow warning metric emitted")


class CloudWatchEmitter(MetricsEmitter):
    """Emits metrics to CloudWatch using PutMetricData with EMF for high-cardinality dimensions.

    Default dimensions: ModelId, Team only (to prevent cardinality explosion).
    High-cardinality fields (AccountId, Application) are EMF properties, not dimensions.
    """

    def __init__(
        self,
        cloudwatch_client: Any = None,
        logs_client: Any = None,
        namespace: str = "BedrockCostTracker/Invocations",
        log_group: str = "/bedrock-cost-tracker/emf",
        metric_dimensions: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._cloudwatch_client = cloudwatch_client
        self._logs_client = logs_client
        self._namespace = namespace
        self._log_group = log_group
        self._metric_dimensions = metric_dimensions or ["ModelId", "Team"]
        self._log_group_ensured = False
        self._log_stream_tokens: Dict[str, Optional[str]] = {}

    def _do_emit(self, records: List[InvocationRecord]) -> None:
        """Emit records using CloudWatch PutMetricData with EMF."""
        if self._cloudwatch_client is None:
            import boto3

            self._cloudwatch_client = boto3.client("cloudwatch")

        # Batch PutMetricData (max 1000 metric data points per call)
        metric_data = []
        for record in records:
            dimensions = self._build_dimensions(record)
            timestamp = record.timestamp

            metric_data.extend([
                {
                    "MetricName": "InputTokenCount",
                    "Dimensions": dimensions,
                    "Timestamp": timestamp,
                    "Value": float(record.input_tokens),
                    "Unit": "Count",
                },
                {
                    "MetricName": "OutputTokenCount",
                    "Dimensions": dimensions,
                    "Timestamp": timestamp,
                    "Value": float(record.output_tokens),
                    "Unit": "Count",
                },
                {
                    "MetricName": "InvocationCount",
                    "Dimensions": dimensions,
                    "Timestamp": timestamp,
                    "Value": 1.0,
                    "Unit": "Count",
                },
                {
                    "MetricName": "LatencyMs",
                    "Dimensions": dimensions,
                    "Timestamp": timestamp,
                    "Value": float(record.latency_ms),
                    "Unit": "Milliseconds",
                },
            ])

            if record.estimated_cost_usd is not None:
                metric_data.append({
                    "MetricName": "EstimatedCostUSD",
                    "Dimensions": dimensions,
                    "Timestamp": timestamp,
                    "Value": float(record.estimated_cost_usd),
                    "Unit": "None",
                })

        # PutMetricData supports max 1000 data points per call
        for i in range(0, len(metric_data), 1000):
            batch = metric_data[i : i + 1000]
            self._cloudwatch_client.put_metric_data(
                Namespace=self._namespace,
                MetricData=batch,
            )

        # Emit EMF log for high-cardinality properties
        self._emit_emf_logs(records)

    def _build_dimensions(self, record: InvocationRecord) -> List[Dict[str, str]]:
        """Build CloudWatch metric dimensions from record."""
        dimension_map = {
            "ModelId": record.model_id,
            "Team": record.tags.get("team", "unknown"),
            "AccountId": record.account_id,
            "Application": record.tags.get("application", "unknown"),
            "Region": record.region,
        }
        return [
            {"Name": name, "Value": dimension_map.get(name, "unknown")}
            for name in self._metric_dimensions
            if name in dimension_map
        ]

    def _emit_emf_logs(self, records: List[InvocationRecord]) -> None:
        """Emit Embedded Metric Format logs for high-cardinality data."""
        if self._logs_client is None:
            try:
                import boto3

                self._logs_client = boto3.client("logs")
            except Exception:
                logger.debug("Could not create logs client for EMF emission")
                return

        self._ensure_log_group()

        for record in records:
            stream_name = f"emf/{record.timestamp.strftime('%Y/%m/%d')}"
            self._ensure_log_stream(stream_name)

            emf_entry = {
                "_aws": {
                    "Timestamp": int(record.timestamp.timestamp() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": self._namespace,
                            "Dimensions": [["ModelId", "Team"]],
                            "Metrics": [
                                {"Name": "InputTokenCount", "Unit": "Count"},
                                {"Name": "OutputTokenCount", "Unit": "Count"},
                                {"Name": "EstimatedCostUSD", "Unit": "None"},
                            ],
                        }
                    ],
                },
                # Dimensions
                "ModelId": record.model_id,
                "Team": record.tags.get("team", "unknown"),
                # EMF Properties (high-cardinality, searchable but not dimensions)
                "AccountId": record.account_id,
                "Application": record.tags.get("application", "unknown"),
                "Region": record.region,
                "RequestId": record.request_id,
                "InferenceProfileArn": record.inference_profile_arn or "",
                "ApiMethod": record.api_method,
                "Stream": record.stream,
                "StreamInterrupted": record.stream_interrupted,
                # Metric values
                "InputTokenCount": record.input_tokens,
                "OutputTokenCount": record.output_tokens,
                "EstimatedCostUSD": float(record.estimated_cost_usd)
                if record.estimated_cost_usd is not None
                else 0.0,
            }

            try:
                put_kwargs: Dict[str, Any] = {
                    "logGroupName": self._log_group,
                    "logStreamName": stream_name,
                    "logEvents": [
                        {
                            "timestamp": int(record.timestamp.timestamp() * 1000),
                            "message": json.dumps(emf_entry, default=str),
                        }
                    ],
                }
                seq_token = self._log_stream_tokens.get(stream_name)
                if seq_token:
                    put_kwargs["sequenceToken"] = seq_token

                response = self._logs_client.put_log_events(**put_kwargs)
                self._log_stream_tokens[stream_name] = response.get(
                    "nextSequenceToken"
                )
            except Exception:
                logger.debug("Failed to emit EMF log entry", exc_info=True)

    def _ensure_log_group(self) -> None:
        """Create the log group if it doesn't exist (idempotent)."""
        if self._log_group_ensured:
            return
        try:
            self._logs_client.create_log_group(logGroupName=self._log_group)
        except self._logs_client.exceptions.ResourceAlreadyExistsException:
            pass
        except Exception:
            logger.debug("Failed to create log group %s", self._log_group, exc_info=True)
        self._log_group_ensured = True

    def _ensure_log_stream(self, stream_name: str) -> None:
        """Create a log stream if it doesn't exist (idempotent)."""
        if stream_name in self._log_stream_tokens:
            return
        try:
            self._logs_client.create_log_stream(
                logGroupName=self._log_group, logStreamName=stream_name
            )
        except self._logs_client.exceptions.ResourceAlreadyExistsException:
            pass
        except Exception:
            logger.debug(
                "Failed to create log stream %s", stream_name, exc_info=True
            )
        self._log_stream_tokens[stream_name] = None

    def _emit_buffer_overflow_warning(self) -> None:
        """Emit BufferOverflow warning as a CloudWatch metric."""
        try:
            if self._cloudwatch_client is not None:
                self._cloudwatch_client.put_metric_data(
                    Namespace=self._namespace,
                    MetricData=[
                        {
                            "MetricName": "BufferOverflow",
                            "Value": 1.0,
                            "Unit": "Count",
                        }
                    ],
                )
        except Exception:
            logger.debug("Failed to emit BufferOverflow metric", exc_info=True)


class S3Emitter(MetricsEmitter):
    """Emits metrics to S3 in JSON-lines format.

    Each flush writes a single S3 object containing one JSON record per line.
    Object key format: {prefix}/{date}/{timestamp}-{batch_id}.jsonl
    """

    def __init__(
        self,
        s3_client: Any = None,
        bucket: str = "",
        prefix: str = "bedrock-cost-tracker/invocations",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._s3_client = s3_client
        self._bucket = bucket
        self._prefix = prefix

    def _do_emit(self, records: List[InvocationRecord]) -> None:
        """Write records to S3 as JSON-lines."""
        if self._s3_client is None:
            import boto3

            self._s3_client = boto3.client("s3")

        if not self._bucket:
            raise ValueError("S3 bucket name is required for S3Emitter")

        now = datetime.now(timezone.utc)
        date_path = now.strftime("%Y/%m/%d")
        timestamp_str = now.strftime("%H%M%S")
        batch_id = f"{timestamp_str}-{id(records) % 10000:04d}"
        key = f"{self._prefix}/{date_path}/{batch_id}.jsonl"

        lines = []
        for record in records:
            line = {
                "request_id": record.request_id,
                "model_id": record.model_id,
                "input_tokens": record.input_tokens,
                "output_tokens": record.output_tokens,
                "estimated_cost_usd": str(record.estimated_cost_usd)
                if record.estimated_cost_usd is not None
                else None,
                "timestamp": record.timestamp.isoformat(),
                "latency_ms": record.latency_ms,
                "account_id": record.account_id,
                "region": record.region,
                "tags": record.tags,
                "inference_profile_arn": record.inference_profile_arn,
                "stream": record.stream,
                "stream_interrupted": record.stream_interrupted,
                "api_method": record.api_method,
            }
            lines.append(json.dumps(line, default=str))

        body = "\n".join(lines) + "\n"

        self._s3_client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="application/x-ndjson",
        )

    def _emit_buffer_overflow_warning(self) -> None:
        """Log buffer overflow warning for S3 emitter."""
        logger.warning("S3Emitter: BufferOverflow — oldest events dropped")


class CompositeEmitter(MetricsEmitter):
    """Fans out metrics to multiple emitters.

    Each emit/flush is forwarded to all child emitters. Failures in one
    emitter do not affect the others.
    """

    def __init__(self, emitters: List[MetricsEmitter], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._emitters = emitters

    def emit(self, record: InvocationRecord) -> None:
        """Forward record to all child emitters."""
        for emitter in self._emitters:
            try:
                emitter.emit(record)
            except Exception:
                logger.debug(
                    "CompositeEmitter: child emitter %s failed on emit",
                    type(emitter).__name__,
                    exc_info=True,
                )

    def flush(self) -> None:
        """Flush all child emitters."""
        for emitter in self._emitters:
            try:
                emitter.flush()
            except Exception:
                logger.debug(
                    "CompositeEmitter: child emitter %s failed on flush",
                    type(emitter).__name__,
                    exc_info=True,
                )

    def _do_emit(self, records: List[InvocationRecord]) -> None:
        """Not used directly — emit/flush are overridden."""
        pass
