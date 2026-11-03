#!/usr/bin/env python3
"""
collab_engine.py - Transactional Local Multi-AI Task, File-Claim, Handoff, Review & Git Workflow Engine.

Key Guarantees:
1. OS kernel-held process lock (fcntl.flock on POSIX, msvcrt.locking on Windows) on .collab/board.tx.lock.
2. Single local ledger authority (optional --backup-path writes one-way backup snapshots).
3. Safe initialization guard (InitFresh refuses to overwrite an existing workspace; ResetWorkspace archives first).
4. Cumulative file claims on SilentHandshake; ReleaseFiles removes from both ownedFiles and targetFiles;
   AtomicHandoff transfers only currently held ownedFiles (never resurrects released files).
5. Strict branch isolation: Checkpoint, Done, AtomicHandoff, and SubmitReview verify that the active repository
   (or isolated worktree) is on the task's exact gitBranch; starting a second branch task in a shared working
   tree without --use-worktree is blocked if another branch task is active in that checkout.
6. Commit-accurate backup tracking: remoteBackupStatus is tracked against lastPushedCommit; any new commit
   without --push resets remoteBackupStatus to UNPUSHED.
7. Intentional file deletion support: tracked files deleted as part of a task are staged and committed cleanly,
   while never-created missing files are rejected with MISSING_TASK_FILES_ERROR.
8. Reopen/Resubmit workflow: ReopenTask (or running Checkpoint/Done as owner during review_pending) updates
   reviewTargetCommit cleanly; CHANGES_REQUESTED works even on stale branches.
9. Verified Merge, ReleaseTag & Revert: MergeTask fixes branch checkout, merges both --squash and --no-ff, and
   verifies resulting file contents on main; RevertTask handles both merge commits (-m 1) and squash commits;
   ReleaseTag tags only verified commits on main (never unmerged task branches).
"""
import argparse
import concurrent.futures
import contextlib
import errno
import hashlib
import json
import os
import posixpath
import random
import re
import shutil
import string
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest().lower()


def ascii_bar(pct):
    filled = int(round((max(0, min(100, pct)) / 100.0) * 10))
    return "[" + ("#" * filled) + ("-" * (10 - filled)) + f"] {pct}%"


def slugify_branch_suffix(text):
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:32] or "task"


def normalize_rel_path(raw_path, local_root):
    cleaned = raw_path.strip().replace("\\", "/")
    if not cleaned:
        return ""
    abs_root = os.path.realpath(local_root)
    abs_target = os.path.realpath(os.path.join(abs_root, cleaned))
    try:
        rel = os.path.relpath(abs_target, abs_root).replace("\\", "/")
    except ValueError:
        rel = posixpath.normpath(cleaned)
    while rel.startswith("./"):
        rel = rel[2:]
    rel = posixpath.normpath(rel)
    if os.name == "nt":
        rel = rel.lower()
    return rel


def normalize_file_list(raw_csv_or_list, local_root):
    if isinstance(raw_csv_or_list, str):
        items = [x.strip() for x in raw_csv_or_list.split(",") if x.strip()]
    else:
        items = [str(x).strip() for x in (raw_csv_or_list or []) if str(x).strip()]
    seen = []
    for item in items:
        norm = normalize_rel_path(item, local_root)
        if norm and norm not in seen:
            seen.append(norm)
    return seen


def union_ordered(*file_lists):
    seen = []
    for flist in file_lists:
        for item in (flist or []):
            if item and item not in seen:
                seen.append(item)
    return seen


@contextlib.contextmanager
def transactional_board_lock(collab_dir, timeout_sec=30.0):
    os.makedirs(collab_dir, exist_ok=True)
    lock_path = os.path.join(collab_dir, "board.tx.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)
    deadline = time.monotonic() + timeout_sec
    acquired = False
    try:
        while True:
            try:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (OSError, IOError):
                if time.monotonic() >= deadline:
                    print("[LOCK_TIMEOUT] Timed out waiting for OS transactional board lock.", file=sys.stderr)
                    sys.exit(1)
                time.sleep(0.015)

        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}:{now_utc()}\n".encode("utf-8"))
        os.fsync(fd)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
        try:
            os.close(fd)
        except OSError:
            pass


def atomic_write_json(path, data):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    bak_path = f"{path}.bak"
    payload = json.dumps(data, indent=2)
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        shutil.copy2(path, bak_path)
    os.replace(tmp_path, path)


def safe_read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        bak = f"{path}.bak"
        if os.path.exists(bak):
            with open(bak, "r", encoding="utf-8") as f:
                return json.load(f)
        raise


def fail(code_tag, message):
    print(f"[{code_tag}] {message}", file=sys.stderr)
    sys.exit(1)


# --- Git Helper Functions ---

def run_git(repo_dir, *git_args, check=True):
    cmd = ["git", "-C", repo_dir] + list(git_args)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or f"git failed: {' '.join(git_args)}")
    return res


_GIT_REPO_CACHE = {}

def is_git_repo(repo_dir):
    abspath = os.path.abspath(repo_dir)
    if abspath in _GIT_REPO_CACHE:
        return _GIT_REPO_CACHE[abspath]
    res = run_git(abspath, "rev-parse", "--is-inside-work-tree", check=False)
    ok = (res.returncode == 0 and res.stdout.strip() == "true")
    if ok:
        _GIT_REPO_CACHE[abspath] = True
    return ok


def get_head_commit(repo_dir, ref="HEAD"):
    res = run_git(repo_dir, "rev-parse", "--short=12", ref, check=False)
    return res.stdout.strip() if res.returncode == 0 else ""


def get_current_branch(repo_dir):
    res = run_git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    return res.stdout.strip() if res.returncode == 0 else ""


def has_git_remote(repo_dir, remote_name="origin"):
    res = run_git(repo_dir, "remote", "get-url", remote_name, check=False)
    return res.returncode == 0


def file_existed_in_git(repo_dir, rel_path, base_commit=""):
    """Return True if rel_path is tracked in the index, HEAD, or base_commit (allowing intentional deletion)."""
    if run_git(repo_dir, "ls-files", "--error-unmatch", "--", rel_path, check=False).returncode == 0:
        return True
    if run_git(repo_dir, "cat-file", "-e", f"HEAD:{rel_path}", check=False).returncode == 0:
        return True
    if base_commit and run_git(repo_dir, "cat-file", "-e", f"{base_commit}:{rel_path}", check=False).returncode == 0:
        return True
    return False


def assert_repo_on_task_branch(repo_dir, target):
    """Ensure the working directory is on the task's expected Git branch before staging/committing/reviewing."""
    expected_branch = target.get("gitBranch", "")
    if not expected_branch or not is_git_repo(repo_dir):
        return
    # Only enforce if the branch has actually been created in Git
    branch_exists = run_git(repo_dir, "show-ref", "--verify", f"refs/heads/{expected_branch}", check=False).returncode == 0
    if not branch_exists:
        return
    curr_branch = get_current_branch(repo_dir)
    if curr_branch != expected_branch:
        fail(
            "BRANCH_MISMATCH_ERROR",
            f"Working tree '{repo_dir}' is currently on branch '{curr_branch}', but task '{target['taskId']}' "
            f"requires branch '{expected_branch}'. Checkout '{expected_branch}' or use --use-worktree for simultaneous tasks.",
        )


def ensure_task_branch(repo_dir, branch_name, use_worktree=False, task_id="", base_branch="main"):
    if not is_git_repo(repo_dir):
        return "", ""
    base_ref = base_branch if run_git(repo_dir, "show-ref", "--verify", f"refs/heads/{base_branch}", check=False).returncode == 0 else "HEAD"
    start_sha = get_head_commit(repo_dir, base_ref)
    exists = run_git(repo_dir, "show-ref", "--verify", f"refs/heads/{branch_name}", check=False).returncode == 0
    if use_worktree and task_id:
        wt_path = os.path.join(repo_dir, ".collab", "worktrees", task_id)
        if not os.path.exists(wt_path):
            os.makedirs(os.path.dirname(wt_path), exist_ok=True)
            if exists:
                run_git(repo_dir, "worktree", "add", wt_path, branch_name)
            else:
                run_git(repo_dir, "worktree", "add", "-b", branch_name, wt_path, base_ref)
        return start_sha, wt_path
    else:
        if exists:
            run_git(repo_dir, "checkout", branch_name)
        else:
            run_git(repo_dir, "checkout", "-b", branch_name, base_ref)
        return start_sha, repo_dir


def get_index_modes(repo_dir, paths):
    """Return dict mapping path -> mode ('100644', '100755', '120000') from current Git index in a single git call."""
    if not paths:
        return {}
    res = run_git(repo_dir, "ls-files", "-s", "-z", "--", *paths, check=False)
    modes = {}
    if res.returncode == 0 and res.stdout:
        for rec in res.stdout.split("\0"):
            if not rec.strip() or "\t" not in rec:
                continue
            meta, path = rec.split("\t", 1)
            parts = meta.split()
            if parts:
                modes[path] = parts[0]
    return modes


def get_head_modes(repo_dir, paths):
    """Return dict mapping path -> mode ('100644', '100755', '120000') from HEAD in a single git call."""
    if not paths:
        return {}
    res = run_git(repo_dir, "ls-tree", "-z", "HEAD", "--", *paths, check=False)
    modes = {}
    if res.returncode == 0 and res.stdout:
        for rec in res.stdout.split("\0"):
            if not rec.strip() or "\t" not in rec:
                continue
            meta, path = rec.split("\t", 1)
            parts = meta.split()
            if parts:
                modes[path] = parts[0]
    return modes


def commit_task_owned_files(repo_dir, owned_files, commit_msg, base_commit=""):
    """
    Stage and commit ONLY the task's ownedFiles (supporting modifications, additions, renames,
    executable permission bit changes on both POSIX and Windows, and intentional deletions of tracked files)
    while leaving unowned files untouched.
    """
    if not is_git_repo(repo_dir):
        fail("GIT_ERROR", f"'{repo_dir}' is not a Git repository.")
    if not owned_files:
        fail("VALIDATION_ERROR", "Task has no ownedFiles to stage for checkpoint commit.")

    truly_missing = [
        f for f in owned_files
        if not os.path.exists(os.path.join(repo_dir, f))
        and not os.path.islink(os.path.join(repo_dir, f))
        and not file_existed_in_git(repo_dir, f, base_commit)
    ]
    if truly_missing:
        fail("MISSING_TASK_FILES_ERROR", f"Intended task file(s) never existed and are missing on disk: {', '.join(truly_missing)}")

    stageable_files = [
        f for f in owned_files
        if os.path.exists(os.path.join(repo_dir, f))
        or os.path.islink(os.path.join(repo_dir, f))
        or run_git(repo_dir, "ls-files", "--error-unmatch", "--", f, check=False).returncode == 0
    ]
    if not stageable_files:
        return get_head_commit(repo_dir, "HEAD"), False

    # Capture any staged mode changes (e.g., git update-index --chmod=+x on Windows or Linux) BEFORE reset HEAD!
    pre_index_modes = get_index_modes(repo_dir, stageable_files)
    head_modes = get_head_modes(repo_dir, stageable_files)

    run_git(repo_dir, "reset", "HEAD", check=False)
    run_git(repo_dir, "add", "-A", "--", *stageable_files)

    for f in stageable_files:
        abs_p = os.path.join(repo_dir, f)
        if os.path.islink(abs_p) or not os.path.isfile(abs_p):
            continue
        h_mode = head_modes.get(f, "100644")
        idx_mode = pre_index_modes.get(f, "")
        target_mode = None
        if idx_mode in ("100755", "100644") and idx_mode != h_mode:
            # Explicit index mode change (works on both Windows and POSIX even when core.filemode=false)
            target_mode = idx_mode
        elif os.name != "nt":
            disk_mode = "100755" if (os.stat(abs_p).st_mode & 0o111) else "100644"
            if disk_mode != h_mode:
                target_mode = disk_mode
        elif h_mode == "100755":
            # Preserve existing executable bit on Windows when editing a 100755 file
            target_mode = "100755"
        if target_mode in ("100755", "100644"):
            run_git(repo_dir, "update-index", "--chmod=+x" if target_mode == "100755" else "--chmod=-x", "--", f, check=False)

    diff_cached = run_git(repo_dir, "diff", "--cached", "--name-only", check=False).stdout.strip()
    if not diff_cached:
        return get_head_commit(repo_dir, "HEAD"), False
    # Commit the staged index directly (after reset HEAD + add -- *stageable_files) so index mode changes (100755) are preserved when core.filemode=false on Windows!
    run_git(repo_dir, "commit", "-m", commit_msg)
    return get_head_commit(repo_dir, "HEAD"), True


def check_task_files_clean(repo_dir, owned_files, base_commit=""):
    """Verify all ownedFiles either exist or were intentionally deleted in Git, with zero uncommitted content or mode changes."""
    if not is_git_repo(repo_dir) or not owned_files:
        return True, []
    truly_missing = [
        f for f in owned_files
        if not os.path.exists(os.path.join(repo_dir, f))
        and not os.path.islink(os.path.join(repo_dir, f))
        and not file_existed_in_git(repo_dir, f, base_commit)
    ]
    if truly_missing:
        return False, [f"MISSING:{f}" for f in truly_missing]
    res = run_git(repo_dir, "status", "--porcelain", "--", *owned_files, check=False)
    dirty_lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    return len(dirty_lines) == 0, dirty_lines


def push_branch_to_remote(repo_dir, branch_name, remote_name="origin"):
    if not is_git_repo(repo_dir):
        return "NO_GIT", ""
    if not has_git_remote(repo_dir, remote_name):
        return "NO_REMOTE", ""
    res = run_git(repo_dir, "push", "-u", remote_name, branch_name, check=False)
    if res.returncode != 0:
        return "FAILED", ""
    local_sha = get_head_commit(repo_dir, branch_name)
    rem_sha = get_head_commit(repo_dir, f"refs/remotes/{remote_name}/{branch_name}")
    if rem_sha and rem_sha == local_sha:
        return "PUSHED", local_sha
    return "FAILED", ""


def sync_backup_status(target):
    """
    Compare remote backup status against the currently displayed revision:
    - If merged on main (and not reverted), compare mergedCommit against lastPushedMainCommit / lastPushedCommit.
    - If a no-op merge, preserve PUSHED / NO_REMOTE status.
    - If reverted, compare revertCommit against lastPushedMainCommit.
    - Otherwise (on task branch), compare reviewTargetCommit/latestCommit against lastPushedTaskCommit / lastPushedCommit.
    """
    if target.get("mergeType") == "noop" or target.get("noopMerge"):
        return
    if target.get("reverted") or target.get("status") == "reverted":
        rev_c = target.get("revertCommit", "")
        pushed_m = target.get("lastPushedMainCommit", "")
        if not rev_c or rev_c != pushed_m:
            if target.get("remoteBackupStatus") == "PUSHED":
                target["remoteBackupStatus"] = "UNPUSHED"
        return
    merged_c = target.get("mergedCommit", "")
    if merged_c:
        pushed_main = target.get("lastPushedMainCommit", "") or target.get("lastPushedCommit", "")
        if merged_c != pushed_main:
            if target.get("remoteBackupStatus") == "PUSHED":
                target["remoteBackupStatus"] = "UNPUSHED"
        return
    active_task_rev = target.get("reviewTargetCommit") or target.get("latestCommit") or ""
    pushed_task = target.get("lastPushedTaskCommit", "") or target.get("lastPushedCommit", "")
    if not active_task_rev or active_task_rev != pushed_task:
        if target.get("remoteBackupStatus") == "PUSHED":
            target["remoteBackupStatus"] = "UNPUSHED"


def get_tree_entry(repo_dir, ref, rel_path):
    """
    Return {'mode': str, 'type': str, 'sha': str, 'data': bytes} for ref:rel_path,
    or None if the path does not exist in ref.
    """
    if not ref:
        return None
    res = run_git(repo_dir, "ls-tree", "-z", ref, "--", rel_path, check=False)
    if res.returncode != 0 or not res.stdout:
        return None
    first = res.stdout.split("\0")[0].strip()
    if not first or "\t" not in first:
        return None
    meta, _ = first.split("\t", 1)
    parts = meta.split()
    if len(parts) < 3:
        return None
    mode_str, obj_type, obj_sha = parts[0], parts[1], parts[2]
    blob_res = subprocess.run(["git", "-C", repo_dir, "cat-file", "blob", obj_sha], capture_output=True)
    data_bytes = blob_res.stdout if blob_res.returncode == 0 else b""
    return {
        "mode": mode_str,
        "type": obj_type,
        "sha": obj_sha,
        "data": data_bytes,
    }


def get_blob_bytes(repo_dir, ref, rel_path):
    """Return raw file bytes for ref:rel_path, or None if the path does not exist in ref."""
    e = get_tree_entry(repo_dir, ref, rel_path)
    return e["data"] if e is not None else None


def tree_entries_equal(ea, eb):
    """Compare two get_tree_entry dicts across presence, file mode (100644/100755/120000), object type, and content bytes."""
    if ea is None or eb is None:
        return ea is eb
    return ea["mode"] == eb["mode"] and ea["type"] == eb["type"] and ea["data"] == eb["data"]


def has_task_changes(repo_dir, base_commit, source_ref, task_files):
    """
    Return True if source_ref introduces any file content, executable mode (100644 vs 100755),
    symlink/type, or presence change relative to base_commit across task_files (or repo).
    """
    if not base_commit or not source_ref or commits_equal(repo_dir, base_commit, source_ref):
        return False
    if task_files:
        for f in task_files:
            if not tree_entries_equal(get_tree_entry(repo_dir, base_commit, f), get_tree_entry(repo_dir, source_ref, f)):
                return True
        return False
    res = run_git(repo_dir, "diff", "--quiet", base_commit, source_ref, check=False)
    return res.returncode != 0


def compute_3way_merged_files(repo_dir, orig_main_ref, base_commit, source_ref, task_files):
    """
    Pre-validate and compute the expected 3-way merged tree entries ({'mode', 'type', 'data'} or None)
    for every file in task_files across both file mode/type and content bytes.
    Returns (ok: bool, message: str, expected_entries: dict[str, dict | None]).
    """
    eff_base = base_commit or orig_main_ref
    expected = {}
    for f in task_files:
        base_e = get_tree_entry(repo_dir, eff_base, f)
        ours_e = get_tree_entry(repo_dir, orig_main_ref, f)
        theirs_e = get_tree_entry(repo_dir, source_ref, f)

        if tree_entries_equal(theirs_e, base_e):
            expected[f] = ours_e
        elif tree_entries_equal(ours_e, base_e):
            expected[f] = theirs_e
        elif tree_entries_equal(ours_e, theirs_e):
            expected[f] = ours_e
        else:
            if ours_e is None or theirs_e is None:
                return False, f"Modify/delete conflict on '{f}' between {orig_main_ref} and {source_ref}", {}
            # 3-way merge of file mode (e.g. 100644 -> 100755)
            b_mode = base_e["mode"] if base_e else None
            if theirs_e["mode"] == b_mode:
                merged_mode = ours_e["mode"]
            elif ours_e["mode"] == b_mode:
                merged_mode = theirs_e["mode"]
            elif ours_e["mode"] == theirs_e["mode"]:
                merged_mode = ours_e["mode"]
            else:
                return False, f"File mode conflict on '{f}' ({ours_e['mode']} vs {theirs_e['mode']})", {}

            # 3-way merge of file content bytes
            b_data = base_e["data"] if base_e else None
            if theirs_e["data"] == b_data:
                merged_data = ours_e["data"]
            elif ours_e["data"] == b_data:
                merged_data = theirs_e["data"]
            elif ours_e["data"] == theirs_e["data"]:
                merged_data = ours_e["data"]
            else:
                if merged_mode == "120000":
                    return False, f"Symlink target conflict on '{f}'", {}
                tmp_dir = tempfile.mkdtemp(prefix="collab_3way_", dir=("/dev/shm" if (os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK)) else None))
                try:
                    p_ours = os.path.join(tmp_dir, "ours")
                    p_base = os.path.join(tmp_dir, "base")
                    p_theirs = os.path.join(tmp_dir, "theirs")
                    with open(p_ours, "wb") as fo:
                        fo.write(ours_e["data"])
                    with open(p_base, "wb") as fb:
                        fb.write(b_data if b_data is not None else b"")
                    with open(p_theirs, "wb") as ft:
                        ft.write(theirs_e["data"])
                    res_mf = subprocess.run(["git", "merge-file", "-p", p_ours, p_base, p_theirs], capture_output=True)
                    if res_mf.returncode != 0:
                        return False, f"3-way merge conflict in '{f}' between {orig_main_ref} and {source_ref}", {}
                    merged_data = res_mf.stdout
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
            expected[f] = {"mode": merged_mode, "type": theirs_e["type"], "data": merged_data}
    return True, "OK", expected


def verify_3way_merge_task_files(repo_dir, target_ref, orig_main_ref, base_commit, source_ref, task_files):
    """
    Verify that target_ref contains the exact 3-way merged file presence, mode (100644/100755/120000),
    object type, and content bytes of orig_main_ref and source_ref (relative to base_commit) across all task_files.
    """
    ok, msg, expected = compute_3way_merged_files(repo_dir, orig_main_ref, base_commit, source_ref, task_files)
    if not ok:
        return False, msg
    for f, exp_e in expected.items():
        actual_e = get_tree_entry(repo_dir, target_ref, f)
        if (exp_e is None) != (actual_e is None):
            return False, f"File presence mismatch on '{f}' in {target_ref}"
        if exp_e is not None:
            if actual_e["mode"] != exp_e["mode"]:
                return False, f"File mode mismatch on '{f}' in {target_ref}: expected {exp_e['mode']}, got {actual_e['mode']}"
            if actual_e["type"] != exp_e["type"]:
                return False, f"Object type mismatch on '{f}' in {target_ref}: expected {exp_e['type']}, got {actual_e['type']}"
            if actual_e["data"] != exp_e["data"]:
                return False, f"Merged content mismatch on '{f}' in {target_ref}"
    return True, "OK"


def assert_clean_integration_checkout(repo_dir, task_files=None):
    """
    Require a clean working tree (no uncommitted tracked changes and no untracked file collisions
    with task_files) before switching branches or performing integration/revert operations.
    """
    if not is_git_repo(repo_dir):
        return
    dirty_tracked = run_git(repo_dir, "status", "--porcelain", "-uno", check=False).stdout.strip()
    if dirty_tracked:
        fail(
            "DIRTY_WORKTREE_ERROR",
            f"Refusing integration operation because the checkout has uncommitted tracked changes: {dirty_tracked}. "
            "Commit or stash your local edits before merging or reverting.",
        )
    if task_files:
        untracked_collide = [
            f for f in task_files
            if os.path.exists(os.path.join(repo_dir, f))
            and run_git(repo_dir, "ls-files", "--error-unmatch", "--", f, check=False).returncode != 0
        ]
        if untracked_collide:
            fail(
                "DIRTY_WORKTREE_ERROR",
                f"Refusing integration operation because untracked file(s) on disk would be overwritten: {', '.join(untracked_collide)}",
            )


def safe_rollback_task_files(repo_dir, orig_main_sha, task_files, pre_op_snapshots):
    """
    Roll back ONLY the merge commit / index state and restore ONLY task_files to their pre-operation
    snapshots without ever running `git reset --hard`, preserving any unrelated files in the working tree.
    """
    git_dir_res = run_git(repo_dir, "rev-parse", "--git-dir", check=False)
    if git_dir_res.returncode == 0:
        merge_head = os.path.join(repo_dir, git_dir_res.stdout.strip(), "MERGE_HEAD")
        if os.path.exists(merge_head):
            try:
                os.remove(merge_head)
            except OSError:
                pass
    if orig_main_sha and get_head_commit(repo_dir, "HEAD") != orig_main_sha:
        run_git(repo_dir, "reset", "--soft", orig_main_sha, check=False)
    if task_files:
        run_git(repo_dir, "reset", "HEAD", "--", *task_files, check=False)
    for rel_f, snap_bytes in (pre_op_snapshots or {}).items():
        abs_f = os.path.join(repo_dir, rel_f)
        if snap_bytes is None:
            if os.path.exists(abs_f):
                try:
                    os.remove(abs_f)
                except OSError:
                    pass
        else:
            os.makedirs(os.path.dirname(abs_f), exist_ok=True)
            with open(abs_f, "wb") as wf:
                wf.write(snap_bytes)


_HEX_CHARS = set("0123456789abcdefABCDEF")

def _is_hex_sha(s):
    return len(s) >= 7 and all(c in _HEX_CHARS for c in s)

def resolve_full_sha(repo_dir, ref):
    """Resolve any commit reference or short SHA to its canonical 40-character commit SHA."""
    if not ref:
        return ""
    s = ref.strip()
    if len(s) == 40 and _is_hex_sha(s):
        return s.lower()
    res = run_git(repo_dir, "rev-parse", "--verify", "-q", f"{s}^{{commit}}", check=False)
    return res.stdout.strip().lower() if res.returncode == 0 else s.lower()


def commits_equal(repo_dir, ref_a, ref_b):
    """Compare two commit references/SHAs after normalizing both (fast-pathing hex prefix matches)."""
    if not ref_a or not ref_b:
        return False
    a, b = ref_a.strip().lower(), ref_b.strip().lower()
    if a == b:
        return True
    if _is_hex_sha(a) and _is_hex_sha(b) and min(len(a), len(b)) >= 12:
        if a.startswith(b) or b.startswith(a):
            return True
    return resolve_full_sha(repo_dir, a) == resolve_full_sha(repo_dir, b)


def parse_nul_name_status(raw_output):
    """
    Parse NUL-delimited `git diff --name-status -z` output, enumerating BOTH rename/copy
    source and destination endpoints (`R...\0old\0new\0` -> `[old, new]`).
    """
    tokens = raw_output.split("\0")
    paths = []
    i = 0
    n = len(tokens)
    while i < n:
        status = tokens[i].strip()
        if not status:
            i += 1
            continue
        code = status[0].upper()
        if code in ("R", "C"):
            if i + 2 < n:
                if tokens[i + 1]:
                    paths.append(tokens[i + 1])
                if tokens[i + 2]:
                    paths.append(tokens[i + 2])
            i += 3
        else:
            if i + 1 < n and tokens[i + 1]:
                paths.append(tokens[i + 1])
            i += 2
    return paths


