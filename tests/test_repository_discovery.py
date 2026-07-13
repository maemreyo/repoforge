import subprocess
from pathlib import Path

from repoforge.adapters.repository.discovery import LocalRepositoryDiscovery
from repoforge.adapters.subprocess import SubprocessCommandExecutor
from repoforge.config import ServerConfig
from repoforge.ports.repository_discovery import DiscoveryRequest


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    git("init", "-q", cwd=path)
    git("config", "user.email", "x@y", cwd=path)
    git("config", "user.name", "x", cwd=path)
    (path / "README.md").write_text("x")
    git("add", ".", cwd=path)
    git("commit", "-qm", "init", cwd=path)


def test_discovery_keeps_primary_and_reports_linked_worktree(tmp_path: Path) -> None:
    primary = tmp_path / "repo"
    linked = tmp_path / ".claude" / "worktrees" / "agent"
    init_repo(primary)
    linked.parent.mkdir(parents=True)
    git("worktree", "add", "-q", str(linked), cwd=primary)
    adapter = LocalRepositoryDiscovery(
        SubprocessCommandExecutor(ServerConfig(tmp_path / "w", tmp_path / "s"))
    )
    identities = adapter.discover(DiscoveryRequest((tmp_path,), 8, (), (), ()))
    by_path = {item.path: item for item in identities}
    assert by_path[str(primary.resolve())].primary is True
    assert by_path[str(linked.resolve())].primary is False


def test_discovery_does_not_follow_symlink_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    init_repo(outside)
    (root / "escape").symlink_to(outside, target_is_directory=True)
    adapter = LocalRepositoryDiscovery(
        SubprocessCommandExecutor(ServerConfig(tmp_path / "w", tmp_path / "s"))
    )
    assert adapter.discover(DiscoveryRequest((root,), 8, (), (), ())) == ()
