---
name: commit
description: Create a conventional git commit from the current changes. Use for "commit this" or "make a commit".
---

- Commit only the changes that belong to the current task. Stage them by path, never with `git add -A` or `git add .`, and unstage unrelated changes that are already staged.
- Message: `type(scope): description` (Conventional Commits), imperative, under 72 characters. Add a body only when the why is not obvious.
- Never commit git-ignored files or skip hooks.