def get_diff_changed_files(repo_dir, ref_a, ref_b, local_root):
    """
    Return normalized list of all files modified, added, deleted, renamed (both old and new paths),
    or copied between ref_a and ref_b using NUL-delimited Git output.
    """
    if not ref_a or not ref_b or not is_git_repo(repo_dir):
        return []
    res_ns = run_git(repo_dir, "diff", "--name-status", "--find-renames", "-z", ref_a, ref_b, check=False)
    paths_ns = parse_nul_name_status(res_ns.stdout) if res_ns.returncode == 0 else []
    res_nr = run_git(repo_dir, "diff", "--no-renames", "--name-only", "-z", ref_a, ref_b, check=False)
    paths_nr = [p for p in res_nr.stdout.split("\0") if p] if res_nr.returncode == 0 else []
    return normalize_file_list(paths_ns + paths_nr, local_root)


def get_commit_changed_files(repo_dir, commit_sha, local_root):
    """
    Return normalized list of all files modified, added, deleted, or renamed (both old and new endpoints)
    by commit_sha relative to its mainline parent using NUL-delimited Git output.
    """
    if not commit_sha or not is_git_repo(repo_dir):
        return []
    rev_list = run_git(repo_dir, "rev-list", "--parents", "-n", "1", commit_sha, check=False).stdout.strip().split()
    if len(rev_list) > 1:
        parent_sha = rev_list[1]
        return get_diff_changed_files(repo_dir, parent_sha, commit_sha, local_root)
    res_ns = run_git(repo_dir, "diff-tree", "--no-commit-id", "--name-status", "--find-renames", "-r", "-z", "--root", commit_sha, check=False)
    paths_ns = parse_nul_name_status(res_ns.stdout) if res_ns.returncode == 0 else []
    res_nr = run_git(repo_dir, "diff-tree", "--no-commit-id", "--no-renames", "--name-only", "-r", "-z", "--root", commit_sha, check=False)
    paths_nr = [p for p in res_nr.stdout.split("\0") if p] if res_nr.returncode == 0 else []
    return normalize_file_list(paths_ns + paths_nr, local_root)


def assert_no_shared_checkout_branch_contention(local_root, board, action_name, target_task_id=""):
    """
    Prevent MergeTask / RevertTask / ReopenTask from switching the shared working tree away
    from another active ('in_progress' or 'review_pending') task's branch.
    """
    if not is_git_repo(local_root):
        return
    curr_branch = get_current_branch(local_root)
    conflicting = [
        t for t in board.get("tasks", [])
        if t["taskId"] != target_task_id
        and t["status"] in ("in_progress", "review_pending")
        and t.get("gitBranch")
        and not t.get("worktreePath")
        and run_git(local_root, "show-ref", "--verify", f"refs/heads/{t['gitBranch']}", check=False).returncode == 0
    ]
    if conflicting:
        ct = conflicting[0]
        fail(
            "BRANCH_CONTENTION_ERROR",
            f"Cannot run {action_name} for '{target_task_id}' in the shared working tree while '{ct['taskId']}' "
            f"is active on branch '{ct['gitBranch']}' (current checkout: '{curr_branch}'). "
            f"Complete '{ct['taskId']}' first or pass --use-worktree to integrate in an isolated worktree.",
        )


def capture_tree_state(repo_dir, ref, task_files):
    """Capture {'exists', 'mode', 'type', 'sha256'} for each file in task_files at Git ref."""
    state = {}
    if not is_git_repo(repo_dir) or not ref:
        return state
    for f in normalize_file_list(task_files or [], repo_dir):
        e = get_tree_entry(repo_dir, ref, f)
        if e is None:
            state[f] = {"exists": False, "mode": None, "type": None, "sha256": None}
        else:
            state[f] = {
                "exists": True,
                "mode": e["mode"],
                "type": e["type"],
                "sha256": hashlib.sha256(e["data"]).hexdigest(),
            }
    return state


def capture_expected_3way_state(expected_3way, task_files, repo_dir):
    """Capture {'exists', 'mode', 'type', 'sha256'} for each file in task_files from compute_3way_merged_files output."""
    state = {}
    for f in normalize_file_list(task_files or [], repo_dir):
        e = (expected_3way or {}).get(f)
        if e is None:
            state[f] = {"exists": False, "mode": None, "type": None, "sha256": None}
        else:
            state[f] = {
                "exists": True,
                "mode": e["mode"],
                "type": e["type"],
                "sha256": hashlib.sha256(e["data"]).hexdigest(),
            }
    return state


def get_index_file_state(repo_dir, rel_path):
    """Return current Git index state for rel_path: {'exists', 'unmerged', 'mode', 'sha256', 'sha256_lf'}."""
    res = run_git(repo_dir, "ls-files", "-s", "-z", "--", rel_path, check=False)
    if res.returncode != 0 or not res.stdout:
        return {"exists": False, "unmerged": False, "mode": None, "sha256": None, "sha256_lf": None}
    entries = [e for e in res.stdout.split("\0") if e.strip()]
    if not entries:
        return {"exists": False, "unmerged": False, "mode": None, "sha256": None, "sha256_lf": None}
    for ent in entries:
        if "\t" not in ent:
            continue
        meta, _ = ent.split("\t", 1)
        parts = meta.split()
        if len(parts) >= 3 and parts[2] != "0":
            return {"exists": True, "unmerged": True, "mode": parts[0], "sha256": None, "sha256_lf": None}
    meta, _ = entries[0].split("\t", 1)
    parts = meta.split()
    mode_str, obj_sha = parts[0], parts[1]
    blob_res = subprocess.run(["git", "-C", repo_dir, "cat-file", "blob", obj_sha], capture_output=True)
    data = blob_res.stdout if blob_res.returncode == 0 else b""
    return {
        "exists": True,
        "unmerged": False,
        "mode": mode_str,
        "sha256": hashlib.sha256(data).hexdigest(),
        "sha256_lf": hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest(),
    }


def get_worktree_file_state(repo_dir, rel_path):
    """Return current working-tree disk state for rel_path: {'exists', 'mode', 'sha256', 'sha256_lf'}."""
    abs_f = os.path.join(repo_dir, rel_path)
    if os.path.islink(abs_f):
        try:
            target_bytes = os.readlink(abs_f).encode("utf-8", errors="surrogateescape")
        except OSError:
            target_bytes = b""
        h = hashlib.sha256(target_bytes).hexdigest()
        return {"exists": True, "mode": "120000", "sha256": h, "sha256_lf": h}
    if not os.path.exists(abs_f):
        return {"exists": False, "mode": None, "sha256": None, "sha256_lf": None}
    try:
        with open(abs_f, "rb") as f:
            data = f.read()
    except OSError:
        return {"exists": False, "mode": None, "sha256": None, "sha256_lf": None}
    return {
        "exists": True,
        "mode": "file",
        "sha256": hashlib.sha256(data).hexdigest(),
        "sha256_lf": hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest(),
    }


def idx_matches_recorded(idx_s, rec_s, check_mode=True):
    if rec_s is None or idx_s.get("unmerged"):
        return False
    if bool(idx_s.get("exists")) != bool(rec_s.get("exists")):
        return False
    if not rec_s.get("exists"):
        return True
    if check_mode and rec_s.get("mode") and idx_s.get("mode") != rec_s.get("mode"):
        return False
    th = rec_s.get("sha256")
    return idx_s.get("sha256") == th or idx_s.get("sha256_lf") == th


def wt_matches_recorded(wt_s, rec_s):
    if rec_s is None:
        return False
    if bool(wt_s.get("exists")) != bool(rec_s.get("exists")):
        return False
    if not rec_s.get("exists"):
        return True
    th = rec_s.get("sha256")
    return wt_s.get("sha256") == th or wt_s.get("sha256_lf") == th


def get_untracked_or_ignored_files(repo_dir):
    """
    Return all untracked AND ignored files in repo_dir (normalized relative paths),
    using `git ls-files --others -z` (without --exclude-standard, so ignored files like ignored-note.txt
    are included) and `git ls-files --others -i --exclude-standard -z`.
    """
    if not is_git_repo(repo_dir):
        return []
    res_all_others = run_git(repo_dir, "ls-files", "--others", "-z", check=False)
    others = [p for p in res_all_others.stdout.split("\0") if p] if res_all_others.returncode == 0 else []
    res_ignored = run_git(repo_dir, "ls-files", "--others", "-i", "--exclude-standard", "-z", check=False)
    ignored = [p for p in res_ignored.stdout.split("\0") if p] if res_ignored.returncode == 0 else []
    return normalize_file_list(others + ignored, repo_dir)


def dispose_integration_worktree(local_root, wt_path):
    """Safely unlock, remove, delete, and prune a temporary integration worktree without touching local_root."""
    if not wt_path:
        return
    abs_wt = os.path.abspath(wt_path)
    if os.path.abspath(local_root) == abs_wt:
        return
    if is_git_repo(local_root):
        run_git(local_root, "worktree", "unlock", abs_wt, check=False)
        run_git(local_root, "worktree", "remove", "--force", abs_wt, check=False)
    if os.path.exists(abs_wt):
        shutil.rmtree(abs_wt, ignore_errors=True)
    if is_git_repo(local_root):
        run_git(local_root, "worktree", "prune", check=False)


def inspect_interrupted_checkout(repo_dir, base_b, orig_m, t_files, orig_state, exp_state, phase=""):
    """
    Verify whether repo_dir still matches the engine's recorded state at orig_m:
    - Returns ('CLEAN_AT_ORIG', reason) if HEAD == orig_m on base_b and all index/working files still match orig_state.
    - Returns ('ENGINE_STAGED_AT_ORIG', reason) if HEAD == orig_m on base_b and index/working files match the engine's
      recorded intermediate staging state (exp_state) with no post-interruption user edits.
    - Returns ('UNRESOLVED', reason) if HEAD/ref moved or any index/working file contains subsequent edits that cannot
      be safely distinguished from engine staging.
    """
    if not is_git_repo(repo_dir) or not orig_m:
        return "UNRESOLVED", "Repository or origMainSha is unavailable."
    curr_branch = get_current_branch(repo_dir)
    head_sha = get_head_commit(repo_dir, "HEAD")
    branch_sha = get_head_commit(repo_dir, base_b)
    if curr_branch != base_b or not commits_equal(repo_dir, head_sha, branch_sha):
        return "UNRESOLVED", f"Checkout or ref moved since interruption (branch={curr_branch}, HEAD={head_sha}, expected {base_b}@{orig_m})."
    head_at_orig = commits_equal(repo_dir, head_sha, orig_m)

    norm_t_files = normalize_file_list(t_files or [], repo_dir)
    t_set = set(norm_t_files)
    porcelain = run_git(repo_dir, "status", "--porcelain", "-uno", check=False).stdout.splitlines()
    for line in porcelain:
        if len(line) > 3:
            path_part = line[3:].strip().strip('"')
            if " -> " in path_part:
                old_p, new_p = path_part.split(" -> ", 1)
                dirty_paths = normalize_file_list([old_p.strip('"'), new_p.strip('"')], repo_dir)
            else:
                dirty_paths = normalize_file_list([path_part], repo_dir)
            if any(dp not in t_set for dp in dirty_paths):
                return "UNRESOLVED", f"Unrelated tracked file modified after interruption: {line.strip()}"

    has_git_marker = False
    git_dir_res = run_git(repo_dir, "rev-parse", "--git-dir", check=False)
    if git_dir_res.returncode == 0:
        gdir = git_dir_res.stdout.strip()
        if not os.path.isabs(gdir):
            gdir = os.path.join(repo_dir, gdir)
        has_git_marker = any(os.path.exists(os.path.join(gdir, m)) for m in ("MERGE_HEAD", "SQUASH_MSG", "MERGE_MSG", "REVERT_HEAD"))

    all_at_orig = True
    all_at_exp = bool(exp_state)
    any_idx_staged = False
    for f in norm_t_files:
        idx_s = get_index_file_state(repo_dir, f)
        wt_s = get_worktree_file_state(repo_dir, f)
        o_s = (orig_state or {}).get(f)
        e_s = (exp_state or {}).get(f)

        is_f_orig = idx_matches_recorded(idx_s, o_s) and wt_matches_recorded(wt_s, o_s)
        if not is_f_orig:
            all_at_orig = False

        if e_s is not None:
            is_f_exp = (idx_matches_recorded(idx_s, e_s, check_mode=False) and wt_matches_recorded(wt_s, e_s)) or is_f_orig
            if not is_f_exp:
                all_at_exp = False
            if idx_matches_recorded(idx_s, e_s, check_mode=False) and not idx_matches_recorded(idx_s, o_s):
                any_idx_staged = True
        else:
            all_at_exp = False

    if head_at_orig and all_at_orig and not has_git_marker:
        return "CLEAN_AT_ORIG", "Checkout and index untouched at origMainSha."
    if head_at_orig and all_at_exp and (has_git_marker or any_idx_staged) and phase in ("staging", "staged", "worktree_created", ""):
        return "ENGINE_STAGED_AT_ORIG", "Checkout and index match recorded intermediate staging state."
    if not head_at_orig:
        return "UNRESOLVED", f"History on {base_b} advanced from {orig_m} to {head_sha} and modified task file(s) outside the journaled operation."
    return "UNRESOLVED", "Working tree or index contains post-interruption edits that do not match recorded operation state."


@contextlib.contextmanager
def integration_checkout_context(local_root, collab_dir, board, base_branch, use_worktree, action_name, target_task_id, task_files):
    """
    Yield the directory (either local_root or a temporary worktree checked out on base_branch)
    where MergeTask or RevertTask should run.
    - When use_worktree is False, enforces assert_no_shared_checkout_branch_contention before switching to base_branch.
    - When use_worktree is True and local_root is on another branch, checks out base_branch in a temporary worktree
      and records its path and phase in op-journal.json immediately so crash recovery targets the temporary worktree
      rather than local_root.
    """
    if use_worktree and get_current_branch(local_root) != base_branch:
        tmp_wt = tempfile.mkdtemp(prefix="collab_integrate_wt_", dir=("/dev/shm" if (os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK)) else None))
        shutil.rmtree(tmp_wt, ignore_errors=True)
        orig_main_sha = get_head_commit(local_root, base_branch)
        write_op_journal(collab_dir, {
            "op": action_name,
            "taskId": target_task_id,
            "baseBranch": base_branch,
            "origMainSha": orig_main_sha,
            "taskFiles": normalize_file_list(task_files or [], local_root),
            "integrationRoot": os.path.abspath(tmp_wt),
            "usedTempWorktree": True,
            "phase": "worktree_created",
            "origState": capture_tree_state(local_root, orig_main_sha, task_files),
            "expectedResultState": {},
        })
        run_git(local_root, "worktree", "add", tmp_wt, base_branch)
        try:
            assert_clean_integration_checkout(tmp_wt, task_files)
            yield tmp_wt
        finally:
            dispose_integration_worktree(local_root, tmp_wt)
    else:
        assert_no_shared_checkout_branch_contention(local_root, board, action_name, target_task_id)
        assert_clean_integration_checkout(local_root, task_files)
        if get_current_branch(local_root) != base_branch:
            run_git(local_root, "checkout", base_branch)
        assert_clean_integration_checkout(local_root, task_files)
        yield local_root


def write_op_journal(collab_dir, payload):
    """Durably record an in-flight Git/ledger operation in .collab/op-journal.json before mutating Git."""
    os.makedirs(collab_dir, exist_ok=True)
    jpath = os.path.join(collab_dir, "op-journal.json")
    data = dict(payload)
    data["journaledAtUtc"] = now_utc()
    atomic_write_json(jpath, data)


def clear_op_journal(collab_dir):
    """Remove .collab/op-journal.json after the ledger and sidecar files are durably saved."""
    for suffix in ("", ".bak"):
        jpath = os.path.join(collab_dir, f"op-journal.json{suffix}")
        if os.path.exists(jpath):
            try:
                os.remove(jpath)
            except OSError:
                pass


def build_handoff_packet(target, prev_owner, chain, to_agent, reason):
    return {
        "taskId": target["taskId"],
        "title": target["title"],
        "handoffFrom": prev_owner,
        "handoffChain": chain,
        "handoffTo": to_agent,
        "reason": reason,
        "lockPreserved": True,
        "lockedFiles": target.get("ownedFiles", []),
        "gitBranch": target.get("gitBranch", ""),
        "handoffCommit": target.get("latestCommit", ""),
        "remoteBackupStatus": target.get("remoteBackupStatus", "UNPUSHED"),
        "progressAtHandoff": target.get("progressPercent", 0),
        "lastUserPrompt": target.get("lastUserPrompt", ""),
        "changesSummary": target.get("changesSummary", ""),
        "remainingTodo": target.get("remainingTodo", ""),
        "timestampUtc": now_utc(),
    }


def cleanup_interrupted_integration_staging(repo_dir, orig_main_sha, task_files):
    """
    Clean up staged / working-tree changes left behind by an interrupted MergeTask or RevertTask
     ONLY after inspect_interrupted_checkout() has proven that repo_dir still holds the engine's
    recorded intermediate staging state at orig_main_sha.
    """
    if not is_git_repo(repo_dir) or not orig_main_sha:
        return
    git_dir_res = run_git(repo_dir, "rev-parse", "--git-dir", check=False)
    if git_dir_res.returncode == 0:
        gdir = git_dir_res.stdout.strip()
        if not os.path.isabs(gdir):
            gdir = os.path.join(repo_dir, gdir)
        if os.path.exists(os.path.join(gdir, "REVERT_HEAD")):
            run_git(repo_dir, "revert", "--abort", check=False)
        for transient in ("MERGE_HEAD", "SQUASH_MSG", "MERGE_MSG", "MERGE_MODE"):
            tp = os.path.join(gdir, transient)
            if os.path.exists(tp):
                try:
                    os.remove(tp)
                except OSError:
                    pass
    norm_files = normalize_file_list(task_files or [], repo_dir)
    if norm_files:
        run_git(repo_dir, "reset", "HEAD", "--", *norm_files, check=False)
        for rel_f in norm_files:
            orig_e = get_tree_entry(repo_dir, orig_main_sha, rel_f)
            abs_f = os.path.join(repo_dir, rel_f)
            if orig_e is None:
                if os.path.exists(abs_f) or os.path.islink(abs_f):
                    try:
                        os.remove(abs_f)
                    except OSError:
                        pass
            else:
                run_git(repo_dir, "checkout", orig_main_sha, "--", rel_f, check=False)


def verify_commit_reverted_target(repo_dir, base_branch, search_range_start, commit_to_revert, task_files, expected_result_state=None):
    """
    Verify that a commit in (search_range_start..base_branch) genuinely reverted commit_to_revert:
    1. Its commit message contains `This reverts commit <full_sha_of_commit_to_revert>`, AND
    2. Across the files changed by commit_to_revert, the revert commit equals the 3-way inverse change
       (applying commit_to_revert -> commit_to_revert^1 onto the pre-revert parent rev_full^1),
       preserving any independent edits made to the same files after commit_to_revert!
    3. If expected_result_state was recorded in op-journal.json for search_range_start, also verify
       that the revert commit matches expected_result_state.
    Returns (ok: bool, revert_short_sha: str).
    """
    if not commit_to_revert or not is_git_repo(repo_dir):
        return False, ""
    mc_full = resolve_full_sha(repo_dir, commit_to_revert)
    if not mc_full:
        return False, ""
    if run_git(repo_dir, "merge-base", "--is-ancestor", mc_full, base_branch, check=False).returncode != 0:
        return False, ""
    if commits_equal(repo_dir, mc_full, base_branch):
        return False, ""

    range_start = search_range_start or mc_full
    log_out = run_git(
        repo_dir, "log", f"--grep=This reverts commit {mc_full}", "--format=%H", f"{range_start}..{base_branch}", check=False
    ).stdout.strip().splitlines()
    if not log_out:
        return False, ""

    rev_full = log_out[0].strip()
    rev_short = get_head_commit(repo_dir, rev_full)
    mc_parents = run_git(repo_dir, "rev-list", "--parents", "-n", "1", mc_full, check=False).stdout.strip().split()
    rev_parents = run_git(repo_dir, "rev-list", "--parents", "-n", "1", rev_full, check=False).stdout.strip().split()
    if len(mc_parents) > 1 and len(rev_parents) > 1:
        pre_merge_parent = mc_parents[1]
        pre_revert_parent = rev_parents[1]
        changed_by_mc = get_commit_changed_files(repo_dir, mc_full, repo_dir)
        check_files = union_ordered(changed_by_mc, normalize_file_list(task_files or [], repo_dir))
        ok_3way, _ = verify_3way_merge_task_files(
            repo_dir, rev_full, pre_revert_parent, mc_full, pre_merge_parent, check_files
        )
        if not ok_3way:
            return False, ""
        if expected_result_state and search_range_start and commits_equal(repo_dir, pre_revert_parent, search_range_start):
            actual_rev_state = capture_tree_state(repo_dir, rev_full, list(expected_result_state.keys()))
            for f, exp_s in expected_result_state.items():
                act_s = actual_rev_state.get(f)
                if not act_s or bool(act_s.get("exists")) != bool(exp_s.get("exists")):
                    return False, ""
                if exp_s.get("exists") and (act_s.get("mode") != exp_s.get("mode") or act_s.get("sha256") != exp_s.get("sha256")):
                    return False, ""
    return True, rev_short


