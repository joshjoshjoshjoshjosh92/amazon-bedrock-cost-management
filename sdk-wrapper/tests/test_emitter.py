"""Tests for MetricsEmitter — buffer, retry, circuit breaker, and thread safety."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from bedrock_cost_tracker.emitter import (
    CircuitBreaker,
    CircuitState,
    CloudWatchEmitter,
    MetricsEmitter,
    S3Emitter,
)
from bedrock_cost_tracker.models import InvocationRecord


def _make_record(**overrides) -> InvocationRecord:
    """Create a sample InvocationRecord for testing."""
    defaults = {
        "request_id": "req-001",
        "model_id": "anthropic.claude-3-sonnet-20240229-v1:0",
        "input_tokens": 100,
        "output_tokens": 50,
        "estimated_cost_usd": Decimal("0.001050"),
        "timestamp": datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        "latency_ms": 250,
        "account_id": "123456789012",
        "region": "us-east-1",
        "tags": {"team": "ml-platform", "application": "chatbot"},
        "inference_profile_arn": None,
        "stream": False,
        "stream_interrupted": False,
        "api_method": "invoke_model",
    }
    defaults.update(overrides)
    return InvocationRecord(**defaults)


class ConcreteEmitter(MetricsEmitter):
    """Concrete emitter for testing the base class logic."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.emitted_batches: List[List[InvocationRecord]] = []
        self.emit_should_fail = False
        self.emit_call_count = 0

    def _do_emit(self, records: List[InvocationRecord]) -> None:
        self.emit_call_count += 1
        if self.emit_should_fail:
            raise RuntimeError("Simulated emission failure")
        self.emitted_batches.append(records)


class TestBufferAppendAndFlush:
    """Test buffer append and flush behavior."""

    def test_emit_appends_to_buffer(self):
        emitter = ConcreteEmitter(buffer_size=10)
        record = _make_record()
        emitter.emit(record)
        assert len(emitter.buffer) == 1
        assert emitter.buffer[0] == record

    def test_flush_sends_buffered_records(self):
        emitter = ConcreteEmitter(buffer_size=10)
        records = [_make_record(request_id=f"req-{i}") for i in range(3)]
        for r in records:
            emitter.emit(r)

        emitter.flush()
        assert len(emitter.emitted_batches) == 1
        assert emitter.emitted_batches[0] == records
        assert len(emitter.buffer) == 0

    def test_auto_flush_when_buffer_full(self):
        emitter = ConcreteEmitter(buffer_size=3)
        for i in range(3):
            emitter.emit(_make_record(request_id=f"req-{i}"))

        # Buffer should have been flushed automatically
        assert len(emitter.emitted_batches) == 1
        assert len(emitter.buffer) == 0

    def test_flush_empty_buffer_is_noop(self):
        emitter = ConcreteEmitter(buffer_size=10)
        emitter.flush()
        assert emitter.emitted_batches == []

    def test_emit_never_raises(self):
        emitter = ConcreteEmitter(buffer_size=10)
        emitter.emit_should_fail = True
        # Should not raise even when emission fails
        record = _make_record()
        emitter.emit(record)  # No exception


class TestRetryWithExponentialBackoff:
    """Test retry logic with exponential backoff."""

    def test_successful_emit_no_retry(self):
        emitter = ConcreteEmitter(buffer_size=10, retry_max_attempts=3)
        emitter.emit(_make_record())
        emitter.flush()
        assert emitter.emit_call_count == 1

    def test_retry_on_failure_then_success(self):
        emitter = ConcreteEmitter(
            buffer_size=10, retry_max_attempts=3, retry_base_ms=1.0
        )
        call_count = [0]
        original_do_emit = emitter._do_emit

        def fail_then_succeed(records):
            call_count[0] += 1
            if call_count[0] < 2:
                raise RuntimeError("Transient failure")
            emitter.emitted_batches.append(records)

        emitter._do_emit = fail_then_succeed
        emitter.emit(_make_record())
        emitter.flush()
        assert call_count[0] == 2
        assert len(emitter.emitted_batches) == 1

    def test_all_retries_exhausted(self):
        emitter = ConcreteEmitter(
            buffer_size=10, retry_max_attempts=3, retry_base_ms=1.0
        )
        emitter.emit_should_fail = True
        emitter.emit(_make_record())
        emitter.flush()
        # All 3 attempts should have been made
        assert emitter.emit_call_count == 3
        # Records should be re-buffered
        assert len(emitter.buffer) == 1

    @patch("bedrock_cost_tracker.emitter.time.sleep")
    def test_backoff_intervals_are_exponential(self, mock_sleep):
        emitter = ConcreteEmitter(
            buffer_size=10, retry_max_attempts=3, retry_base_ms=100.0
        )
        emitter.emit_should_fail = True
        emitter.emit(_make_record())
        emitter.flush()

        # Should have slept twice (between attempt 1→2 and 2→3)
        assert mock_sleep.call_count == 2
        delays = [call.args[0] for call in mock_sleep.call_args_list]

        # First delay: base_ms/1000 * 2^0 + jitter = 0.1s + jitter (0 to 0.05)
        assert delays[0] >= 0.1
        assert delays[0] <= 0.15  # base + max jitter

        # Second delay: base_ms/1000 * 2^1 + jitter = 0.2s + jitter (0 to 0.1)
        assert delays[1] >= 0.2
        assert delays[1] <= 0.3  # base + max jitter


