# Changes in this fork

This repository is a fork of [herdrdev/herdr](https://github.com/herdrdev/herdr) (Apache-2.0).
Per the Apache License §4(b), this file states the changes made to the original work. Base: upstream v0.9.1.

## The Optimize button
- Every supported agent pane (Claude Code, Codex, Antigravity, Hermes) gets a `✦ Optimize` button in its chrome
  (`src/client/shell/composition.rs`, hit-testing in `mouse.rs`, the `OptimizePrompt` action in `state.rs` / `shell_runtime.rs`).
- A click runs `optimize/workspace_optimize.py --inline <pane> <socket>` in a thread; while it runs the button reads `◌ Working`.
  The helper reads the pane's unsent one-line draft through Herdr's socket API, asks a **local** model for a plan
  (what to keep, add, subtract; effort; skills to read; whether to compact), clears the editor and types the prepared
  prompt back **without pressing Enter**. Nothing is ever sent on your behalf.
- Provider-aware: separate Claude and Codex policies (`optimizer_providers.py`), a real inventory of installed skills and
  plugins so the rewrite never names one that does not exist, conversation-bound recovery files, cross-client locks and
  native compaction receipts (`optimizer_session.py`). Explicit constraints in the draft ("don't touch the database")
  are re-appended if the model drops them.
- Backends (`optimizer_backend.py`): an exo cluster if `~/.config/herdr/optimizer.json` prefers one **and it is up**,
  otherwise local Ollama (`qwen3-coder:30b` → `qwen3.6:27b` → `qwen3:4b-instruct`). No cloud fallback.
- Context meter in the settings overlay from the agent's own session file (`usage.py`).
- Helper location is resolved by `src/client/optimize_helpers.rs`: `$HERDR_OPTIMIZE_HOME`, then
  `~/.local/lib/herdr-personal`, then `$XDG_DATA_HOME/herdr/optimize`.

## Other client changes
- Pointer focus: entering a pane with an unmodified mouse move focuses it; moving inside it does not refocus.
- Combine workspaces: drag a workspace row onto another to move all its panes there; drop between rows to reorder.
- Agent-list drag joining; right-click → Paste into a pane (multiline stays one paste, Enter is never generated).
- Native settings pane and local-model picker (`native_settings.py`, `settings.py`, `local_models.py`) and a live review
  queue (`review.py`). These two are optional and degrade when their inputs are absent.

## Not changed
Server, protocol, persistence and the upstream build (Rust + vendored libghostty-vt, which needs Zig 0.16).
