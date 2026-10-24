"""Read app repositories: list their versions and fetch the files the API needs.

App repositories are public and read anonymously over HTTPS (.github#7 B).
Everything goes through the git protocol rather than a hosting provider's
REST API: ``git ls-remote`` works the same on GitHub, GitLab and Forgejo,
needs no token, and is not subject to GitHub's limit of 60 anonymous API
requests per hour. There is no platform token any more, so a registered URL
pointing at someone else's server can no longer collect one, and nothing
token-bearing is ever written into a clone.

A version is a tag, and what gets approved and deployed is the commit the
tag points to (its SHA), never the tag name alone (.github#7 G): a tag can be
moved, and an approval must not silently carry over to code nobody reviewed.

Every URL passes the host allowlist (``APP_GIT_ALLOWED_HOSTS``) first.
"""

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

import git

from app.config import settings
from app.git_urls import checked_https_url, parse_allowed_hosts

logger = logging.getLogger(__name__)

# Bounded so a hanging remote cannot block a request worker indefinitely.
LS_REMOTE_TIMEOUT_SECONDS = 20
FETCH_TIMEOUT_SECONDS = 60

# Anonymous only: never ask for credentials (a private or missing repository
# then fails instead of hanging on a prompt), never consult a helper.
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}
_NO_HELPER = ["-c", "credential.helper="]


class CommitNotServed(Exception):
    """The pinned commit cannot be fetched: the server refuses fetching by SHA
    and ``tag`` no longer points to it."""

    def __init__(self, tag: str, commit_sha: str) -> None:
        super().__init__(f"tag {tag} no longer points to {commit_sha[:8]}, which the server does not serve directly")
        self.tag = tag
        self.commit_sha = commit_sha


def allowed_hosts() -> frozenset[str]:
    """``APP_GIT_ALLOWED_HOSTS`` as a set."""
    return parse_allowed_hosts(settings.APP_GIT_ALLOWED_HOSTS)


def checked_url(git_url: str) -> str:
    """The HTTPS URL to fetch from. Raises ``GitHostNotAllowed``."""
    return checked_https_url(git_url, allowed_hosts())


def _version_sort_key(v: dict[str, Any]) -> tuple:
    """Newest first for ``vX.Y.Z`` tags; anything else sorts after, by name."""
    try:
        return (1, tuple(map(int, v["version"].lstrip("v").split("."))))
    except (ValueError, AttributeError):
        return (0, v["version"])


