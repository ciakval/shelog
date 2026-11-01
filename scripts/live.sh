#!/usr/bin/env bash
# Run this checkout's shelog with the main checkout root as the working directory.
# Git worktrees do not contain the owner's gitignored config and data/, so live runs
# go through here: the app still finds and loads its own config from its working
# directory, and no command line ever names the secrets file.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
WT=$(git rev-parse --show-toplevel)
MAIN=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")

if [ "$#" -eq 1 ] && [ "$1" = "--root" ]; then
  printf '%s\n' "$MAIN"
  exit 0
fi

uv sync --locked --quiet --project "$WT"
cd "$MAIN"
# RateGuard spaces requests inside one process; this start delay extends the 1 req/s
# rule across sequential live commands (the previous command's last request went out
# just before that process exited).
sleep 1.1
exec "$WT/.venv/bin/shelog" "$@"
