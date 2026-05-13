"""Tests for TrackerConfig dataclass and validation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bedrock_cost_tracker.config import (
    ALLOWED_METRIC_DIMENSIONS,
    VALID_EMIT_TARGETS,
    TrackerConfig,
)


class TestTrackerConfigValid:
    """Tests for valid TrackerConfig construction."""

    def test_minimal_valid_config(self):
        """Minimal required fields produce a valid config with defaults."""
        with patch("bedrock_cost_tracker.config.boto3") as mock_boto3:
            mock_sts = MagicMock()
            mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
            mock_boto3.client.return_value = mock_sts

            config = TrackerConfig(
                team="ml-platform",
                application="chatbot",
                environment="production",
            )

        assert config.team == "ml-platform"
        assert config.application == "chatbot"
        assert config.environment == "production"
        assert config.custom_tags == {}
        assert config.emit_to == "cloudwatch"
        assert config.buffer_size == 100
        assert config.retry_max_attempts == 3
        assert config.metric_dimensions == ["ModelId", "Team"]
        assert config.account_id == "123456789012"

    def test_full_config_with_explicit_account_id(self):
        """All fields explicitly provided, no STS call needed."""
        config = TrackerConfig(
            team="data-science",
            application="recommender",
            environment="staging",
            custom_tags={"cost_center": "CC-5678"},
            emit_to="both",
            buffer_size=200,
            retry_max_attempts=5,
            metric_dimensions=["ModelId", "Team", "Application"],
            account_id="987654321098",
        )

        assert config.team == "data-science"
        assert config.application == "recommender"
        assert config.environment == "staging"
        assert config.custom_tags == {"cost_center": "CC-5678"}
        assert config.emit_to == "both"
        assert config.buffer_size == 200
        assert config.retry_max_attempts == 5
        assert config.metric_dimensions == ["ModelId", "Team", "Application"]
        assert config.account_id == "987654321098"

    @pytest.mark.parametrize("emit_to", VALID_EMIT_TARGETS)
    def test_all_valid_emit_targets(self, emit_to):
        """Each valid emit_to value is accepted."""
        config = TrackerConfig(
            team="team",
            application="app",
            environment="dev",
            emit_to=emit_to,
            account_id="111111111111",
        )
        assert config.emit_to == emit_to

    def test_all_allowed_metric_dimensions(self):
        """All allowed metric dimensions can be used together."""
        config = TrackerConfig(
            team="team",
            application="app",
            environment="dev",
            metric_dimensions=sorted(ALLOWED_METRIC_DIMENSIONS),
            account_id="111111111111",
        )
        assert set(config.metric_dimensions) == ALLOWED_METRIC_DIMENSIONS

    def test_empty_metric_dimensions_list(self):
        """An empty metric_dimensions list is valid."""
        config = TrackerConfig(
            team="team",
            application="app",
            environment="dev",
            metric_dimensions=[],
            account_id="111111111111",
        )
        assert config.metric_dimensions == []

    def test_retry_max_attempts_zero(self):
        """Zero retries is valid (non-negative)."""
        config = TrackerConfig(
            team="team",
            application="app",
            environment="dev",
            retry_max_attempts=0,
            account_id="111111111111",
        )
        assert config.retry_max_attempts == 0

    def test_buffer_size_one(self):
        """Buffer size of 1 is valid (positive integer)."""
        config = TrackerConfig(
            team="team",
            application="app",
            environment="dev",
            buffer_size=1,
            account_id="111111111111",
        )
        assert config.buffer_size == 1


class TestTrackerConfigInvalid:
    """Tests for invalid TrackerConfig construction raising ValueError."""

    @pytest.mark.parametrize("field_name", ["team", "application", "environment"])
    def test_empty_string_required_fields(self, field_name):
        """Empty string for required fields raises ValueError."""
        kwargs = {
            "team": "team",
            "application": "app",
            "environment": "dev",
            "account_id": "111111111111",
        }
        kwargs[field_name] = ""
        with pytest.raises(ValueError, match=field_name):
            TrackerConfig(**kwargs)

    @pytest.mark.parametrize("field_name", ["team", "application", "environment"])
    def test_whitespace_only_required_fields(self, field_name):
        """Whitespace-only string for required fields raises ValueError."""
        kwargs = {
            "team": "team",
            "application": "app",
            "environment": "dev",
            "account_id": "111111111111",
        }
        kwargs[field_name] = "   "
        with pytest.raises(ValueError, match=field_name):
            TrackerConfig(**kwargs)

    def test_invalid_emit_to(self):
        """Invalid emit_to value raises ValueError."""
        with pytest.raises(ValueError, match="emit_to"):
            TrackerConfig(
                team="team",
                application="app",
                environment="dev",
                emit_to="kafka",
                account_id="111111111111",
            )

    def test_buffer_size_zero(self):
        """Buffer size of 0 raises ValueError."""
        with pytest.raises(ValueError, match="buffer_size"):
            TrackerConfig(
                team="team",
                application="app",
                environment="dev",
                buffer_size=0,
                account_id="111111111111",
            )

    def test_buffer_size_negative(self):
        """Negative buffer size raises ValueError."""
        with pytest.raises(ValueError, match="buffer_size"):
            TrackerConfig(
                team="team",
                application="app",
                environment="dev",
                buffer_size=-1,
                account_id="111111111111",
            )

    def test_retry_max_attempts_negative(self):
        """Negative retry_max_attempts raises ValueError."""
        with pytest.raises(ValueError, match="retry_max_attempts"):
            TrackerConfig(
                team="team",
                application="app",
                environment="dev",
                retry_max_attempts=-1,
                account_id="111111111111",
            )

    def test_invalid_metric_dimension(self):
        """Unknown metric dimension raises ValueError."""
        with pytest.raises(ValueError, match="Invalid metric dimension"):
            TrackerConfig(
                team="team",
                application="app",
                environment="dev",
                metric_dimensions=["ModelId", "InvalidDim"],
                account_id="111111111111",
            )

    def test_metric_dimensions_not_a_list(self):
        """Non-list metric_dimensions raises ValueError."""
        with pytest.raises(ValueError, match="metric_dimensions"):
            TrackerConfig(
                team="team",
                application="app",
                environment="dev",
                metric_dimensions="ModelId",  # type: ignore[arg-type]
                account_id="111111111111",
            )

    def test_empty_account_id_string(self):
        """Empty string account_id raises ValueError."""
        with pytest.raises(ValueError, match="account_id"):
            TrackerConfig(
                team="team",
                application="app",
                environment="dev",
                account_id="",
            )

    def test_whitespace_account_id(self):
        """Whitespace-only account_id raises ValueError."""
        with pytest.raises(ValueError, match="account_id"):
            TrackerConfig(
                team="team",
                application="app",
                environment="dev",
                account_id="   ",
            )


class TestTrackerConfigAutoDetect:
    """Tests for account_id auto-detection via STS."""

    def test_auto_detect_account_id_success(self):
        """account_id is auto-detected via STS when not provided."""
        with patch("bedrock_cost_tracker.config.boto3") as mock_boto3:
            mock_sts = MagicMock()
            mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
            mock_boto3.client.return_value = mock_sts

            config = TrackerConfig(
                team="team",
                application="app",
                environment="dev",
            )

        assert config.account_id == "123456789012"
        mock_boto3.client.assert_called_once_with("sts")

    def test_auto_detect_account_id_failure_fallback(self):
        """Falls back to 'unknown' when STS call fails."""
        with patch("bedrock_cost_tracker.config.boto3") as mock_boto3:
            mock_boto3.client.side_effect = Exception("No credentials")

            config = TrackerConfig(
                team="team",
                application="app",
                environment="dev",
            )

        assert config.account_id == "unknown"

    def test_explicit_account_id_skips_sts(self):
        """Providing account_id explicitly skips the STS call."""
        with patch("bedrock_cost_tracker.config.boto3") as mock_boto3:
            config = TrackerConfig(
                team="team",
                application="app",
                environment="dev",
                account_id="999999999999",
            )

        mock_boto3.client.assert_not_called()
        assert config.account_id == "999999999999"