class TestCircuitBreaker:
    """Test circuit breaker state transitions."""

    def test_initial_state_is_closed(self):
        cb = CircuitBreaker(failure_threshold=5, cooldown_seconds=60.0)
        assert cb.state == CircuitState.CLOSED

    def test_stays_closed_below_threshold(self):
        cb = CircuitBreaker(failure_threshold=5, cooldown_seconds=60.0)
        for _ in range(4):
            cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_opens_at_failure_threshold(self):
        cb = CircuitBreaker(failure_threshold=5, cooldown_seconds=60.0)
        for _ in range(5):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_open_blocks_requests(self):
        cb = CircuitBreaker(failure_threshold=5, cooldown_seconds=60.0)
        for _ in range(5):
            cb.record_failure()
        assert not cb.allow_request()

    def test_transitions_to_half_open_after_cooldown(self):
        cb = CircuitBreaker(failure_threshold=5, cooldown_seconds=0.1)
        for _ in range(5):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN

        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.allow_request()

    def test_half_open_to_closed_on_success(self):
        cb = CircuitBreaker(failure_threshold=5, cooldown_seconds=0.1)
        for _ in range(5):
            cb.record_failure()
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_half_open_to_open_on_failure(self):
        cb = CircuitBreaker(failure_threshold=5, cooldown_seconds=0.1)
        for _ in range(5):
            cb.record_failure()
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=5, cooldown_seconds=60.0)
        for _ in range(4):
            cb.record_failure()
        cb.record_success()
        # After reset, need 5 more failures to open
        for _ in range(4):
            cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_emitter_skips_flush_when_circuit_open(self):
        emitter = ConcreteEmitter(
            buffer_size=10,
            retry_max_attempts=1,
            retry_base_ms=1.0,
            circuit_failure_threshold=2,
            circuit_cooldown_seconds=60.0,
        )
        emitter.emit_should_fail = True

        # Trigger enough failures to open the circuit
        emitter.emit(_make_record(request_id="r1"))
        emitter.flush()
        emitter.emit(_make_record(request_id="r2"))
        emitter.flush()

        assert emitter.circuit_breaker.state == CircuitState.OPEN

        # Now flush should be skipped
        initial_call_count = emitter.emit_call_count
        emitter.flush()
        assert emitter.emit_call_count == initial_call_count


class TestBufferOverflow:
    """Test buffer overflow handling — oldest events dropped."""

    def test_overflow_drops_oldest(self):
        # Use an emitter that fails on emit so records stay in buffer after flush
        emitter = ConcreteEmitter(buffer_size=3, retry_max_attempts=1, retry_base_ms=1.0)
        emitter.emit_should_fail = True

        # Fill buffer manually to capacity
        with emitter._lock:
            for i in range(3):
                emitter._buffer.append(_make_record(request_id=f"old-{i}"))

        # Emit one more — should drop the oldest, then auto-flush (which fails),
        # then records get re-buffered
        emitter.emit(_make_record(request_id="new-1"))

        buffer = emitter.buffer
        # After overflow + failed flush + re-buffer, we should have the records
        assert len(buffer) <= 3
        request_ids = [r.request_id for r in buffer]
        assert "new-1" in request_ids
        # The oldest record should have been dropped
        assert "old-0" not in request_ids

    def test_overflow_emits_warning(self):
        emitter = ConcreteEmitter(buffer_size=2)
        # Fill buffer manually
        with emitter._lock:
            emitter._buffer = [
                _make_record(request_id="old-0"),
                _make_record(request_id="old-1"),
            ]

        with patch.object(emitter, "_emit_buffer_overflow_warning") as mock_warn:
            emitter._append_to_buffer(_make_record(request_id="new-0"))
            mock_warn.assert_called_once()


