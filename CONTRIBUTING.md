# Working rules for this repository (humans and AI agents)

The build specification is `PLR_System_Build_Prompt.md`. Work phase by phase, stop at the
end of each phase, and wait for approval.

## Git: branching, backups, rollback

A bad commit must never be able to destroy the system. These rules are mandatory.

1. **`main` is always releasable.** Never develop on `main` directly. Every change goes on a
   branch and is merged only after `make check` (lint, mypy --strict, full test suite) passes.
2. **Branch for every change.**
   * Build phases: `phase/<n>-<slug>` (e.g. `phase/1-template-spec`).
   * Features: `feat/<slug>`. Fixes: `fix/<slug>`. Docs only: `docs/<slug>`.
   * Anything major (schema/migration changes, dependency upgrades, refactors that touch
     several modules, deleting files) always gets its own branch, even when small.
3. **Back up before merging.** Run `scripts/backup.sh`, which writes a verified git bundle
   of every branch and tag outside the repo. Then tag the current `main`:
   `git tag -a backup/pre-merge-<branch>-<YYYYMMDD> -m "state of main before merging <branch>"`.
4. **Merge with history kept:** `git switch main && git merge --no-ff <branch>`. Tag each
   finished phase: `git tag -a phase-<n> -m "..."`. Push the branch, `main` and tags
   (`git push origin main <branch> --follow-tags`). GitHub is the off-machine backup.
5. **Never rewrite shared history.** No `git push --force` to `main`, no `git reset --hard`,
   `git rebase` or `git commit --amend` on anything already pushed, and no deleting tags
   or backup bundles.
6. **Commit small and often** on the branch, with messages that say *why*. Never commit
   secrets (`.env`), data dumps, or the reference PDFs.

## Rollback recipes

| Situation | Command |
|---|---|
| Undo one bad commit on `main` (history kept) | `git revert <sha>` |
| Undo a whole merged branch | `git revert -m 1 <merge-sha>` |
| Restore one file from a known-good point | `git restore --source=<tag-or-sha> -- <path>` |
| Inspect an old state without changing anything | `git switch --detach <tag>` |
| Start over from a known-good point | `git switch -c recover/<slug> <tag>` and merge back after review |
| Repo lost or corrupted | `git clone ~/plr-backups/<latest>.bundle patsquire-plr` |

Rollbacks are new commits (`revert`) on a branch that gets merged like any other change,
never history rewrites.

## Code rules (short form; the spec is authoritative)
* Code computes, LLMs describe. No fabricated data; fail loudly; no silent fallbacks.
* `mypy --strict`, `ruff`, and tests must pass before merging; the coverage gate is 90%.
* Secrets only in `.env` / environment. Config keys are all explicit in `config/settings.yaml`.
