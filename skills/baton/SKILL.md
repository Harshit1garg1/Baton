---
name: baton
license: MIT
description: Coordinate coding agents on a shared local project with task ownership, file claims, Git checkpoints, context handoffs, independent review and recovery. Use when the user requests collaboration between agents such as Codex, Claude Code or Antigravity.
---

# Baton

Use the bundled Python engine as the authoritative local task ledger. Run commands explicitly; this skill does not start a daemon, detect model usage limits, or contact other agents automatically.

## Interpret chat actions

Users normally invoke this skill in chat, such as `/baton handover TASK-01 to pc1-codex`, `$baton continue TASK-01`, or `/baton:baton status`, depending on the host. Interpret the remaining words as an action request and execute the corresponding engine command for the user. These are requests to this one skill, not separate registered commands or terminal commands.

| Requested action | Engine action |
| --- | --- |
| `init` | Bootstrap if needed; inspect the project; `InitFresh` only for a new board. |
| `join` | `JoinExisting`. |
| `status`, `tasks`, `packet TASK` | `Status`, `Tasks`, `WebPacket`. |
| `add TASK "Title" with files ...` | `AddTask`, with dependencies when supplied. |
| `start TASK`, `claim TASK` | `SilentHandshake`; use `--git-branch` for Git work and an isolated worktree when requested. Then implement only if the user asked for implementation. |
| `checkpoint TASK with message ...` | `Checkpoint`; add `--push` only within the user's upload authorization. |
| `progress TASK to 60 percent` | `UpdateProgress`, with a truthful progress figure and notes. |
| `handover TASK to AGENT`, `handoff TASK to AGENT` | `AtomicHandoff`; include the current prompt, changes and remaining work from the conversation. |
| `continue TASK`, `resume TASK` | `CheckOrContinue`; act on its ownership decision before editing. |
| `done TASK` | Run relevant project checks, then `Done` when the implementation is ready. |
| `review TASK` | Inspect the pinned changes and relevant checks as a different agent, then `SubmitReview`. |
| `reopen TASK`, `merge TASK`, `release TAG`, `revert TASK`, `recover TAG` | `ReopenTask`, `MergeTask`, `ReleaseTag`, `RevertTask`, `RecoverTag` respectively. |
| `release files ... from TASK` | `ReleaseFiles` for the specified claims. |
| `resolve recovery` | `ResolveRecovery`; retain unresolved work and its journal if refused. |
| `abandon recovery` | `ResolveRecovery --abandon-recovery` only when abandonment is explicitly requested. Report remaining repairs. |
| `selftest` | `SelfTest`. |
| `reset workspace` | `ResetWorkspace --confirm-reset` only for an explicit authorized reset by the lead. |

Resolve task IDs and paths from the request and current board. Ask for genuinely missing information, such as a new project's key or an ambiguous collaborator identity. Always pass this conversation's collaborator ID explicitly to identity-bearing engine actions; another agent can have changed the shared local session's saved ID. Respect earlier authorization in the conversation. MergeTask and ReleaseTag attempt remote upload, so do not use them against a configured remote when the user's request forbids uploading.

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

The skill requires a coding agent with filesystem and shell access. Use the host's actual skill invocation; `/baton` is not a universally registered terminal or chatbot API.