class TestThreadSafety:
    """Basic concurrent access test for thread safety."""

    def test_concurrent_emits(self):
        # Use a buffer larger than total records to avoid auto-flush
        num_threads = 10
        records_per_thread = 20
        total_records = num_threads * records_per_thread
        emitter = ConcreteEmitter(buffer_size=total_records + 1)
        errors = []

        def emit_records(thread_id):
            try:
                for i in range(records_per_thread):
                    emitter.emit(
                        _make_record(request_id=f"thread-{thread_id}-req-{i}")
                    )
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=emit_records, args=(t,)) for t in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # All records should be in buffer (no auto-flush since buffer > total records)
        assert len(emitter.buffer) == total_records

    def test_concurrent_emit_and_flush(self):
        emitter = ConcreteEmitter(buffer_size=50)
        num_emitters = 5
        records_per_emitter = 10
        errors = []

        def emit_records(thread_id):
            try:
                for i in range(records_per_emitter):
                    emitter.emit(
                        _make_record(request_id=f"thread-{thread_id}-req-{i}")
                    )
            except Exception as e:
                errors.append(e)

        def flush_periodically():
            try:
                for _ in range(5):
                    emitter.flush()
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=emit_records, args=(t,))
            for t in range(num_emitters)
        ]
        threads.append(threading.Thread(target=flush_periodically))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # Total records emitted should equal buffer + flushed
        total_emitted = sum(len(b) for b in emitter.emitted_batches) + len(
            emitter.buffer
        )
        assert total_emitted == num_emitters * records_per_emitter


class TestCloudWatchEmitter:
    """Test CloudWatchEmitter with mocked boto3 client."""

    def test_emit_calls_put_metric_data(self):
        mock_cw = MagicMock()
        mock_logs = MagicMock()
        emitter = CloudWatchEmitter(
            cloudwatch_client=mock_cw,
            logs_client=mock_logs,
            buffer_size=10,
        )
        record = _make_record()
        emitter.emit(record)
        emitter.flush()

        mock_cw.put_metric_data.assert_called()
        call_kwargs = mock_cw.put_metric_data.call_args[1]
        assert call_kwargs["Namespace"] == "BedrockCostTracker/Invocations"
        assert len(call_kwargs["MetricData"]) == 5  # 4 base + 1 cost metric

    def test_emit_without_cost_skips_cost_metric(self):
        mock_cw = MagicMock()
        mock_logs = MagicMock()
        emitter = CloudWatchEmitter(
            cloudwatch_client=mock_cw,
            logs_client=mock_logs,
            buffer_size=10,
        )
        record = _make_record(estimated_cost_usd=None)
        emitter.emit(record)
        emitter.flush()

        call_kwargs = mock_cw.put_metric_data.call_args[1]
        assert len(call_kwargs["MetricData"]) == 4  # No cost metric


class TestS3Emitter:
    """Test S3Emitter with mocked boto3 client."""

    def test_emit_calls_put_object(self):
        mock_s3 = MagicMock()
        emitter = S3Emitter(
            s3_client=mock_s3,
            bucket="my-bucket",
            prefix="cost-data",
            buffer_size=10,
        )
        record = _make_record()
        emitter.emit(record)
        emitter.flush()

        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "my-bucket"
        assert call_kwargs["Key"].startswith("cost-data/")
        assert call_kwargs["Key"].endswith(".jsonl")
        assert call_kwargs["ContentType"] == "application/x-ndjson"

    def test_emit_writes_jsonlines_format(self):
        mock_s3 = MagicMock()
        emitter = S3Emitter(
            s3_client=mock_s3,
            bucket="my-bucket",
            buffer_size=10,
        )
        records = [_make_record(request_id=f"req-{i}") for i in range(3)]
        for r in records:
            emitter.emit(r)
        emitter.flush()

        body = mock_s3.put_object.call_args[1]["Body"].decode("utf-8")
        lines = body.strip().split("\n")
        assert len(lines) == 3

        import json

        for i, line in enumerate(lines):
            data = json.loads(line)
            assert data["request_id"] == f"req-{i}"
            assert data["model_id"] == "anthropic.claude-3-sonnet-20240229-v1:0"

    def test_raises_without_bucket(self):
        mock_s3 = MagicMock()
        emitter = S3Emitter(s3_client=mock_s3, bucket="", buffer_size=10)
        emitter.emit(_make_record())

        # Flush should fail but not raise (error is logged)
        emitter.flush()
        mock_s3.put_object.assert_not_called()
