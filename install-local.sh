#!/bin/bash
# install-local.sh — Install all ATLAS packages from local repo into current environment.
# Usage: bash /path/to/ATLAS/install-local.sh

set -e

ATLAS="$(cd "$(dirname "$0")" && pwd)"

echo "Installing ATLAS from: $ATLAS"
echo ""

pip install \
  -e "$ATLAS/libs/atlas-core/" \
  -e "$ATLAS/platform/atlas-memory/" \
  -e "$ATLAS/platform/atlas-bus/" \
  -e "$ATLAS/platform/atlas-judge/" \
  -e "$ATLAS/platform/atlas-orchestrator/" \
  -e "$ATLAS/agents/cdc-router/" \
  -e "$ATLAS/agents/agent-forge/" \
  -e "$ATLAS/agents/pair-forge/" \
  -e "$ATLAS/agents/atlas-review/" \
  -e "$ATLAS/tools/atlas-dashboard/" \
  -e "$ATLAS/tools/atlas-audit/" \
  -e "$ATLAS/tools/atlas-cli/"

echo ""
echo "Done. Run 'atlas check' to verify."
