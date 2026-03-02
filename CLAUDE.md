# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Claude + GitHub Copilot Integration — a two-phase pipeline where Claude acts as architect/reviewer and Copilot handles implementation:

1. **Design phase**: Claude Opus receives a natural-language requirement and returns pseudocode as Python comments, which Copilot expands into real code in the IDE.
2. **Review phase**: Claude Haiku receives a git diff and returns a JSON array of `ReviewIssue` objects with `issue`, `severity` (`critical`/`warning`/`suggestion`), and `suggestion`.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
cp /path/to/your/system_prompt.txt .   # required, not in repo
python bootstrap.py                    # validate environment
python bootstrap.py --full-test        # also run E2E test (consumes real tokens)
python bootstrap.py --skip-redis       # skip Redis check
```

## Global CLI (`cc`)

A global wrapper `~/bin/cc` is installed so you can invoke Claude Copilot from **any project or folder** without `cd`-ing to this repo first. It auto-loads `.env` and `SYSTEM_PROMPT_PATH`.

```bash
# First-time shell setup (once per session until ~/.bashrc is sourced by your terminal)
source ~/.bashrc

# From any project directory:
cc design "Crear endpoint REST para registro de usuario"
cc design "Crear endpoint REST..." app/service.py
cc review origin/main
cc review-batch
cc invalidate-cache
```

The wrapper lives at `~/bin/cc` and is configured in `~/.bashrc` (`export PATH="$HOME/bin:$PATH"`).

## CLI Commands (direct invocation from repo root)

```bash
# Design a feature (outputs pseudocode to stdout or injects into file)
python orchestrator.py design "Crear endpoint REST para registro de usuario"
python orchestrator.py design "Crear endpoint REST..." app/service.py

# Review a diff
python orchestrator.py review path/to/file.diff
python orchestrator.py review origin/main          # git diff against branch
python orchestrator.py review                      # git diff against origin/main

# Review all changed files in batch (one API call per group)
python orchestrator.py review-batch

# Invalidate system prompt cache
python orchestrator.py invalidate-cache

# IDE Injector CLI
python ide_injector.py inject --file app/service.py --mode append --tag my-feature < pseudocode.txt
python ide_injector.py list --file app/service.py
python ide_injector.py remove --file app/service.py --tag my-feature
python ide_injector.py batch --json module_design.json
python ide_injector.py restore --file app/service.py
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | **Required** |
| `DESIGN_MODEL` | `claude-opus-4-5` | Model for design phase |
| `REVIEW_MODEL` | `claude-haiku-4-5-20251001` | Model for review phase |
| `MAX_TOKENS_DESIGN` | `1500` | Token cap for design responses |
| `MAX_TOKENS_REVIEW` | `800` | Token cap for review responses |
| `SYSTEM_PROMPT_PATH` | `system_prompt.txt` | Path to system prompt file |
| `REDIS_URL` | `redis://localhost:6379/0` | Cache URL (optional) |
| `CACHE_TTL_SECONDS` | `86400` | System prompt cache TTL |
| `LOG_LEVEL` | `INFO` | Logging level |

## Architecture

### Files
- **`orchestrator.py`** — Main entry point. Contains `OrchestratorConfig`, `ClaudeClient`, `SystemPromptCache`, `TokenTracker`, `DiffExtractor`, and a simplified `IDEInjector`. All async internals, synchronous Anthropic SDK calls.
- **`ide_injector.py`** — Full-featured injector with backup management, 6 injection modes (`APPEND`, `PREPEND`, `REPLACE`, `AT_LINE`, `AFTER_CLASS`, `AFTER_FUNC`), `BatchInjector`, and `FileWatcher`.
- **`bootstrap.py`** — Environment validation only; runs 7 checks and exits with code 1 if critical checks fail.
- **`system_prompt.txt`** — Project-specific system prompt (must be created; not in repo). Should contain STACK, AL DISEÑAR, AL REVISAR, and severity format sections.

### Key design decisions
- **Output contract**: JSON goes to stdout; all logs go to stderr. Critical for GitHub Actions pipelines.
- **System prompt caching**: Cache-aside pattern with SHA256 hash as key. Redis preferred, falls back to in-memory dict. Reduces input tokens ~40%.
- **Token KPI**: `TokenTracker.KPI_LIMIT = 1500` tokens/feature. Logs a warning when exceeded.
- **Diff filtering**: `DiffExtractor.filter_files()` keeps only `.py`/`.ts`/`.js`, skips migrations, node_modules, lock files, and binaries. Reduces input tokens ~70%.
- **Chunked review**: Diffs >3000 chars are split into 3000-char chunks, reviewed separately, then merged and capped at 10 issues sorted by severity.
- **Batch review**: `review-batch` groups up to 3 diffs or 4000 chars per API call to reduce per-request overhead.
- **Retry**: `ClaudeClient` retries `RateLimitError` up to 3 times with exponential backoff (1s, 2s, 4s).
- **Pseudocode markers**: Injected blocks are delimited by `# --- @claude:start {tag} ---` / `# --- @claude:end {tag} ---` for identification and replacement.
- **Metrics export**: Pass `--export-metrics` to any orchestrator command to write token data to `/tmp/token_metrics.json` (Grafana-compatible format).
