#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

mise install
mise exec -- uv sync --locked
