# Makefile for building the bedrock-cost-tracker Lambda layer
#
# The layer structure follows AWS Lambda layer conventions:
#   build/layer/python/lib/python3.12/site-packages/
#
# Usage:
#   make build    - Install SDK wrapper into layer directory structure
#   make clean    - Remove build artifacts
#   make rebuild  - Clean and rebuild

.PHONY: build clean rebuild

# Path to the SDK wrapper source
SDK_WRAPPER_DIR := ../sdk-wrapper

# Layer output directory (matches ContentUri in template.yaml)
LAYER_DIR := build/layer
SITE_PACKAGES := $(LAYER_DIR)/python/lib/python3.12/site-packages

build: $(SITE_PACKAGES)

$(SITE_PACKAGES): $(SDK_WRAPPER_DIR)/pyproject.toml $(SDK_WRAPPER_DIR)/src/bedrock_cost_tracker/*.py
	@echo "Building bedrock-cost-tracker Lambda layer..."
	@mkdir -p $(SITE_PACKAGES)
	pip install $(SDK_WRAPPER_DIR) \
		--target $(SITE_PACKAGES) \
		--no-deps \
		--upgrade \
		--quiet
	@# Install runtime dependencies (boto3 is provided by Lambda runtime, skip it)
	pip install typing-extensions>=4.0.0 \
		--target $(SITE_PACKAGES) \
		--upgrade \
		--quiet
	@# Remove unnecessary files to reduce layer size
	@find $(SITE_PACKAGES) -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find $(SITE_PACKAGES) -type d -name "*.dist-info" -exec rm -rf {} + 2>/dev/null || true
	@find $(SITE_PACKAGES) -name "*.pyc" -delete 2>/dev/null || true
	@echo "Layer built successfully at $(LAYER_DIR)/"
	@echo "Layer size: $$(du -sh $(LAYER_DIR) | cut -f1)"

clean:
	@echo "Cleaning build artifacts..."
	rm -rf build/
	@echo "Clean complete."

rebuild: clean build