def reconcile_journal_and_git(local_root, collab_dir, handoffs_dir, board, save_state, active_action="", active_task_id=""):
    """
    Reconcile any unfinished operations from .collab/op-journal.json AND inspect Git history
    to recover from process crashes between Git mutations (merge/push/revert/handoff) and save_state().
    - Never overwrites post-interruption working-tree or committed edits: verifies checkout/ref and proves
      index and working files still match the recorded intermediate state before cleaning up.
    - When subsequent edits or history cannot be safely distinguished, preserves them, retains op-journal.json,
      and reports RECOVERY_STATUS: UNRESOLVED.
    - Targets the recorded temporary integration worktree when --use-worktree was used, leaving local_root untouched.
    - Saves recovered board state BEFORE clearing .collab/op-journal.json.
    """
    changed = False
    keep_journal = False
    unresolved_messages = []
    journal_path = os.path.join(collab_dir, "op-journal.json")
    has_journal = os.path.exists(journal_path)

    # 1. Reconcile any missing handoff packet sidecar files from board state
    for t in board.get("tasks", []):
        if t.get("handoffFrom") or t.get("lastHandoffPacket"):
            hp_path = os.path.join(handoffs_dir, f"{t['taskId']}-handoff.json")
            if not os.path.exists(hp_path):
                os.makedirs(handoffs_dir, exist_ok=True)
                pkt = t.get("lastHandoffPacket") or build_handoff_packet(
                    t, t.get("handoffFrom", ""), t.get("handoffChain", []), t.get("assignedTo", ""), "Reconciled handoff"
                )
                atomic_write_json(hp_path, pkt)

    # 2. Reconcile explicit write-ahead operation journal (.collab/op-journal.json) if present
    if has_journal:
        j = safe_read_json(journal_path, default={})
        op = j.get("op", "")
        tid = j.get("taskId", "")
        target = next((x for x in board.get("tasks", []) if x["taskId"] == tid), None) if tid else None

        if op == "AtomicHandoff" and target:
            pkt = j.get("packet") if isinstance(j.get("packet"), dict) else {}
            if j.get("toAgent"):
                target["handoffFrom"] = j.get("prevOwner", target.get("handoffFrom", ""))
                target["handoffChain"] = j.get("chain", target.get("handoffChain", []))
                target["assignedTo"] = j["toAgent"]
                target["deviceClusterId"] = j.get("targetClusterId", target.get("deviceClusterId", ""))
                target["status"] = "in_progress"
                if j.get("heldFiles") is not None:
                    target["ownedFiles"] = j["heldFiles"]
                for ctx_key in ("lastUserPrompt", "changesSummary", "remainingTodo"):
                    val = j.get(ctx_key) if j.get(ctx_key) is not None else pkt.get(ctx_key)
                    if val:
                        target[ctx_key] = val
                reason_val = j.get("reason") or pkt.get("reason") or "Reconciled handoff"
                if j.get("gitBranch") and is_git_repo(local_root):
                    b_sha = get_head_commit(local_root, j["gitBranch"])
                    if b_sha:
                        target["latestCommit"] = b_sha
                        rem_name = j.get("remote", "origin")
                        rem_sha = get_head_commit(local_root, f"refs/remotes/{rem_name}/{j['gitBranch']}")
                        if rem_sha and commits_equal(local_root, rem_sha, b_sha):
                            target["remoteBackupStatus"] = "PUSHED"
                            target["lastPushedCommit"] = b_sha
                            target["lastPushedTaskCommit"] = b_sha
                        else:
                            sync_backup_status(target)
                if pkt:
                    pkt["handoffCommit"] = target.get("latestCommit", "")
                    pkt["remoteBackupStatus"] = target.get("remoteBackupStatus", "UNPUSHED")
                    pkt["lastUserPrompt"] = target.get("lastUserPrompt", "")
                    pkt["changesSummary"] = target.get("changesSummary", "")
                    pkt["remainingTodo"] = target.get("remainingTodo", "")
                    target["lastHandoffPacket"] = pkt
                    os.makedirs(handoffs_dir, exist_ok=True)
                    atomic_write_json(os.path.join(handoffs_dir, f"{tid}-handoff.json"), pkt)
                target["currentStep"] = f"Handed off from {target['handoffFrom']} to {target['assignedTo']} ({reason_val}) [reconciled]"
                target["updatedAtUtc"] = now_utc()
                changed = True

        elif op in ("MergeTask", "RevertTask") and target and is_git_repo(local_root):
            base_b = j.get("baseBranch", "main")
            orig_m = j.get("origMainSha", "")
            curr_m = get_head_commit(local_root, base_b)
            t_files = normalize_file_list(j.get("taskFiles", []), local_root)
            int_root = j.get("integrationRoot") or local_root
            used_wt = bool(j.get("usedTempWorktree")) or (os.path.abspath(int_root) != os.path.abspath(local_root))
            phase = j.get("phase", "")

            orig_state = j.get("origState")
            if not orig_state and orig_m:
                orig_state = capture_tree_state(local_root, orig_m, t_files)
            exp_state = j.get("expectedResultState")
            if not exp_state and orig_m:
                if op == "MergeTask":
                    eff_b = j.get("effectiveBaseCommit") or target.get("baseCommit", "") or orig_m
                    src_r = j.get("sourceRef") or target.get("reviewTargetCommit") or target.get("gitBranch", "")
                    if src_r:
                        ok_3w, _, exp_3w = compute_3way_merged_files(local_root, orig_m, eff_b, src_r, t_files)
                        if ok_3w:
                            exp_state = capture_expected_3way_state(exp_3w, t_files, local_root)
                elif op == "RevertTask":
                    c_rev = j.get("commitToRevert") or target.get("mergedCommit", "")
                    if c_rev:
                        rev_p = run_git(local_root, "rev-list", "--parents", "-n", "1", c_rev, check=False).stdout.strip().split()
                        if len(rev_p) > 1:
                            ok_3w, _, exp_3w = compute_3way_merged_files(local_root, orig_m, c_rev, rev_p[1], t_files)
                            if ok_3w:
                                exp_state = capture_expected_3way_state(exp_3w, t_files, local_root)

            completed_integration = False
            if op == "MergeTask" and curr_m and orig_m and not commits_equal(local_root, curr_m, orig_m):
                ok_v, _ = verify_3way_merge_task_files(
                    local_root,
                    base_b,
                    orig_m,
                    j.get("effectiveBaseCommit", "") or target.get("baseCommit", ""),
                    j.get("sourceRef", "") or target.get("reviewTargetCommit") or target.get("gitBranch", ""),
                    t_files,
                )
                if ok_v:
                    completed_integration = True
                    target["status"] = "done"
                    target["reverted"] = False
                    target["wasReverted"] = bool(j.get("wasReverted", target.get("wasReverted", False)))
                    target["preMergeMainCommit"] = orig_m
                    target["effectiveBaseCommit"] = j.get("effectiveBaseCommit", "")
                    target["mergedCommit"] = curr_m
                    target["mergeType"] = j.get("mergeType", "merge")
                    target["noopMerge"] = False
                    rem_name = j.get("remote", "origin")
                    rem_sha = get_head_commit(local_root, f"refs/remotes/{rem_name}/{base_b}")
                    if rem_sha and commits_equal(local_root, rem_sha, curr_m):
                        target["remoteBackupStatus"] = "PUSHED"
                        target["lastPushedMainCommit"] = curr_m
                        target["lastPushedCommit"] = curr_m
                        target["deliveryStatus"] = "COMPLETE"
                    else:
                        target["remoteBackupStatus"] = "UNPUSHED"
                        target["deliveryStatus"] = "INCOMPLETE"
                    target["currentStep"] = f"Merged to {base_b} ({curr_m}) [reconciled]"
                    target["updatedAtUtc"] = now_utc()
                    changed = True

            elif op == "RevertTask" and curr_m and orig_m and not commits_equal(local_root, curr_m, orig_m):
                c_to_rev = j.get("commitToRevert", target.get("mergedCommit", ""))
                ok_rev, verified_rev_sha = verify_commit_reverted_target(
                    local_root, base_b, orig_m, c_to_rev, t_files, expected_result_state=exp_state
                )
                if ok_rev:
                    completed_integration = True
                    target["status"] = "reverted"
                    target["reverted"] = True
                    target["wasReverted"] = True
                    target["revertedCommit"] = c_to_rev
                    target["revertCommit"] = verified_rev_sha
                    target["mergedCommit"] = ""
                    target["deliveryStatus"] = "REVERTED"
                    target["reconciledInterruptedRevert"] = True
                    rem_name = j.get("remote", "origin")
                    rem_sha = get_head_commit(local_root, f"refs/remotes/{rem_name}/{base_b}")
                    if rem_sha and commits_equal(local_root, rem_sha, verified_rev_sha):
                        target["remoteBackupStatus"] = "PUSHED"
                        target["lastPushedMainCommit"] = verified_rev_sha
                        target["lastPushedCommit"] = verified_rev_sha
                    else:
                        target["remoteBackupStatus"] = "UNPUSHED"
                    target["currentStep"] = f"Reverted commit {c_to_rev} via {verified_rev_sha} [reconciled]"
                    target["updatedAtUtc"] = now_utc()
                    changed = True

            if completed_integration:
                if used_wt and os.path.exists(int_root):
                    wt_dirty = run_git(int_root, "status", "--porcelain", "--ignored", check=False).stdout.strip() if is_git_repo(int_root) else ""
                    extra_wt = get_untracked_or_ignored_files(int_root) if is_git_repo(int_root) else []
                    if not wt_dirty and not extra_wt:
                        dispose_integration_worktree(local_root, int_root)
                    else:
                        keep_journal = True
                        unresolved_messages.append(
                            f"RECOVERY_STATUS: UNRESOLVED (op={op}, task={tid}: temporary worktree '{int_root}' has post-commit or ignored files: {', '.join(extra_wt) or wt_dirty})"
                        )
            else:
                if used_wt:
                    # [P2] Target the abandoned temporary integration worktree, NEVER local_root!
                    if not os.path.exists(int_root):
                        run_git(local_root, "worktree", "prune", check=False)
                    elif not is_git_repo(int_root):
                        dispose_integration_worktree(local_root, int_root)
                    else:
                        state_kind, reason = inspect_interrupted_checkout(
                            int_root, base_b, orig_m, t_files, orig_state, exp_state, phase
                        )
                        # [P1] Account for BOTH ignored files (.gitignore) and ordinary untracked files before deleting an abandoned worktree!
                        all_untracked_or_ignored = get_untracked_or_ignored_files(int_root)
                        extra_wt_files = [
                            p for p in all_untracked_or_ignored
                            if p not in set(t_files)
                            or not (
                                wt_matches_recorded(get_worktree_file_state(int_root, p), (orig_state or {}).get(p))
                                or wt_matches_recorded(get_worktree_file_state(int_root, p), (exp_state or {}).get(p))
                            )
                        ]
                        if state_kind in ("CLEAN_AT_ORIG", "ENGINE_STAGED_AT_ORIG") and not extra_wt_files:
                            dispose_integration_worktree(local_root, int_root)
                        else:
                            keep_journal = True
                            detail = f"Untracked or ignored file(s) present: {', '.join(extra_wt_files)}" if extra_wt_files else reason
                            unresolved_messages.append(
                                f"RECOVERY_STATUS: UNRESOLVED (op={op}, task={tid}, worktree={int_root}: {detail})"
                            )
                else:
                    # [P1] Shared checkout: prove index and working files still match recorded intermediate state before cleaning up!
                    state_kind, reason = inspect_interrupted_checkout(
                        local_root, base_b, orig_m, t_files, orig_state, exp_state, phase
                    )
                    if state_kind == "CLEAN_AT_ORIG":
                        # Nothing was staged or modified by the interrupted op; clear journal without touching files.
                        pass
                    elif state_kind == "ENGINE_STAGED_AT_ORIG":
                        cleanup_interrupted_integration_staging(local_root, orig_m, t_files)
                    else:
                        keep_journal = True
                        unresolved_messages.append(
                            f"RECOVERY_STATUS: UNRESOLVED (op={op}, task={tid}: {reason})"
                        )

    # 3. Fast Git-graph reconciliation when MergeTask, RevertTask, ReleaseTag, or Status runs (or after journal recovery)
    if (has_journal or active_action in ("MergeTask", "RevertTask", "ReleaseTag", "Status")) and is_git_repo(local_root) and run_git(local_root, "show-ref", "--verify", "refs/heads/main", check=False).returncode == 0:
        base_b = "main"
        head_main = get_head_commit(local_root, base_b)
        rev_list = run_git(local_root, "rev-list", "--parents", "-n", "1", head_main, check=False).stdout.strip().split() if head_main else []
        subj = run_git(local_root, "log", "-1", "--format=%s", head_main, check=False).stdout.strip() if head_main else ""
        for t in board.get("tasks", []):
            if active_task_id and t["taskId"] != active_task_id:
                continue
            # 3a. Reconcile completed merge on main if ledger still has mergedCommit == ""
            if t["status"] == "done" and not t.get("mergedCommit") and not t.get("noopMerge") and not t.get("reverted") and len(rev_list) >= 2:
                source_ref = t.get("reviewTargetCommit") or t.get("gitBranch", "")
                base_c = t.get("baseCommit", "")
                is_subj = f"({t['taskId']})" in subj or f"Merge {t.get('gitBranch', '')}:" in subj
                is_2p = len(rev_list) > 2 and source_ref and commits_equal(local_root, rev_list[2], source_ref)
                if source_ref and base_c and (is_subj or is_2p):
                    parent_main = get_head_commit(local_root, rev_list[1])
                    t_files = union_ordered(
                        normalize_file_list(t.get("targetFiles", []), local_root),
                        get_diff_changed_files(local_root, base_c, source_ref, local_root),
                    )
                    if has_task_changes(local_root, base_c, source_ref, t_files):
                        ok_v, _ = verify_3way_merge_task_files(local_root, base_b, parent_main, base_c, source_ref, t_files)
                        if ok_v:
                            t["preMergeMainCommit"] = parent_main
                            t["effectiveBaseCommit"] = base_c
                            t["mergedCommit"] = head_main
                            t["mergeType"] = "merge" if len(rev_list) > 2 else "squash"
                            t["noopMerge"] = False
                            rem_sha = get_head_commit(local_root, f"refs/remotes/origin/{base_b}")
                            if rem_sha and commits_equal(local_root, rem_sha, head_main):
                                t["remoteBackupStatus"] = "PUSHED"
                                t["lastPushedMainCommit"] = head_main
                                t["lastPushedCommit"] = head_main
                                t["deliveryStatus"] = "COMPLETE"
                            changed = True

            # 3b. Reconcile completed revert on main if ledger still says status == 'done' and mergedCommit != ""
            if t["status"] == "done" and t.get("mergedCommit") and not t.get("reverted"):
                mc = t["mergedCommit"]
                ok_rev, rev_sha = verify_commit_reverted_target(
                    local_root, base_b, mc, mc, t.get("targetFiles", [])
                )
                if ok_rev:
                    t["status"] = "reverted"
                    t["reverted"] = True
                    t["wasReverted"] = True
                    t["revertedCommit"] = mc
                    t["revertCommit"] = rev_sha
                    t["mergedCommit"] = ""
                    t["deliveryStatus"] = "REVERTED"
                    t["reconciledInterruptedRevert"] = True
                    rem_sha = get_head_commit(local_root, f"refs/remotes/origin/{base_b}")
                    if rem_sha and commits_equal(local_root, rem_sha, rev_sha):
                        t["remoteBackupStatus"] = "PUSHED"
                        t["lastPushedMainCommit"] = rev_sha
                        t["lastPushedCommit"] = rev_sha
                    else:
                        t["remoteBackupStatus"] = "UNPUSHED"
                    t["currentStep"] = f"Reverted commit {mc} via {rev_sha} [reconciled]"
                    t["updatedAtUtc"] = now_utc()
                    changed = True

    # [P1] ALWAYS save the recovered board state BEFORE clearing the operation journal, and retain journal if unresolved!
    if changed:
        save_state()
    if has_journal and not keep_journal:
        clear_op_journal(collab_dir)
    if unresolved_messages:
        board["_unresolvedRecovery"] = unresolved_messages
        for msg in unresolved_messages:
            print(msg)


# --- 42-Stage Self-Test Suite ---


