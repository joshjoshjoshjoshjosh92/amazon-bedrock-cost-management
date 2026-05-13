"""TrackerConfig — configuration dataclass for BedrockCostTracker."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import boto3

logger = logging.getLogger(__name__)

VALID_EMIT_TARGETS = ("cloudwatch", "s3", "both")

ALLOWED_METRIC_DIMENSIONS = frozenset(
    ["ModelId", "Team", "AccountId", "Application", "Environment"]
)


@dataclass
class TrackerConfig:
    """Configuration for the Bedrock Cost Tracker SDK wrapper.

    Validates all fields at initialization (fail-fast). Raises ``ValueError``
    for any invalid configuration value.

    Attributes:
        team: Team identifier. Required, non-empty.
        application: Application name. Required, non-empty.
        environment: Deployment environment (e.g. production, staging). Required, non-empty.
        custom_tags: Optional dictionary of additional cost allocation tags.
        emit_to: Metrics emission target. One of "cloudwatch", "s3", or "both".
        buffer_size: Number of records to buffer before flushing. Must be a positive integer.
        retry_max_attempts: Maximum retry attempts for metric emission. Non-negative integer.
        metric_dimensions: CloudWatch metric dimensions to include.
            Must be a subset of allowed dimensions.
        account_id: AWS account ID. Auto-detected via STS if not provided.
    """

    team: str
    application: str
    environment: str
    custom_tags: Dict[str, str] = field(default_factory=dict)
    emit_to: str = "cloudwatch"
    buffer_size: int = 100
    retry_max_attempts: int = 3
    metric_dimensions: List[str] = field(default_factory=lambda: ["ModelId", "Team"])
    account_id: Optional[str] = None

    def __post_init__(self) -> None:
        """Validate configuration values at initialization."""
        self._validate_required_strings()
        self._validate_emit_to()
        self._validate_buffer_size()
        self._validate_retry_max_attempts()
        self._validate_metric_dimensions()
        self._resolve_account_id()

    def _validate_required_strings(self) -> None:
        """Validate that team, application, and environment are non-empty strings."""
        for field_name in ("team", "application", "environment"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"'{field_name}' must be a non-empty string, got {value!r}"
                )

    def _validate_emit_to(self) -> None:
        """Validate emit_to is one of the allowed targets."""
        if self.emit_to not in VALID_EMIT_TARGETS:
            raise ValueError(
                f"'emit_to' must be one of {VALID_EMIT_TARGETS}, got {self.emit_to!r}"
            )

    def _validate_buffer_size(self) -> None:
        """Validate buffer_size is a positive integer."""
        if not isinstance(self.buffer_size, int) or self.buffer_size <= 0:
            raise ValueError(
                f"'buffer_size' must be a positive integer, got {self.buffer_size!r}"
            )

    def _validate_retry_max_attempts(self) -> None:
        """Validate retry_max_attempts is a non-negative integer."""
        if not isinstance(self.retry_max_attempts, int) or self.retry_max_attempts < 0:
            raise ValueError(
                f"'retry_max_attempts' must be a non-negative integer, "
                f"got {self.retry_max_attempts!r}"
            )

    def _validate_metric_dimensions(self) -> None:
        """Validate metric_dimensions is a list of allowed dimension strings."""
        if not isinstance(self.metric_dimensions, list):
            raise ValueError(
                f"'metric_dimensions' must be a list, got {type(self.metric_dimensions).__name__}"
            )
        for dim in self.metric_dimensions:
            if not isinstance(dim, str):
                raise ValueError(
                    f"'metric_dimensions' entries must be strings, got {type(dim).__name__}"
                )
            if dim not in ALLOWED_METRIC_DIMENSIONS:
                raise ValueError(
                    f"Invalid metric dimension {dim!r}. "
                    f"Allowed: {sorted(ALLOWED_METRIC_DIMENSIONS)}"
                )

    def _resolve_account_id(self) -> None:
        """Auto-detect account_id via STS if not provided."""
        if self.account_id is not None:
            if not isinstance(self.account_id, str) or not self.account_id.strip():
                raise ValueError(
                    f"'account_id' must be a non-empty string if provided, "
                    f"got {self.account_id!r}"
                )
            return

        try:
            sts_client = boto3.client("sts")
            identity = sts_client.get_caller_identity()
            self.account_id = identity["Account"]
        except Exception as exc:
            logger.warning(
                "Failed to auto-detect account_id via STS: %s. Using 'unknown'.", exc
            )
            self.account_id = "unknown"
