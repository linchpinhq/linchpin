#!/usr/bin/env bash
# Create a pull request using the gh CLI.
#
# Usage:
#   scripts/create_pr.sh "<title>" <body-file> [--draft]
#
# - <title>      : PR title (≤70 chars recommended)
# - <body-file>  : Path to a file containing the PR body markdown
# - --draft      : Optional. Opens the PR in draft mode.
#
# Honors the repo's default branch as the base. Requires the GH CLI
# (gh) to be authenticated.

set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "usage: scripts/create_pr.sh \"<title>\" <body-file> [--draft]" >&2
    exit 1
fi

TITLE="$1"
BODY_FILE="$2"
DRAFT_FLAG=""

if [ "${3:-}" = "--draft" ]; then
    DRAFT_FLAG="--draft"
fi

if [ ! -f "$BODY_FILE" ]; then
    echo "error: body file not found: $BODY_FILE" >&2
    exit 1
fi

# Use $TITLE verbatim; pipe body through a heredoc-equivalent via stdin.
gh pr create \
    --title "$TITLE" \
    --body-file "$BODY_FILE" \
    $DRAFT_FLAG
