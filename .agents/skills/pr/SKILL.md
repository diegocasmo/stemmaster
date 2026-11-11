---
name: pr
description: Push the current branch and open a GitHub pull request. Use for "open a PR" or "create a pull request".
---

- Refuse on `main`.
- Run `git push -u origin HEAD`. If the branch already has an open PR, stop there. Otherwise run `gh pr create --title "<type(scope): description>" --body "<a few short, plain lines on what changed and why>"`.
- Never force push.
