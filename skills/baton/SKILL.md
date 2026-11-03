---
name: baton
description: Coordinate coding agents on a shared local project with task ownership, file claims, Git checkpoints, context handoffs, independent review and recovery. Use when the user requests collaboration between agents such as Codex, Claude Code or Antigravity.
---

# Baton

Use the bundled Python engine as the authoritative local task ledger. Run commands explicitly; this skill does not start a daemon, detect model usage limits, or contact other agents automatically.

## Set up a project

Requirements: Python 3.10+ and Git. Select an available Python 3 interpreter (`python3`, `python`, or `py -3` on Windows); do not assume the Windows `python3` alias works.

Before the first engine command in a project, locate this skill's own `scripts/bootstrap.py` and run it with the chosen interpreter and `--project <absolute-project-path>`. It copies the bundled engine into `.collab/collab_engine.py` and adds `/.collab/` to the project's `.gitignore`. It does not initialize the ledger or replace a different existing engine. Resolve an upgrade separately if it refuses.

The commands below run from the project root. Replace `python3` with the selected interpreter when needed. For Git-backed tasks, first check that the project is a Git repository with a baseline commit and a configured Git identity. Use its actual base branch with `--base-branch <branch>` when it is not `main`.

## Establish ownership before editing

1. Run `Status`. Join the initialized workspace with a distinct collaborator ID; obtain the project key through the user's authorized local context. For a new workspace, use `InitFresh` with the chosen lead and a project key. Keep the key and local session out of Git.
2. Add a task with its intended file paths and dependencies if it does not exist.
3. Run `SilentHandshake` before editing. A BUSY_REDIRECT is a refusal to claim that task; choose an eligible alternative. Use `--use-worktree` for simultaneous branch-backed tasks sharing the same project.
4. Checkpoint meaningful progress. When transferring ownership, use AtomicHandoff; the receiving agent runs CheckOrContinue. An agent that has handed off must respect SKIP_ALREADY_HANDED_OFF.
5. Run Done when implementation is ready, then have a different agent review the pinned commit. A genuine review means inspecting the changes and relevant checks before submitting the verdict.

Respect the user's scope and existing authorization for commits, uploads, merges and releases. Peer approval does not by itself authorize external publication. Do not send messages to other agents or people without authorization; handoff records can be consumed by the next agent when it runs.

## Engine commands

Prefix each row with `python3 .collab/collab_engine.py --local-path .`. Supply a stable `--collaborator-id <agent>` where applicable.

| Action | Arguments and behavior |
| --- | --- |
| `InitFresh` | `--project-key <key> --collaborator-id <lead>`; refuses an existing ledger. |
| `JoinExisting` | `--project-key <key> --collaborator-id <agent> --device-cluster-id <cluster> --same-device true` for same-PC collaboration. |
| `Status` / `Tasks` | Inspect ownership, progress and delivery. Status accepts `--render-format CLI` or `Markdown`. |
| `AddTask` | `--task-id T1 --task-title "Implement feature" --files src/feature.py`; optional `--depends-on T0`. |
| `SilentHandshake` | `--task-id T1 --collaborator-id <owner> --git-branch`; optional `--use-worktree`. |
| `Checkpoint` | `--task-id T1 --collaborator-id <owner> --commit-message "checkpoint: meaningful progress"`; optional `--push`. Commits currently owned files. |
| `ReleaseFiles` | `--task-id T1 --collaborator-id <owner> --files <paths>` for intentional claim release. |
| `AtomicHandoff` | `--task-id T1 --collaborator-id <owner> --target-agent-id <receiver> --reason "Continue implementation"`; add context with `--last-prompt`, `--changes-summary`, `--remaining-todo`; optional `--push`. |
| `CheckOrContinue` | `--task-id T1 --collaborator-id <agent>`; load the handoff or respect the resume-skip guard. |
| `Done` | `--task-id T1 --collaborator-id <owner>`; pins clean work for review; optional `--push`. |
| `SubmitReview` | `--task-id T1 --collaborator-id <reviewer> --review-verdict APPROVED\|CHANGES_REQUESTED --review-notes "Findings"`; self-review is blocked. |
| `ReopenTask` | `--task-id T1 --collaborator-id <owner-or-lead>` to revise pending or reverted work. |
| `MergeTask` | `--task-id T1 --collaborator-id <agent> --squash`; attempts a push. Use `--use-worktree` to preserve another task's shared checkout; `--reapply` explicitly reapplies reverted work. |
| `ReleaseTag` | `--tag v1.0.0 --tag-message "Accepted milestone" --collaborator-id <agent>`; tags the verified base branch and attempts upload. |
| `RevertTask` | `--task-id T1 --collaborator-id <agent>`; creates an undo commit; optional `--push` and `--use-worktree`. |
| `RecoverTag` | `--tag v1.0.0 --recovery-branch recover/v1.0.0` to branch from a milestone. |
| `ResolveRecovery` | Resolve only after the affected checkout is repaired. `--abandon-recovery` explicitly archives the journal and lists manual repairs; it does not perform those repairs. |
| `SelfTest` | Run the engine's 42-stage suite in disposable fixtures. |

For a branch-backed task using a worktree, edit and run project checks inside the task's recorded `worktreePath`.

## Limits and recovery

- Claims are cooperative engine-level checks; editors and arbitrary Git commands can bypass them. Collaborator IDs are labels, not authenticated identities.
- One PC's board is the local authority. Separate PCs use branches and Git synchronization; synced board replicas do not provide a distributed lock. Do not treat a drive folder as a shared transactional ledger.
- `--backup-path <local-synced-folder>` writes a one-way board snapshot. The sync client handles cloud upload; the engine has no native Drive, OneDrive or TeraBox API adapter.
- Review approval marks the task done and releases its claims; verified merge/delivery is a separate operation. Report `DELIVERY: COMPLETE` only when the engine verifies the remote push; retain LOCAL_ONLY or INCOMPLETE when returned.
- If recovery reports UNRESOLVED, preserve the journal and local files. Inspect the named checkout, save relevant edits, and retry after cleanup. Do not use abandonment as a substitute for preserving files.
- ResetWorkspace archives the board and requires lead identity plus `--confirm-reset`; use it only when resetting the workspace is within the user's request.

`/collab` phrases are shorthand for these engine actions where the host understands them, not a universally registered slash-command API. The skill requires a coding agent with filesystem and shell access.
