"""PricingLookup — embedded pricing table with manifest override support."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Minimum cost floor: no model's input price can be below $0.0001/1K tokens
MINIMUM_COST_FLOOR = Decimal("0.0001")

# Environment variable for manifest override
PRICING_MANIFEST_ENV_VAR = "BEDROCK_PRICING_MANIFEST"

# Bundled default pricing table — known-good prices at release time.
# Prices are per 1K tokens in USD.
BEDROCK_PRICING: Dict[str, Dict[str, Decimal]] = {
    # Claude 3.5 family
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {
        "input_per_1k_tokens": Decimal("0.003"),
        "output_per_1k_tokens": Decimal("0.015"),
    },
    "anthropic.claude-3-5-sonnet-20240620-v1:0": {
        "input_per_1k_tokens": Decimal("0.003"),
        "output_per_1k_tokens": Decimal("0.015"),
    },
    "anthropic.claude-3-5-haiku-20241022-v1:0": {
        "input_per_1k_tokens": Decimal("0.0008"),
        "output_per_1k_tokens": Decimal("0.004"),
    },
    # Claude 3 family
    "anthropic.claude-3-opus-20240229-v1:0": {
        "input_per_1k_tokens": Decimal("0.015"),
        "output_per_1k_tokens": Decimal("0.075"),
    },
    "anthropic.claude-3-sonnet-20240229-v1:0": {
        "input_per_1k_tokens": Decimal("0.003"),
        "output_per_1k_tokens": Decimal("0.015"),
    },
    "anthropic.claude-3-haiku-20240307-v1:0": {
        "input_per_1k_tokens": Decimal("0.00025"),
        "output_per_1k_tokens": Decimal("0.00125"),
    },
    # Amazon Titan family
    "amazon.titan-text-express-v1": {
        "input_per_1k_tokens": Decimal("0.0002"),
        "output_per_1k_tokens": Decimal("0.0006"),
    },
    "amazon.titan-text-lite-v1": {
        "input_per_1k_tokens": Decimal("0.00015"),
        "output_per_1k_tokens": Decimal("0.0002"),
    },
    "amazon.titan-text-premier-v1:0": {
        "input_per_1k_tokens": Decimal("0.0005"),
        "output_per_1k_tokens": Decimal("0.0015"),
    },
    # Meta Llama family
    "meta.llama3-70b-instruct-v1:0": {
        "input_per_1k_tokens": Decimal("0.00265"),
        "output_per_1k_tokens": Decimal("0.0035"),
    },
    "meta.llama3-8b-instruct-v1:0": {
        "input_per_1k_tokens": Decimal("0.0003"),
        "output_per_1k_tokens": Decimal("0.0006"),
    },
    # Mistral family
    "mistral.mistral-large-2402-v1:0": {
        "input_per_1k_tokens": Decimal("0.004"),
        "output_per_1k_tokens": Decimal("0.012"),
    },
    "mistral.mixtral-8x7b-instruct-v0:1": {
        "input_per_1k_tokens": Decimal("0.00045"),
        "output_per_1k_tokens": Decimal("0.0007"),
    },
    # Cohere family
    "cohere.command-r-plus-v1:0": {
        "input_per_1k_tokens": Decimal("0.003"),
        "output_per_1k_tokens": Decimal("0.015"),
    },
    "cohere.command-r-v1:0": {
        "input_per_1k_tokens": Decimal("0.0005"),
        "output_per_1k_tokens": Decimal("0.0015"),
    },
}


class PricingLookup:
    """Pricing lookup with bundled defaults and optional manifest override.

    The pricing table is loaded in this order:
    1. Bundled defaults (BEDROCK_PRICING dictionary above)
    2. Override via BEDROCK_PRICING_MANIFEST environment variable (local file or S3 URI)

    If the manifest fails validation, the class falls back to bundled defaults
    and emits a PricingManifestValidationFailed warning metric.
    """

    def __init__(self, boto3_session: Optional[Any] = None) -> None:
        """Initialize PricingLookup.

        Args:
            boto3_session: Optional boto3 session for S3 access. If not provided,
                a default session is created only when needed for S3 manifests.
        """
        self._boto3_session = boto3_session
        self._pricing: Dict[str, Dict[str, Decimal]] = dict(BEDROCK_PRICING)
        self._load_manifest_override()

    def get_price(self, model_id: str, token_type: str) -> Optional[Decimal]:
        """Get price per 1K tokens for a model.

        Args:
            model_id: Bedrock model identifier
            token_type: "input" or "output"

        Returns:
            Price per 1K tokens as Decimal, or None if model is unknown.
        """
        model_pricing = self._pricing.get(model_id)
        if model_pricing is None:
            logger.warning("Unknown model ID for pricing: %s", model_id)
            return None

        key = f"{token_type}_per_1k_tokens"
        price = model_pricing.get(key)
        if price is None:
            logger.warning(
                "Unknown token type '%s' for model '%s'", token_type, model_id
            )
            return None

        return price

    def _load_manifest_override(self) -> None:
        """Load pricing manifest from environment variable if set."""
        manifest_path = os.environ.get(PRICING_MANIFEST_ENV_VAR)
        if not manifest_path:
            return

        try:
            if manifest_path.startswith("s3://"):
                manifest_data = self._load_from_s3(manifest_path)
            else:
                manifest_data = self._load_from_file(manifest_path)

            if manifest_data is None:
                # Loading failed, already logged
                self._emit_validation_failed_metric()
                return

            parsed = self._parse_manifest(manifest_data)
            if parsed is None:
                self._emit_validation_failed_metric()
                return

            if not self._validate_manifest(parsed):
                self._emit_validation_failed_metric()
                return

            # Validation passed — override bundled pricing
            self._pricing = parsed
            logger.info(
                "Loaded pricing manifest override with %d models from %s",
                len(parsed),
                manifest_path,
            )

        except Exception as exc:
            logger.warning(
                "Failed to load pricing manifest from %s: %s. "
                "Falling back to bundled defaults.",
                manifest_path,
                exc,
            )
            self._emit_validation_failed_metric()

    def _load_from_file(self, path: str) -> Optional[str]:
        """Load manifest content from a local file."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except (OSError, IOError) as exc:
            logger.warning("Failed to read pricing manifest file %s: %s", path, exc)
            return None

    def _load_from_s3(self, s3_uri: str) -> Optional[str]:
        """Load manifest from S3 with SHA-256 integrity verification."""
        try:
            import boto3

            # Parse S3 URI
            parts = s3_uri.replace("s3://", "").split("/", 1)
            if len(parts) != 2:
                logger.warning("Invalid S3 URI format: %s", s3_uri)
                return None

            bucket, key = parts[0], parts[1]

            session = self._boto3_session or boto3
            s3_client = session.client("s3") if hasattr(session, "client") else session.client("s3")

            # Get object with metadata
            response = s3_client.get_object(Bucket=bucket, Key=key)
            content = response["Body"].read().decode("utf-8")

            # Verify SHA-256 integrity
            metadata = response.get("Metadata", {})
            expected_sha256 = metadata.get("manifest-sha256")

            if expected_sha256:
                computed_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if computed_sha256 != expected_sha256:
                    logger.warning(
                        "SHA-256 integrity check failed for %s. "
                        "Expected: %s, Computed: %s",
                        s3_uri,
                        expected_sha256,
                        computed_sha256,
                    )
                    return None
            else:
                logger.warning(
                    "S3 manifest %s missing x-amz-meta-manifest-sha256 metadata. "
                    "Skipping integrity check.",
                    s3_uri,
                )

            return content

        except Exception as exc:
            logger.warning("Failed to load pricing manifest from S3 %s: %s", s3_uri, exc)
            return None

    def _parse_manifest(self, content: str) -> Optional[Dict[str, Dict[str, Decimal]]]:
        """Parse JSON manifest content into pricing dictionary."""
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse pricing manifest JSON: %s", exc)
            return None

        if not isinstance(raw, dict):
            logger.warning("Pricing manifest must be a JSON object, got %s", type(raw).__name__)
            return None

        pricing: Dict[str, Dict[str, Decimal]] = {}
        for model_id, model_prices in raw.items():
            if not isinstance(model_prices, dict):
                logger.warning(
                    "Invalid pricing entry for model %s: expected dict, got %s",
                    model_id,
                    type(model_prices).__name__,
                )
                return None

            parsed_prices: Dict[str, Decimal] = {}
            for price_key, price_value in model_prices.items():
                try:
                    parsed_prices[price_key] = Decimal(str(price_value))
                except (InvalidOperation, TypeError, ValueError) as exc:
                    logger.warning(
                        "Invalid price value for %s.%s: %s",
                        model_id,
                        price_key,
                        exc,
                    )
                    return None

            pricing[model_id] = parsed_prices

        return pricing

    def _validate_manifest(self, pricing: Dict[str, Dict[str, Decimal]]) -> bool:
        """Validate manifest pricing data.

        Checks:
        - All prices must be positive (> $0)
        - No model's input price below $0.0001/1K tokens (minimum cost floor)

        Returns:
            True if validation passes, False otherwise.
        """
        if not pricing:
            logger.warning("Pricing manifest is empty")
            return False

        for model_id, model_prices in pricing.items():
            for price_key, price_value in model_prices.items():
                # All prices must be positive
                if price_value <= Decimal("0"):
                    logger.warning(
                        "Pricing manifest validation failed: %s.%s has non-positive "
                        "price %s. All prices must be > $0.",
                        model_id,
                        price_key,
                        price_value,
                    )
                    return False

            # Minimum cost floor check on input price
            input_price = model_prices.get("input_per_1k_tokens")
            if input_price is not None and input_price < MINIMUM_COST_FLOOR:
                logger.warning(
                    "Pricing manifest validation failed: %s input price %s is below "
                    "minimum cost floor of %s/1K tokens.",
                    model_id,
                    input_price,
                    MINIMUM_COST_FLOOR,
                )
                return False

        return True

    def _emit_validation_failed_metric(self) -> None:
        """Emit PricingManifestValidationFailed warning metric to CloudWatch."""
        try:
            import boto3

            session = self._boto3_session or boto3
            cw_client = (
                session.client("cloudwatch")
                if hasattr(session, "client")
                else session.client("cloudwatch")
            )
            cw_client.put_metric_data(
                Namespace="BedrockCostTracker/Warnings",
                MetricData=[
                    {
                        "MetricName": "PricingManifestValidationFailed",
                        "Value": 1.0,
                        "Unit": "Count",
                    }
                ],
            )
        except Exception as exc:
            # Best-effort metric emission — don't fail the fallback path
            logger.debug(
                "Failed to emit PricingManifestValidationFailed metric: %s", exc
            )
