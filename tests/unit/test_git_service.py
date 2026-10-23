"""Unit tests for :mod:`app.services.git_service` and :mod:`app.git_urls`.

App repositories are public and read anonymously (.github#7 B); what is
read is always one commit (.github#7 G). GitPython is mocked: no network
and nothing on disk beyond ``tmp_path``.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import git
import pytest

from app.git_urls import (
    GitHostNotAllowed,
    checked_https_url,
    is_commit_sha,
    parse_allowed_hosts,
    to_https_url,
)
from app.services import git_service as git_module
from app.services.git_service import CommitNotServed, GitService

pytestmark = [pytest.mark.unit, pytest.mark.real_git]

SHA = "0123456789abcdef0123456789abcdef01234567"
ALLOWED = {"github.com", "gitlab.dhbw.cloud"}


# ----------------------------------------------------------------
# git_urls
# ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://github.com/acme/app", "https://github.com/acme/app"),
        ("https://github.com/acme/app.git", "https://github.com/acme/app.git"),
        ("git@github.com:acme/app.git", "https://github.com/acme/app.git"),
        ("http://github.com/acme/app/", "https://github.com/acme/app"),
        ("HTTPS://GitHub.com/acme/app", "https://github.com/acme/app"),
        ("https://user:token@github.com/acme/app", "https://github.com/acme/app"),
        ("https://gitlab.dhbw.cloud/group/sub/app", "https://gitlab.dhbw.cloud/group/sub/app"),
    ],
)
def test_checked_url_normalises_to_credential_free_https(raw, expected):
    assert checked_https_url(raw, ALLOWED) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://evil.example/acme/app",          # host not allowed
        "https://gitlab.evil.example/acme/app",   # "gitlab" in the name is no longer special
        "https://github.com:8443/acme/app",       # a port never matches
        "https://10.0.0.1/acme/app",              # IP literal
        "https://[::1]/acme/app",                 # IPv6 literal
        "https://github.com/acme",                # no repository part
        "https://github.com/acme/../secret/app",  # traversal
        "https://github.com/-oProxyCommand/app",  # option-like segment
        "https://github.com/acme/app?ref=x",      # query
        "file:///etc/passwd",
        "",
    ],
)
def test_checked_url_refuses(raw):
    with pytest.raises(GitHostNotAllowed):
        checked_https_url(raw, ALLOWED)


def test_to_https_url_drops_userinfo_and_parse_hosts():
    assert to_https_url("https://x:y@github.com/a/b") == "https://github.com/a/b"
    assert parse_allowed_hosts(" GitHub.com, ,gitlab.dhbw.cloud ") == frozenset({"github.com", "gitlab.dhbw.cloud"})


def test_is_commit_sha():
    assert is_commit_sha(SHA)
    assert not is_commit_sha(SHA[:8])
    assert not is_commit_sha(SHA.upper())
    assert not is_commit_sha(None)


# ----------------------------------------------------------------
# GitService
# ----------------------------------------------------------------
@pytest.fixture
def allowed(monkeypatch):
    monkeypatch.setattr(git_module.settings, "APP_GIT_ALLOWED_HOSTS", "github.com")


def _repo_mock(head_sha: str = SHA) -> MagicMock:
    repo = MagicMock(name="Repo")
    origin = MagicMock(name="Origin")
    repo.create_remote.return_value = origin
    repo.head.commit.hexsha = head_sha
    return repo


def _service(tmp_path, monkeypatch) -> GitService:
    svc = GitService()
    monkeypatch.setattr(svc, "base_path", tmp_path)
    return svc


def test_service_has_no_token():
    svc = GitService()
    assert not hasattr(svc, "token")
    assert not hasattr(git_module.settings, "GIT_ACCESS_TOKEN")


@patch("app.services.git_service.git.Repo")
def test_clone_fetches_exactly_the_commit_anonymously(mock_repo_cls, tmp_path, monkeypatch, allowed):
    repo = _repo_mock()
    mock_repo_cls.init.return_value = repo
    svc = _service(tmp_path, monkeypatch)

    path = svc.clone_release_vars("git@github.com:acme/widgets.git", "v1.2.3", SHA)

    assert path.startswith(str(tmp_path))
    # Remote is the credential-free HTTPS URL; nothing carries a token.
    repo.create_remote.assert_called_once_with("origin", "https://github.com/acme/widgets.git")
    origin = repo.create_remote.return_value
    origin.fetch.assert_called_once()
    assert origin.fetch.call_args.args[0] == SHA
    assert origin.fetch.call_args.kwargs["depth"] == 1
    repo.git.checkout.assert_called_once_with("--detach", SHA, force=True)
    # Git never asks for credentials.
    env = repo.git.update_environment.call_args.kwargs
    assert env["GIT_TERMINAL_PROMPT"] == "0"


@patch("app.services.git_service.git.Repo")
def test_clone_falls_back_to_the_tag_and_checks_it_still_points_there(mock_repo_cls, tmp_path, monkeypatch, allowed):
    repo = _repo_mock()
    origin = repo.create_remote.return_value
    origin.fetch.side_effect = [git.GitCommandError(["git", "fetch"], 128), None]
    mock_repo_cls.init.return_value = repo
    svc = _service(tmp_path, monkeypatch)

    svc.clone_release_vars("https://github.com/acme/widgets", "v1", SHA)

    assert origin.fetch.call_count == 2
    assert origin.fetch.call_args.kwargs["refspec"] == "refs/tags/v1:refs/tags/v1"


@patch("app.services.git_service.git.Repo")
def test_clone_refuses_a_moved_tag(mock_repo_cls, tmp_path, monkeypatch, allowed):
    repo = _repo_mock(head_sha="f" * 40)
    mock_repo_cls.init.return_value = repo
    svc = _service(tmp_path, monkeypatch)

    with pytest.raises(CommitNotServed):
        svc.clone_release_vars("https://github.com/acme/widgets", "v1", SHA)
    # The partial clone is gone.
    assert list(tmp_path.iterdir()) == []


@patch("app.services.git_service.git.Repo")
def test_clone_refuses_a_host_outside_the_allowlist(mock_repo_cls, tmp_path, monkeypatch, allowed):
    mock_repo_cls.init.return_value = _repo_mock()
    svc = _service(tmp_path, monkeypatch)

    with pytest.raises(GitHostNotAllowed):
        svc.clone_release_vars("https://evil.example/acme/widgets", "v1", SHA)
    mock_repo_cls.init.return_value.create_remote.assert_not_called()


LS_REMOTE = "\n".join([
    f"{'a' * 40}\trefs/tags/v1.0.0",
    f"{'b' * 40}\trefs/tags/v1.1.0",
    f"{'c' * 40}\trefs/tags/v1.1.0^{{}}",
    f"{'d' * 40}\trefs/heads/main",
    f"{'e' * 40}\trefs/tags/nightly",
])


def test_versions_peel_annotated_tags_and_sort(monkeypatch, allowed):
    execute = MagicMock(return_value=LS_REMOTE)
    monkeypatch.setattr(git_module.git.cmd.Git, "execute", execute)

    versions = GitService().get_versions("https://github.com/acme/widgets")

    assert [v["version"] for v in versions] == ["v1.1.0", "v1.0.0", "nightly"]
    assert versions[0]["sha"] == "c" * 40  # the commit, not the tag object
    cmd = execute.call_args.args[0]
    assert cmd[:3] == ["git", "-c", "credential.helper="]
    assert cmd[-1] == "https://github.com/acme/widgets"
    assert execute.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_resolve_tag(monkeypatch, allowed):
    monkeypatch.setattr(git_module.git.cmd.Git, "execute", MagicMock(return_value=LS_REMOTE))
    svc = GitService()
    assert svc.resolve_tag("https://github.com/acme/widgets", "v1.0.0") == "a" * 40
    assert svc.resolve_tag("https://github.com/acme/widgets", "v9") is None


def test_unreadable_repository_is_reported_not_raised(monkeypatch, allowed):
    monkeypatch.setattr(
        git_module.git.cmd.Git, "execute",
        MagicMock(side_effect=git.GitCommandError(["git", "ls-remote"], 128, stderr="not found")),
    )
    result = GitService().verify_repository_access("https://github.com/acme/private")
    assert result["success"] is False
    assert "public" in result["message"]


def test_disallowed_host_propagates_from_verify(allowed):
    with pytest.raises(GitHostNotAllowed):
        GitService().verify_repository_access("https://evil.example/acme/app")
