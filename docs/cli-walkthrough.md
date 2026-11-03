# Advanced CLI walkthrough

For everyday use, send [Baton chat requests](../README.md#how-to-use-baton-with-your-agents) and let your coding agent run the engine. This page is an optional reference for direct terminal use.

Run these commands from **your working project's root**, after installing Baton. The example uses a Git repository with a baseline commit on `main` and a configured Git identity. If your base branch has another name, pass `--base-branch <name>` to the branch-backed workflow commands. The file `src/greet.py` is an example task path; use a task and file appropriate to your project.

Use an available Python 3 interpreter throughout. On Windows, replace `python3` with `python` or `py -3` when needed. Replace the sample project key before setting up a real workspace.

## 1. Set up the project and join

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

## 2. Antigravity claims and checkpoints the task

```sh
python3 .collab/collab_engine.py --action AddTask --local-path . --task-id TASK-01 --task-title "Add greeting helper" --files src/greet.py
python3 .collab/collab_engine.py --action SilentHandshake --local-path . --task-id TASK-01 --collaborator-id pc1-antigravity --git-branch
```

Antigravity now creates or edits `src/greet.py` and runs the relevant project checks. After a meaningful step, it saves a checkpoint:

```sh
python3 .collab/collab_engine.py --action Checkpoint --local-path . --task-id TASK-01 --collaborator-id pc1-antigravity --commit-message "checkpoint: greeting helper implemented"
```

Checkpoint commits the currently owned files on the task branch. Add `--push` when remote backup is wanted and a remote is configured. If another branch-backed task is already active in the shared checkout, start this task with `--use-worktree` and edit inside its recorded worktree directory.

## 3. Hand the task to Codex

```sh
python3 .collab/collab_engine.py --action AtomicHandoff --local-path . --task-id TASK-01 --collaborator-id pc1-antigravity --target-agent-id pc1-codex --reason "Continue in Codex" --last-prompt "Add a greeting helper" --changes-summary "Added the greeting helper" --remaining-todo "Handle empty names and run the project checks"
python3 .collab/collab_engine.py --action CheckOrContinue --local-path . --task-id TASK-01 --collaborator-id pc1-codex
```

Expected: ownership transfers to `pc1-codex` with file claims preserved; Codex receives `ACTION: CONTINUE_APPROVED` and the recorded context. If Antigravity later checks the same task, it receives `SKIP_ALREADY_HANDED_OFF`.

## 4. Finish, review, and deliver

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

## Contributor checks

Run these from the cloned Baton repository:

```sh
python3 -m unittest discover -s tests -v
python3 skills/baton/scripts/collab_engine.py --action SelfTest
```

Use the installed engine's `--help` for its complete terminal interface. On Windows, select a working Python 3 interpreter such as `python` or `py -3`.
