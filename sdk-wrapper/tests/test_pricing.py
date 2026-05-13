"""Tests for PricingLookup — embedded pricing, manifest override, and validation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from bedrock_cost_tracker.pricing import (
    BEDROCK_PRICING,
    MINIMUM_COST_FLOOR,
    PRICING_MANIFEST_ENV_VAR,
    PricingLookup,
)


class TestKnownModelPriceLookup:
    """Test price lookup for known models in the bundled pricing table."""

    def test_claude_3_sonnet_input_price(self):
        lookup = PricingLookup()
        price = lookup.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "input"
        )
        assert price == Decimal("0.003")

    def test_claude_3_sonnet_output_price(self):
        lookup = PricingLookup()
        price = lookup.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "output"
        )
        assert price == Decimal("0.015")

    def test_claude_3_haiku_input_price(self):
        lookup = PricingLookup()
        price = lookup.get_price(
            "anthropic.claude-3-haiku-20240307-v1:0", "input"
        )
        assert price == Decimal("0.00025")

    def test_claude_3_haiku_output_price(self):
        lookup = PricingLookup()
        price = lookup.get_price(
            "anthropic.claude-3-haiku-20240307-v1:0", "output"
        )
        assert price == Decimal("0.00125")

    def test_titan_text_express_input_price(self):
        lookup = PricingLookup()
        price = lookup.get_price("amazon.titan-text-express-v1", "input")
        assert price == Decimal("0.0002")

    def test_all_bundled_models_have_both_prices(self):
        """Every bundled model must have both input and output prices."""
        lookup = PricingLookup()
        for model_id in BEDROCK_PRICING:
            input_price = lookup.get_price(model_id, "input")
            output_price = lookup.get_price(model_id, "output")
            assert input_price is not None, f"{model_id} missing input price"
            assert output_price is not None, f"{model_id} missing output price"
            assert input_price > Decimal("0"), f"{model_id} input price must be positive"
            assert output_price > Decimal("0"), f"{model_id} output price must be positive"


class TestUnknownModelReturnsNone:
    """Test that unknown models return None."""

    def test_unknown_model_returns_none(self):
        lookup = PricingLookup()
        price = lookup.get_price("unknown.model-v1:0", "input")
        assert price is None

    def test_unknown_model_output_returns_none(self):
        lookup = PricingLookup()
        price = lookup.get_price("nonexistent.model-xyz", "output")
        assert price is None

    def test_invalid_token_type_returns_none(self):
        lookup = PricingLookup()
        price = lookup.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "embedding"
        )
        assert price is None


class TestManifestOverrideFromLocalFile:
    """Test loading pricing manifest from a local JSON file."""

    def test_valid_manifest_overrides_bundled_pricing(self, tmp_path):
        manifest = {
            "custom.model-v1:0": {
                "input_per_1k_tokens": "0.005",
                "output_per_1k_tokens": "0.025",
            },
            "anthropic.claude-3-sonnet-20240229-v1:0": {
                "input_per_1k_tokens": "0.004",
                "output_per_1k_tokens": "0.020",
            },
        }
        manifest_file = tmp_path / "pricing.json"
        manifest_file.write_text(json.dumps(manifest))

        with patch.dict(os.environ, {PRICING_MANIFEST_ENV_VAR: str(manifest_file)}):
            lookup = PricingLookup()

        # Custom model from manifest is available
        assert lookup.get_price("custom.model-v1:0", "input") == Decimal("0.005")
        assert lookup.get_price("custom.model-v1:0", "output") == Decimal("0.025")

        # Overridden model uses manifest price
        assert lookup.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "input"
        ) == Decimal("0.004")

        # Bundled model NOT in manifest is no longer available (manifest replaces all)
        assert lookup.get_price("amazon.titan-text-express-v1", "input") is None

    def test_manifest_with_many_models(self, tmp_path):
        manifest = {
            "model.a-v1:0": {
                "input_per_1k_tokens": "0.001",
                "output_per_1k_tokens": "0.002",
            },
            "model.b-v1:0": {
                "input_per_1k_tokens": "0.003",
                "output_per_1k_tokens": "0.006",
            },
        }
        manifest_file = tmp_path / "pricing.json"
        manifest_file.write_text(json.dumps(manifest))

        with patch.dict(os.environ, {PRICING_MANIFEST_ENV_VAR: str(manifest_file)}):
            lookup = PricingLookup()

        assert lookup.get_price("model.a-v1:0", "input") == Decimal("0.001")
        assert lookup.get_price("model.b-v1:0", "output") == Decimal("0.006")

    def test_nonexistent_file_falls_back_to_bundled(self):
        with patch.dict(
            os.environ, {PRICING_MANIFEST_ENV_VAR: "/nonexistent/path/pricing.json"}
        ):
            lookup = PricingLookup()

        # Should fall back to bundled defaults
        assert lookup.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "input"
        ) == Decimal("0.003")

    def test_invalid_json_falls_back_to_bundled(self, tmp_path):
        manifest_file = tmp_path / "bad.json"
        manifest_file.write_text("not valid json {{{")

        with patch.dict(os.environ, {PRICING_MANIFEST_ENV_VAR: str(manifest_file)}):
            lookup = PricingLookup()

        # Should fall back to bundled defaults
        assert lookup.get_price(
            "anthropic.claude-3-haiku-20240307-v1:0", "input"
        ) == Decimal("0.00025")


class TestManifestValidationFailures:
    """Test manifest validation: zero price, below floor, negative prices."""

    def test_zero_price_rejected(self, tmp_path):
        manifest = {
            "model.zero-v1:0": {
                "input_per_1k_tokens": "0",
                "output_per_1k_tokens": "0.015",
            },
        }
        manifest_file = tmp_path / "pricing.json"
        manifest_file.write_text(json.dumps(manifest))

        with patch.dict(os.environ, {PRICING_MANIFEST_ENV_VAR: str(manifest_file)}):
            lookup = PricingLookup()

        # Should fall back to bundled defaults
        assert lookup.get_price("model.zero-v1:0", "input") is None
        assert lookup.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "input"
        ) == Decimal("0.003")

    def test_negative_price_rejected(self, tmp_path):
        manifest = {
            "model.negative-v1:0": {
                "input_per_1k_tokens": "-0.001",
                "output_per_1k_tokens": "0.015",
            },
        }
        manifest_file = tmp_path / "pricing.json"
        manifest_file.write_text(json.dumps(manifest))

        with patch.dict(os.environ, {PRICING_MANIFEST_ENV_VAR: str(manifest_file)}):
            lookup = PricingLookup()

        # Should fall back to bundled defaults
        assert lookup.get_price("model.negative-v1:0", "input") is None
        assert lookup.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "input"
        ) == Decimal("0.003")

    def test_below_minimum_cost_floor_rejected(self, tmp_path):
        # Price below $0.0001/1K tokens should be rejected
        manifest = {
            "model.cheap-v1:0": {
                "input_per_1k_tokens": "0.00001",
                "output_per_1k_tokens": "0.015",
            },
        }
        manifest_file = tmp_path / "pricing.json"
        manifest_file.write_text(json.dumps(manifest))

        with patch.dict(os.environ, {PRICING_MANIFEST_ENV_VAR: str(manifest_file)}):
            lookup = PricingLookup()

        # Should fall back to bundled defaults
        assert lookup.get_price("model.cheap-v1:0", "input") is None
        assert lookup.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "input"
        ) == Decimal("0.003")

    def test_price_at_exact_floor_is_accepted(self, tmp_path):
        # Price exactly at $0.0001/1K tokens should be accepted
        manifest = {
            "model.floor-v1:0": {
                "input_per_1k_tokens": "0.0001",
                "output_per_1k_tokens": "0.015",
            },
        }
        manifest_file = tmp_path / "pricing.json"
        manifest_file.write_text(json.dumps(manifest))

        with patch.dict(os.environ, {PRICING_MANIFEST_ENV_VAR: str(manifest_file)}):
            lookup = PricingLookup()

        assert lookup.get_price("model.floor-v1:0", "input") == Decimal("0.0001")

    def test_empty_manifest_rejected(self, tmp_path):
        manifest_file = tmp_path / "pricing.json"
        manifest_file.write_text(json.dumps({}))

        with patch.dict(os.environ, {PRICING_MANIFEST_ENV_VAR: str(manifest_file)}):
            lookup = PricingLookup()

        # Should fall back to bundled defaults
        assert lookup.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "input"
        ) == Decimal("0.003")

    def test_zero_output_price_rejected(self, tmp_path):
        manifest = {
            "model.zero-output-v1:0": {
                "input_per_1k_tokens": "0.003",
                "output_per_1k_tokens": "0",
            },
        }
        manifest_file = tmp_path / "pricing.json"
        manifest_file.write_text(json.dumps(manifest))

        with patch.dict(os.environ, {PRICING_MANIFEST_ENV_VAR: str(manifest_file)}):
            lookup = PricingLookup()

        # Should fall back to bundled defaults
        assert lookup.get_price("model.zero-output-v1:0", "input") is None
        assert lookup.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "input"
        ) == Decimal("0.003")


class TestS3ManifestWithSHA256Verification:
    """Test S3 manifest loading with SHA-256 integrity check."""

    def _make_manifest_content(self) -> str:
        manifest = {
            "anthropic.claude-3-sonnet-20240229-v1:0": {
                "input_per_1k_tokens": "0.004",
                "output_per_1k_tokens": "0.020",
            },
        }
        return json.dumps(manifest)

    def test_valid_s3_manifest_with_matching_sha256(self):
        content = self._make_manifest_content()
        sha256_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        mock_s3_client = MagicMock()
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=content.encode("utf-8"))),
            "Metadata": {"manifest-sha256": sha256_hash},
        }

        mock_session = MagicMock()
        mock_session.client.return_value = mock_s3_client

        with patch.dict(
            os.environ, {PRICING_MANIFEST_ENV_VAR: "s3://my-bucket/pricing.json"}
        ):
            lookup = PricingLookup(boto3_session=mock_session)

        assert lookup.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "input"
        ) == Decimal("0.004")

    def test_s3_manifest_with_mismatched_sha256_falls_back(self):
        content = self._make_manifest_content()

        mock_s3_client = MagicMock()
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=content.encode("utf-8"))),
            "Metadata": {"manifest-sha256": "invalid_hash_value"},
        }

        mock_session = MagicMock()
        mock_session.client.return_value = mock_s3_client

        with patch.dict(
            os.environ, {PRICING_MANIFEST_ENV_VAR: "s3://my-bucket/pricing.json"}
        ):
            lookup = PricingLookup(boto3_session=mock_session)

        # Should fall back to bundled defaults
        assert lookup.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "input"
        ) == Decimal("0.003")

    def test_s3_manifest_without_sha256_metadata_still_loads(self):
        """S3 manifest without SHA-256 metadata logs warning but still loads."""
        content = self._make_manifest_content()

        mock_s3_client = MagicMock()
        mock_s3_client.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=content.encode("utf-8"))),
            "Metadata": {},
        }

        mock_session = MagicMock()
        mock_session.client.return_value = mock_s3_client

        with patch.dict(
            os.environ, {PRICING_MANIFEST_ENV_VAR: "s3://my-bucket/pricing.json"}
        ):
            lookup = PricingLookup(boto3_session=mock_session)

        # Should still load the manifest (warning logged but not a hard failure)
        assert lookup.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "input"
        ) == Decimal("0.004")

    def test_s3_access_error_falls_back_to_bundled(self):
        mock_s3_client = MagicMock()
        mock_s3_client.get_object.side_effect = Exception("Access Denied")

        mock_session = MagicMock()
        mock_session.client.return_value = mock_s3_client

        with patch.dict(
            os.environ, {PRICING_MANIFEST_ENV_VAR: "s3://my-bucket/pricing.json"}
        ):
            lookup = PricingLookup(boto3_session=mock_session)

        # Should fall back to bundled defaults
        assert lookup.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "input"
        ) == Decimal("0.003")

    def test_invalid_s3_uri_falls_back_to_bundled(self):
        mock_session = MagicMock()

        with patch.dict(
            os.environ, {PRICING_MANIFEST_ENV_VAR: "s3://bucket-only-no-key"}
        ):
            lookup = PricingLookup(boto3_session=mock_session)

        # Should fall back to bundled defaults
        assert lookup.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "input"
        ) == Decimal("0.003")


class TestNoManifestEnvVar:
    """Test behavior when no manifest environment variable is set."""

    def test_uses_bundled_defaults_when_no_env_var(self):
        with patch.dict(os.environ, {}, clear=False):
            # Ensure the env var is not set
            os.environ.pop(PRICING_MANIFEST_ENV_VAR, None)
            lookup = PricingLookup()

        assert lookup.get_price(
            "anthropic.claude-3-sonnet-20240229-v1:0", "input"
        ) == Decimal("0.003")
        assert lookup.get_price(
            "anthropic.claude-3-opus-20240229-v1:0", "input"
        ) == Decimal("0.015")
