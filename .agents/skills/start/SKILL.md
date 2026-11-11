---
name: start
description: Run any change (a song, skill, doc, or code) from its own git branch to a pull request. Use before making a change.
---

- If `git status` shows changes, stop and ask the user. Never discard or stash them.
- Run `git fetch origin`, then `git switch --no-track -c <type>/<short-name> origin/main`.
- `<type>` is a Conventional Commits type. `<short-name>` is 2 to 4 kebab-case words, such as the song id.
- To stack on an unmerged branch, use `origin/<parent>` instead of `origin/main`, then run `git config branch.<type>/<short-name>.gh-merge-base <parent>`.
- A song keeps one branch from its first step to the finished master. To continue it, switch to that branch instead.
- Use `justify` for every claim and suggestion. Use `adversarial` to verify the task and settle technical choices, then `interview` only for decisions only the user can make (goals and licensing).
- When the requested change is done (for a song, each step you were asked for), run `prune`, then `improve` on all work to commit. Fix or disprove each material finding. If anything changed, repeat both. Run `commit` and `pr` only after an `improve` pass that leads to no change and leaves no finding open.
- If reviews keep reversing the same point, stop without committing and report the evidence on both sides.
- A song's PR stays open as its branch grows and merges only after its master passes review.
