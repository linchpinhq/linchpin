#!/usr/bin/env bash
# Update an existing pull request's title and/or body.
#
# Usage:
#   scripts/update_pr.sh <pr-number> [--title "<title>"] [--body-file <path>]
#
# At least one of --title or --body-file must be provided.

set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "usage: scripts/update_pr.sh <pr-number> [--title TITLE] [--body-file PATH]" >&2
    exit 1
fi

PR_NUMBER="$1"
shift

ARGS=()

while [ "$#" -gt 0 ]; do
    case "$1" in
        --title)
            ARGS+=(--title "$2")
            shift 2
            ;;
        --body-file)
            if [ ! -f "$2" ]; then
                echo "error: body file not found: $2" >&2
                exit 1
            fi
            ARGS+=(--body-file "$2")
            shift 2
            ;;
        *)
            echo "error: unknown flag $1" >&2
            exit 1
            ;;
    esac
done

if [ "${#ARGS[@]}" -eq 0 ]; then
    echo "error: at least one of --title or --body-file is required" >&2
    exit 1
fi

gh pr edit "$PR_NUMBER" "${ARGS[@]}"