class GitService:
    """Git operations on app repositories: tag listing, tag resolution and
    sparse clones of one commit.

    Stateless apart from ``TEMP_REPO_BASE_PATH``, the directory sparse
    clones are written to; use the module-level ``git_service`` singleton.
    """

    # Sparse-checkout allowlist (non-cone mode, ``.gitignore`` pathspecs).
    # ``appstore.yaml`` makes a version a pod app (.github#4); ``packer/``
    # matches the whole subtree so both the flat layout and the
    # per-template layout are fetched; of ``terraform/`` only the variables.
    SPARSE_CHECKOUT_FILES = [
        'appstore.yaml',
        'terraform/variables.tf',
        'packer/',
    ]

    def __init__(self) -> None:
        self.base_path = Path(settings.TEMP_REPO_BASE_PATH)

    def _ls_remote_tags(self, git_url: str) -> list[dict[str, Any]]:
        """List the repository's tags with the commit each one points to.

        Each entry is ``{"version": tag, "commit": short SHA, "sha": full
        SHA, "type": "tag"}``. Raises GitHostNotAllowed for a URL outside
        the allowlist and git.GitCommandError if the remote cannot be read.

        Annotated tags are listed twice by ls-remote: the tag object, and
        ``<tag>^{}`` for the commit it peels to. The peeled line wins,
        because the commit is what gets deployed.
        """
        out = str(
            git.cmd.Git().execute(
                ["git", *_NO_HELPER, "ls-remote", "--tags", checked_url(git_url)],
                kill_after_timeout=LS_REMOTE_TIMEOUT_SECONDS,
                env=_GIT_ENV,
            )
        )
        commits: dict[str, str] = {}
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if not ref.startswith("refs/tags/"):
                continue
            name = ref[len("refs/tags/"):]
            if name.endswith("^{}"):
                commits[name[:-3]] = sha
            else:
                commits.setdefault(name, sha)
        return [
            {"version": name, "commit": sha[:8], "sha": sha, "type": "tag"}
            for name, sha in commits.items()
        ]

    def resolve_tag(self, git_url: str, tag: str) -> str | None:
        """The full SHA of the commit ``tag`` points to now, or None if there is no such tag."""
        for v in self._ls_remote_tags(git_url):
            if v["version"] == tag:
                return str(v["sha"])
        return None

    def verify_repository_access(self, git_url: str) -> dict[str, Any]:
        """Check that the repository can be read anonymously.

        Returns a dict with ``success`` and a ``message`` for the user; a
        git failure becomes ``success=False``. GitHostNotAllowed is not
        caught and propagates to the caller.
        """
        try:
            self._ls_remote_tags(git_url)
        except git.GitCommandError as e:
            logger.info("repository %s is not readable: %s", checked_url(git_url), (e.stderr or "").strip())
            return {
                'success': False,
                'message': (
                    f"Repository {checked_url(git_url)} cannot be read. Apps must be "
                    "in a public Git repository reachable over HTTPS."
                ),
            }
        return {'success': True, 'message': "Repository access verified successfully"}

    def clone_release_vars(self, git_url: str, tag: str, commit_sha: str) -> str:
        """Sparse-clone ``commit_sha`` (only ``SPARSE_CHECKOUT_FILES``) and return the local path.

        Reads the commit that is reviewed or deployed, not whatever ``tag``
        points to now. It fetches the commit itself (GitHub, GitLab and
        Forgejo serve any reachable commit); a server that refuses gets the
        tag fetched instead, which must then point at ``commit_sha`` (else
        ``CommitNotServed``). Each call clones into a fresh directory under
        ``TEMP_REPO_BASE_PATH``, so concurrent reads of the same version
        cannot delete each other's clone. The caller removes it with
        ``cleanup_repository``. ``git.GitCommandError`` (repository
        unreadable) propagates; the partial clone is removed.
        """
        self.base_path.mkdir(parents=True, exist_ok=True)
        repo_path = Path(tempfile.mkdtemp(prefix="vars_", dir=self.base_path))
        try:
            logger.info("Fetching %s at %s (%s, sparse)", git_url, tag, commit_sha[:8])
            repo = git.Repo.init(repo_path)
            repo.git.update_environment(**_GIT_ENV)
            with repo.config_writer() as cfg:
                cfg.set_value("core", "sparseCheckout", "true")
                cfg.set_value("credential", "helper", "")
            origin = repo.create_remote('origin', checked_url(git_url))
            sparse_file = repo_path / '.git' / 'info' / 'sparse-checkout'
            sparse_file.parent.mkdir(parents=True, exist_ok=True)
            sparse_file.write_text('\n'.join(self.SPARSE_CHECKOUT_FILES) + '\n')

            try:
                origin.fetch(commit_sha, depth=1, kill_after_timeout=FETCH_TIMEOUT_SECONDS)
            except git.GitCommandError:
                logger.info("fetching the commit was refused; fetching tag %s instead", tag)
                origin.fetch(
                    refspec=f'refs/tags/{tag}:refs/tags/{tag}', depth=1,
                    kill_after_timeout=FETCH_TIMEOUT_SECONDS,
                )
            try:
                repo.git.checkout('--detach', commit_sha, force=True)
            except git.GitCommandError as e:
                raise CommitNotServed(tag, commit_sha) from e
            if repo.head.commit.hexsha != commit_sha:
                raise CommitNotServed(tag, commit_sha)
            return str(repo_path)
        except BaseException:
            shutil.rmtree(repo_path, ignore_errors=True)
            raise

    def get_versions(self, git_url: str) -> list[dict[str, Any]]:
        """List all tags of the repository, newest ``vX.Y.Z`` first.

        Entries have the shape described in ``_ls_remote_tags``. Raises
        GitHostNotAllowed for a URL outside the allowlist and a plain
        Exception carrying git's stderr if the remote cannot be read.
        """
        try:
            versions = self._ls_remote_tags(git_url)
        except git.GitCommandError as e:
            logger.error("Failed to list tags of %s: %s", git_url, (e.stderr or "").strip())
            raise Exception(f"Failed to fetch versions: {(e.stderr or '').strip()}") from e
        versions.sort(key=_version_sort_key, reverse=True)
        logger.info("Found %d versions", len(versions))
        return versions

    def cleanup_repository(self, repo_path: str) -> None:
        """Delete a clone made by ``clone_release_vars``; no-op if it is gone."""
        path = Path(repo_path)
        if path.exists():
            logger.info("Cleaning up %s", repo_path)
            shutil.rmtree(path, ignore_errors=True)


# Singleton instance
git_service = GitService()
