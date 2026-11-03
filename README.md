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
3. Follow [the worked example](#worked-example-antigravity-hands-a-task-to-codex) if you want the exact CLI commands.

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
python3 skills/baton/scripts/bootstrap.py --project /path/to/your/project
```

Install `skills/baton/` in your agent's supported skills directory as well, or give the agent that folder's `SKILL.md`. On Windows, use `python` or `py -3` if `python3` is an unavailable Store alias.

## How to use Baton with your agents

Open the same local project in the participating agents. Give each agent a distinct collaborator ID, such as `pc1-antigravity` and `pc1-codex`, and tell them which task each should handle. Choose one lead to initialize a new board; other agents join the existing one using the same project key.

You can use ordinary prompts:

| What you want | Example prompt |
| --- | --- |
| Set up collaboration | "Use baton to inspect this project. If it has no board, initialize one with pc1-antigravity as lead; use pc1-codex for Codex. Tell me how the other agent should join." |
| Start implementation | "Use baton to create and claim TASK-01 for the greeting helper in src/greet.py. Save meaningful progress on its task branch." |
| Transfer unfinished work | "Use baton to hand TASK-01 to pc1-codex. Record the latest request, what you changed, and what remains." |
| Resume as the receiver | "Use baton as pc1-codex. Run CheckOrContinue for TASK-01, read the handoff, and continue the remaining work." |
| Request review | "Use baton to review the pinned TASK-01 commit as pc1-antigravity. Inspect the diff and relevant checks, then record your verdict." |
| Deliver an accepted result | "I accept TASK-01. Use baton to squash-merge it into the base branch and push it. Report the local commit and verified delivery status." |

The agents execute the engine commands. Baton does not start another agent or send it a message: open the receiving conversation and ask that agent to continue. Only the requested lead initializes a new board; joining agents should not reset it.

## Worked example: Antigravity hands a task to Codex

Run these commands from **your working project's root**, after installing Baton. The example uses a Git repository with a baseline commit on `main` and a configured Git identity. If your base branch has another name, pass `--base-branch <name>` to the branch-backed workflow commands. The file `src/greet.py` is an example task path; use a task and file appropriate to your project.

Use an available Python 3 interpreter throughout. On Windows, replace `python3` with `python` or `py -3` when needed. Replace the sample project key before setting up a real workspace.

### 1. Set up the project and join

For the project-local Skills CLI installation used by Codex and Antigravity:

```sh
python3 .agents/skills/baton/scripts/bootstrap.py --project .
```

If bootstrap added the `.collab/` ignore rule, review and commit that `.gitignore` setup change before starting task branches. Save unrelated tracked edits separately as well: the final integration requires a clean checkout and task checkpoints only commit their owned files.

Initialize a **new** board, then have Codex join it:

```sh
python3 .collab/collab_engine.py --action InitFresh --local-path . --project-key "your-private-project-key" --collaborator-id pc1-antigravity --device-cluster-id pc1-local-cluster --local-agents antigravity,codex
python3 .collab/collab_engine.py --action JoinExisting --local-path . --project-key "your-private-project-key" --collaborator-id pc1-codex --device-cluster-id pc1-local-cluster --same-device true
```

For a native plugin or global skill installation, ask the agent to run the bootstrap from its installed skill directory instead. If the board already exists, join it; InitFresh intentionally refuses to overwrite it. Keep the real key private and out of committed files.

### 2. Antigravity claims and checkpoints the task

```sh
python3 .collab/collab_engine.py --action AddTask --local-path . --task-id TASK-01 --task-title "Add greeting helper" --files src/greet.py
python3 .collab/collab_engine.py --action SilentHandshake --local-path . --task-id TASK-01 --collaborator-id pc1-antigravity --git-branch
```

Antigravity now creates or edits `src/greet.py` and runs the relevant project checks. After a meaningful step, it saves a checkpoint:

```sh
python3 .collab/collab_engine.py --action Checkpoint --local-path . --task-id TASK-01 --collaborator-id pc1-antigravity --commit-message "checkpoint: greeting helper implemented"
```

Checkpoint commits the currently owned files on the task branch. Add `--push` when remote backup is wanted and a remote is configured. If another branch-backed task is already active in the shared checkout, start this task with `--use-worktree` and edit inside its recorded worktree directory.

### 3. Hand the task to Codex

```sh
python3 .collab/collab_engine.py --action AtomicHandoff --local-path . --task-id TASK-01 --collaborator-id pc1-antigravity --target-agent-id pc1-codex --reason "Continue in Codex" --last-prompt "Add a greeting helper" --changes-summary "Added the greeting helper" --remaining-todo "Handle empty names and run the project checks"
python3 .collab/collab_engine.py --action CheckOrContinue --local-path . --task-id TASK-01 --collaborator-id pc1-codex
```

Expected: ownership transfers to `pc1-codex` with file claims preserved; Codex receives `ACTION: CONTINUE_APPROVED` and the recorded context. If Antigravity later checks the same task, it receives `SKIP_ALREADY_HANDED_OFF`.

### 4. Finish, review, and deliver

Codex completes the remaining edits and runs the relevant project checks, then:

```sh
python3 .collab/collab_engine.py --action Checkpoint --local-path . --task-id TASK-01 --collaborator-id pc1-codex --commit-message "feat: finish greeting helper"
python3 .collab/collab_engine.py --action Done --local-path . --task-id TASK-01 --collaborator-id pc1-codex
```

Antigravity inspects the pinned commit and check results before approving:

```sh
python3 .collab/collab_engine.py --action SubmitReview --local-path . --task-id TASK-01 --collaborator-id pc1-antigravity --review-verdict APPROVED --review-notes "Reviewed the helper and check results"
```

Use `--review-verdict CHANGES_REQUESTED` when revisions are needed. The owner can reopen, checkpoint, and submit the updated work for review again.

Once you accept delivery and authorize its upload:

```sh
python3 .collab/collab_engine.py --action MergeTask --local-path . --task-id TASK-01 --collaborator-id pc1-codex --squash
python3 .collab/collab_engine.py --action Status --local-path . --render-format CLI
```

MergeTask attempts a push to `origin`. With no remote, it reports `DELIVERY: LOCAL_ONLY`; a failed push reports `INCOMPLETE`. Only verified remote delivery reports `COMPLETE`. Add `--use-worktree` when integration must preserve another task's shared checkout.

## How the workflow works

1. **Initialize or join.** Each agent uses a distinct collaborator ID and the same local project key.
2. **Claim a task before editing.** Dependencies and file claims determine what is available. Use isolated Git worktrees for simultaneous branch-backed tasks.
3. **Checkpoint meaningful progress.** Commit currently owned files on the task branch. Push when you want remote backup.
4. **Hand off when needed.** Transfer the task's claims and commit context to the next agent. The old agent's continuation check tells it to skip handed-off work.
5. **Review the pinned commit.** Done moves work to review_pending. A different agent inspects and approves it; approval marks it done and releases its file claims.
6. **Deliver accepted work.** MergeTask merges the approved branch into the base branch and attempts a verified push. ReleaseTag gives an accepted milestone a stable name.
7. **Recover deliberately.** RevertTask creates an undo commit. RecoverTag creates a branch from a stable tag. Unresolved interrupted operations preserve their journal and local work until repaired or explicitly abandoned.

User authorization for merging and publishing remains part of the agent conversation. Peer review alone does not authorize publication.

After project setup, inspect the board with:

```sh
python3 .collab/collab_engine.py --action Status --local-path .
```

For the complete action table, see [the skill instructions](skills/baton/SKILL.md). For every available engine flag, use `python3 .collab/collab_engine.py --help`.

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

```sh
python3 .collab/collab_engine.py --action ResolveRecovery --local-path .
```

For deliberate abandonment after inspecting the situation:

```sh
python3 .collab/collab_engine.py --action ResolveRecovery --abandon-recovery --local-path .
```

Abandonment archives the journal under `.collab/snapshots/`, prints `RECOVERY_ABANDONED`, and lists remaining repairs. It leaves those repairs to you. Keep local keys and session files private; bootstrap ignores `.collab/` for this reason. Review before sharing board snapshots, which may contain prompts, paths and handoff notes.

## Verify or contribute

```sh
python3 -m unittest discover -s tests -v
python3 skills/baton/scripts/collab_engine.py --action SelfTest
```

CI runs installer tests and the engine's 42-stage suite on Windows and Linux. macOS installation uses the same Python bootstrap; no native macOS runner has been verified for this release. Process-termination recovery has been tested; physical power loss and filesystem corruption have not.

Report reproducible bugs through [GitHub Issues](https://github.com/Harshit1garg1/Baton/issues), with your OS, Python/Git versions, command, and sanitized output. Keep credentials, project keys, private code and runtime session files out of issues and pull requests.

Licensed under [MIT License](LICENSE). Copyright 2026 Harshit1garg1.
