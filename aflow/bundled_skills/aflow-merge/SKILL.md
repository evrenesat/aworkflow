---
name: aflow-merge
description: "Merge a feature branch into a local target branch after an aflow workflow completes. Operates local-only, preserves commit history, resolves conflicts without dropping either side, and emits AFLOW_STOP for irrecoverable ambiguous states."
---

# AFlow Merge

Use this skill only when the aflow engine invokes it as the post-workflow merge handoff. The engine supplies the exact branch names, repo paths, and plan context in the prompt. Do not invoke this skill manually unless replicating the engine's exact handoff call.

## Rules

- Operate only on local refs and local worktrees. Do not run `git fetch`, `git pull`, or any command that contacts a remote.
- The primary checkout is your working root. Treat the feature worktree as read-only except for a required rebase when it is the worktree that owns the feature branch.
- Preserve commits. Do not squash, collapse, or rebase onto anything other than the local target branch.
- Leave worktree removal to the engine. Do not run `git worktree remove`.
- Do not delete the feature branch.
- If you cannot determine the correct resolution for a conflict without dropping or misinterpreting either side's intent, abort the rebase with `git rebase --abort` and emit `AFLOW_STOP: conflict resolution is ambiguous`.

## Merge Strategy

1. Check the current branch in the primary checkout. It must equal the target branch (`{MAIN_BRANCH}` in the engine's prompt). If it does not, emit `AFLOW_STOP: primary checkout is not on the expected target branch`.
2. Check whether the feature branch is already based on the current target branch tip:
   - Run `git merge-base --is-ancestor <target_branch> <feature_branch>`.
   - If exit code is 0, the feature branch is already based on target — fast-forward merge directly with `git merge --ff-only <feature_branch>`.
   - If exit code is non-zero, rebase is needed first.
3. If rebase is needed:
   - Use `git worktree list --porcelain` to determine whether the feature branch is checked out in a linked worktree.
   - If a linked worktree owns the feature branch, require that worktree to be clean and on the exact feature branch, then run `git -C <feature_worktree> rebase <target_branch>`. Do not run `git rebase <target_branch> <feature_branch>` from the primary checkout while another worktree owns the branch; Git refuses to update a branch checked out elsewhere.
   - If no linked worktree owns the feature branch, run `git rebase <target_branch> <feature_branch>` from the primary checkout.
   - For each conflict, examine both sides carefully and resolve by preserving the intent of both. Run `git rebase --abort` or `git rebase --continue` in the same checkout where the rebase started. If the intent is genuinely ambiguous, abort and emit `AFLOW_STOP: conflict resolution is ambiguous for <file>`.
   - After a successful rebase, fast-forward merge with `git merge --ff-only <feature_branch>`.
4. After the merge, verify:
   - `git ls-files --unmerged` is empty (no unresolved index entries).
   - `git status --porcelain` is empty (working tree is clean).
   - `git symbolic-ref --short HEAD` equals the target branch.
   - `git merge-base --is-ancestor <feature_branch> <target_branch>` exits 0.
5. If any check fails, emit `AFLOW_STOP: post-merge verification failed: <check name>`.
6. If all checks pass, report a brief summary: which branches were merged, whether a rebase was needed, and the final HEAD SHA.

## Stop And Escalate If

- The primary checkout is on a detached HEAD.
- The target branch or feature branch does not exist locally.
- The `git rebase` command fails for a reason other than a resolvable conflict (e.g., corrupted state, missing objects).
- Any verification check fails after a seemingly successful merge.
- You are uncertain whether a conflict resolution is correct.

Emit `AFLOW_STOP: <reason>` on its own line. The engine detects this and preserves the feature branch and worktree for manual inspection.