def run_self_test():
    """Run 42-stage adversarial, concurrency, and Git delivery/recovery self-test suite in an isolated temp folder."""
    os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["HOME"] = "/dev/shm" if (os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK)) else tempfile.gettempdir()
    engine_path = os.path.abspath(__file__)
    fast_tmp = "/dev/shm" if (os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK)) else None
    ws = tempfile.mkdtemp(prefix="collab_selftest_", dir=fast_tmp)
    remote_bare = tempfile.mkdtemp(prefix="collab_remote_bare_", dir=fast_tmp)
    try:
        os.makedirs(os.path.join(ws, "src"), exist_ok=True)
        run_git(remote_bare, "init", "--bare")
        run_git(ws, "init", "-b", "main")
        run_git(ws, "config", "user.email", "test@example.com")
        run_git(ws, "config", "user.name", "Collab SelfTest")
        with open(os.path.join(ws, ".gitignore"), "w", encoding="utf-8") as f:
            f.write(".collab/\n")
        with open(os.path.join(ws, "src", "auth.js"), "w", encoding="utf-8") as f:
            f.write("// initial auth\n")
        with open(os.path.join(ws, "src", "profile.js"), "w", encoding="utf-8") as f:
            f.write("// initial profile\n")
        with open(os.path.join(ws, "src", "obsolete.js"), "w", encoding="utf-8") as f:
            f.write("// obsolete file to be deleted\n")
        with open(os.path.join(ws, "src", "unowned.js"), "w", encoding="utf-8") as f:
            f.write("// initial unowned\n")
        with open(os.path.join(ws, "src", "shared.js"), "w", encoding="utf-8") as f:
            f.write("// line 1: header\n// line 2\n// line 3\n// line 4\n// line 5: footer\n")
        run_git(ws, "add", ".gitignore", "src/auth.js", "src/profile.js", "src/obsolete.js", "src/unowned.js", "src/shared.js")
        run_git(ws, "commit", "-m", "chore: initial commit")
        run_git(ws, "remote", "add", "origin", remote_bare)
        run_git(ws, "push", "-u", "origin", "main")

        import io
        def invoke(*cli_args, expect_fail=False, use_subprocess=False):
            full_args = ["--local-path", ws] + list(cli_args)
            if use_subprocess:
                cmd = [sys.executable, engine_path] + full_args
                res = subprocess.run(cmd, capture_output=True, text=True)
                rc, out, err = res.returncode, res.stdout, res.stderr
            else:
                buf_out, buf_err = io.StringIO(), io.StringIO()
                rc = 0
                with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                    try:
                        main(full_args)
                    except SystemExit as se:
                        rc = se.code if isinstance(se.code, int) else (1 if se.code else 0)
                out, err = buf_out.getvalue(), buf_err.getvalue()
            if expect_fail:
                if rc == 0:
                    raise AssertionError(f"Expected failure but succeeded: {full_args}\nSTDOUT: {out}")
                return err.strip()
            if rc != 0:
                raise AssertionError(f"Unexpected failure: {full_args}\nSTDERR: {err}\nSTDOUT: {out}")
            return out.strip()

        print("[SELFTEST 1/42] Initialize workspace & block accidental re-initialization...")
        invoke("--action", "InitFresh", "--project-key", "COLLAB-SELF-TEST", "--collaborator-id", "pc1-antigravity")
        err_reinit = invoke("--action", "InitFresh", "--project-key", "COLLAB-SELF-TEST", "--collaborator-id", "pc1-antigravity", expect_fail=True)
        assert "ALREADY_INITIALIZED_ERROR" in err_reinit

        print("[SELFTEST 2/42] Reject nonexistent task handshake & duplicate task IDs...")
        err1 = invoke("--action", "SilentHandshake", "--task-id", "TASK-404", "--collaborator-id", "pc1-codex", expect_fail=True)
        assert "INVALID_TASK_ERROR" in err1
        invoke("--action", "AddTask", "--task-id", "TASK-01", "--task-title", "Auth Module", "--files", "./src\\auth.js,src/profile.js,src/obsolete.js")
        err2 = invoke("--action", "AddTask", "--task-id", "TASK-01", "--task-title", "Dup", "--files", "src/auth.js", expect_fail=True)
        assert "DUPLICATE_TASK_ERROR" in err2

        print("[SELFTEST 3/42] Block overlapping default files even when --files is omitted...")
        invoke("--action", "AddTask", "--task-id", "TASK-02", "--task-title", "Overlapping Auth", "--files", "src/auth.js")
        invoke("--action", "AddTask", "--task-id", "TASK-03", "--task-title", "Unrelated Feature", "--files", "src/other.js")
        out_claim = invoke("--action", "SilentHandshake", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", "--git-branch")
        assert "STATUS: APPROVED" in out_claim
        out_collide = invoke("--action", "SilentHandshake", "--task-id", "TASK-02", "--collaborator-id", "pc1-codex")
        assert "STATUS: BUSY_REDIRECT" in out_collide

        print("[SELFTEST 4/42] Block cross-branch hijacking in shared working tree...")
        err_branch_clash = invoke("--action", "SilentHandshake", "--task-id", "TASK-03", "--collaborator-id", "pc1-codex", "--git-branch", expect_fail=True)
        assert "BRANCH_CONTENTION_ERROR" in err_branch_clash

        print("[SELFTEST 5/42] Preserve earlier file claims on repeated handshakes...")
        out_re = invoke("--action", "SilentHandshake", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", "--files", "src/profile.js")
        assert "STATUS: APPROVED" in out_re
        out_still_locked = invoke("--action", "SilentHandshake", "--task-id", "TASK-02", "--collaborator-id", "pc1-codex", "--files", "src/auth.js")
        assert "STATUS: BUSY_REDIRECT" in out_still_locked

        print("[SELFTEST 6/42] ReleaseFiles + AtomicHandoff never resurrects released claims...")
        with open(os.path.join(ws, "src", "releaseme.js"), "w", encoding="utf-8") as f:
            f.write("// release test\n")
        invoke("--action", "AddTask", "--task-id", "TASK-04", "--task-title", "Claim Released File", "--files", "src/releaseme.js")
        invoke("--action", "SilentHandshake", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", "--files", "src/releaseme.js")
        invoke("--action", "ReleaseFiles", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", "--files", "src/releaseme.js")
        out_t4 = invoke("--action", "SilentHandshake", "--task-id", "TASK-04", "--collaborator-id", "pc2-other")
        assert "STATUS: APPROVED" in out_t4
        invoke("--action", "AtomicHandoff", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", "--target-agent-id", "pc1-codex")
        board_now = safe_read_json(os.path.join(ws, ".collab", "state-board.json"))
        t1_obj = next(t for t in board_now["tasks"] if t["taskId"] == "TASK-01")
        assert "src/releaseme.js" not in t1_obj["ownedFiles"]

        print("[SELFTEST 7/42] Enforce strict ownership & --force-handoff lead-only rule...")
        err_done = invoke("--action", "Done", "--task-id", "TASK-01", "--collaborator-id", "pc2-intruder", expect_fail=True)
        assert "OWNERSHIP_ERROR" in err_done
        err_force_nonlead = invoke("--action", "AtomicHandoff", "--task-id", "TASK-01", "--collaborator-id", "pc2-intruder", "--target-agent-id", "pc2-intruder", "--force-handoff", expect_fail=True)
        assert "OWNERSHIP_ERROR" in err_force_nonlead
        err_lead_noforce = invoke("--action", "AtomicHandoff", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", "--target-agent-id", "pc1-antigravity", expect_fail=True)
        assert "OWNERSHIP_ERROR" in err_lead_noforce

        print("[SELFTEST 8/42] Verify resume-skip guard after handoff...")
        out_skip = invoke("--action", "CheckOrContinue", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity")
        assert "ACTION: SKIP_ALREADY_HANDED_OFF" in out_skip
        out_cont = invoke("--action", "CheckOrContinue", "--task-id", "TASK-01", "--collaborator-id", "pc1-codex")
        assert "ACTION: CONTINUE_APPROVED" in out_cont

        print("[SELFTEST 9/42] Reject Checkpoint on mismatched Git branch...")
        run_git(ws, "checkout", "main")
        err_mismatch = invoke("--action", "Checkpoint", "--task-id", "TASK-01", "--collaborator-id", "pc1-codex", "--commit-message", "wrong branch", expect_fail=True)
        assert "BRANCH_MISMATCH_ERROR" in err_mismatch
        run_git(ws, "checkout", "task/TASK-01-auth-module")

        print("[SELFTEST 10/42] Task-scoped Checkpoint supports intentional file deletion & ignores unowned files...")
        with open(os.path.join(ws, "src", "auth.js"), "a", encoding="utf-8") as f:
            f.write("export function verifyToken() { return true; }\n")
        os.remove(os.path.join(ws, "src", "obsolete.js"))
        with open(os.path.join(ws, "src", "unowned.js"), "a", encoding="utf-8") as f:
            f.write("// dirty edit by another window\n")
        out_cp1 = invoke("--action", "Checkpoint", "--task-id", "TASK-01", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-01): add verifyToken and remove obsolete.js", "--push")
        assert "CHECKPOINT_COMMIT:" in out_cp1 and "REMOTE_BACKUP: PUSHED" in out_cp1
        unowned_status = run_git(ws, "status", "--porcelain", "--", "src/unowned.js").stdout.strip()
        assert unowned_status.startswith("M")
        run_git(ws, "checkout", "--", "src/unowned.js")

        print("[SELFTEST 11/42] New checkpoint without --push resets REMOTE_BACKUP to UNPUSHED...")
        with open(os.path.join(ws, "src", "profile.js"), "a", encoding="utf-8") as f:
            f.write("export const profileReady = true;\n")
        out_cp2 = invoke("--action", "Checkpoint", "--task-id", "TASK-01", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-01): update profile locally")
        assert "REMOTE_BACKUP: UNPUSHED" in out_cp2

        print("[SELFTEST 12/42] Block Done when ownedFiles have uncommitted edits, then succeed once clean...")
        with open(os.path.join(ws, "src", "profile.js"), "a", encoding="utf-8") as f:
            f.write("export const extraLine = 1;\n")
        err_dirty_done = invoke("--action", "Done", "--task-id", "TASK-01", "--collaborator-id", "pc1-codex", expect_fail=True)
        assert "UNCOMMITTED_WORK_ERROR" in err_dirty_done
        invoke("--action", "Checkpoint", "--task-id", "TASK-01", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-01): finalize profile", "--push")
        invoke("--action", "Done", "--task-id", "TASK-01", "--collaborator-id", "pc1-codex")

        print("[SELFTEST 13/42] Stale review recovery: post-Done edit blocks approval, but allows ReopenTask / resubmit...")
        with open(os.path.join(ws, "src", "auth.js"), "a", encoding="utf-8") as f:
            f.write("export const postDoneFix = true;\n")
        err_stale_rev = invoke("--action", "SubmitReview", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED", expect_fail=True)
        assert "STALE_REVIEW_COMMIT_ERROR" in err_stale_rev
        invoke("--action", "ReopenTask", "--task-id", "TASK-01", "--collaborator-id", "pc1-codex")
        invoke("--action", "Checkpoint", "--task-id", "TASK-01", "--collaborator-id", "pc1-codex", "--commit-message", "fix(TASK-01): include postDoneFix", "--push")
        invoke("--action", "Done", "--task-id", "TASK-01", "--collaborator-id", "pc1-codex", "--push")

        print("[SELFTEST 14/42] Block review_pending takeover, block self-review, & approve exact resubmitted commit...")
        out_rev_block = invoke("--action", "SilentHandshake", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity")
        assert "STATUS: BUSY_REDIRECT" in out_rev_block
        err_self_rev = invoke("--action", "SubmitReview", "--task-id", "TASK-01", "--collaborator-id", "pc1-codex", "--review-verdict", "APPROVED", expect_fail=True)
        assert "SELF_REVIEW_FORBIDDEN" in err_self_rev
        invoke("--action", "SubmitReview", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED", "--review-notes", "Verified resubmitted commit")

        print("[SELFTEST 15/42] Default ReleaseTag rejects unmerged branch & unmerged approved tasks...")
        err_branch_tag = invoke("--action", "ReleaseTag", "--tag", "v0.8.0", "--collaborator-id", "pc1-antigravity", expect_fail=True)
        assert "UNMERGED_RELEASE_ERROR" in err_branch_tag
        run_git(ws, "checkout", "main")
        err_default_tag = invoke("--action", "ReleaseTag", "--tag", "v0.9.0", "--collaborator-id", "pc1-antigravity", expect_fail=True)
        assert "UNMERGED_RELEASE_ERROR" in err_default_tag
        run_git(ws, "checkout", "task/TASK-01-auth-module")

        print("[SELFTEST 16/42] MergeTask with no remote reports DELIVERY: LOCAL_ONLY (never COMPLETE), then COMPLETE once pushed (Must Fix #4)...")
        assert get_current_branch(ws) == "task/TASK-01-auth-module"
        # First remove origin completely: must report REMOTE_BACKUP: NO_REMOTE and DELIVERY: LOCAL_ONLY (NOT COMPLETE!)
        run_git(ws, "remote", "remove", "origin")
        out_no_remote = invoke("--action", "MergeTask", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", "--squash")
        assert "REMOTE_BACKUP: NO_REMOTE" in out_no_remote and "DELIVERY: LOCAL_ONLY" in out_no_remote and "DELIVERY: COMPLETE" not in out_no_remote
        # Next set broken remote: must report REMOTE_BACKUP: FAILED and DELIVERY: INCOMPLETE
        run_git(ws, "remote", "add", "origin", "/nonexistent/broken/remote.git")
        out_merge_fail = invoke("--action", "MergeTask", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", "--squash")
        assert "REMOTE_BACKUP: FAILED" in out_merge_fail and "DELIVERY: INCOMPLETE" in out_merge_fail
        # Next restore valid remote: must push and report DELIVERY: COMPLETE and Status PUSHED
        run_git(ws, "remote", "set-url", "origin", remote_bare)
        out_merge_ok = invoke("--action", "MergeTask", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", "--squash")
        assert "REMOTE_BACKUP: PUSHED" in out_merge_ok and "DELIVERY: COMPLETE" in out_merge_ok
        status_cli = invoke("--action", "Status", "--render-format", "CLI")
        t1_cli_line = next(line for line in status_cli.splitlines() if "TASK-01" in line)
        assert "PUSHED" in t1_cli_line and "UNPUSHED" not in t1_cli_line

        print("[SELFTEST 17/42] Create immutable ReleaseTag on main & revert squash commit...")
        out_tag = invoke("--action", "ReleaseTag", "--tag", "v1.0.0", "--tag-message", "First accepted flow", "--collaborator-id", "pc1-antigravity")
        assert "RELEASE_TAG_CREATED: v1.0.0" in out_tag
        out_rev_squash = invoke("--action", "RevertTask", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", "--push")
        assert "REVERT_COMMIT:" in out_rev_squash

        print("[SELFTEST 18/42] Block --reapply when another task claims overlapping files or checkout has unsaved edits (Must Fix #2)...")
        # Have TASK-02 claim src/auth.js and start editing it!
        out_t2_claim = invoke("--action", "SilentHandshake", "--task-id", "TASK-02", "--collaborator-id", "pc1-codex")
        assert "STATUS: APPROVED" in out_t2_claim
        with open(os.path.join(ws, "src", "auth.js"), "a", encoding="utf-8") as f:
            f.write("// unfinished edit by TASK-02\n")
        # Attempting to reapply TASK-01 while TASK-02 owns src/auth.js must fail with FILE_CONFLICT_ERROR and keep TASK-02's edit intact!
        err_reapply_locked = invoke("--action", "MergeTask", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", "--reapply", "--squash", expect_fail=True)
        assert "FILE_CONFLICT_ERROR" in err_reapply_locked
        assert "unfinished edit by TASK-02" in open(os.path.join(ws, "src", "auth.js"), encoding="utf-8").read()
        # Now release TASK-02's claim on src/auth.js, but keep unsaved edit on disk: reapply must still fail with DIRTY_WORKTREE_ERROR and preserve the edit!
        invoke("--action", "ReleaseFiles", "--task-id", "TASK-02", "--collaborator-id", "pc1-codex", "--files", "src/auth.js")
        err_reapply_dirty = invoke("--action", "MergeTask", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", "--reapply", "--squash", expect_fail=True)
        assert "DIRTY_WORKTREE_ERROR" in err_reapply_dirty
        assert "unfinished edit by TASK-02" in open(os.path.join(ws, "src", "auth.js"), encoding="utf-8").read()
        run_git(ws, "checkout", "--", "src/auth.js")

        print("[SELFTEST 19/42] Block retrying MergeTask on a reverted task without --reapply, then explicitly reapply...")
        err_retry_reverted = invoke("--action", "MergeTask", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", expect_fail=True)
        assert "REVERTED_TASK_ERROR" in err_retry_reverted
        out_reapply = invoke("--action", "MergeTask", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", "--reapply", "--squash")
        assert "DELIVERY: COMPLETE" in out_reapply
        assert "verifyToken" in open(os.path.join(ws, "src", "auth.js"), encoding="utf-8").read()
        assert "verifyToken" in run_git(remote_bare, "show", "main:src/auth.js").stdout
        invoke("--action", "RevertTask", "--task-id", "TASK-01", "--collaborator-id", "pc1-antigravity", "--push")

        print("[SELFTEST 20/42] No-change task records NOOP merge and forbids RevertTask from undoing unrelated commits...")
        invoke("--action", "AddTask", "--task-id", "TASK-NOOP", "--task-title", "No Change Inspection Task", "--files", "src/profile.js")
        invoke("--action", "SilentHandshake", "--task-id", "TASK-NOOP", "--collaborator-id", "pc1-codex", "--git-branch")
        invoke("--action", "Done", "--task-id", "TASK-NOOP", "--collaborator-id", "pc1-codex", "--push")
        invoke("--action", "SubmitReview", "--task-id", "TASK-NOOP", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED")
        main_before_noop = get_head_commit(ws, "main")
        out_noop_merge = invoke("--action", "MergeTask", "--task-id", "TASK-NOOP", "--collaborator-id", "pc1-antigravity")
        assert "LOCAL_COMMIT: NOOP" in out_noop_merge
        assert get_head_commit(ws, "main") == main_before_noop
        err_noop_rev = invoke("--action", "RevertTask", "--task-id", "TASK-NOOP", "--collaborator-id", "pc1-antigravity", expect_fail=True)
        assert "NOOP_REVERT_ERROR" in err_noop_rev
        assert os.path.exists(os.path.join(ws, "src", "auth.js")) and os.path.exists(os.path.join(ws, "src", "profile.js"))

        print("[SELFTEST 21/42] Clean 3-way merge when main and task modify different parts of the same file...")
        invoke("--action", "AddTask", "--task-id", "TASK-3WAY", "--task-title", "Bottom Footer Edit", "--files", "src/shared.js")
        invoke("--action", "SilentHandshake", "--task-id", "TASK-3WAY", "--collaborator-id", "pc1-codex", "--git-branch")
        with open(os.path.join(ws, "src", "shared.js"), "w", encoding="utf-8") as f:
            f.write("// line 1: header\n// line 2\n// line 3\n// line 4\n// line 5: footer + task3wayFeature\n")
        invoke("--action", "Checkpoint", "--task-id", "TASK-3WAY", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-3WAY): update footer", "--push")
        invoke("--action", "Done", "--task-id", "TASK-3WAY", "--collaborator-id", "pc1-codex", "--push")
        invoke("--action", "SubmitReview", "--task-id", "TASK-3WAY", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED")
        run_git(ws, "checkout", "main")
        with open(os.path.join(ws, "src", "shared.js"), "w", encoding="utf-8") as f:
            f.write("// line 1: header + mainBanner\n// line 2\n// line 3\n// line 4\n// line 5: footer\n")
        run_git(ws, "add", "src/shared.js")
        run_git(ws, "commit", "-m", "chore(main): add mainBanner to header")
        run_git(ws, "push", "origin", "main")
        out_3way = invoke("--action", "MergeTask", "--task-id", "TASK-3WAY", "--collaborator-id", "pc1-antigravity", "--squash")
        assert "DELIVERY: COMPLETE" in out_3way
        shared_merged = open(os.path.join(ws, "src", "shared.js"), encoding="utf-8").read()
        assert "mainBanner" in shared_merged and "task3wayFeature" in shared_merged

        print("[SELFTEST 22/42] Dirty worktree guard & non-destructive rollback preserve unsaved edits (Must Fix #1)...")
        invoke("--action", "AddTask", "--task-id", "TASK-CONFLICT", "--task-title", "Conflicting Line Edit", "--files", "src/shared.js")
        invoke("--action", "SilentHandshake", "--task-id", "TASK-CONFLICT", "--collaborator-id", "pc1-codex", "--git-branch")
        with open(os.path.join(ws, "src", "shared.js"), "w", encoding="utf-8") as f:
            f.write("// line 1: TASK_CONFLICT_OVERRIDE\n// line 2\n// line 3\n// line 4\n// line 5: footer + task3wayFeature\n")
        invoke("--action", "Checkpoint", "--task-id", "TASK-CONFLICT", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-CONFLICT): change line 1", "--push")
        invoke("--action", "Done", "--task-id", "TASK-CONFLICT", "--collaborator-id", "pc1-codex", "--push")
        invoke("--action", "SubmitReview", "--task-id", "TASK-CONFLICT", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED")
        run_git(ws, "checkout", "main")
        with open(os.path.join(ws, "src", "shared.js"), "w", encoding="utf-8") as f:
            f.write("// line 1: MAIN_CONFLICT_OVERRIDE\n// line 2\n// line 3\n// line 4\n// line 5: footer + task3wayFeature\n")
        run_git(ws, "add", "src/shared.js")
        run_git(ws, "commit", "-m", "chore(main): conflicting change on line 1")
        main_before_conflict = get_head_commit(ws, "main")
        # 1) Put unsaved edits in BOTH src/shared.js and unrelated src/unowned.js, then run MergeTask:
        # Must fail with DIRTY_WORKTREE_ERROR and preserve BOTH unsaved edits!
        with open(os.path.join(ws, "src", "shared.js"), "a", encoding="utf-8") as f:
            f.write("// unsaved shared edit\n")
        with open(os.path.join(ws, "src", "unowned.js"), "a", encoding="utf-8") as f:
            f.write("// unsaved unrelated edit\n")
        err_dirty_merge = invoke("--action", "MergeTask", "--task-id", "TASK-CONFLICT", "--collaborator-id", "pc1-antigravity", expect_fail=True)
        assert "DIRTY_WORKTREE_ERROR" in err_dirty_merge
        assert "unsaved shared edit" in open(os.path.join(ws, "src", "shared.js"), encoding="utf-8").read()
        assert "unsaved unrelated edit" in open(os.path.join(ws, "src", "unowned.js"), encoding="utf-8").read()
        # 2) Directly exercise safe_rollback_task_files while src/unowned.js still has unsaved work:
        snap_shared = get_blob_bytes(ws, main_before_conflict, "src/shared.js")
        safe_rollback_task_files(ws, main_before_conflict, ["src/shared.js"], {"src/shared.js": snap_shared})
        assert "unsaved unrelated edit" in open(os.path.join(ws, "src", "unowned.js"), encoding="utf-8").read(), "Rollback erased unrelated unsaved work!"
        run_git(ws, "checkout", "--", "src/shared.js", "src/unowned.js")
        # 3) Clean conflict check also leaves main at main_before_conflict
        err_conflict = invoke("--action", "MergeTask", "--task-id", "TASK-CONFLICT", "--collaborator-id", "pc1-antigravity", expect_fail=True)
        assert "MERGE_CONFLICT_ERROR" in err_conflict
        assert get_head_commit(ws, "main") == main_before_conflict

        print("[SELFTEST 23/42] Default non-squash merge (--no-ff) & mainline-parent RevertTask (-m 1)...")
        invoke("--action", "ReleaseFiles", "--task-id", "TASK-04", "--collaborator-id", "pc2-other", "--files", "src/releaseme.js")
        run_git(ws, "add", "src/releaseme.js")
        run_git(ws, "commit", "-m", "chore: track releaseme.js on main")
        run_git(ws, "push", "origin", "main")
        invoke("--action", "AddTask", "--task-id", "TASK-05", "--task-title", "NonSquash Merge Feature", "--files", "src/releaseme.js")
        invoke("--action", "SilentHandshake", "--task-id", "TASK-05", "--collaborator-id", "pc1-codex", "--git-branch")
        with open(os.path.join(ws, "src", "releaseme.js"), "a", encoding="utf-8") as f:
            f.write("export const nonSquashFeature = 42;\n")
        invoke("--action", "Checkpoint", "--task-id", "TASK-05", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-05): add nonSquashFeature", "--push")
        invoke("--action", "Done", "--task-id", "TASK-05", "--collaborator-id", "pc1-codex", "--push")
        invoke("--action", "SubmitReview", "--task-id", "TASK-05", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED")
        out_nosquash = invoke("--action", "MergeTask", "--task-id", "TASK-05", "--collaborator-id", "pc1-antigravity")
        assert "DELIVERY: COMPLETE" in out_nosquash
        assert "nonSquashFeature" in open(os.path.join(ws, "src", "releaseme.js"), encoding="utf-8").read()
        out_rev_merge = invoke("--action", "RevertTask", "--task-id", "TASK-05", "--collaborator-id", "pc1-antigravity", "--push")
        assert "REVERT_COMMIT:" in out_rev_merge
        assert "nonSquashFeature" not in open(os.path.join(ws, "src", "releaseme.js"), encoding="utf-8").read()

        print("[SELFTEST 24/42] Reopen reverted non-squash task, edit, approve & merge delivers BOTH original and follow-up changes (Must Fix #3)...")
        out_reopen_rev = invoke("--action", "ReopenTask", "--task-id", "TASK-05", "--collaborator-id", "pc1-codex")
        assert "[TASK_REOPENED]" in out_reopen_rev
        assert get_current_branch(ws) == "task/TASK-05-nonsquash-merge-feature"
        with open(os.path.join(ws, "src", "releaseme.js"), "a", encoding="utf-8") as f:
            f.write("export const reopenedFollowUp = 99;\n")
        invoke("--action", "Checkpoint", "--task-id", "TASK-05", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-05): add reopenedFollowUp", "--push")
        invoke("--action", "Done", "--task-id", "TASK-05", "--collaborator-id", "pc1-codex", "--push")
        invoke("--action", "SubmitReview", "--task-id", "TASK-05", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED")
        # Merge WITHOUT --squash: BOTH nonSquashFeature (42) and reopenedFollowUp (99) must be delivered to main and origin/main!
        out_remerged = invoke("--action", "MergeTask", "--task-id", "TASK-05", "--collaborator-id", "pc1-antigravity")
        assert "DELIVERY: COMPLETE" in out_remerged
        local_rel_content = open(os.path.join(ws, "src", "releaseme.js"), encoding="utf-8").read()
        remote_rel_content = run_git(remote_bare, "show", "main:src/releaseme.js").stdout
        assert "nonSquashFeature = 42" in local_rel_content and "reopenedFollowUp = 99" in local_rel_content, f"Incomplete local feature after reopen! {local_rel_content}"
        assert "nonSquashFeature = 42" in remote_rel_content and "reopenedFollowUp = 99" in remote_rel_content, f"Incomplete remote feature after reopen! {remote_rel_content}"

        print("[SELFTEST 25/42] Revert re-merged non-squash task cleanly (-m 1)...")
        out_rev_remerged = invoke("--action", "RevertTask", "--task-id", "TASK-05", "--collaborator-id", "pc1-antigravity", "--push")
        assert "REVERT_COMMIT:" in out_rev_remerged
        after_second_rev = open(os.path.join(ws, "src", "releaseme.js"), encoding="utf-8").read()
        assert "nonSquashFeature = 42" not in after_second_rev and "reopenedFollowUp = 99" not in after_second_rev

        print("[SELFTEST 26/42] Block RevertTask when another task claims a file committed earlier & later released (Round 6 Fix #1)...")
        # TASK-REL commits changes to BOTH src/a_rel.js and src/b_rel.js, then releases src/a_rel.js before finishing!
        with open(os.path.join(ws, "src", "a_rel.js"), "w", encoding="utf-8") as f:
            f.write("// initial a_rel\n")
        with open(os.path.join(ws, "src", "b_rel.js"), "w", encoding="utf-8") as f:
            f.write("// initial b_rel\n")
        run_git(ws, "add", "src/a_rel.js", "src/b_rel.js")
        run_git(ws, "commit", "-m", "chore: add a_rel and b_rel")
        run_git(ws, "push", "origin", "main")
        invoke("--action", "AddTask", "--task-id", "TASK-REL", "--task-title", "Commit Two Then Release One", "--files", "src/a_rel.js,src/b_rel.js")
        invoke("--action", "SilentHandshake", "--task-id", "TASK-REL", "--collaborator-id", "pc1-codex", "--git-branch")
        with open(os.path.join(ws, "src", "a_rel.js"), "a", encoding="utf-8") as f:
            f.write("export const fromTaskRelA = 1;\n")
        with open(os.path.join(ws, "src", "b_rel.js"), "a", encoding="utf-8") as f:
            f.write("export const fromTaskRelB = 2;\n")
        invoke("--action", "Checkpoint", "--task-id", "TASK-REL", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-REL): edit both a_rel and b_rel", "--push")
        # Now release src/a_rel.js so targetFiles only lists src/b_rel.js!
        invoke("--action", "ReleaseFiles", "--task-id", "TASK-REL", "--collaborator-id", "pc1-codex", "--files", "src/a_rel.js")
        invoke("--action", "Done", "--task-id", "TASK-REL", "--collaborator-id", "pc1-codex", "--push")
        invoke("--action", "SubmitReview", "--task-id", "TASK-REL", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED")
        out_merge_rel = invoke("--action", "MergeTask", "--task-id", "TASK-REL", "--collaborator-id", "pc1-antigravity")
        assert "DELIVERY: COMPLETE" in out_merge_rel
        # Now TASK-04 claims src/a_rel.js and edits it!
        invoke("--action", "SilentHandshake", "--task-id", "TASK-04", "--collaborator-id", "pc2-other", "--files", "src/a_rel.js")
        with open(os.path.join(ws, "src", "a_rel.js"), "a", encoding="utf-8") as f:
            f.write("// active edit by TASK-04 on released file\n")
        # Reverting TASK-REL must detect that the integration commit touches src/a_rel.js (even though targetFiles dropped it) and fail with FILE_CONFLICT_ERROR!
        err_rev_released = invoke("--action", "RevertTask", "--task-id", "TASK-REL", "--collaborator-id", "pc1-antigravity", expect_fail=True)
        assert "FILE_CONFLICT_ERROR" in err_rev_released
        assert "active edit by TASK-04 on released file" in open(os.path.join(ws, "src", "a_rel.js"), encoding="utf-8").read()
        invoke("--action", "ReleaseFiles", "--task-id", "TASK-04", "--collaborator-id", "pc2-other", "--files", "src/a_rel.js")
        run_git(ws, "checkout", "--", "src/a_rel.js")

        print("[SELFTEST 27/42] Block MergeTask/RevertTask from hijacking another active task's branch in shared checkout (Round 6 Fix #2)...")
        # Prepare TASK-A (approved and ready to merge) while TASK-B starts on its own branch in the shared checkout!
        with open(os.path.join(ws, "src", "feat_a.js"), "w", encoding="utf-8") as f:
            f.write("// feat_a initial\n")
        with open(os.path.join(ws, "src", "feat_b.js"), "w", encoding="utf-8") as f:
            f.write("// feat_b initial\n")
        run_git(ws, "add", "src/feat_a.js", "src/feat_b.js")
        run_git(ws, "commit", "-m", "chore: add feat_a and feat_b")
        run_git(ws, "push", "origin", "main")
        invoke("--action", "AddTask", "--task-id", "TASK-A", "--task-title", "Feature A", "--files", "src/feat_a.js")
        invoke("--action", "AddTask", "--task-id", "TASK-B", "--task-title", "Feature B", "--files", "src/feat_b.js")
        invoke("--action", "SilentHandshake", "--task-id", "TASK-A", "--collaborator-id", "pc1-codex", "--git-branch")
        with open(os.path.join(ws, "src", "feat_a.js"), "a", encoding="utf-8") as f:
            f.write("export const featA = true;\n")
        invoke("--action", "Checkpoint", "--task-id", "TASK-A", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-A): add featA", "--push")
        invoke("--action", "Done", "--task-id", "TASK-A", "--collaborator-id", "pc1-codex", "--push")
        invoke("--action", "SubmitReview", "--task-id", "TASK-A", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED")
        # Now TASK-A is 'done', so TASK-B starts on its own branch in the shared checkout!
        invoke("--action", "SilentHandshake", "--task-id", "TASK-B", "--collaborator-id", "pc1-codex", "--git-branch")
        assert get_current_branch(ws) == "task/TASK-B-feature-b"
        # 1) Calling MergeTask for TASK-A without --use-worktree must fail with BRANCH_CONTENTION_ERROR and keep checkout on task/TASK-B-feature-b!
        err_merge_contention = invoke("--action", "MergeTask", "--task-id", "TASK-A", "--collaborator-id", "pc1-antigravity", expect_fail=True)
        assert "BRANCH_CONTENTION_ERROR" in err_merge_contention
        assert get_current_branch(ws) == "task/TASK-B-feature-b"
        # 2) Calling MergeTask for TASK-A WITH --use-worktree integrates TASK-A onto main in a temp worktree while keeping local checkout on task/TASK-B-feature-b!
        out_merge_wt = invoke("--action", "MergeTask", "--task-id", "TASK-A", "--collaborator-id", "pc1-antigravity", "--use-worktree")
        assert "DELIVERY: COMPLETE" in out_merge_wt
        assert get_current_branch(ws) == "task/TASK-B-feature-b"
        # 3) TASK-B can now edit and Checkpoint on its branch without failing!
        with open(os.path.join(ws, "src", "feat_b.js"), "a", encoding="utf-8") as f:
            f.write("export const featB = true;\n")
        out_cp_b = invoke("--action", "Checkpoint", "--task-id", "TASK-B", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-B): add featB", "--push")
        assert "CHECKPOINT_COMMIT:" in out_cp_b
        # 4) Calling RevertTask for TASK-A without --use-worktree while TASK-B is active also blocks with BRANCH_CONTENTION_ERROR!
        err_rev_contention = invoke("--action", "RevertTask", "--task-id", "TASK-A", "--collaborator-id", "pc1-antigravity", expect_fail=True)
        assert "BRANCH_CONTENTION_ERROR" in err_rev_contention
        assert get_current_branch(ws) == "task/TASK-B-feature-b"
        # Finish TASK-B so shared checkout is free
        invoke("--action", "Done", "--task-id", "TASK-B", "--collaborator-id", "pc1-codex", "--push")
        invoke("--action", "SubmitReview", "--task-id", "TASK-B", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED")
        run_git(ws, "checkout", "main")

        print("[SELFTEST 28/42] Approved rename (a -> c) with old path released deletes source file on local & remote main (Round 7 Fix)...")
        with open(os.path.join(ws, "src", "rename_old.js"), "w", encoding="utf-8") as f:
            f.write("export const renamedSymbol = 'preserved across rename';\n")
        run_git(ws, "add", "src/rename_old.js")
        run_git(ws, "commit", "-m", "chore: add rename_old.js")
        run_git(ws, "push", "origin", "main")
        invoke("--action", "AddTask", "--task-id", "TASK-RENAME", "--task-title", "Rename Old To New", "--files", "src/rename_old.js,src/rename_new.js")
        invoke("--action", "SilentHandshake", "--task-id", "TASK-RENAME", "--collaborator-id", "pc1-codex", "--git-branch")
        # Rename src/rename_old.js -> src/rename_new.js
        old_content = open(os.path.join(ws, "src", "rename_old.js"), encoding="utf-8").read()
        os.remove(os.path.join(ws, "src", "rename_old.js"))
        with open(os.path.join(ws, "src", "rename_new.js"), "w", encoding="utf-8") as f:
            f.write(old_content)
        invoke("--action", "Checkpoint", "--task-id", "TASK-RENAME", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-RENAME): rename rename_old.js to rename_new.js", "--push")
        # Release the old path after checkpointing (exact reproduction from Round 7 audit!)
        invoke("--action", "ReleaseFiles", "--task-id", "TASK-RENAME", "--collaborator-id", "pc1-codex", "--files", "src/rename_old.js")
        invoke("--action", "Done", "--task-id", "TASK-RENAME", "--collaborator-id", "pc1-codex", "--push")
        invoke("--action", "SubmitReview", "--task-id", "TASK-RENAME", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED")
        out_merge_ren = invoke("--action", "MergeTask", "--task-id", "TASK-RENAME", "--collaborator-id", "pc1-antigravity")
        assert "DELIVERY: COMPLETE" in out_merge_ren
        # Verify source file src/rename_old.js is ABSENT on both local main and remote origin/main, and destination src/rename_new.js is PRESENT!
        assert not os.path.exists(os.path.join(ws, "src", "rename_old.js")), "Old renamed path still exists on local main!"
        assert os.path.exists(os.path.join(ws, "src", "rename_new.js")), "New renamed path missing on local main!"
        assert run_git(remote_bare, "cat-file", "-e", "main:src/rename_old.js", check=False).returncode != 0, "Old renamed path still exists on remote main!"
        assert run_git(remote_bare, "cat-file", "-e", "main:src/rename_new.js", check=False).returncode == 0, "New renamed path missing on remote main!"
        # Also test RevertTask + MergeTask --reapply on TASK-RENAME to verify manual reapplication path also deletes src/rename_old.js!
        invoke("--action", "RevertTask", "--task-id", "TASK-RENAME", "--collaborator-id", "pc1-antigravity", "--push")
        assert os.path.exists(os.path.join(ws, "src", "rename_old.js")) and not os.path.exists(os.path.join(ws, "src", "rename_new.js"))
        out_reapply_ren = invoke("--action", "MergeTask", "--task-id", "TASK-RENAME", "--collaborator-id", "pc1-antigravity", "--reapply")
        assert "DELIVERY: COMPLETE" in out_reapply_ren
        assert not os.path.exists(os.path.join(ws, "src", "rename_old.js")), "Old renamed path still exists after --reapply!"
        assert os.path.exists(os.path.join(ws, "src", "rename_new.js")), "New renamed path missing after --reapply!"
        assert run_git(remote_bare, "cat-file", "-e", "main:src/rename_old.js", check=False).returncode != 0, "Old renamed path still exists on remote main after --reapply!"
        assert run_git(remote_bare, "cat-file", "-e", "main:src/rename_new.js", check=False).returncode == 0

        print("[SELFTEST 29/42] Executable permission bit (100644 -> 100755) mode-only change & --reapply delivery (Round 8 Fix #2)...")
        script_path = os.path.join(ws, "src", "script.sh")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho hello\n")
        os.chmod(script_path, 0o644)
        run_git(ws, "add", "src/script.sh")
        run_git(ws, "update-index", "--chmod=-x", "--", "src/script.sh")
        run_git(ws, "commit", "-m", "chore: add non-executable script.sh (100644)")
        run_git(ws, "push", "origin", "main")
        assert get_tree_entry(ws, "main", "src/script.sh")["mode"] == "100644"
        invoke("--action", "AddTask", "--task-id", "TASK-EXEC", "--task-title", "Make Script Executable", "--files", "src/script.sh")
        invoke("--action", "SilentHandshake", "--task-id", "TASK-EXEC", "--collaborator-id", "pc1-codex", "--git-branch")
        # Simulate Windows (core.filemode=false) where executable bit is staged in index via update-index --chmod=+x!
        run_git(ws, "config", "core.filemode", "false")
        os.chmod(script_path, 0o755)
        run_git(ws, "update-index", "--chmod=+x", "--", "src/script.sh")
        out_cp_exec = invoke("--action", "Checkpoint", "--task-id", "TASK-EXEC", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-EXEC): chmod +x script.sh", "--push")
        assert "new commit" in out_cp_exec
        assert get_tree_entry(ws, "HEAD", "src/script.sh")["mode"] == "100755"
        invoke("--action", "Done", "--task-id", "TASK-EXEC", "--collaborator-id", "pc1-codex", "--push")
        invoke("--action", "SubmitReview", "--task-id", "TASK-EXEC", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED")
        out_merge_exec = invoke("--action", "MergeTask", "--task-id", "TASK-EXEC", "--collaborator-id", "pc1-antigravity")
        assert "LOCAL_COMMIT: NOOP" not in out_merge_exec and "DELIVERY: COMPLETE" in out_merge_exec
        assert get_tree_entry(ws, "main", "src/script.sh")["mode"] == "100755", "Local main lost 100755 executable mode!"
        assert get_tree_entry(remote_bare, "main", "src/script.sh")["mode"] == "100755", "Remote main retained 100644 instead of 100755!"
        # Also test RevertTask + MergeTask --reapply on mode-only change!
        invoke("--action", "RevertTask", "--task-id", "TASK-EXEC", "--collaborator-id", "pc1-antigravity", "--push")
        assert get_tree_entry(ws, "main", "src/script.sh")["mode"] == "100644"
        out_reapply_exec = invoke("--action", "MergeTask", "--task-id", "TASK-EXEC", "--collaborator-id", "pc1-antigravity", "--reapply")
        assert "LOCAL_COMMIT: NOOP" not in out_reapply_exec and "DELIVERY: COMPLETE" in out_reapply_exec
        assert get_tree_entry(ws, "main", "src/script.sh")["mode"] == "100755"
        assert get_tree_entry(remote_bare, "main", "src/script.sh")["mode"] == "100755"

        print("[SELFTEST 30/42] Crash recovery: kill after merge push & kill after revert commit reconcile cleanly (Round 8 Fix #1)...")
        with open(os.path.join(ws, "src", "crash_test.js"), "w", encoding="utf-8") as f:
            f.write("// initial crash_test\n")
        run_git(ws, "add", "src/crash_test.js")
        run_git(ws, "commit", "-m", "chore: add crash_test.js")
        run_git(ws, "push", "origin", "main")
        invoke("--action", "AddTask", "--task-id", "TASK-CRASH", "--task-title", "Crash Recovery Feature", "--files", "src/crash_test.js")
        invoke("--action", "SilentHandshake", "--task-id", "TASK-CRASH", "--collaborator-id", "pc1-codex", "--git-branch")
        with open(os.path.join(ws, "src", "crash_test.js"), "a", encoding="utf-8") as f:
            f.write("export const crashFeature = 777;\n")
        invoke("--action", "Checkpoint", "--task-id", "TASK-CRASH", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-CRASH): add crashFeature", "--push")
        invoke("--action", "Done", "--task-id", "TASK-CRASH", "--collaborator-id", "pc1-codex", "--push")
        invoke("--action", "SubmitReview", "--task-id", "TASK-CRASH", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED")
        # Snapshot state-board.json BEFORE MergeTask to simulate process killed after git merge + push before ledger write!
        board_file = os.path.join(ws, ".collab", "state-board.json")
        pre_merge_board_bytes = open(board_file, "rb").read()
        out_m_first = invoke("--action", "MergeTask", "--task-id", "TASK-CRASH", "--collaborator-id", "pc1-antigravity")
        assert "DELIVERY: COMPLETE" in out_m_first
        # Simulate crash before save_state() by restoring pre_merge_board_bytes!
        with open(board_file, "wb") as bf:
            bf.write(pre_merge_board_bytes)
        # Retry MergeTask: must reconcile against Git, NOT report NOOP, preserve mergedCommit, and allow RevertTask!
        out_m_retry = invoke("--action", "MergeTask", "--task-id", "TASK-CRASH", "--collaborator-id", "pc1-antigravity")
        assert "LOCAL_COMMIT: NOOP" not in out_m_retry and "DELIVERY: COMPLETE" in out_m_retry
        # Now snapshot state-board.json BEFORE RevertTask to simulate process killed after git revert commit before ledger write!
        pre_revert_board_bytes = open(board_file, "rb").read()
        out_r_first = invoke("--action", "RevertTask", "--task-id", "TASK-CRASH", "--collaborator-id", "pc1-antigravity")
        assert "REVERT_COMMIT:" in out_r_first
        with open(board_file, "wb") as bf:
            bf.write(pre_revert_board_bytes)
        # Retry RevertTask after simulated kill: must reconcile the revert commit on main, update ledger to 'reverted', and succeed!
        out_r_retry = invoke("--action", "RevertTask", "--task-id", "TASK-CRASH", "--collaborator-id", "pc1-antigravity", "--push")
        assert "REVERT_COMMIT:" in out_r_retry
        board_after_r = safe_read_json(board_file)
        t_crash = next(x for x in board_after_r["tasks"] if x["taskId"] == "TASK-CRASH")
        assert t_crash["status"] == "reverted" and t_crash["reverted"] is True

        print("[SELFTEST 31/42] Handoff crash recovery reconstructs missing handoff packet sidecar (Round 8 Fix #3)...")
        handoff_pkt_file = os.path.join(ws, ".collab", "handoffs", "TASK-01-handoff.json")
        if os.path.exists(handoff_pkt_file):
            os.remove(handoff_pkt_file)
        assert not os.path.exists(handoff_pkt_file)
        invoke("--action", "Status")
        assert os.path.exists(handoff_pkt_file), "Missing handoff packet was not reconstructed during reconciliation!"

        print("[SELFTEST 32/42] Unrelated commit after interrupted RevertTask is NOT recorded as revert & interrupted squash staging recovers (Round 9 P1/P2)...")
        # 1) Reapply TASK-CRASH so it is merged on main
        out_reapply_crash = invoke("--action", "MergeTask", "--task-id", "TASK-CRASH", "--collaborator-id", "pc1-antigravity", "--reapply", "--squash")
        assert "DELIVERY: COMPLETE" in out_reapply_crash
        board_now = safe_read_json(board_file)
        t_c = next(x for x in board_now["tasks"] if x["taskId"] == "TASK-CRASH")
        mc_crash = t_c["mergedCommit"]
        # Simulate RevertTask interrupted BEFORE git revert ran (op-journal written, then unrelated commit made on main!)
        write_op_journal(os.path.join(ws, ".collab"), {
            "op": "RevertTask",
            "taskId": "TASK-CRASH",
            "caller": "pc1-antigravity",
            "baseBranch": "main",
            "remote": "origin",
            "origMainSha": get_head_commit(ws, "main"),
            "commitToRevert": mc_crash,
            "isMergeCommit": False,
            "taskFiles": ["src/crash_test.js"],
        })
        with open(os.path.join(ws, "src", "unowned.js"), "a", encoding="utf-8") as f:
            f.write("// unrelated main commit while RevertTask journal was pending\n")
        run_git(ws, "add", "src/unowned.js")
        run_git(ws, "commit", "-m", "chore: unrelated commit on main")
        run_git(ws, "push", "origin", "main")
        # Run Status: recovery must NOT mark TASK-CRASH as reverted because the commit on main was unrelated and crashFeature is still installed!
        out_st_unrel = invoke("--action", "Status")
        assert "RECOVERY_STATUS: UNRESOLVED" in out_st_unrel
        board_after_unrel = safe_read_json(board_file)
        t_c2 = next(x for x in board_after_unrel["tasks"] if x["taskId"] == "TASK-CRASH")
        assert t_c2["status"] == "done" and not t_c2.get("reverted") and t_c2["mergedCommit"] == mc_crash, "Unrelated commit was falsely marked as revert!"
        # Normal ResolveRecovery without --abandon-recovery must refuse while history moved outside the journaled op!
        err_premature_res = invoke("--action", "ResolveRecovery", expect_fail=True)
        assert "UNRESOLVED_RECOVERY_ERROR" in err_premature_res
        assert os.path.exists(os.path.join(ws, ".collab", "op-journal.json")), "Failed ResolveRecovery must retain op-journal.json!"
        # Explicit --abandon-recovery archives the journal and reports REMAINING_REPAIR_REQUIRED:
        out_abandon = invoke("--action", "ResolveRecovery", "--abandon-recovery")
        assert "[RECOVERY_ABANDONED]" in out_abandon and "JOURNAL_ARCHIVED:" in out_abandon and "REMAINING_REPAIR_REQUIRED:" in out_abandon
        assert "[RECOVERY_RESOLVED]" not in out_abandon
        out_real_rev = invoke("--action", "RevertTask", "--task-id", "TASK-CRASH", "--collaborator-id", "pc1-antigravity", "--push")
        assert "REVERT_COMMIT:" in out_real_rev
        assert "crashFeature" not in open(os.path.join(ws, "src", "crash_test.js"), encoding="utf-8").read()

        # 2) Simulate MergeTask --squash interrupted AFTER staging changes on main but BEFORE committing!
        invoke("--action", "ReopenTask", "--task-id", "TASK-CRASH", "--collaborator-id", "pc1-codex")
        with open(os.path.join(ws, "src", "crash_test.js"), "a", encoding="utf-8") as f:
            f.write("export const squashStageRecovery = 888;\n")
        invoke("--action", "Checkpoint", "--task-id", "TASK-CRASH", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-CRASH): add squashStageRecovery", "--push")
        invoke("--action", "Done", "--task-id", "TASK-CRASH", "--collaborator-id", "pc1-codex", "--push")
        invoke("--action", "SubmitReview", "--task-id", "TASK-CRASH", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED")
        run_git(ws, "checkout", "main")
        orig_m_sq = get_head_commit(ws, "main")
        write_op_journal(os.path.join(ws, ".collab"), {
            "op": "MergeTask",
            "taskId": "TASK-CRASH",
            "baseBranch": "main",
            "remote": "origin",
            "origMainSha": orig_m_sq,
            "effectiveBaseCommit": t_c2.get("baseCommit", ""),
            "sourceRef": "task/TASK-CRASH-crash-recovery-feature",
            "taskBranch": "task/TASK-CRASH-crash-recovery-feature",
            "taskFiles": ["src/crash_test.js"],
            "mergeType": "squash",
            "wasReverted": True,
        })
        # Stage changes on main without committing (simulating kill after staging, before commit!)
        with open(os.path.join(ws, "src", "crash_test.js"), "a", encoding="utf-8") as f:
            f.write("export const crashFeature = 777;\nexport const squashStageRecovery = 888;\n")
        run_git(ws, "add", "src/crash_test.js")
        assert run_git(ws, "status", "--porcelain", "-uno").stdout.strip() != ""
        # Retry MergeTask --squash: recovery must clean up the interrupted staged phase before clearing the journal so retry succeeds without DIRTY_WORKTREE_ERROR!
        out_sq_retry = invoke("--action", "MergeTask", "--task-id", "TASK-CRASH", "--collaborator-id", "pc1-antigravity", "--squash")
        assert "DELIVERY: COMPLETE" in out_sq_retry
        assert "squashStageRecovery = 888" in open(os.path.join(ws, "src", "crash_test.js"), encoding="utf-8").read()

        print("[SELFTEST 33/42] Handoff journal recovery saves ownership AND restores context fields for CheckOrContinue (Round 9 P1 & Round 11 P2 #3)...")
        # Simulate AtomicHandoff killed immediately after its first journal write (with new --last-prompt, --changes-summary, --remaining-todo)
        pkt_sim = {
            "taskId": "TASK-04",
            "title": "Claim Released File",
            "handoffFrom": "pc2-other",
            "handoffChain": ["pc2-other"],
            "handoffTo": "pc1-codex",
            "reason": "Simulated early-journal interrupted handoff",
            "lockPreserved": True,
            "lockedFiles": [],
            "gitBranch": "task/TASK-04-claim-released-file",
            "handoffCommit": "",
            "remoteBackupStatus": "UNPUSHED",
            "progressAtHandoff": 15,
            "lastUserPrompt": "Finish auth token refresh",
            "changesSummary": "Added token parser helper",
            "remainingTodo": "Wire refresh retry and unit tests",
            "timestampUtc": now_utc(),
        }
        write_op_journal(os.path.join(ws, ".collab"), {
            "op": "AtomicHandoff",
            "phase": "pre_handoff_git",
            "taskId": "TASK-04",
            "prevOwner": "pc2-other",
            "chain": ["pc2-other"],
            "toAgent": "pc1-codex",
            "targetClusterId": "pc1-local-cluster",
            "heldFiles": [],
            "gitBranch": "",
            "packet": pkt_sim,
        })
        out_coc_rec = invoke("--action", "CheckOrContinue", "--task-id", "TASK-04", "--collaborator-id", "pc1-codex")
        assert "ACTION: CONTINUE_APPROVED" in out_coc_rec
        assert "LAST_USER_PROMPT: Finish auth token refresh" in out_coc_rec, f"Missing lastUserPrompt after handoff recovery: {out_coc_rec}"
        assert "CHANGES_MADE_SO_FAR: Added token parser helper" in out_coc_rec, f"Missing changesSummary after handoff recovery: {out_coc_rec}"
        assert "REMAINING_TODO: Wire refresh retry and unit tests" in out_coc_rec, f"Missing remainingTodo after handoff recovery: {out_coc_rec}"
        b_after_h = safe_read_json(board_file)
        t4_rec = next(x for x in b_after_h["tasks"] if x["taskId"] == "TASK-04")
        assert t4_rec["assignedTo"] == "pc1-codex" and t4_rec["lastUserPrompt"] == "Finish auth token refresh"
        assert os.path.exists(os.path.join(ws, ".collab", "handoffs", "TASK-04-handoff.json"))

        print("[SELFTEST 34/42] Post-interruption edits preserved by Status (Round 10 P1 #1)...")
        run_git(ws, "checkout", "main")
        orig_m_r10 = get_head_commit(ws, "main")
        orig_crash_content = open(os.path.join(ws, "src", "crash_test.js"), encoding="utf-8").read()
        # Reproduction 1A: Kill MergeTask just after journal creation (before Git stages anything), write a new unsaved edit, and run Status!
        write_op_journal(os.path.join(ws, ".collab"), {
            "op": "MergeTask",
            "phase": "staging",
            "taskId": "TASK-CRASH",
            "baseBranch": "main",
            "remote": "origin",
            "origMainSha": orig_m_r10,
            "effectiveBaseCommit": t_c2.get("baseCommit", ""),
            "sourceRef": "task/TASK-CRASH-crash-recovery-feature",
            "taskBranch": "task/TASK-CRASH-crash-recovery-feature",
            "taskFiles": ["src/crash_test.js"],
            "mergeType": "squash",
            "wasReverted": False,
            "integrationRoot": ws,
            "usedTempWorktree": False,
            "origState": capture_tree_state(ws, orig_m_r10, ["src/crash_test.js"]),
            "expectedResultState": {},
        })
        unsaved_edit_str = orig_crash_content + "// post-interruption unsaved developer edit\n"
        with open(os.path.join(ws, "src", "crash_test.js"), "w", encoding="utf-8") as f:
            f.write(unsaved_edit_str)
        out_st_1a = invoke("--action", "Status")
        assert "RECOVERY_STATUS: UNRESOLVED" in out_st_1a, f"Expected RECOVERY_STATUS: UNRESOLVED in Status output, got: {out_st_1a}"
        assert os.path.exists(os.path.join(ws, ".collab", "op-journal.json")), "op-journal.json must be retained when recovery is unresolved!"
        assert open(os.path.join(ws, "src", "crash_test.js"), encoding="utf-8").read() == unsaved_edit_str, "Status overwrote post-interruption unsaved edit!"

        # Reproduction 1B: Commit a new correction on main after the interruption; Status must NOT check out old origMainSha contents!
        committed_correction_str = orig_crash_content + "// committed correction after interruption\n"
        with open(os.path.join(ws, "src", "crash_test.js"), "w", encoding="utf-8") as f:
            f.write(committed_correction_str)
        run_git(ws, "add", "src/crash_test.js")
        run_git(ws, "commit", "-m", "fix: committed correction after interrupted MergeTask")
        run_git(ws, "push", "origin", "main")
        head_after_corr = get_head_commit(ws, "main")
        out_st_1b = invoke("--action", "Status")
        assert "RECOVERY_STATUS: UNRESOLVED" in out_st_1b, f"Expected RECOVERY_STATUS: UNRESOLVED in Status output, got: {out_st_1b}"
        assert os.path.exists(os.path.join(ws, ".collab", "op-journal.json")), "op-journal.json must be retained when history advanced outside journaled op!"
        assert open(os.path.join(ws, "src", "crash_test.js"), encoding="utf-8").read() == committed_correction_str, "Status overwrote committed correction!"
        assert run_git(ws, "status", "--porcelain", "-uno").stdout.strip() == "", "Status staged a regression against HEAD!"
        assert get_head_commit(ws, "HEAD") == head_after_corr
        clear_op_journal(os.path.join(ws, ".collab"))

        print("[SELFTEST 35/42] Valid revert preserving later independent edits in the same file recovers cleanly (Round 10 P1 #2)...")
        with open(os.path.join(ws, "src", "samefile_rev.ini"), "w", encoding="utf-8") as f:
            f.write("feature=off\nline2=keep\nline3=keep\nline4=keep\nother=old\n")
        run_git(ws, "add", "src/samefile_rev.ini")
        run_git(ws, "commit", "-m", "chore: add samefile_rev.ini")
        run_git(ws, "push", "origin", "main")
        invoke("--action", "AddTask", "--task-id", "TASK-SAME-REV", "--task-title", "Toggle feature on", "--files", "src/samefile_rev.ini")
        invoke("--action", "SilentHandshake", "--task-id", "TASK-SAME-REV", "--collaborator-id", "pc1-codex", "--git-branch")
        with open(os.path.join(ws, "src", "samefile_rev.ini"), "w", encoding="utf-8") as f:
            f.write("feature=on\nline2=keep\nline3=keep\nline4=keep\nother=old\n")
        invoke("--action", "Checkpoint", "--task-id", "TASK-SAME-REV", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-SAME-REV): turn feature on", "--push")
        invoke("--action", "Done", "--task-id", "TASK-SAME-REV", "--collaborator-id", "pc1-codex", "--push")
        invoke("--action", "SubmitReview", "--task-id", "TASK-SAME-REV", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED")
        invoke("--action", "MergeTask", "--task-id", "TASK-SAME-REV", "--collaborator-id", "pc1-antigravity", "--squash")
        b_sr = safe_read_json(board_file)
        mc_sr = next(x for x in b_sr["tasks"] if x["taskId"] == "TASK-SAME-REV")["mergedCommit"]

        # Later independent commit changes other=old -> other=new far away in the same file
        with open(os.path.join(ws, "src", "samefile_rev.ini"), "w", encoding="utf-8") as f:
            f.write("feature=on\nline2=keep\nline3=keep\nline4=keep\nother=new\n")
        run_git(ws, "add", "src/samefile_rev.ini")
        run_git(ws, "commit", "-m", "chore: independent update other=new")
        run_git(ws, "push", "origin", "main")
        orig_m_sr = get_head_commit(ws, "main")

        # Simulate RevertTask killed immediately AFTER git revert committed (preserving other=new while reverting feature=on -> feature=off)
        rev_p_sr = run_git(ws, "rev-list", "--parents", "-n", "1", mc_sr).stdout.strip().split()[1]
        _, _, exp_rev_sr = compute_3way_merged_files(ws, orig_m_sr, mc_sr, rev_p_sr, ["src/samefile_rev.ini"])
        write_op_journal(os.path.join(ws, ".collab"), {
            "op": "RevertTask",
            "phase": "staging",
            "taskId": "TASK-SAME-REV",
            "caller": "pc1-antigravity",
            "baseBranch": "main",
            "remote": "origin",
            "origMainSha": orig_m_sr,
            "commitToRevert": mc_sr,
            "isMergeCommit": False,
            "taskFiles": ["src/samefile_rev.ini"],
            "integrationRoot": ws,
            "usedTempWorktree": False,
            "origState": capture_tree_state(ws, orig_m_sr, ["src/samefile_rev.ini"]),
            "expectedResultState": capture_expected_3way_state(exp_rev_sr, ["src/samefile_rev.ini"], ws),
        })
        run_git(ws, "revert", "--no-edit", mc_sr)
        # Run Status: recovery must accept the valid revert, keep feature=off and other=new on disk, and mark TASK-SAME-REV reverted!
        invoke("--action", "Status")
        sr_disk = open(os.path.join(ws, "src", "samefile_rev.ini"), encoding="utf-8").read()
        assert "feature=off" in sr_disk and "other=new" in sr_disk, f"Revert recovery corrupted same-file independent edit: {sr_disk}"
        assert run_git(ws, "status", "--porcelain", "-uno").stdout.strip() == "", "Revert recovery dirtied working tree!"
        b_sr_after = safe_read_json(board_file)
        t_sr_after = next(x for x in b_sr_after["tasks"] if x["taskId"] == "TASK-SAME-REV")
        assert t_sr_after["status"] == "reverted" and t_sr_after.get("reverted")
        out_sr_retry = invoke("--action", "RevertTask", "--task-id", "TASK-SAME-REV", "--collaborator-id", "pc1-antigravity", "--push")
        assert "REVERT_COMMIT:" in out_sr_retry

        print("[SELFTEST 36/42] Interrupted isolated-worktree integration targets temp worktree & leaves shared checkout alone (Round 10 P2 #3)...")
        with open(os.path.join(ws, "src", "wt_a.js"), "w", encoding="utf-8") as f:
            f.write("// initial wt_a\n")
        with open(os.path.join(ws, "src", "wt_b.js"), "w", encoding="utf-8") as f:
            f.write("// initial wt_b\n")
        run_git(ws, "add", "src/wt_a.js", "src/wt_b.js")
        run_git(ws, "commit", "-m", "chore: add wt_a and wt_b")
        run_git(ws, "push", "origin", "main")
        invoke("--action", "AddTask", "--task-id", "TASK-WT-A", "--task-title", "Isolated Worktree Feature A", "--files", "src/wt_a.js")
        invoke("--action", "AddTask", "--task-id", "TASK-WT-B", "--task-title", "Shared Checkout Active Task B", "--files", "src/wt_b.js")
        invoke("--action", "SilentHandshake", "--task-id", "TASK-WT-A", "--collaborator-id", "pc1-codex", "--git-branch")
        with open(os.path.join(ws, "src", "wt_a.js"), "w", encoding="utf-8") as f:
            f.write("// initial wt_a\nexport const wtA = 'delivered';\n")
        invoke("--action", "Checkpoint", "--task-id", "TASK-WT-A", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-WT-A): add wtA", "--push")
        invoke("--action", "Done", "--task-id", "TASK-WT-A", "--collaborator-id", "pc1-codex", "--push")
        invoke("--action", "SubmitReview", "--task-id", "TASK-WT-A", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED")

        # Start TASK-WT-B in the shared checkout and make an unsaved edit on TASK-WT-B's branch
        run_git(ws, "checkout", "main")
        invoke("--action", "SilentHandshake", "--task-id", "TASK-WT-B", "--collaborator-id", "pc1-antigravity", "--git-branch")
        wt_b_unsaved = "// initial wt_b\nexport const wtB_in_progress = true;\n"
        with open(os.path.join(ws, "src", "wt_b.js"), "w", encoding="utf-8") as f:
            f.write(wt_b_unsaved)

        # Simulate MergeTask --use-worktree for TASK-WT-A killed after staging in a temporary worktree on main
        sim_tmp_wt = tempfile.mkdtemp(prefix="collab_integrate_wt_", dir=fast_tmp)
        shutil.rmtree(sim_tmp_wt, ignore_errors=True)
        orig_m_wt = get_head_commit(ws, "main")
        run_git(ws, "worktree", "add", sim_tmp_wt, "main")
        run_git(sim_tmp_wt, "merge", "--squash", "task/TASK-WT-A-isolated-worktree-feature-a")
        b_wta = safe_read_json(board_file)
        t_wta = next(x for x in b_wta["tasks"] if x["taskId"] == "TASK-WT-A")
        _, _, exp_wta_3way = compute_3way_merged_files(
            sim_tmp_wt, orig_m_wt, t_wta["baseCommit"], "task/TASK-WT-A-isolated-worktree-feature-a", ["src/wt_a.js"]
        )
        write_op_journal(os.path.join(ws, ".collab"), {
            "op": "MergeTask",
            "phase": "staging",
            "taskId": "TASK-WT-A",
            "baseBranch": "main",
            "remote": "origin",
            "origMainSha": orig_m_wt,
            "effectiveBaseCommit": t_wta["baseCommit"],
            "sourceRef": "task/TASK-WT-A-isolated-worktree-feature-a",
            "taskBranch": "task/TASK-WT-A-isolated-worktree-feature-a",
            "taskFiles": ["src/wt_a.js"],
            "mergeType": "squash",
            "wasReverted": False,
            "integrationRoot": sim_tmp_wt,
            "usedTempWorktree": True,
            "origState": capture_tree_state(sim_tmp_wt, orig_m_wt, ["src/wt_a.js"]),
            "expectedResultState": capture_expected_3way_state(exp_wta_3way, ["src/wt_a.js"], sim_tmp_wt),
        })
        # Retry MergeTask --squash --use-worktree: recovery must dispose of sim_tmp_wt, preserve TASK-WT-B's shared checkout, and complete delivery!
        out_wta_retry = invoke("--action", "MergeTask", "--task-id", "TASK-WT-A", "--collaborator-id", "pc1-antigravity", "--squash", "--use-worktree")
        assert "DELIVERY: COMPLETE" in out_wta_retry
        assert not os.path.exists(sim_tmp_wt), "Abandoned temporary integration worktree was not removed!"
        assert get_current_branch(ws) == "task/TASK-WT-B-shared-checkout-active-task-b", "Shared checkout branch was disturbed!"
        assert open(os.path.join(ws, "src", "wt_b.js"), encoding="utf-8").read() == wt_b_unsaved, "Shared checkout unsaved edit was disturbed!"
        # Clean up TASK-WT-B so subsequent tests on main have a clean tree
        run_git(ws, "checkout", "--", "src/wt_b.js")
        run_git(ws, "checkout", "main")
        invoke("--action", "ReleaseFiles", "--task-id", "TASK-WT-B", "--collaborator-id", "pc1-antigravity", "--files", "src/wt_b.js")
        b_clean_wtb = safe_read_json(board_file)
        for tx in b_clean_wtb["tasks"]:
            if tx["taskId"] == "TASK-WT-B":
                tx["status"] = "done"
                tx["ownedFiles"] = []
        atomic_write_json(board_file, b_clean_wtb)

        print("[SELFTEST 37/42] Temporary-worktree recovery preserves ignored files (.gitignore) created after interruption (Round 10/11 P1 #1)...")
        with open(os.path.join(ws, ".gitignore"), "a", encoding="utf-8") as f:
            f.write("ignored-note.txt\n")
        run_git(ws, "add", ".gitignore")
        run_git(ws, "commit", "-m", "chore: ignore ignored-note.txt")
        run_git(ws, "push", "origin", "main")
        invoke("--action", "RevertTask", "--task-id", "TASK-WT-A", "--collaborator-id", "pc1-antigravity", "--push")
        invoke("--action", "ReopenTask", "--task-id", "TASK-WT-A", "--collaborator-id", "pc1-codex")
        with open(os.path.join(ws, "src", "wt_a.js"), "a", encoding="utf-8") as f:
            f.write("export const wtA2 = 'round11';\n")
        invoke("--action", "Checkpoint", "--task-id", "TASK-WT-A", "--collaborator-id", "pc1-codex", "--commit-message", "feat(TASK-WT-A): add wtA2", "--push")
        invoke("--action", "Done", "--task-id", "TASK-WT-A", "--collaborator-id", "pc1-codex", "--push")
        invoke("--action", "SubmitReview", "--task-id", "TASK-WT-A", "--collaborator-id", "pc1-antigravity", "--review-verdict", "APPROVED")
        # Switch shared checkout to TASK-WT-B's branch and simulate interrupted isolated squash merge for TASK-WT-A
        run_git(ws, "checkout", "task/TASK-WT-B-shared-checkout-active-task-b")
        sim_tmp_wt2 = tempfile.mkdtemp(prefix="collab_integrate_wt_", dir=fast_tmp)
        shutil.rmtree(sim_tmp_wt2, ignore_errors=True)
        orig_m_wt2 = get_head_commit(ws, "main")
        run_git(ws, "worktree", "add", sim_tmp_wt2, "main")
        run_git(sim_tmp_wt2, "merge", "--squash", "task/TASK-WT-A-isolated-worktree-feature-a")
        b_wta2 = safe_read_json(board_file)
        t_wta2 = next(x for x in b_wta2["tasks"] if x["taskId"] == "TASK-WT-A")
        _, _, exp_wta2_3way = compute_3way_merged_files(
            sim_tmp_wt2, orig_m_wt2, t_wta2["baseCommit"], "task/TASK-WT-A-isolated-worktree-feature-a", ["src/wt_a.js"]
        )
        write_op_journal(os.path.join(ws, ".collab"), {
            "op": "MergeTask",
            "phase": "staging",
            "taskId": "TASK-WT-A",
            "baseBranch": "main",
            "remote": "origin",
            "origMainSha": orig_m_wt2,
            "effectiveBaseCommit": t_wta2["baseCommit"],
            "sourceRef": "task/TASK-WT-A-isolated-worktree-feature-a",
            "taskBranch": "task/TASK-WT-A-isolated-worktree-feature-a",
            "taskFiles": ["src/wt_a.js"],
            "mergeType": "squash",
            "wasReverted": False,
            "integrationRoot": sim_tmp_wt2,
            "usedTempWorktree": True,
            "origState": capture_tree_state(sim_tmp_wt2, orig_m_wt2, ["src/wt_a.js"]),
            "expectedResultState": capture_expected_3way_state(exp_wta2_3way, ["src/wt_a.js"], sim_tmp_wt2),
        })
        # Create ignored-note.txt inside the temporary worktree after the interruption!
        ignored_note_path = os.path.join(sim_tmp_wt2, "ignored-note.txt")
        with open(ignored_note_path, "w", encoding="utf-8") as f:
            f.write("important local developer note in ignored file\n")
        out_st_ign = invoke("--action", "Status")
        assert "RECOVERY_STATUS: UNRESOLVED" in out_st_ign, f"Status did not report UNRESOLVED for ignored file in temp worktree: {out_st_ign}"
        assert os.path.exists(ignored_note_path), "Status deleted ignored-note.txt in temporary worktree!"
        assert os.path.exists(os.path.join(ws, ".collab", "op-journal.json")), "Status cleared journal despite ignored file in temp worktree!"

        print("[SELFTEST 38/42] Retrying MergeTask while recovery is unresolved stops before overwriting retained journal (Round 11 P2 #2)...")
        err_retry_unres = invoke("--action", "MergeTask", "--task-id", "TASK-WT-A", "--collaborator-id", "pc1-antigravity", "--squash", "--use-worktree", expect_fail=True)
        assert "UNRESOLVED_RECOVERY_ERROR" in err_retry_unres, f"Expected UNRESOLVED_RECOVERY_ERROR, got: {err_retry_unres}"
        j_retained = safe_read_json(os.path.join(ws, ".collab", "op-journal.json"))
        assert j_retained.get("integrationRoot") == sim_tmp_wt2, f"MergeTask retry overwrote retained journal integrationRoot! Got: {j_retained}"
        out_st_after_retry = invoke("--action", "Status")
        assert "RECOVERY_STATUS: UNRESOLVED" in out_st_after_retry and os.path.exists(os.path.join(ws, ".collab", "op-journal.json"))

        print("[SELFTEST 39/42] ResolveRecovery refuses while blocking worktree is unresolved, then succeeds once cleaned (Round 12 P2 #1)...")
        err_res_wt = invoke("--action", "ResolveRecovery", expect_fail=True)
        assert "UNRESOLVED_RECOVERY_ERROR" in err_res_wt, f"Expected UNRESOLVED_RECOVERY_ERROR from premature ResolveRecovery, got: {err_res_wt}"
        assert "[RECOVERY_RESOLVED]" not in err_res_wt
        assert os.path.exists(os.path.join(ws, ".collab", "op-journal.json")), "Premature ResolveRecovery deleted op-journal.json!"
        assert safe_read_json(os.path.join(ws, ".collab", "op-journal.json")).get("integrationRoot") == sim_tmp_wt2
        # Now remove the extra ignored-note.txt so the temp worktree matches the recorded disposable state:
        os.remove(ignored_note_path)
        out_res_clean = invoke("--action", "ResolveRecovery")
        assert "[RECOVERY_RESOLVED]" in out_res_clean and "RECOVERY_STATUS: UNRESOLVED" not in out_res_clean
        assert not os.path.exists(sim_tmp_wt2), "Cleaned temporary worktree should be disposed during ResolveRecovery!"
        assert not os.path.exists(os.path.join(ws, ".collab", "op-journal.json"))
        out_wta2_ok = invoke("--action", "MergeTask", "--task-id", "TASK-WT-A", "--collaborator-id", "pc1-antigravity", "--squash", "--use-worktree")
        assert "DELIVERY: COMPLETE" in out_wta2_ok
        run_git(ws, "checkout", "main")

        print("[SELFTEST 40/42] Block RevertTask when another task claims the target files...")
        invoke("--action", "MergeTask", "--task-id", "TASK-05", "--collaborator-id", "pc1-antigravity", "--reapply")
        invoke("--action", "SilentHandshake", "--task-id", "TASK-04", "--collaborator-id", "pc1-codex", "--files", "src/releaseme.js")
        err_rev_locked = invoke("--action", "RevertTask", "--task-id", "TASK-05", "--collaborator-id", "pc1-antigravity", expect_fail=True)
        assert "FILE_CONFLICT_ERROR" in err_rev_locked
        invoke("--action", "ReleaseFiles", "--task-id", "TASK-04", "--collaborator-id", "pc1-codex", "--files", "src/releaseme.js")

        print("[SELFTEST 41/42] Verify live OS kernel lock never expires by age & serializes 8 racers...")
        os.makedirs(os.path.join(ws, "src"), exist_ok=True)
        with open(os.path.join(ws, "src", "other.js"), "w", encoding="utf-8") as f:
            f.write("// other\n")
        holder_code = (
            f"import os, sys, time; sys.path.insert(0, {repr(os.path.dirname(engine_path))}); "
            f"import collab_engine; "
            f"collab_dir = os.path.join({repr(ws)}, '.collab'); "
            f"lock_path = os.path.join(collab_dir, 'board.tx.lock'); "
            f"ctx = collab_engine.transactional_board_lock(collab_dir); ctx.__enter__(); "
            f"os.utime(lock_path, (time.time() - 60, time.time() - 60)); "
            f"print('LOCKED_READY', flush=True); "
            f"time.sleep(0.5); "
            f"ctx.__exit__(None, None, None)"
        )
        proc = subprocess.Popen([sys.executable, "-c", holder_code], stdout=subprocess.PIPE, text=True)
        assert proc.stdout.readline().strip() == "LOCKED_READY"
        t0 = time.monotonic()
        invoke("--action", "Status", use_subprocess=True)
        elapsed = time.monotonic() - t0
        proc.wait()
        assert elapsed >= 0.35

        def racer(idx):
            return invoke("--action", "SilentHandshake", "--task-id", "TASK-03", "--collaborator-id", f"racer-{idx}", use_subprocess=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outputs = list(pool.map(racer, range(8)))
        wins = [o for o in outputs if "STATUS: APPROVED" in o]
        losses = [o for o in outputs if "STATUS: BUSY_REDIRECT" in o]
        assert len(wins) == 1 and len(losses) == 7

        print("[SELFTEST 42/42] Verify ResetWorkspace requires --confirm-reset and saves snapshot...")
        err_reset = invoke("--action", "ResetWorkspace", "--collaborator-id", "pc1-antigravity", expect_fail=True)
        assert "CONFIRMATION_REQUIRED" in err_reset
        out_reset = invoke("--action", "ResetWorkspace", "--collaborator-id", "pc1-antigravity", "--confirm-reset")
        assert "SNAPSHOT_SAVED:" in out_reset

        print("[OK] ALL 42 ADVERSARIAL, CONCURRENCY & GIT DELIVERY SELF-TESTS PASSED!")
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(remote_bare, ignore_errors=True)


def main(argv=None):
    p = argparse.ArgumentParser(description="Transactional Local Multi-AI Collaboration & Git Workflow Engine")
    p.add_argument("--action", required=True, choices=[
        "InitFresh", "ResetWorkspace", "JoinExisting", "SilentHandshake", "ReleaseFiles",
        "AddTask", "UpdateProgress", "Checkpoint", "AtomicHandoff", "CheckOrContinue",
        "Done", "ReopenTask", "SubmitReview", "MergeTask", "ReleaseTag", "RevertTask", "RecoverTag",
        "ResolveRecovery", "WebPacket", "Status", "Tasks", "SelfTest"
    ])
    p.add_argument("--local-path", default=".")
    p.add_argument("--backup-path", "--cloud-path", dest="backup_path", default="")
    p.add_argument("--folder-url", default="")
    p.add_argument("--project-key", default="")
    p.add_argument("--collaborator-id", default="")
    p.add_argument("--device-cluster-id", default="pc1-local-cluster")
    p.add_argument("--target-agent-id", default="")
    p.add_argument("--target-cluster-id", default="pc1-local-cluster")
    p.add_argument("--local-agents", default="antigravity,claude,codex,grok,gemini,on-web-ai")
    p.add_argument("--same-device", default="true")
    p.add_argument("--task-id", default="")
    p.add_argument("--task-title", default="")
    p.add_argument("--depends-on", default="")
    p.add_argument("--current-step", default="")
    p.add_argument("--last-prompt", default="")
    p.add_argument("--changes-summary", default="")
    p.add_argument("--remaining-todo", default="")
    p.add_argument("--reason", default="Manual handoff")
    p.add_argument("--force-handoff", action="store_true", help="Allow PrimaryLead to recover a stale task")
    p.add_argument("--confirm-reset", action="store_true", help="Confirm archiving and resetting an active workspace")
    p.add_argument("--abandon-recovery", action="store_true", help="Explicitly archive and abandon an unresolved operation journal")
    p.add_argument("--git-branch", action="store_true", help="Create/checkout a dedicated task/<ID>-<slug> git branch on task start")
    p.add_argument("--use-worktree", action="store_true", help="Create an isolated git worktree under .collab/worktrees/<ID>")
    p.add_argument("--commit-message", default="")
    p.add_argument("--push", action="store_true", help="Push task branch to remote after checkpoint/handoff/done")
    p.add_argument("--remote", default="origin")
    p.add_argument("--base-branch", default="main")
    p.add_argument("--squash", action="store_true", help="Squash task branch commits into a single commit on merge")
    p.add_argument("--reapply", action="store_true", help="Explicitly reapply a previously reverted task onto base-branch")
    p.add_argument("--tag", default="")
    p.add_argument("--tag-message", default="")
    p.add_argument("--require-merged-task", default="", help="Verify specified taskId is merged into base-branch before tagging")
    p.add_argument("--recovery-branch", default="")
    p.add_argument("--review-verdict", default="APPROVED", choices=["APPROVED", "CHANGES_REQUESTED", "COMMENT"])
    p.add_argument("--review-notes", default="")
    p.add_argument("--progress-percent", type=int, default=0)
    p.add_argument("--files", default="")
    p.add_argument("--render-format", default="CLI", choices=["CLI", "Markdown"])
    args = p.parse_args(argv)

    if args.action == "SelfTest":
        run_self_test()
        return

    local_root = os.path.abspath(args.local_path)
    collab_dir = os.path.join(local_root, ".collab")
    handoffs_dir = os.path.join(collab_dir, "handoffs")
    reviews_dir = os.path.join(collab_dir, "reviews")
    onboard_dir = os.path.join(collab_dir, "onboarding")
    snapshots_dir = os.path.join(collab_dir, "snapshots")
    config_path = os.path.join(collab_dir, "collab-config.json")
    board_path = os.path.join(collab_dir, "state-board.json")
    welcome_path = os.path.join(onboard_dir, "welcome-brief.json")
    session_path = os.path.join(collab_dir, ".local-session.json")

    with transactional_board_lock(collab_dir):
        if args.action == "InitFresh":
            if os.path.exists(config_path) or os.path.exists(board_path):
                fail(
                    "ALREADY_INITIALIZED_ERROR",
                    "Workspace is already initialized. Use --action Status to inspect or --action ResetWorkspace --confirm-reset to archive and reset.",
                )
            for d in (collab_dir, handoffs_dir, reviews_dir, onboard_dir, snapshots_dir):
                os.makedirs(d, exist_ok=True)
            if args.backup_path:
                try:
                    os.makedirs(args.backup_path, exist_ok=True)
                    probe = os.path.join(args.backup_path, ".perm-probe.tmp")
                    with open(probe, "w", encoding="utf-8") as f:
                        f.write("ok")
                    os.remove(probe)
                except Exception as e:
                    fail("PERMISSION_ERROR", f"Cannot write to BackupPath '{args.backup_path}': {e}")

            proj_key = args.project_key.strip()
            if not proj_key:
                chars = string.ascii_uppercase + string.digits
                p1 = "".join(random.choices(chars, k=4))
                p2 = "".join(random.choices(chars, k=4))
                proj_key = f"COLLAB-{p1}-{p2}"

            collab_id = args.collaborator_id.strip() or "pc1-antigravity"
            agents = [x.strip() for x in args.local_agents.split(",") if x.strip()]
            ts = now_utc()

            config = {
                "projectName": os.path.basename(local_root),
                "scopedFolderUrl": args.folder_url,
                "backupPath": args.backup_path,
                "projectKeyHash": sha256_text(proj_key),
                "primaryLeadAgent": collab_id,
                "createdAtUtc": ts,
                "deviceClusters": [{
                    "deviceClusterId": args.device_cluster_id,
                    "samePcAgents": agents
                }]
            }
            atomic_write_json(config_path, config)
            atomic_write_json(session_path, {
                "projectKey": proj_key,
                "collaboratorId": collab_id,
                "deviceClusterId": args.device_cluster_id
            })
            board = {
                "projectName": config["projectName"],
                "primaryLeadAgent": collab_id,
                "revision": 1,
                "updatedAtUtc": ts,
                "activeAgents": [{"agentId": collab_id, "cluster": args.device_cluster_id, "role": "PrimaryLead", "lastSeenUtc": ts}],
                "releases": [],
                "tasks": []
            }
            atomic_write_json(board_path, board)
            atomic_write_json(welcome_path, {
                "generatedByPrimaryLead": collab_id,
                "projectSummary": f"Local transactional task & Git handoff ledger initialized for {config['projectName']}.",
                "updatedAtUtc": ts
            })
            print(f"[OK] Fresh Project Initialized | Key: {proj_key} | Lead AI: {collab_id}")
            return

        if not os.path.exists(config_path):
            fail("NOT_INITIALIZED", "Workspace not initialized. Run --action InitFresh first.")

        saved_config = safe_read_json(config_path)
        local_session = safe_read_json(session_path, default={})
        eff_key = args.project_key.strip() or local_session.get("projectKey", "")
        eff_agent = args.collaborator_id.strip() or local_session.get("collaboratorId", "")
        eff_cluster = args.device_cluster_id.strip() or local_session.get("deviceClusterId", "pc1-local-cluster")

        if sha256_text(eff_key) != saved_config.get("projectKeyHash"):
            fail("ACCESS_DENIED", "Invalid Unique Project Key.")

        board = safe_read_json(board_path)
        welcome = safe_read_json(welcome_path, default={})

        def save_state():
            board["revision"] = int(board.get("revision", 0)) + 1
            board["updatedAtUtc"] = now_utc()
            clean_board = {k: v for k, v in board.items() if not k.startswith("_")}
            atomic_write_json(board_path, clean_board)
            bpath = saved_config.get("backupPath", "") or saved_config.get("scopedCloudPath", "")
            if bpath and os.path.exists(bpath):
                backup_collab = os.path.join(bpath, ".collab-backup")
                os.makedirs(backup_collab, exist_ok=True)
                atomic_write_json(os.path.join(backup_collab, "state-board.backup.json"), clean_board)

        reconcile_journal_and_git(local_root, collab_dir, handoffs_dir, board, save_state, active_action=args.action, active_task_id=args.task_id.strip())

        if args.action == "ResolveRecovery":
            unres_list = board.get("_unresolvedRecovery") or []
            if unres_list:
                if not args.abandon_recovery:
                    fail(
                        "UNRESOLVED_RECOVERY_ERROR",
                        f"Cannot declare recovery resolved while the checkout/worktree remains unresolved: "
                        f"{' | '.join(unres_list)}. "
                        "Clean or preserve changes in the affected checkout/worktree first so recovery can complete automatically, "
                        "or pass --abandon-recovery to explicitly archive and abandon the journal.",
                    )
                jpath = os.path.join(collab_dir, "op-journal.json")
                jdata = safe_read_json(jpath, default={}) if os.path.exists(jpath) else {}
                os.makedirs(snapshots_dir, exist_ok=True)
                arch_name = f"abandoned-op-journal-{jdata.get('taskId', 'unknown')}-{int(time.time() * 1000)}.json"
                arch_path = os.path.join(snapshots_dir, arch_name)
                if os.path.exists(jpath):
                    shutil.copy2(jpath, arch_path)
                clear_op_journal(collab_dir)
                board.pop("_unresolvedRecovery", None)
                int_wt = jdata.get("integrationRoot", "")
                used_wt = bool(jdata.get("usedTempWorktree"))
                base_b = jdata.get("baseBranch", "main")
                if used_wt and int_wt and os.path.exists(int_wt):
                    repair_hint = (
                        f"Temporary integration worktree '{int_wt}' still exists and holds branch '{base_b}'; "
                        f"inspect/preserve any local files inside '{int_wt}' and run `git worktree remove --force '{int_wt}'` before integrating onto '{base_b}'."
                    )
                else:
                    repair_hint = (
                        f"Inspect branch '{base_b}' and working tree in '{local_root}' (journaled at {jdata.get('origMainSha', 'unknown')}) before retrying '{jdata.get('op', 'operation')}'."
                    )
                print(f"[RECOVERY_ABANDONED] Unresolved operation journal archived by explicit --abandon-recovery request.")
                print(f"JOURNAL_ARCHIVED: .collab/snapshots/{arch_name}")
                print(f"REMAINING_REPAIR_REQUIRED: {repair_hint}")
                return
            clear_op_journal(collab_dir)
            print("[RECOVERY_RESOLVED] Checkout/worktree verified clean and operation journal resolved.")
            return

        # [P2] Stop new journal-writing or mutating operations when reconciliation remains unresolved!
        read_only_actions = {"Status", "Tasks", "WebPacket", "CheckOrContinue"}
        if board.get("_unresolvedRecovery") and args.action not in read_only_actions:
            fail(
                "UNRESOLVED_RECOVERY_ERROR",
                f"Cannot execute '{args.action}' while an earlier interrupted operation remains unresolved: "
                f"{' | '.join(board['_unresolvedRecovery'])}. "
                "Inspect and resolve the affected checkout/worktree first (or run --action ResolveRecovery once resolved).",
            )

        def require_agent():
            if not eff_agent:
                fail("VALIDATION_ERROR", "--collaborator-id is required.")
            return eff_agent

        if args.action == "ResetWorkspace":
            caller = require_agent()
            if caller != board.get("primaryLeadAgent"):
                fail("OWNERSHIP_ERROR", f"Only PrimaryLead ('{board.get('primaryLeadAgent')}') can reset the workspace.")
            if not args.confirm_reset:
                fail("CONFIRMATION_REQUIRED", "Pass --confirm-reset to archive the current board and reset the workspace.")
            os.makedirs(snapshots_dir, exist_ok=True)
            snap_name = f"board-rev{board.get('revision', 1)}-{int(time.time())}.json"
            snap_path = os.path.join(snapshots_dir, snap_name)
            atomic_write_json(snap_path, board)
            board["tasks"] = []
            board["revision"] = int(board.get("revision", 0)) + 1
            save_state()
            print(f"[WORKSPACE_RESET] Active board cleared. SNAPSHOT_SAVED: .collab/snapshots/{snap_name}")
            return

        def get_all_locked_files(exclude_task_id=None):
            locked = set()
            for t in board["tasks"]:
                if t["taskId"] != exclude_task_id and t["status"] in ("in_progress", "review_pending"):
                    locked.update(normalize_file_list(t.get("ownedFiles", []), local_root))
            return locked

        def is_unblocked_and_free(task_obj):
            deps = task_obj.get("dependsOn", [])
            done_ids = {t["taskId"] for t in board["tasks"] if t["status"] == "done"}
            if not all(d in done_ids for d in deps):
                return False
            t_files = set(normalize_file_list(task_obj.get("targetFiles", []), local_root))
            if t_files & get_all_locked_files(exclude_task_id=task_obj["taskId"]):
                return False
            return True

        def get_task_or_fail(tid):
            if not tid:
                fail("VALIDATION_ERROR", "--task-id is required for this action.")
            t = next((x for x in board["tasks"] if x["taskId"] == tid), None)
            if not t:
                fail("INVALID_TASK_ERROR", f"Task '{tid}' does not exist on the board.")
            return t

        if args.action == "JoinExisting":
            agent_id = require_agent()
            same_dev = args.same_device.lower() == "true"
            existing_ag = next((a for a in board["activeAgents"] if a["agentId"] == agent_id), None)
            if existing_ag:
                existing_ag["lastSeenUtc"] = now_utc()
                existing_ag["cluster"] = eff_cluster
            else:
                board["activeAgents"].append({
                    "agentId": agent_id,
                    "cluster": eff_cluster,
                    "sameDevice": same_dev,
                    "role": "Collaborator",
                    "lastSeenUtc": now_utc()
                })
            atomic_write_json(session_path, {
                "projectKey": eff_key,
                "collaboratorId": agent_id,
                "deviceClusterId": eff_cluster
            })
            save_state()
            ready = [f"{t['taskId']}: {t['title']}" for t in board["tasks"] if t["status"] == "todo" and is_unblocked_and_free(t)]
            print(f"[JOINED] {agent_id} (Cluster: {eff_cluster}, SamePC: {same_dev})")
            print(f"[BRIEF FROM PRIMARY LEAD ({board['primaryLeadAgent']})]: {welcome.get('projectSummary', '')}")
            print(f"[READY UNBLOCKED TASKS]: {' | '.join(ready) if ready else 'None'}")
            return

        if args.action == "AddTask":
            tid = args.task_id.strip()
            title = args.task_title.strip()
            if not tid or not title:
                fail("VALIDATION_ERROR", "Both --task-id and --task-title are required for AddTask.")
            if any(t["taskId"] == tid for t in board["tasks"]):
                fail("DUPLICATE_TASK_ERROR", f"Task ID '{tid}' already exists on the board.")
            deps = [x.strip() for x in args.depends_on.split(",") if x.strip()]
            if tid in deps:
                fail("VALIDATION_ERROR", f"Task '{tid}' cannot depend on itself.")
            known_ids = {t["taskId"] for t in board["tasks"]}
            unknown_deps = [d for d in deps if d not in known_ids]
            if unknown_deps:
                fail("VALIDATION_ERROR", f"Unknown dependency task ID(s): {', '.join(unknown_deps)}")

            req_files = normalize_file_list(args.files, local_root)
            branch_name = f"task/{tid}-{slugify_branch_suffix(title)}"
            board["tasks"].append({
                "taskId": tid,
                "title": title,
                "status": "todo",
                "assignedTo": "",
                "handoffFrom": "",
                "handoffChain": [],
                "deviceClusterId": "",
                "progressPercent": 0,
                "currentStep": "Ready in workflow queue",
                "lastUserPrompt": "",
                "changesSummary": "",
                "remainingTodo": "",
                "dependsOn": deps,
                "ownedFiles": [],
                "targetFiles": req_files,
                "gitBranch": branch_name,
                "worktreePath": "",
                "baseCommit": "",
                "latestCommit": "",
                "lastPushedCommit": "",
                "lastPushedTaskCommit": "",
                "lastPushedMainCommit": "",
                "reviewTargetCommit": "",
                "preMergeMainCommit": "",
                "mergedCommit": "",
                "mergeType": "",
                "noopMerge": False,
                "reverted": False,
                "wasReverted": False,
                "revertedCommit": "",
                "revertCommit": "",
                "deliveryStatus": "PENDING",
                "remoteBackupStatus": "UNPUSHED",
                "checkpoints": [],
                "reviews": [],
                "updatedAtUtc": now_utc()
            })
            save_state()
            print(f"[TASK_ADDED] {tid} ({title}) | Branch: {branch_name} | Target files: {', '.join(req_files) if req_files else 'None'}")
            return

        if args.action == "SilentHandshake":
            agent_id = require_agent()
            target = get_task_or_fail(args.task_id.strip())

            if target["status"] == "done":
                print("STATUS: BUSY_REDIRECT")
                print(f"BUSY_TASK: {target['taskId']} ({target['title']}) is already DONE (100%).")
                return

            if target["status"] == "reverted" or target.get("reverted"):
                print("STATUS: BUSY_REDIRECT")
                print(f"BUSY_TASK: {target['taskId']} ({target['title']}) was REVERTED. Run --action ReopenTask to reopen it for edits or --action MergeTask --reapply.")
                return

            if target["status"] == "review_pending":
                ready = [f"{t['taskId']} [TODO]: {t['title']}" for t in board["tasks"] if t["status"] == "todo" and is_unblocked_and_free(t)]
                print("STATUS: BUSY_REDIRECT")
                print(f"BUSY_TASK: {target['taskId']} ({target['title']}) is awaiting independent peer review (locked by {target['assignedTo']} on files: {', '.join(target.get('ownedFiles', []))}).")
                print(f"READY_ALTERNATIVES: {' | '.join(ready) if ready else 'None'}")
                return

            chain = target.get("handoffChain", [])
            if (target.get("handoffFrom") == agent_id or agent_id in chain) and target["assignedTo"] != agent_id:
                ready = [f"{t['taskId']}: {t['title']}" for t in board["tasks"] if t["status"] == "todo" and is_unblocked_and_free(t)]
                print("STATUS: HANDED_OFF_SKIP")
                print(f"MESSAGE: Task {target['taskId']} was already handed off to '{target['assignedTo']}' ({target['progressPercent']}% done). Skipping continuation.")
                print(f"READY_ALTERNATIVES: {' | '.join(ready) if ready else 'None'}")
                return

            deps = target.get("dependsOn", [])
            done_ids = {t["taskId"] for t in board["tasks"] if t["status"] == "done"}
            if not all(d in done_ids for d in deps):
                print("STATUS: DEPENDENCY_BLOCKED")
                print(f"MESSAGE: Task {target['taskId']} is waiting on unfinished dependencies: {', '.join(deps)}")
                return

            explicit_files = normalize_file_list(args.files, local_root)
            default_files = normalize_file_list(target.get("targetFiles", []), local_root)
            existing_owned = normalize_file_list(target.get("ownedFiles", []), local_root)
            effective_files = union_ordered(existing_owned, default_files, explicit_files)

            other_locked_tasks = [
                t for t in board["tasks"]
                if t["taskId"] != target["taskId"] and t["status"] in ("in_progress", "review_pending")
            ]
            file_conflict = [
                t for t in other_locked_tasks
                if set(effective_files) & set(normalize_file_list(t.get("ownedFiles", []), local_root))
            ]

            if (target["status"] == "in_progress" and target["assignedTo"] != agent_id) or file_conflict:
                busy = target if (target["status"] == "in_progress" and target["assignedTo"] != agent_id) else file_conflict[0]
                other_locks = " ; ".join(
                    f"{t['assignedTo']} ({t['taskId']}) -> {','.join(t.get('ownedFiles', []))} ({t['progressPercent']}%)"
                    for t in board["tasks"] if t["status"] in ("in_progress", "review_pending") and t["assignedTo"] != agent_id
                )
                ready = [
                    f"{t['taskId']} [{t['status'].upper()}]: {t['title']}"
                    for t in board["tasks"]
                    if (t["status"] == "todo" and is_unblocked_and_free(t)) or (t["status"] == "review_pending" and t["assignedTo"] != agent_id)
                ]
                print("STATUS: BUSY_REDIRECT")
                print(f"BUSY_TASK: {busy['taskId']} ({busy['title']}) exclusively locked by {busy['assignedTo']} [{busy['progressPercent']}% - {busy['currentStep']}] on files: {', '.join(busy.get('ownedFiles', []))}")
                print(f"OTHER_ACTIVE_LOCKS: {other_locks}")
                print(f"READY_ALTERNATIVES: {' | '.join(ready) if ready else 'None'}")
                return

            # Must Fix #2: Prevent switching branches in the shared working tree if another active task is using it!
            if args.git_branch and not args.use_worktree and is_git_repo(local_root):
                conflicting_branch_tasks = [
                    t for t in other_locked_tasks
                    if t.get("gitBranch") and not t.get("worktreePath")
                    and run_git(local_root, "show-ref", "--verify", f"refs/heads/{t['gitBranch']}", check=False).returncode == 0
                ]
                if conflicting_branch_tasks:
                    ct = conflicting_branch_tasks[0]
                    fail(
                        "BRANCH_CONTENTION_ERROR",
                        f"Cannot switch shared working tree to branch for '{target['taskId']}' while '{ct['taskId']}' "
                        f"is active on branch '{ct['gitBranch']}' in the same checkout. Pass --use-worktree for simultaneous branch tasks.",
                    )

            if (args.git_branch or args.use_worktree) and is_git_repo(local_root):
                bname = target.get("gitBranch") or f"task/{target['taskId']}-{slugify_branch_suffix(target['title'])}"
                target["gitBranch"] = bname
                start_sha, wt_path = ensure_task_branch(
                    local_root, bname, use_worktree=args.use_worktree, task_id=target["taskId"], base_branch=args.base_branch
                )
                if not target.get("baseCommit"):
                    target["baseCommit"] = start_sha
                if not target.get("latestCommit"):
                    target["latestCommit"] = start_sha
                if args.use_worktree:
                    target["worktreePath"] = wt_path

            target["status"] = "in_progress"
            target["assignedTo"] = agent_id
            target["deviceClusterId"] = eff_cluster
            if args.progress_percent > 0:
                target["progressPercent"] = min(99, max(1, args.progress_percent))
            elif target["progressPercent"] == 0:
                target["progressPercent"] = 15
            if args.last_prompt:
                target["lastUserPrompt"] = args.last_prompt
            if args.changes_summary:
                target["changesSummary"] = args.changes_summary
            if args.remaining_todo:
                target["remainingTodo"] = args.remaining_todo
            if args.current_step:
                target["currentStep"] = args.current_step
            else:
                target["currentStep"] = f"Actively being built by {agent_id}"
            target["ownedFiles"] = effective_files
            target["targetFiles"] = effective_files
            target["updatedAtUtc"] = now_utc()
            save_state()
            print("STATUS: APPROVED")
            if target.get("gitBranch") and (args.git_branch or args.use_worktree):
                print(f"TASK_BRANCH: {target['gitBranch']} (baseCommit: {target.get('baseCommit', '')})")
            return

        if args.action == "ReleaseFiles":
            agent_id = require_agent()
            target = get_task_or_fail(args.task_id.strip())
            if target["status"] != "in_progress" or target["assignedTo"] != agent_id:
                fail("OWNERSHIP_ERROR", f"Only active owner '{target['assignedTo']}' can release file claims on '{target['taskId']}'.")
            to_release = set(normalize_file_list(args.files, local_root))
            if not to_release:
                fail("VALIDATION_ERROR", "--files is required for ReleaseFiles.")
            # Must Fix #4: Remove from BOTH ownedFiles and targetFiles so handoffs never resurrect released claims!
            target["ownedFiles"] = [f for f in target.get("ownedFiles", []) if f not in to_release]
            target["targetFiles"] = [f for f in target.get("targetFiles", []) if f not in to_release]
            target["updatedAtUtc"] = now_utc()
            save_state()
            print(f"[FILES_RELEASED] Released {', '.join(sorted(to_release))} from {target['taskId']}. Remaining locked: {', '.join(target['ownedFiles']) or 'None'}")
            return

        if args.action == "ReopenTask":
            agent_id = require_agent()
            target = get_task_or_fail(args.task_id.strip())
            was_rev = bool(target.get("reverted") or target.get("status") == "reverted" or target.get("wasReverted"))
            if target["status"] not in ("review_pending", "reverted") and not target.get("reverted"):
                fail("STATE_ERROR", f"Task '{target['taskId']}' is in status '{target['status']}' (ReopenTask applies to 'review_pending' or 'reverted').")
            if agent_id != target["assignedTo"] and agent_id != board.get("primaryLeadAgent"):
                fail("OWNERSHIP_ERROR", f"Only task owner '{target['assignedTo']}' or PrimaryLead can reopen '{target['taskId']}'.")
            restored_files = normalize_file_list(target.get("ownedFiles") or target.get("targetFiles", []), local_root)
            conflict_locked = get_all_locked_files(exclude_task_id=target["taskId"])
            overlap = set(restored_files) & conflict_locked
            if overlap:
                fail("FILE_CONFLICT_ERROR", f"Cannot reopen '{target['taskId']}' because file(s) {', '.join(sorted(overlap))} are locked by another active task.")

            # If reopening from 'reverted' on main, switch cleanly back to the task branch if no other active task holds the shared checkout
            bname = target.get("gitBranch", "")
            if was_rev and bname and not target.get("worktreePath") and is_git_repo(local_root):
                if get_current_branch(local_root) != bname and run_git(local_root, "show-ref", "--verify", f"refs/heads/{bname}", check=False).returncode == 0:
                    other_branch_holders = [
                        t for t in board["tasks"]
                        if t["taskId"] != target["taskId"] and t["status"] in ("in_progress", "review_pending")
                        and t.get("gitBranch") and not t.get("worktreePath")
                        and run_git(local_root, "show-ref", "--verify", f"refs/heads/{t['gitBranch']}", check=False).returncode == 0
                    ]
                    if other_branch_holders:
                        fail("BRANCH_CONTENTION_ERROR", f"Cannot switch shared checkout to '{bname}' while '{other_branch_holders[0]['taskId']}' is active.")
                    assert_clean_integration_checkout(local_root, restored_files)
                    run_git(local_root, "checkout", bname)

            target["ownedFiles"] = restored_files
            target["status"] = "in_progress"
            target["progressPercent"] = 85
            target["reviewTargetCommit"] = ""
            target["reverted"] = False
            target["wasReverted"] = was_rev
            target["mergedCommit"] = ""
            target["mergeType"] = ""
            target["noopMerge"] = False
            target["deliveryStatus"] = "PENDING"
            target["currentStep"] = f"Reopened by {agent_id} to checkpoint/resubmit edits"
            target["updatedAtUtc"] = now_utc()
            save_state()
            print(f"[TASK_REOPENED] {target['taskId']} returned to 'in_progress' with file locks preserved ({', '.join(target.get('ownedFiles', []))}).")
            return

        if args.action == "Checkpoint":
            agent_id = require_agent()
            target = get_task_or_fail(args.task_id.strip())
            # Must Fix #7: Allow owner to Checkpoint directly from in_progress OR review_pending (auto-reopening review_pending)
            if target["status"] not in ("in_progress", "review_pending") or target["assignedTo"] != agent_id:
                fail("OWNERSHIP_ERROR", f"Only active owner '{target['assignedTo']}' can create a checkpoint on '{target['taskId']}'.")
            if target["status"] == "review_pending":
                target["status"] = "in_progress"
                target["reviewTargetCommit"] = ""

            repo_dir = target.get("worktreePath") or local_root
            # Must Fix #2: Reject mismatched current branch!
            assert_repo_on_task_branch(repo_dir, target)

            msg = args.commit_message.strip() or f"checkpoint({target['taskId']}): {args.current_step or 'progress update'}"
            sha, created = commit_task_owned_files(repo_dir, target.get("ownedFiles", []), msg, base_commit=target.get("baseCommit", ""))
            target["latestCommit"] = sha
            if not target.get("baseCommit"):
                target["baseCommit"] = sha
            if created:
                target.setdefault("checkpoints", []).append({
                    "commit": sha,
                    "message": msg,
                    "authorAgent": agent_id,
                    "timestampUtc": now_utc()
                })
            if args.progress_percent > 0:
                target["progressPercent"] = min(99, max(1, args.progress_percent))
            if args.last_prompt:
                target["lastUserPrompt"] = args.last_prompt
            if args.changes_summary:
                target["changesSummary"] = args.changes_summary
            if args.remaining_todo:
                target["remainingTodo"] = args.remaining_todo
            if args.current_step:
                target["currentStep"] = args.current_step

            # Must Fix #3: Track backup status against exact commit!
            if args.push:
                bname = target.get("gitBranch") or get_current_branch(repo_dir)
                pstat, pushed_sha = push_branch_to_remote(repo_dir, bname, args.remote)
                target["remoteBackupStatus"] = pstat
                if pstat == "PUSHED":
                    target["lastPushedCommit"] = pushed_sha
                    target["lastPushedTaskCommit"] = pushed_sha
            else:
                sync_backup_status(target)

            target["updatedAtUtc"] = now_utc()
            save_state()
            print(f"CHECKPOINT_COMMIT: {sha} ({'new commit' if created else 'no file changes'}) | REMOTE_BACKUP: {target.get('remoteBackupStatus', 'UNPUSHED')}")
            return

        if args.action == "AtomicHandoff":
            caller_id = require_agent()
            target = get_task_or_fail(args.task_id.strip())
            to_agent = args.target_agent_id.strip()
            if not to_agent:
                fail("VALIDATION_ERROR", "--target-agent-id is required for AtomicHandoff.")
            if target["status"] != "in_progress":
                fail("STATE_ERROR", f"Cannot hand off task '{target['taskId']}' because its status is '{target['status']}' (must be 'in_progress').")

            is_owner = (caller_id == target["assignedTo"])
            is_lead_recovery = (caller_id == board.get("primaryLeadAgent") and args.force_handoff)
            if not (is_owner or is_lead_recovery):
                fail(
                    "OWNERSHIP_ERROR",
                    f"Caller '{caller_id}' cannot hand off '{target['taskId']}' owned by '{target['assignedTo']}'. "
                    f"Ordinary handoffs require the task owner; lead recovery requires both PrimaryLead ('{board.get('primaryLeadAgent')}') and --force-handoff.",
                )
            if to_agent == target["assignedTo"]:
                fail("VALIDATION_ERROR", f"Task '{target['taskId']}' is already assigned to '{to_agent}'.")

            # Must Fix #4: Transfer ONLY currently held ownedFiles (never resurrect released targetFiles!)
            held_files = normalize_file_list(target.get("ownedFiles", []), local_root)
            conflict_locked = get_all_locked_files(exclude_task_id=target["taskId"])
            overlap = set(held_files) & conflict_locked
            if overlap:
                fail("FILE_CONFLICT_ERROR", f"Cannot hand off '{target['taskId']}' because file(s) {', '.join(sorted(overlap))} are locked by another task.")

            repo_dir = target.get("worktreePath") or local_root
            if is_git_repo(repo_dir) and held_files:
                assert_repo_on_task_branch(repo_dir, target)

            prev_owner = target["assignedTo"]
            chain = [a for a in target.get("handoffChain", []) if a != to_agent]
            if prev_owner and prev_owner not in chain:
                chain.append(prev_owner)
            if args.last_prompt:
                target["lastUserPrompt"] = args.last_prompt
            if args.changes_summary:
                target["changesSummary"] = args.changes_summary
            if args.remaining_todo:
                target["remainingTodo"] = args.remaining_todo

            # Journal the handoff intent BEFORE any pre-handoff Git commit or push!
            initial_packet = build_handoff_packet(target, prev_owner, chain, to_agent, args.reason)
            write_op_journal(collab_dir, {
                "op": "AtomicHandoff",
                "phase": "pre_handoff_git",
                "taskId": target["taskId"],
                "prevOwner": prev_owner,
                "chain": chain,
                "toAgent": to_agent,
                "targetClusterId": args.target_cluster_id,
                "heldFiles": held_files,
                "gitBranch": target.get("gitBranch", ""),
                "remote": args.remote,
                "reason": args.reason,
                "lastUserPrompt": target.get("lastUserPrompt", ""),
                "changesSummary": target.get("changesSummary", ""),
                "remainingTodo": target.get("remainingTodo", ""),
                "packet": initial_packet,
            })

            if is_git_repo(repo_dir) and held_files:
                clean, _ = check_task_files_clean(repo_dir, held_files, base_commit=target.get("baseCommit", ""))
                if not clean:
                    sha, _ = commit_task_owned_files(
                        repo_dir,
                        held_files,
                        f"checkpoint({target['taskId']}): pre-handoff from {prev_owner} to {to_agent}",
                        base_commit=target.get("baseCommit", ""),
                    )
                    target["latestCommit"] = sha
                else:
                    target["latestCommit"] = get_head_commit(repo_dir, "HEAD")
                if args.push:
                    bname = target.get("gitBranch") or get_current_branch(repo_dir)
                    pstat, pushed_sha = push_branch_to_remote(repo_dir, bname, args.remote)
                    target["remoteBackupStatus"] = pstat
                    if pstat == "PUSHED":
                        target["lastPushedCommit"] = pushed_sha
                        target["lastPushedTaskCommit"] = pushed_sha
                else:
                    sync_backup_status(target)

            target["handoffFrom"] = prev_owner
            target["handoffChain"] = chain
            target["assignedTo"] = to_agent
            target["deviceClusterId"] = args.target_cluster_id
            target["status"] = "in_progress"
            target["ownedFiles"] = held_files
            target["currentStep"] = f"Handed off from {prev_owner} to {to_agent} ({args.reason}) - Lock preserved"

            packet = build_handoff_packet(target, prev_owner, chain, to_agent, args.reason)
            target["lastHandoffPacket"] = packet
            write_op_journal(collab_dir, {
                "op": "AtomicHandoff",
                "phase": "saving_board",
                "taskId": target["taskId"],
                "prevOwner": prev_owner,
                "chain": chain,
                "toAgent": to_agent,
                "targetClusterId": args.target_cluster_id,
                "heldFiles": held_files,
                "gitBranch": target.get("gitBranch", ""),
                "remote": args.remote,
                "reason": args.reason,
                "lastUserPrompt": target.get("lastUserPrompt", ""),
                "changesSummary": target.get("changesSummary", ""),
                "remainingTodo": target.get("remainingTodo", ""),
                "packet": packet,
            })
            os.makedirs(handoffs_dir, exist_ok=True)
            atomic_write_json(os.path.join(handoffs_dir, f"{target['taskId']}-handoff.json"), packet)
            save_state()
            clear_op_journal(collab_dir)
            print(f"[ATOMIC_HANDOFF_COMPLETE] {target['taskId']} transferred directly from '{prev_owner}' -> '{to_agent}'.")
            print(f"LOCK_STATUS: Files ({', '.join(target['ownedFiles'])}) remain strictly LOCKED for '{to_agent}' only (Commit: {target.get('latestCommit') or 'N/A'}).")
            return

        if args.action == "CheckOrContinue":
            agent_id = require_agent()
            target = get_task_or_fail(args.task_id.strip())
            chain = target.get("handoffChain", [])
            if (target.get("handoffFrom") == agent_id or agent_id in chain) and target["assignedTo"] != agent_id:
                ready = [f"{t['taskId']}: {t['title']}" for t in board["tasks"] if t["status"] == "todo" and is_unblocked_and_free(t)]
                print("ACTION: SKIP_ALREADY_HANDED_OFF")
                print(f"REASON: Task {target['taskId']} was handed off from you ('{agent_id}') to '{target['assignedTo']}' (currently at {target['progressPercent']}%). Do NOT continue {target['taskId']}.")
                print(f"READY_ALTERNATIVES: {' | '.join(ready) if ready else 'None'}")
                return
            if target["status"] not in ("in_progress", "review_pending") or target["assignedTo"] != agent_id:
                print("ACTION: BLOCKED_BY_OTHER_AI")
                print(f"OWNER: {target['assignedTo'] or 'UNCLAIMED'} (status: {target['status']}, {target['progressPercent']}%)")
                return
            print("ACTION: CONTINUE_APPROVED")
            print(f"ACTIVE_OWNER: {agent_id} (Resuming at {target['progressPercent']}%)")
            print(f"HANDOFF_FROM: {target.get('handoffFrom') or 'None (Self-continuation)'}")
            print(f"GIT_BRANCH_AND_COMMIT: {target.get('gitBranch', 'N/A')} @ {target.get('latestCommit', 'N/A')}")
            hpkt = target.get("lastHandoffPacket") if isinstance(target.get("lastHandoffPacket"), dict) else {}
            print(f"LAST_USER_PROMPT: {target.get('lastUserPrompt') or hpkt.get('lastUserPrompt', '')}")
            print(f"CHANGES_MADE_SO_FAR: {target.get('changesSummary') or hpkt.get('changesSummary', '')}")
            print(f"REMAINING_TODO: {target.get('remainingTodo') or hpkt.get('remainingTodo', '')}")
            print(f"EXCLUSIVE_LOCKED_FILES: {', '.join(target.get('ownedFiles', []))}")
            return

        if args.action in ("UpdateProgress", "Done"):
            agent_id = require_agent()
            target = get_task_or_fail(args.task_id.strip())
            # Must Fix #7: Allow owner to re-run Done from review_pending to resubmit a new commit!
            if target["status"] not in ("in_progress", "review_pending"):
                fail("STATE_ERROR", f"Cannot update or complete task '{target['taskId']}' because its status is '{target['status']}'.")
            if target["assignedTo"] != agent_id:
                fail("OWNERSHIP_ERROR", f"Agent '{agent_id}' cannot update or complete '{target['taskId']}' because it is owned by '{target['assignedTo']}'.")

            pct = 100 if args.action == "Done" else max(1, min(100, args.progress_percent))
            repo_dir = target.get("worktreePath") or local_root

            if pct >= 100 and is_git_repo(repo_dir) and target.get("ownedFiles"):
                assert_repo_on_task_branch(repo_dir, target)
                if args.commit_message.strip():
                    sha, _ = commit_task_owned_files(
                        repo_dir, target["ownedFiles"], args.commit_message.strip(), base_commit=target.get("baseCommit", "")
                    )
                    target["latestCommit"] = sha
                clean, dirty_list = check_task_files_clean(repo_dir, target["ownedFiles"], base_commit=target.get("baseCommit", ""))
                if not clean:
                    fail(
                        "UNCOMMITTED_WORK_ERROR",
                        f"Cannot mark '{target['taskId']}' Done because intended task file(s) have uncommitted or missing changes: {', '.join(dirty_list)}. "
                        "Run --action Checkpoint first.",
                    )
                head_sha = get_head_commit(repo_dir, "HEAD")
                target["latestCommit"] = head_sha
                target["reviewTargetCommit"] = head_sha
                if args.push:
                    bname = target.get("gitBranch") or get_current_branch(repo_dir)
                    pstat, pushed_sha = push_branch_to_remote(repo_dir, bname, args.remote)
                    target["remoteBackupStatus"] = pstat
                    if pstat == "PUSHED":
                        target["lastPushedCommit"] = pushed_sha
                        target["lastPushedTaskCommit"] = pushed_sha
                else:
                    sync_backup_status(target)

            target["progressPercent"] = pct
            if args.last_prompt:
                target["lastUserPrompt"] = args.last_prompt
            if args.changes_summary:
                target["changesSummary"] = args.changes_summary
            if args.remaining_todo:
                target["remainingTodo"] = args.remaining_todo
            if pct >= 100:
                target["status"] = "review_pending"
                target["currentStep"] = f"Code complete by {agent_id} (commit {target.get('reviewTargetCommit') or 'local'}) - awaiting peer review"
            elif args.current_step:
                target["currentStep"] = args.current_step
            target["updatedAtUtc"] = now_utc()
            save_state()
            print(f"[UPDATED] {target['taskId']} -> {pct}% (status: {target['status']}, commit: {target.get('reviewTargetCommit') or target.get('latestCommit') or 'N/A'})")
            return

        if args.action == "SubmitReview":
            reviewer_id = require_agent()
            target = get_task_or_fail(args.task_id.strip())
            if target["status"] != "review_pending":
                fail("REVIEW_STATE_ERROR", f"Task '{target['taskId']}' is in status '{target['status']}' and is not awaiting review ('review_pending').")
            if reviewer_id == target["assignedTo"]:
                fail("SELF_REVIEW_FORBIDDEN", f"Owner '{reviewer_id}' cannot review or approve their own work on '{target['taskId']}'. An independent agent must review it.")

            repo_dir = target.get("worktreePath") or local_root
            reviewed_commit = target.get("reviewTargetCommit", "")
            # Must Fix #7: Enforce strict commit/clean match on APPROVED, while allowing CHANGES_REQUESTED to reopen a stale branch!
            if is_git_repo(repo_dir) and reviewed_commit and args.review_verdict == "APPROVED":
                bname = target.get("gitBranch", "")
                ref_to_check = bname if (bname and run_git(repo_dir, "show-ref", "--verify", f"refs/heads/{bname}", check=False).returncode == 0) else "HEAD"
                clean, dirty_list = check_task_files_clean(repo_dir, target.get("ownedFiles", []), base_commit=target.get("baseCommit", ""))
                current_sha = get_head_commit(repo_dir, ref_to_check)
                if not clean or not commits_equal(repo_dir, current_sha, reviewed_commit):
                    fail(
                        "STALE_REVIEW_COMMIT_ERROR",
                        f"Task '{target['taskId']}' was submitted for review at commit '{reviewed_commit}', "
                        f"but '{ref_to_check}' is at '{current_sha}' (clean={clean}). Run --action ReopenTask or --action Checkpoint & --action Done to resubmit.",
                    )

            target["reviews"].append({
                "reviewer": reviewer_id,
                "verdict": args.review_verdict,
                "reviewedCommit": reviewed_commit,
                "notes": args.review_notes,
                "timestampUtc": now_utc()
            })
            if args.review_verdict == "APPROVED":
                target["status"] = "done"
                target["progressPercent"] = 100
                target["ownedFiles"] = []
                target["currentStep"] = f"Peer Review APPROVED by {reviewer_id} @ {reviewed_commit or 'local'} (Files Unlocked)"
            elif args.review_verdict == "CHANGES_REQUESTED":
                target["status"] = "in_progress"
                target["progressPercent"] = 80
                target["reviewTargetCommit"] = ""
                target["currentStep"] = f"Changes requested by {reviewer_id}: {args.review_notes}"
            save_state()
            print(f"[PEER_REVIEW_RECORDED] {target['taskId']} reviewed by {reviewer_id} -> {args.review_verdict} (commit: {reviewed_commit or 'N/A'})")
            return

        if args.action == "MergeTask":
            caller = require_agent()
            target = get_task_or_fail(args.task_id.strip())
            if not is_git_repo(local_root):
                fail("GIT_ERROR", f"'{local_root}' is not a Git repository.")

            task_branch = target.get("gitBranch", "")
            base_branch = args.base_branch.strip() or "main"
            reviewed_sha = target.get("reviewTargetCommit", "")
            base_commit = target.get("baseCommit", "")
            branch_diff_files = []
            if base_commit and (reviewed_sha or task_branch):
                branch_diff_files = get_diff_changed_files(local_root, base_commit, reviewed_sha or task_branch, local_root)
            prev_commit_files = get_commit_changed_files(
                local_root, target.get("mergedCommit") or target.get("revertedCommit", ""), local_root
            )
            task_files = union_ordered(
                normalize_file_list(target.get("targetFiles", []), local_root),
                normalize_file_list(target.get("ownedFiles", []), local_root),
                branch_diff_files,
                prev_commit_files,
            )

            # Check active file claims by other tasks BEFORE any merge or --reapply write!
            active_locked = get_all_locked_files(exclude_task_id=target["taskId"])
            overlap = set(task_files) & active_locked
            if overlap:
                fail(
                    "FILE_CONFLICT_ERROR",
                    f"Cannot merge or reapply '{target['taskId']}' because file(s) {', '.join(sorted(overlap))} are currently locked by another active task.",
                )

            is_reapply = False
            if target.get("reverted") or target.get("status") == "reverted":
                if not args.reapply:
                    fail(
                        "REVERTED_TASK_ERROR",
                        f"Task '{target['taskId']}' was reverted in commit '{target.get('revertCommit', 'unknown')}'. "
                        "Pass --reapply to MergeTask to explicitly reapply it onto main, or run --action ReopenTask to modify and re-review first.",
                    )
                is_reapply = True
                target["status"] = "done"
                target["reverted"] = False
                target["wasReverted"] = True
                target["mergedCommit"] = ""
            elif target["status"] != "done":
                fail("MERGE_STATE_ERROR", f"Task '{target['taskId']}' cannot be merged until status is 'done' (REVIEW: APPROVED). Current status: '{target['status']}'.")

            with integration_checkout_context(
                local_root, collab_dir, board, base_branch, args.use_worktree, "MergeTask", target["taskId"], task_files
            ) as int_root:
                if not target.get("mergedCommit") and not target.get("noopMerge"):
                    if not task_branch or run_git(int_root, "show-ref", "--verify", f"refs/heads/{task_branch}", check=False).returncode != 0:
                        fail("GIT_ERROR", f"Task branch '{task_branch}' does not exist in Git.")
                    branch_sha = get_head_commit(int_root, task_branch)
                    if reviewed_sha and not commits_equal(int_root, branch_sha, reviewed_sha):
                        fail("STALE_REVIEW_COMMIT_ERROR", f"Branch '{task_branch}' head ({branch_sha}) differs from approved commit ({reviewed_sha}).")

                    orig_main_sha = get_head_commit(int_root, base_branch)
                    source_ref = reviewed_sha or task_branch
                    mb_res = run_git(int_root, "merge-base", orig_main_sha, source_ref, check=False)
                    raw_mb = mb_res.stdout.strip() if mb_res.returncode == 0 else ""
                    merge_base_sha = get_head_commit(int_root, raw_mb) if raw_mb else ""

                    # Normalize commit IDs before comparing so 12-char base_commit matches 40-char merge-base on ordinary merges!
                    needs_full_history_reapply = bool(
                        is_reapply
                        or target.get("wasReverted")
                        or target.get("revertCommit")
                        or (base_commit and merge_base_sha and not commits_equal(int_root, merge_base_sha, base_commit))
                    )
                    effective_base = (base_commit if needs_full_history_reapply else (merge_base_sha or base_commit)) or orig_main_sha

                    # Expand task_files with NUL-delimited diff (including both rename endpoints) between effective_base and source_ref
                    eff_diff_files = get_diff_changed_files(int_root, effective_base, source_ref, local_root)
                    task_files = union_ordered(task_files, eff_diff_files)
                    overlap2 = set(task_files) & active_locked
                    if overlap2:
                        fail(
                            "FILE_CONFLICT_ERROR",
                            f"Cannot merge or reapply '{target['taskId']}' because file(s) {', '.join(sorted(overlap2))} are currently locked by another active task.",
                        )
                    assert_clean_integration_checkout(int_root, task_files)

                    if not has_task_changes(int_root, effective_base, source_ref, task_files):
                        target["mergeType"] = "noop"
                        target["noopMerge"] = True
                        target["mergedCommit"] = ""
                        target["preMergeMainCommit"] = orig_main_sha
                    else:
                        ok_pre, pre_msg, expected_3way = compute_3way_merged_files(
                            int_root, orig_main_sha, effective_base, source_ref, task_files
                        )
                        if not ok_pre:
                            fail("MERGE_CONFLICT_ERROR", f"Cannot merge '{target['taskId']}' into '{base_branch}': {pre_msg}")

                        pre_op_snapshots = {
                            f: (open(os.path.join(int_root, f), "rb").read() if os.path.exists(os.path.join(int_root, f)) else None)
                            for f in task_files
                        }
                        planned_mtype = "squash" if args.squash else "merge"
                        orig_state_m = capture_tree_state(int_root, orig_main_sha, task_files)
                        exp_state_m = capture_expected_3way_state(expected_3way, task_files, int_root)
                        write_op_journal(collab_dir, {
                            "op": "MergeTask",
                            "phase": "staging",
                            "taskId": target["taskId"],
                            "baseBranch": base_branch,
                            "remote": args.remote,
                            "origMainSha": orig_main_sha,
                            "effectiveBaseCommit": effective_base,
                            "sourceRef": source_ref,
                            "taskBranch": task_branch,
                            "taskFiles": task_files,
                            "mergeType": planned_mtype,
                            "wasReverted": bool(target.get("wasReverted", False)),
                            "integrationRoot": os.path.abspath(int_root),
                            "usedTempWorktree": (os.path.abspath(int_root) != os.path.abspath(local_root)),
                            "origState": orig_state_m,
                            "expectedResultState": exp_state_m,
                        })
                        try:
                            if needs_full_history_reapply:
                                for rel_f, exp_e in expected_3way.items():
                                    abs_f = os.path.join(int_root, rel_f)
                                    if exp_e is None:
                                        if os.path.exists(abs_f) or os.path.islink(abs_f):
                                            os.remove(abs_f)
                                    else:
                                        os.makedirs(os.path.dirname(abs_f), exist_ok=True)
                                        if os.path.islink(abs_f) or (os.path.exists(abs_f) and exp_e["mode"] == "120000"):
                                            try:
                                                os.remove(abs_f)
                                            except OSError:
                                                pass
                                        if exp_e["mode"] == "120000" and hasattr(os, "symlink"):
                                            os.symlink(exp_e["data"].decode("utf-8", errors="surrogateescape"), abs_f)
                                        else:
                                            with open(abs_f, "wb") as wf:
                                                wf.write(exp_e["data"])
                                            if exp_e["mode"] == "100755":
                                                try:
                                                    os.chmod(abs_f, 0o755)
                                                except OSError:
                                                    pass
                                            elif exp_e["mode"] == "100644":
                                                try:
                                                    os.chmod(abs_f, 0o644)
                                                except OSError:
                                                    pass
                                if task_files:
                                    run_git(int_root, "add", "-A", "--", *task_files)
                                    for rel_f, exp_e in expected_3way.items():
                                        if exp_e is not None and exp_e["mode"] in ("100755", "100644"):
                                            run_git(
                                                int_root,
                                                "update-index",
                                                "--chmod=+x" if exp_e["mode"] == "100755" else "--chmod=-x",
                                                "--",
                                                rel_f,
                                                check=False,
                                            )
                                diff_cached = run_git(int_root, "diff", "--cached", "--name-only", check=False).stdout.strip()
                                if diff_cached:
                                    if args.squash:
                                        run_git(int_root, "commit", "-m", f"feat({target['taskId']}): {target['title']}")
                                        target["mergeType"] = "squash"
                                    else:
                                        tree_sha = run_git(int_root, "write-tree").stdout.strip()
                                        commit_msg = f"Merge {task_branch}: {target['title']}"
                                        new_m_sha = run_git(
                                            int_root, "commit-tree", tree_sha, "-p", orig_main_sha, "-p", branch_sha, "-m", commit_msg
                                        ).stdout.strip()
                                        run_git(int_root, "reset", "--soft", new_m_sha)
                                        target["mergeType"] = "merge"
                            elif task_branch != base_branch:
                                if args.squash:
                                    run_git(int_root, "merge", "--squash", task_branch)
                                    for rel_f, exp_e in expected_3way.items():
                                        if exp_e is not None and exp_e["mode"] in ("100755", "100644"):
                                            run_git(
                                                int_root,
                                                "update-index",
                                                "--chmod=+x" if exp_e["mode"] == "100755" else "--chmod=-x",
                                                "--",
                                                rel_f,
                                                check=False,
                                            )
                                    diff_cached = run_git(int_root, "diff", "--cached", "--name-only", check=False).stdout.strip()
                                    if diff_cached:
                                        run_git(int_root, "commit", "-m", f"feat({target['taskId']}): {target['title']}")
                                    target["mergeType"] = "squash"
                                else:
                                    run_git(int_root, "merge", "--no-ff", "-m", f"Merge {task_branch}: {target['title']}", task_branch)
                                    target["mergeType"] = "merge"

                            ok_verify, verify_msg = verify_3way_merge_task_files(
                                int_root, base_branch, orig_main_sha, effective_base, source_ref, task_files
                            )
                            if not ok_verify:
                                raise RuntimeError(verify_msg)
                        except Exception as merge_exc:
                            safe_rollback_task_files(int_root, orig_main_sha, task_files, pre_op_snapshots)
                            clear_op_journal(collab_dir)
                            fail("MERGE_VERIFICATION_ERROR", f"Merge into '{base_branch}' failed and was rolled back cleanly: {merge_exc}")

                        new_main_sha = get_head_commit(int_root, base_branch)
                        if commits_equal(int_root, new_main_sha, orig_main_sha):
                            target["mergeType"] = "noop"
                            target["noopMerge"] = True
                            target["mergedCommit"] = ""
                        else:
                            target["preMergeMainCommit"] = orig_main_sha
                            target["effectiveBaseCommit"] = effective_base
                            target["mergedCommit"] = new_main_sha
                        # Save local merge state immediately BEFORE remote push so a crash during push never loses mergedCommit!
                        save_state()
                else:
                    if target.get("mergedCommit"):
                        is_anc = run_git(int_root, "merge-base", "--is-ancestor", target["mergedCommit"], base_branch, check=False).returncode == 0
                        eff_b = target.get("effectiveBaseCommit") or base_commit
                        ok_still, still_msg = verify_3way_merge_task_files(
                            int_root,
                            base_branch,
                            target.get("preMergeMainCommit") or eff_b,
                            eff_b,
                            reviewed_sha or task_branch,
                            task_files,
                        )
                        if not is_anc or not ok_still:
                            fail(
                                "MERGE_VERIFICATION_ERROR",
                                f"Cannot report delivery for '{target['taskId']}': '{base_branch}' no longer contains the merged changes ({still_msg}).",
                            )

                local_commit = "NOOP" if target.get("noopMerge") else target.get("mergedCommit", "")
                remote_status, pushed_sha = push_branch_to_remote(int_root, base_branch, args.remote)
            if remote_status == "PUSHED" and not target.get("noopMerge"):
                eff_b = target.get("effectiveBaseCommit") or base_commit
                ok_rem, rem_msg = verify_3way_merge_task_files(
                    local_root,
                    f"refs/remotes/{args.remote}/{base_branch}",
                    target.get("preMergeMainCommit") or eff_b,
                    eff_b,
                    reviewed_sha or task_branch,
                    task_files,
                )
                if not ok_rem:
                    remote_status = "FAILED"
            target["remoteBackupStatus"] = remote_status
            if remote_status == "PUSHED":
                target["lastPushedMainCommit"] = pushed_sha
                target["lastPushedCommit"] = pushed_sha

            # Must Fix #4: Require verified remote upload for DELIVERY: COMPLETE; distinguish LOCAL_ONLY when no remote is configured!
            if remote_status == "PUSHED":
                delivery_status = "COMPLETE"
            elif remote_status in ("NO_REMOTE", "NO_GIT"):
                delivery_status = "LOCAL_ONLY"
            else:
                delivery_status = "INCOMPLETE"

            target["deliveryStatus"] = delivery_status
            target["currentStep"] = f"Merged to {base_branch} ({local_commit}) | Remote: {remote_status} | Delivery: {delivery_status}"
            save_state()
            clear_op_journal(collab_dir)

            print("IMPLEMENTATION: COMPLETE (NOOP)" if target.get("noopMerge") else "IMPLEMENTATION: COMPLETE")
            print("REVIEW: APPROVED")
            print(f"LOCAL_COMMIT: {local_commit}")
            print(f"REMOTE_BACKUP: {remote_status}")
            print(f"DELIVERY: {delivery_status}")
            return

        if args.action == "ReleaseTag":
            caller = require_agent()
            tag_name = args.tag.strip()
            base_branch = args.base_branch.strip() or "main"
            tag_msg = args.tag_message.strip() or f"Stable release {tag_name}"
            if not tag_name:
                fail("VALIDATION_ERROR", "--tag is required for ReleaseTag (e.g., --tag v1.0.0).")
            if not is_git_repo(local_root):
                fail("GIT_ERROR", "Not a Git repository.")
            exists = run_git(local_root, "rev-parse", "-q", "--verify", f"refs/tags/{tag_name}", check=False).returncode == 0
            if exists:
                fail("TAG_EXISTS_ERROR", f"Release tag '{tag_name}' already exists. Tags are immutable once published.")

            if run_git(local_root, "show-ref", "--verify", f"refs/heads/{base_branch}", check=False).returncode != 0:
                fail("GIT_ERROR", f"Base branch '{base_branch}' does not exist.")
            if get_current_branch(local_root) != base_branch:
                fail(
                    "UNMERGED_RELEASE_ERROR",
                    f"Cannot create release tag '{tag_name}' while checked out on '{get_current_branch(local_root)}'. "
                    f"Switch to '{base_branch}' after merging reviewed tasks.",
                )

            if args.require_merged_task.strip():
                req_t = get_task_or_fail(args.require_merged_task.strip())
                if req_t.get("reverted") or req_t.get("status") == "reverted" or (not req_t.get("mergedCommit") and not req_t.get("noopMerge")):
                    fail("UNMERGED_RELEASE_ERROR", f"Task '{req_t['taskId']}' has not been merged into '{base_branch}' (or was reverted).")
                if req_t.get("mergedCommit"):
                    is_anc = run_git(local_root, "merge-base", "--is-ancestor", req_t["mergedCommit"], base_branch, check=False).returncode == 0
                    if not is_anc:
                        fail("UNMERGED_RELEASE_ERROR", f"Merged commit '{req_t['mergedCommit']}' of '{req_t['taskId']}' is not on '{base_branch}'.")

            unmerged_approved = [
                t["taskId"] for t in board["tasks"]
                if t["status"] == "done" and not t.get("mergedCommit") and not t.get("noopMerge")
            ]
            if unmerged_approved:
                fail(
                    "UNMERGED_RELEASE_ERROR",
                    f"Approved task(s) {', '.join(unmerged_approved)} have not been merged into '{base_branch}' yet.",
                )
            merged_tasks = [t for t in board["tasks"] if t.get("mergedCommit") and not t.get("reverted")]
            if not merged_tasks and not any(t.get("noopMerge") for t in board["tasks"]):
                fail("UNMERGED_RELEASE_ERROR", f"No reviewed tasks have been merged into '{base_branch}' yet.")
            for mt in merged_tasks:
                is_anc = run_git(local_root, "merge-base", "--is-ancestor", mt["mergedCommit"], base_branch, check=False).returncode == 0
                if not is_anc:
                    fail(
                        "UNMERGED_RELEASE_ERROR",
                        f"Task '{mt['taskId']}' mergedCommit '{mt['mergedCommit']}' is not an ancestor of '{base_branch}'.",
                    )

            run_git(local_root, "tag", "-a", tag_name, base_branch, "-m", tag_msg)
            tag_sha = get_head_commit(local_root, f"refs/tags/{tag_name}^{{commit}}")
            remote_status = "NO_REMOTE"
            if has_git_remote(local_root, args.remote):
                res = run_git(local_root, "push", args.remote, tag_name, check=False)
                remote_status = "PUSHED" if res.returncode == 0 else "FAILED"
            board.setdefault("releases", []).append({
                "tag": tag_name,
                "targetBranch": base_branch,
                "commit": tag_sha,
                "description": tag_msg,
                "remoteBackupStatus": remote_status,
                "createdBy": caller,
                "createdAtUtc": now_utc()
            })
            save_state()
            print(f"RELEASE_TAG_CREATED: {tag_name} on {base_branch} @ {tag_sha} | REMOTE_BACKUP: {remote_status} | {tag_msg}")
            return

        if args.action == "RevertTask":
            caller = require_agent()
            target = get_task_or_fail(args.task_id.strip())
            base_branch = args.base_branch.strip() or "main"

            if target.get("mergeType") == "noop" or target.get("noopMerge"):
                fail("NOOP_REVERT_ERROR", f"Task '{target['taskId']}' was a no-op merge (introduced no file changes) and cannot be reverted.")

            # If an interrupted RevertTask was reconciled from op-journal / Git history, allow the retry to complete cleanly!
            if (target.get("reverted") or target.get("status") == "reverted") and target.get("reconciledInterruptedRevert"):
                target["reconciledInterruptedRevert"] = False
                rev_sha = target.get("revertCommit", "")
                reverted_c = target.get("revertedCommit", "")
                if args.push and target.get("remoteBackupStatus") != "PUSHED":
                    pstat, pushed_sha = push_branch_to_remote(local_root, base_branch, args.remote)
                    target["remoteBackupStatus"] = pstat
                    if pstat == "PUSHED":
                        target["lastPushedMainCommit"] = pushed_sha
                        target["lastPushedCommit"] = pushed_sha
                save_state()
                is_mc = target.get("mergeType") == "merge"
                print(f"REVERT_COMMIT: {rev_sha} (undid {reverted_c} for {target['taskId']}, mergeCommit={is_mc})")
                return

            if target.get("reverted") or target.get("status") == "reverted":
                fail("ALREADY_REVERTED_ERROR", f"Task '{target['taskId']}' has already been reverted (revert commit: {target.get('revertCommit', 'unknown')}).")

            commit_to_revert = target.get("mergedCommit", "")
            if not commit_to_revert:
                fail("STATE_ERROR", f"Task '{target['taskId']}' has no recorded merge commit on '{base_branch}' to revert.")
            if commits_equal(local_root, commit_to_revert, target.get("baseCommit", "")):
                fail("NOOP_REVERT_ERROR", f"Refusing to revert base commit '{commit_to_revert}' for '{target['taskId']}'.")

            commit_changed = get_commit_changed_files(local_root, commit_to_revert, local_root)
            branch_changed = []
            if target.get("baseCommit") and (target.get("reviewTargetCommit") or target.get("gitBranch")):
                ref_b = target.get("reviewTargetCommit") or target.get("gitBranch")
                branch_changed = get_diff_changed_files(local_root, target["baseCommit"], ref_b, local_root)
            task_files = union_ordered(
                commit_changed,
                branch_changed,
                normalize_file_list(target.get("targetFiles", []), local_root),
                normalize_file_list(target.get("ownedFiles", []), local_root),
            )
            active_locked = get_all_locked_files(exclude_task_id=target["taskId"])
            overlap = set(task_files) & active_locked
            if overlap:
                fail(
                    "FILE_CONFLICT_ERROR",
                    f"Cannot revert '{target['taskId']}' because file(s) {', '.join(sorted(overlap))} modified by commit '{commit_to_revert}' are currently locked by another active task.",
                )

            with integration_checkout_context(
                local_root, collab_dir, board, base_branch, args.use_worktree, "RevertTask", target["taskId"], task_files
            ) as int_root:
                orig_main_sha = get_head_commit(int_root, base_branch)
                pre_rev_snapshots = {
                    f: (open(os.path.join(int_root, f), "rb").read() if os.path.exists(os.path.join(int_root, f)) else None)
                    for f in task_files
                }
                rev_list = run_git(int_root, "rev-list", "--parents", "-n", "1", commit_to_revert).stdout.strip().split()
                is_merge_commit = len(rev_list) > 2 or target.get("mergeType") == "merge"
                pre_merge_parent = rev_list[1] if len(rev_list) > 1 else ""
                orig_state_r = capture_tree_state(int_root, orig_main_sha, task_files)
                exp_state_r = {}
                if pre_merge_parent:
                    ok_r3w, _, exp_rev_3way = compute_3way_merged_files(
                        int_root, orig_main_sha, commit_to_revert, pre_merge_parent, task_files
                    )
                    if ok_r3w:
                        exp_state_r = capture_expected_3way_state(exp_rev_3way, task_files, int_root)
                write_op_journal(collab_dir, {
                    "op": "RevertTask",
                    "phase": "staging",
                    "taskId": target["taskId"],
                    "caller": caller,
                    "baseBranch": base_branch,
                    "remote": args.remote,
                    "origMainSha": orig_main_sha,
                    "commitToRevert": commit_to_revert,
                    "isMergeCommit": is_merge_commit,
                    "taskFiles": task_files,
                    "integrationRoot": os.path.abspath(int_root),
                    "usedTempWorktree": (os.path.abspath(int_root) != os.path.abspath(local_root)),
                    "origState": orig_state_r,
                    "expectedResultState": exp_state_r,
                })
                try:
                    if is_merge_commit:
                        run_git(int_root, "revert", "-m", "1", "--no-edit", commit_to_revert)
                    else:
                        run_git(int_root, "revert", "--no-edit", commit_to_revert)
                except Exception as rev_exc:
                    run_git(int_root, "revert", "--abort", check=False)
                    safe_rollback_task_files(int_root, orig_main_sha, task_files, pre_rev_snapshots)
                    clear_op_journal(collab_dir)
                    fail("REVERT_CONFLICT_ERROR", f"Revert of {commit_to_revert} failed and was rolled back cleanly: {rev_exc}")

                rev_sha = get_head_commit(int_root, "HEAD")
                target["status"] = "reverted"
                target["reverted"] = True
                target["wasReverted"] = True
                target["revertedCommit"] = commit_to_revert
                target["revertCommit"] = rev_sha
                target["mergedCommit"] = ""
                target["deliveryStatus"] = "REVERTED"
                target["remoteBackupStatus"] = "UNPUSHED"
                target["currentStep"] = f"Reverted commit {commit_to_revert} via {rev_sha} by {caller}"
                target["updatedAtUtc"] = now_utc()
                # Save local revert state immediately BEFORE remote push!
                save_state()

                if args.push:
                    pstat, pushed_sha = push_branch_to_remote(int_root, base_branch, args.remote)
                else:
                    pstat, pushed_sha = "UNPUSHED", ""
            target["remoteBackupStatus"] = pstat
            if pstat == "PUSHED":
                target["lastPushedMainCommit"] = pushed_sha
                target["lastPushedCommit"] = pushed_sha
            target["updatedAtUtc"] = now_utc()
            save_state()
            clear_op_journal(collab_dir)
            print(f"REVERT_COMMIT: {rev_sha} (undid {commit_to_revert} for {target['taskId']}, mergeCommit={is_merge_commit})")
            return

        if args.action == "RecoverTag":
            tag_name = args.tag.strip()
            rec_branch = args.recovery_branch.strip() or f"recover/{tag_name}"
            if not tag_name:
                fail("VALIDATION_ERROR", "--tag is required for RecoverTag.")
            run_git(local_root, "checkout", "-b", rec_branch, tag_name)
            sha = get_head_commit(local_root, "HEAD")
            print(f"RECOVERY_BRANCH_CREATED: {rec_branch} from {tag_name} @ {sha}")
            return

        if args.action == "WebPacket":
            target = get_task_or_fail(args.task_id.strip())
            sync_backup_status(target)
            print(f"=== ON-WEB AI HANDOFF PACKET ({target['taskId']}) ===")
            print(f"Project: {board['projectName']} | Repo/Backup URL: {saved_config.get('scopedFolderUrl', '')}")
            print(f"Exclusive Lock Owner: {target['assignedTo']} (Handoff from: {target.get('handoffFrom', 'None')})")
            print(f"Git Branch & Commit: {target.get('gitBranch', 'N/A')} @ {target.get('latestCommit', 'N/A')} (Remote: {target.get('remoteBackupStatus', 'UNPUSHED')})")
            print(f"Last Prompt: {target.get('lastUserPrompt', '')}")
            print(f"Changes So Far: {target.get('changesSummary', '')}")
            print(f"Remaining Todo: {target.get('remainingTodo', '')}")
            print(f"Locked Files: {', '.join(target.get('ownedFiles', []))}")
            return

        if args.action == "Tasks":
            for t in board["tasks"]:
                if t["status"] == "todo" and is_unblocked_and_free(t):
                    print(f"- {t['taskId']}: {t['title']} (Branch: {t.get('gitBranch', '')} | Files: {', '.join(t.get('targetFiles', []))})")
            return

        if args.action == "Status":
            for t in board["tasks"]:
                sync_backup_status(t)
            if args.render_format == "CLI":
                print("+--------------------------------------------------------------------------------------------+")
                print(f"| /collab status :: {board['projectName']} (Lead AI: {board['primaryLeadAgent']} | Rev: {board.get('revision', 1)})")
                print("+---------+----------------------+--------------------+------------+------------+------------+")
                print("| TASK ID | TITLE                | LOCK / OWNER       | PROGRESS   | COMMIT     | BACKUP     |")
                print("+---------+----------------------+--------------------+------------+------------+------------+")
                for t in board["tasks"]:
                    bar = ascii_bar(t["progressPercent"])
                    if t["status"] == "done":
                        owner = f"DONE({t['assignedTo']})"
                    elif t["status"] == "reverted" or t.get("reverted"):
                        owner = f"REVERTED({t['assignedTo']})"
                    elif t["status"] == "review_pending":
                        owner = f"REVIEW({t['assignedTo']})"
                    else:
                        owner = t["assignedTo"] or "UNCLAIMED"
                    commit_str = ("NOOP" if t.get("noopMerge") else (t.get("mergedCommit") or t.get("revertCommit") or t.get("reviewTargetCommit") or t.get("latestCommit") or "-"))[:10]
                    bstat = (t.get("remoteBackupStatus") or "UNPUSHED")[:10]
                    print(f"| {t['taskId']:<7} | {t['title'][:20]:<20} | {owner[:18]:<18} | {bar:<10} | {commit_str:<10} | {bstat:<10} |")
                print("+---------+----------------------+--------------------+------------+------------+------------+")
            else:
                print(f"### `/collab status` - {board['projectName']} (Lead AI: `{board['primaryLeadAgent']}` | Rev: `{board.get('revision', 1)}`)\n")
                print("| Task | Status | Lock / Owner | Branch & Commit | Remote Backup | Progress | Last Prompt & Changes |")
                print("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
                for t in board["tasks"]:
                    if t["status"] == "done":
                        owner = f"Unlocked (Done by `{t['assignedTo']}`)"
                    elif t["status"] == "reverted" or t.get("reverted"):
                        owner = f"Reverted (`{t['assignedTo']}`)"
                    elif t["status"] == "review_pending":
                        owner = f"Locked for Review (`{t['assignedTo']}`)"
                    else:
                        owner = f"Locked by `{t['assignedTo']}`" if t["assignedTo"] else "*Unclaimed*"
                    csha = "NOOP" if t.get("noopMerge") else (t.get("mergedCommit") or t.get("revertCommit") or t.get("reviewTargetCommit") or t.get("latestCommit") or "none")
                    binfo = f"`{t.get('gitBranch', '-')}` (`{csha}`)"
                    pinfo = f"**Prompt:** *{t['lastUserPrompt']}* - **Changes:** {t['changesSummary']}" if t.get("lastUserPrompt") else f"*{t['currentStep']}*"
                    print(f"| **{t['taskId']}**: {t['title']} | `{t['status'].upper()}` | {owner} | {binfo} | `{t.get('remoteBackupStatus', 'UNPUSHED')}` | **{t['progressPercent']}%** | {pinfo} |")


if __name__ == "__main__":
    main()
