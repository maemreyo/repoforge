# Starter prompts

## Architecture/read-only investigation

```text
Use RepoForge on work-frontier. Inspect status, repository context, recent commits, docs/anatomy and
the relevant issue. Do not create a workspace or modify anything. Identify architecture risks,
unknowns and the recommended verification profile.
```

## Implement an issue, stop before commit

```text
Use RepoForge on work-frontier for issue #460. Create an isolated workspace from main. Read the issue
and relevant architecture files, implement only the scoped work, review the complete diff, run the
default full verification profile, and stop before commit. Do not touch denied paths.
```

## Publish approved changes

```text
The current RepoForge workspace and diff are approved. Confirm that verification still matches the
current fingerprint, commit with a meaningful message, push the ai/* branch, and create a draft PR.
Do not mark ready or merge. Return the URL and CI check buckets.
```

## Resume existing workspace

```text
Use RepoForge. List active workspaces for work-frontier, inspect their status and identify the one
related to issue #460. Do not edit until you have shown its branch, diff metrics and last verification.
```

## Safely undo a mistake

```text
Use RepoForge to refresh workspace status, then restore only src/example.ts and remove the untracked
scratch.txt. Do not restore any other path. Show the diff afterward.
```
