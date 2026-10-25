#!/usr/bin/env bash
# Build this fork and install the Optimize helpers.
#   ZIG=/path/to/zig-0.16/zig ./install.sh        (Zig 0.16 is required by the vendored libghostty-vt)
set -euo pipefail
cd "$(dirname "$0")"
HELPERS="${HERDR_OPTIMIZE_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/herdr/optimize}"
command -v cargo >/dev/null || { echo "cargo not found: https://rustup.rs"; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }
if [ -z "${ZIG:-}" ] && ! command -v zig >/dev/null; then
  echo "Zig 0.16 is required (set ZIG=/path/to/zig or put zig on PATH). Install: https://ziglang.org/download/ or: mise install zig@0.16.0"; exit 1
fi
cargo build --release
mkdir -p "$HELPERS"
cp optimize/*.py "$HELPERS"/
echo
echo "Built:   $(pwd)/target/release/herdr"
echo "Helpers: $HELPERS  (set HERDR_OPTIMIZE_HOME to use another directory)"
echo "Model:   local Ollama at 127.0.0.1:11434 with one of qwen3-coder:30b, qwen3.6:27b, qwen3:4b-instruct; or an exo URL in ~/.config/herdr/optimizer.json"
echo "Run:     ./target/release/herdr      then click ✦ Optimize on any Claude/Codex/Hermes pane with an unsent draft."
