from __future__ import annotations

import logging
import os

from typing import TYPE_CHECKING, Dict, Optional, List
from checkov.common.util import stopit
from detect_secrets.core import scan

from checkov.secrets.git_history_store import GitHistorySecretStore
from checkov.secrets.consts import GIT_HISTORY_NOT_BEEN_REMOVED

if TYPE_CHECKING:
    from detect_secrets import SecretsCollection

os.environ["GIT_PYTHON_REFRESH"] = "quiet"
try:
    import git
    git_import_error = None
except ImportError as e:
    git_import_error = e
COMMIT_HASH_KEY = '==commit_hash=='


def _get_commits_diff(root_folder: str, last_commit_sha: Optional[str] = None) -> List[Dict[str, str | Dict[str, str]]]:
    """
    :param: last_commit_sha = is the last commit we have already scanned. in case it exist the function will
    return the commits from the revision of param to the current head
    """
    commits_diff: List[Dict[str, str | Dict[str, str]]] = []
    if git_import_error is not None:
        logging.warning(f"Unable to load git module (is the git executable available?) {git_import_error}")
        return commits_diff
    try:
        repo = git.Repo(root_folder)
    except Exception as e:
        logging.error(f"Folder {root_folder} is not a GIT project {e}")
        return commits_diff
    if last_commit_sha:
        curr_rev = repo.head.commit.hexsha
        commits = list(repo.iter_commits(last_commit_sha + '..' + curr_rev))
    else:
        commits = list(repo.iter_commits(repo.active_branch))
    for previous_commit_idx in range(len(commits) - 1, 0, -1):
        current_commit_idx = previous_commit_idx - 1
        current_commit_hash = commits[current_commit_idx].hexsha
        git_diff = commits[previous_commit_idx].diff(current_commit_hash, create_patch=True)

        for file_diff in git_diff:
            curr_diff: Dict[str, str | Dict[str, str]] = {
                COMMIT_HASH_KEY: current_commit_hash,
            }
            if file_diff.renamed:
                logging.info(f"File was renamed from {file_diff.rename_from} to {file_diff.rename_to}")
                curr_diff[file_diff.a_path] = {
                    'rename_from': file_diff.rename_from,
                    'rename_to': file_diff.rename_to
                }
                commits_diff.append(curr_diff)
                continue

            elif file_diff.deleted_file:
                logging.info(f"File {file_diff.b_path} was delete")

            base_diff_format = f'diff --git a/{file_diff.a_path} b/{file_diff.b_path}' \
                               f'\nindex 0000..0000 0000\n--- a/{file_diff.a_path}\n+++ b/{file_diff.b_path}\n'
            file_name = file_diff.a_path if file_diff.a_path else file_diff.b_path
            curr_diff[file_name] = base_diff_format + file_diff.diff.decode()
            commits_diff.append(curr_diff)
    return commits_diff


def _scan_history(root_folder: str, secret_store: SecretsCollection,
                  history_store: GitHistorySecretStore, last_commit_scanned: Optional[str] = '') -> None:
    commits_diff = _get_commits_diff(root_folder, last_commit_sha=last_commit_scanned)
    if not commits_diff:
        return

    scanned_file_count = 0
    # the secret key will be {file name}_{hash_value}_{type}
    for commit in commits_diff:
        commit_hash = str(commit[COMMIT_HASH_KEY])
        for file_name in commit.keys():
            if file_name == COMMIT_HASH_KEY:
                continue
            file_diff = commit[file_name]
            if isinstance(file_diff, str):
                file_results = [*scan.scan_diff(file_diff)]
                if file_results:
                    logging.info(
                        f"Found {len(file_results)} secrets in file path {file_name} in commit {commit_hash}, file_results = {file_results}")
                    history_store.set_secret_map(file_results, file_name, commit_hash, commit)
            elif isinstance(file_diff, dict):
                rename_from = file_diff['rename_from']
                rename_to = file_diff['rename_to']
                history_store.handle_renamed_file(rename_from, rename_to, commit_hash)
            scanned_file_count += 1

    # run over the entire history store and create the secret collection
    for secrets_data in history_store.secrets_by_file_value_type.values():
        for secret_data in secrets_data:
            removed = secret_data["removed_commit_hash"] if secret_data[
                "removed_commit_hash"] else GIT_HISTORY_NOT_BEEN_REMOVED
            key = f'{secret_data["added_commit_hash"]}_{removed}_{secret_data["potential_secret"].filename}'
            secret_store[key].add(secret_data["potential_secret"])
    logging.info(f"Scanned {scanned_file_count} git history files")


class GitHistoryScanner:
    def __init__(self, root_folder: str, secrets: SecretsCollection,
                 history_store: Optional[GitHistorySecretStore] = None, timeout: int = 43200):
        self.root_folder = root_folder
        self.secrets = secrets
        self.timeout = timeout
        # in case we start from mid-history (git) we want to continue from where we've been
        self.history_store = history_store or GitHistorySecretStore()

    def scan_history(self, last_commit_scanned: Optional[str] = '') -> bool:
        """return true if the scan finished without timeout"""
        # mark the scan to finish within the timeout
        with stopit.ThreadingTimeout(self.timeout) as to_ctx_mgr:
            _scan_history(self.root_folder, self.secrets, self.history_store, last_commit_scanned)
        if to_ctx_mgr.state == to_ctx_mgr.TIMED_OUT:
            logging.info(f"timeout reached ({self.timeout}), stopping scan.")
            return False
        # else: everything was OK
        return True
