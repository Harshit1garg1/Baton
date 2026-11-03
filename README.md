# Baton

**Task ownership, Git checkpoints and reliable handoffs for AI coding agents.**

Coordinate Codex, Claude Code, Antigravity and other shell-capable agents working on a project. Keep intermediate work on task branches, review a specific commit, merge accepted results, and return to named milestones when needed.

[![Checks](https://github.com/Harshit1garg1/baton/actions/workflows/verify.yml/badge.svg)](https://github.com/Harshit1garg1/baton/actions/workflows/verify.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

## Install with npx

Run this **inside the project where your agents will work**:

```sh
npx skills add Harshit1garg1/baton --skill baton
```

Choose your coding agent in the installer. Add `-g` for a global skill installation. For a non-interactive project installation in Codex:

```sh
npx skills add Harshit1garg1/baton --skill baton --agent codex --copy --yes
```

This uses the community [Skills CLI](https://github.com/vercel-labs/skills); this repository does not require its own npm registry package or npm account. Its current CLI requires Node.js 22.20+. The engine requires Python 3.10+ and Git, and uses only the Python standard library.

Then ask your agent:

> Use baton to set up this project for Codex and another coding agent. Inspect the existing workspace before initializing anything, and ask me for the project key if needed.

The skill locates its bundled bootstrap script, installs the engine in the selected project's `.collab/` folder, and ignores that runtime folder in Git. Installation does not initialize a ledger, alter task ownership, or upload your project. A different existing engine is preserved; upgrades require a separate reviewed replacement.

## Native plugin installation

Choose **one** installation route to avoid duplicate copies of the skill.

### Codex CLI

```sh
codex plugin marketplace add Harshit1garg1/baton
codex plugin add baton@baton
```

Restart the agent session if necessary, then ask it to use `baton`. These commands use this repository's own marketplace, not a listing in the universal public Plugins Directory.

### Claude Code

Inside Claude Code:

```text
/plugin marketplace add Harshit1garg1/baton
/plugin install baton@baton
```

Start a new session and invoke `/baton:baton`, or ask Claude to use the skill. Shell equivalents are `claude plugin marketplace add Harshit1garg1/baton` and `claude plugin install baton@baton`.

These are coding-agent integrations that need a filesystem and shell. An ordinary web chatbot without those tools cannot execute the engine. `/collab` phrases in examples are workflow shorthand; each host decides which slash commands it actually registers.

## Manual installation

```sh
git clone https://github.com/Harshit1garg1/baton.git
cd baton
python3 skills/baton/scripts/bootstrap.py --project /path/to/your/project
```

Install `skills/baton/` in your agent's supported skills directory as well, or give the agent that folder's `SKILL.md`. On Windows, use `python` or `py -3` if `python3` is an unavailable Store alias.

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

Report reproducible bugs through [GitHub Issues](https://github.com/Harshit1garg1/baton/issues), with your OS, Python/Git versions, command, and sanitized output. Keep credentials, project keys, private code and runtime session files out of issues and pull requests.

Licensed under [Apache License 2.0](LICENSE). Copyright 2026 Harshit1garg1.
