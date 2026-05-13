# Architecture Diagrams

This directory contains Mermaid source files (`.mmd`) for each tier's architecture diagram.

## Files

| File | Description |
|------|-------------|
| `tier1.mmd` | Tier 1 — Cost Explorer, Budgets, Anomaly Detection |
| `tier2.mmd` | Tier 2 — Application Inference Profiles and Cost Allocation Tags |
| `tier3.mmd` | Tier 3 — CloudWatch Monitoring Pipeline |
| `tier4.mmd` | Tier 4 — CUR Analytics Pipeline |
| `tier5.mmd` | Tier 5 — Cost Sentry Enforcement System |

## Rendering Diagrams

These `.mmd` files are [Mermaid](https://mermaid.js.org/) diagram sources. You can render them to PNG/SVG using any of these methods:

### Option 1: Mermaid CLI (recommended for CI/CD)

```bash
npm install -g @mermaid-js/mermaid-cli

# Render a single diagram
mmdc -i diagrams/tier3.mmd -o diagrams/tier3-architecture.png -t neutral -w 1200

# Render all diagrams
for f in diagrams/*.mmd; do
  mmdc -i "$f" -o "${f%.mmd}-architecture.png" -t neutral -w 1200
done
```

### Option 2: VS Code Extension

Install the [Mermaid Preview](https://marketplace.visualstudio.com/items?itemName=bierner.markdown-mermaid) extension to preview diagrams inline.

### Option 3: GitHub Rendering

GitHub natively renders Mermaid in markdown files. Wrap the content in a ` ```mermaid ` code block.

### Option 4: Mermaid Live Editor

Paste the `.mmd` file content into [mermaid.live](https://mermaid.live/) for interactive editing and export.

## Editing Guidelines

- Keep diagrams focused on the architecture of a single tier
- Use consistent node naming across diagrams (e.g., `BR` for Bedrock Runtime)
- Use subgraphs to group related resources
- Dashed lines (`-.->`) indicate optional or async connections
- Solid lines (`-->`) indicate synchronous data flow
