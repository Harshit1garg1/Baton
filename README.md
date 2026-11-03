# Baton

**Task ownership, Git checkpoints and reliable handoffs for AI coding agents.**

Coordinate Codex, Claude Code, Antigravity and other shell-capable agents working on a project. Keep intermediate work on task branches, review a specific commit, merge accepted results, and return to named milestones when needed.

[![Checks](https://github.com/Harshit1garg1/Baton/actions/workflows/verify.yml/badge.svg)](https://github.com/Harshit1garg1/Baton/actions/workflows/verify.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Why Baton?

When several coding agents work on one project, each conversation can have a different picture of what is finished, who owns a file, and what should happen next. Baton records those decisions in a shared local task board and connects them to real Git commits.

For example, Antigravity can implement part of a feature, checkpoint it, and pass the task to Codex with the latest prompt, changes made, and remaining steps. Codex checks ownership before continuing. Antigravity's later continuation check tells it that the task was handed off.

The result is a repeatable workflow for task claims, handoffs and review, plus Git history for recovering accepted work. Your coding agents still write code and run checks; Baton keeps the coordination record.

## What can I use it for?

| Situation | How Baton helps |
| --- | --- |
| Antigravity and Codex work on one project | Gives tasks distinct owners and checks claimed files before another agent starts. |
| You switch agents or end a session midway | Records a checkpoint and a handoff with the next steps. The transfer is explicit; it does not detect usage limits automatically. |
| Agents implement separate features in parallel | Uses task branches and optional isolated worktrees, with dependencies deciding what is ready. |
| You want an independent code review | Pins a specific implementation commit and requires a different reviewer ID. The reviewer must actually inspect the work. |
| You want frequent saves and readable accepted history | Keeps checkpoints on task branches; a squash merge can put one accepted feature commit on the base branch. |
| A delivered feature needs to be undone | Records merge identity for RevertTask and supports named stable tags for recovery. |

It is most useful when multiple agents share a local project or you regularly hand work from one agent to another. A small project with one agent may only need normal Git commits and reviews. See [support boundaries](#honest-support-boundaries) for separate-PC and cloud-drive limitations.

## Start here

1. [Install Baton](#install-with-npx), including [the Antigravity instructions](#antigravity).
2. [Ask your agents to use it](#how-to-use-baton-with-your-agents).
3. Follow [the chat walkthrough](#example-antigravity-hands-a-task-to-codex). The [advanced CLI reference](docs/cli-walkthrough.md) is available for direct terminal use.

## Install with npx

Run this **inside the project where your agents will work**:

```sh
npx skills add Harshit1garg1/Baton --skill baton
```

Choose your coding agent in the installer. Add `-g` for a global skill installation. For a non-interactive project installation in Codex:

```sh
npx skills add Harshit1garg1/Baton --skill baton --agent codex --copy --yes
```

This uses the community [Skills CLI](https://github.com/vercel-labs/skills); this repository does not require its own npm registry package or npm account. Its current CLI requires Node.js 22.20+. The engine requires Python 3.10+ and Git, and uses only the Python standard library.

Then ask your agent:

> Use baton to set up this project for Codex and another coding agent. Inspect the existing workspace before initializing anything, and ask me for the project key if needed.

The skill locates its bundled bootstrap script, installs the engine in the selected project's `.collab/` folder, and ignores that runtime folder in Git. Installation does not initialize a ledger, alter task ownership, or upload your project. A different existing engine is preserved; upgrades require a separate reviewed replacement.

### Antigravity

Open your project in Antigravity, open its terminal, and run:

```sh
npx skills add Harshit1garg1/Baton --skill baton --agent antigravity --copy --yes
```

This installs the complete skill into `.agents/skills/baton/` in that project. Start a new conversation and ask:

> Use baton to coordinate this project with Codex. Use pc1-antigravity as your collaborator ID, inspect any existing board, and claim a task before editing.

In supported Antigravity App/CLI versions, `/baton` can also invoke the skill. For manual setup, copy this repository's complete `skills/baton/` folder into `<project>/.agents/skills/baton/`. For a manual global installation, use `~/.gemini/config/skills/baton/`. See [Antigravity's skill documentation](https://antigravity.google/docs/skills) for discovery and reload behavior in your version.

If Codex and Antigravity both use project-local `.agents/skills/`, they can discover that same copy. The `.collab/` board belongs to the project; it is separate from the skill's installation directory.

## Native plugin installation

Choose **one** installation route to avoid duplicate copies of the skill.

### Codex CLI

```sh
codex plugin marketplace add Harshit1garg1/Baton
codex plugin add baton@baton
```

Restart the agent session if necessary, then ask it to use `baton`. These commands use this repository's own marketplace, not a listing in the universal public Plugins Directory.

### Claude Code

Inside Claude Code:

```text
/plugin marketplace add Harshit1garg1/Baton
/plugin install baton@baton
```

Start a new session and invoke `/baton:baton`, or ask Claude to use the skill. Shell equivalents are `claude plugin marketplace add Harshit1garg1/Baton` and `claude plugin install baton@baton`.

These are coding-agent integrations that need a filesystem and shell. An ordinary web chatbot without those tools cannot execute the engine. `/collab` phrases in examples are workflow shorthand; each host decides which slash commands it actually registers.

## Manual installation

```sh
git clone https://github.com/Harshit1garg1/Baton.git
cd Baton
```

Copy the complete `skills/baton/` folder into your agent's supported skills directory, or give the agent that folder's `SKILL.md`. Then ask Baton to set up your working project. Direct bootstrap commands are in the [advanced CLI reference](docs/cli-walkthrough.md).

## How to use Baton with your agents

Send requests in your agent's chat. The agent uses Baton and runs the Python engine for you.

| Installation / agent | How to invoke the skill |
| --- | --- |
| Antigravity project skill | `/baton` followed by your request. |
| Codex installed skill | `$baton` followed by your request, or "Use baton to ...". |
| Claude Code native plugin | `/baton:baton` followed by your request. |
| Other shell-capable agents | "Use baton to ...", using that host's skill invocation if available. |

The examples below use Antigravity's `/baton` prefix. Replace it with your agent's invocation from the table. Baton is one skill: the words after its name are requests the agent interprets, rather than separate built-in slash commands. Some hosts only autocomplete the skill name.

### Example: Antigravity hands a task to Codex

Open the same local project in both agents. Give them distinct IDs, such as `pc1-antigravity` and `pc1-codex`. One lead initializes a new board; the other joins it with the same project key. Your agent will ask for missing information rather than inventing a key or resetting an existing board.

**1. Set up the collaboration.** In Antigravity:

```text
/baton init as pc1-antigravity for this project, with pc1-codex on the same PC.
```

In Codex:

```text
$baton join this project's existing board as pc1-codex.
```

**2. Create, claim and work on a task.** In Antigravity:

```text
/baton add TASK-01 "Add greeting helper" with files src/greet.py.
/baton start TASK-01 as pc1-antigravity and implement the greeting helper.
/baton checkpoint TASK-01 as pc1-antigravity with message "Greeting helper implemented".
```

Baton checks ownership before work begins. The agent edits files and runs the relevant checks; checkpoints save meaningful progress on the task branch. For simultaneous branch-backed tasks, request an isolated worktree.

**3. Hand the task over.** In Antigravity:

```text
/baton handover TASK-01 to pc1-codex. Record what you changed and that empty-name handling and project checks remain.
```

In Codex:

```text
$baton continue TASK-01 as pc1-codex and finish the remaining work.
$baton checkpoint TASK-01 as pc1-codex with message "Finish greeting helper".
$baton done TASK-01 as pc1-codex.
```

The receiving agent checks the recorded ownership and context before continuing. The old agent's continuation check tells it to skip handed-off work. `done` submits the finished implementation for review.

**4. Review it in another agent.** In Antigravity:

```text
/baton review TASK-01 as pc1-antigravity. Inspect the pinned commit and relevant check results, then record your verdict.
```

The reviewer can approve or request changes. If revisions are needed, ask the owner to reopen the task, revise it, checkpoint, and submit it again.

**5. Deliver when you accept the result.** In Codex:

```text
I accept TASK-01. $baton merge TASK-01 as pc1-codex using squash and push it.
$baton status
```

Baton reports the local commit and verified delivery status. Only a verified remote push is `DELIVERY: COMPLETE`; missing remotes and failed pushes remain `LOCAL_ONLY` or `INCOMPLETE`.

Baton does not open or message another agent automatically. Open the receiving conversation and ask it to continue. Existing authorization for commits, merges, pushes and releases still comes from your conversation with the agent.

### Chat action reference

Use your host's invocation prefix. Supply your collaborator ID and any other missing details when asked.

| Request after `/baton` | What the agent runs |
| --- | --- |
| `init` | Set up the local engine and initialize a new board with `InitFresh`. |
| `join as pc1-codex` | Join an existing board with `JoinExisting`. |
| `status` / `tasks` | Inspect the board with `Status` / `Tasks`. |
| `add TASK-01 "Title" with files src/file.py` | Add a task with `AddTask`; include dependencies when needed. |
| `start TASK-01` | Claim the task with `SilentHandshake`, using a task branch for Git work. |
| `checkpoint TASK-01 with message "Progress"` | Save owned files with `Checkpoint`; add "and push" when authorized remote backup is wanted. |
| `progress TASK-01 to 60 percent` | Record progress and notes with `UpdateProgress`. |
| `handover TASK-01 to pc1-codex` | Transfer claims and context with `AtomicHandoff`; `handoff` is an equivalent request. |
| `continue TASK-01` | Run `CheckOrContinue` before resuming; respect its ownership decision. |
| `done TASK-01` | Submit clean implementation work for review with `Done`. |
| `review TASK-01` | Inspect the pinned work, then record `SubmitReview` as a different reviewer. |
| `reopen TASK-01` | Reopen pending or reverted work with `ReopenTask`. |
| `merge TASK-01 using squash and push` | Deliver an approved task with `MergeTask`, within the user's authorization. |
| `release v1.0.0` | Name an accepted base-branch milestone with `ReleaseTag`; this action attempts upload. |
| `revert TASK-01` | Create an undo commit with `RevertTask`; add "and push" when authorized. |
| `recover v1.0.0 on branch recover/v1.0.0` | Branch from a milestone with `RecoverTag`. |
| `release files src/file.py from TASK-01` | Intentionally release a file claim with `ReleaseFiles`. |
| `resolve recovery` | Run `ResolveRecovery` after repairing the affected checkout. |
| `abandon recovery` | Explicitly archive the journal and report remaining repairs; this does not repair the checkout. |
| `selftest` | Run the engine's disposable 42-stage `SelfTest`. |
| `packet TASK-01` | Render the task's handoff context with `WebPacket`. |
| `reset workspace` | Archive and reset the board with `ResetWorkspace`; requires the lead and an explicit reset request. |

For direct engine arguments, see [the skill instructions](skills/baton/SKILL.md) and the optional [advanced CLI walkthrough](docs/cli-walkthrough.md).

## How the workflow works

1. **Initialize or join.** Each agent uses a distinct collaborator ID and the same local project key.
2. **Claim a task before editing.** Dependencies and file claims determine what is available. Use isolated Git worktrees for simultaneous branch-backed tasks.
3. **Checkpoint meaningful progress.** Commit currently owned files on the task branch. Push when you want remote backup.
4. **Hand off when needed.** Transfer the task's claims and commit context to the next agent. The old agent's continuation check tells it to skip handed-off work.
5. **Review the pinned commit.** Done moves work to review_pending. A different agent inspects and approves it; approval marks it done and releases its file claims.
6. **Deliver accepted work.** MergeTask merges the approved branch into the base branch and attempts a verified push. ReleaseTag gives an accepted milestone a stable name.
7. **Recover deliberately.** RevertTask creates an undo commit. RecoverTag creates a branch from a stable tag. Unresolved interrupted operations preserve their journal and local work until repaired or explicitly abandoned.

User authorization for merging and publishing remains part of the agent conversation. Peer review alone does not authorize publication.

After project setup, ask `/baton status` (using your host's invocation prefix). The [advanced CLI reference](docs/cli-walkthrough.md) covers direct terminal use.

## Honest support boundaries

| Capability | Current behavior |
| --- | --- |
| Several agents on one PC | One local ledger serializes engine operations with an OS-held lock. |
| Git hosting | Uses standard Git remotes. Real GitHub delivery has been verified; other Git hosts need their own integration checks before claiming tested support. |
| Separate PCs | Exchange work through Git branches/remotes. Each PC's local board is separate; there is no global cross-PC ownership authority. |
| Google Drive, OneDrive, TeraBox | `--backup-path` can write a one-way board snapshot into a local synced folder. Cloud upload is handled by the sync client. No native drive API or verified remote-upload status is provided for these snapshots. |
| Agent identities | Cooperative labels, not authenticated accounts. Direct editor/Git operations can bypass engine file claims. |
| Automation | Explicit commands. No background watcher, automatic usage-limit detection, hosted agent execution or hidden auto-push loop. |

Keep the live ledger local. Synced copies of `state-board.json` are backups, not a distributed transaction system. Preserve `DELIVERY: LOCAL_ONLY` or `INCOMPLETE` when reported; only verified Git remote delivery is `COMPLETE`.

## Interrupted operations

If a command reports `RECOVERY_STATUS: UNRESOLVED`, preserve the named worktree and journal. Save needed edits and clean up the blocking state before retrying. Normal resolution refuses while that state is unresolved:

```text
/baton resolve recovery
```

For deliberate abandonment after inspecting the situation:

```text
/baton abandon recovery
```

Abandonment archives the journal under `.collab/snapshots/`, prints `RECOVERY_ABANDONED`, and lists remaining repairs. It leaves those repairs to you. Keep local keys and session files private; bootstrap ignores `.collab/` for this reason. Review before sharing board snapshots, which may contain prompts, paths and handoff notes.

## Verify or contribute

Ask `/baton selftest`, or run the [contributor commands](docs/cli-walkthrough.md#contributor-checks) from a cloned repository.

CI runs installer tests and the engine's 42-stage suite on Windows and Linux. macOS installation uses the same Python bootstrap; no native macOS runner has been verified for this release. Process-termination recovery has been tested; physical power loss and filesystem corruption have not.

Report reproducible bugs through [GitHub Issues](https://github.com/Harshit1garg1/Baton/issues), with your OS, Python/Git versions, command, and sanitized output. Keep credentials, project keys, private code and runtime session files out of issues and pull requests.

Licensed under [MIT License](LICENSE). Copyright 2026 Harshit1garg1.
