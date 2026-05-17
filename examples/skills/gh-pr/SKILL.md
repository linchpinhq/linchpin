---
name: gh-pr
description: Creates and updates pull requests in a GitHub repository using the gh CLI. Use when the user asks to open, draft, review, or update a pull request.
---

# gh-pr

This skill helps you create high-quality pull requests with the GitHub `gh` CLI.

## When to use this skill

Use this skill when the user asks you to:

- Open a new pull request.
- Update the title or description of an existing PR.
- Draft a PR from the current branch's changes.
- Merge, close, or reopen a PR.

## How to use this skill

1. **Inspect the current branch.** Run `git status` and `git log <base>..HEAD --oneline` to summarize what's about to be proposed.
2. **Write a tight title.** Aim for ≤70 characters. Use the imperative mood — "Add X" not "Adds X". Skip trailing punctuation.
3. **Write the body.** Always include:
   - A 1–3 bullet **Summary** of what changed and why.
   - A **Test plan** checklist describing how to verify the change.
4. **Create or update the PR.** Use `scripts/create_pr.sh "<title>" <body-file>` to open the PR; `scripts/update_pr.sh <pr-number> [--title TITLE] [--body-file PATH]` to edit an existing one. Both scripts wrap `gh pr create` / `gh pr edit` with sane defaults (HEREDOC body, base branch detection, sign-off line).

## Conventions

- Never force-push to `main` or `master`.
- Don't include `Co-Authored-By:` unless explicitly asked.
- When the user asks for a draft, pass `--draft` to `create_pr.sh`.

## Output

The PR URL — that's what the user wants to click on.
