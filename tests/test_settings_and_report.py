import stat
import subprocess

from weaveconnect import settings
from weaveconnect.report import Report, Status


def test_a_report_with_no_checks_proves_nothing():
    assert Report("empty").status is Status.UNCHECKED
    assert Report("empty").exit_code == 2


def test_one_failure_outranks_everything_and_unchecked_outranks_pass():
    report = Report("mixed")
    report.add("a", Status.PASS, "")
    report.add("b", Status.UNCHECKED, "")
    assert report.status is Status.UNCHECKED
    report.add("c", Status.FAIL, "")
    assert report.status is Status.FAIL
    assert report.exit_code == 1


def test_write_env_keeps_other_lines_and_is_private(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# keep me\nOTHER=1\nWEAVE_PROJECT=old\n")
    settings.write_env(path, {"WEAVE_PROJECT": "team/new", "WANDB_API_KEY": "k" * 40})
    text = path.read_text()
    assert "# keep me" in text and "OTHER=1" in text
    assert "WEAVE_PROJECT=team/new" in text and "WEAVE_PROJECT=old" not in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_read_env_handles_quotes_exports_and_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text("export A='one'\nB=\"two\"\n# C=three\n")
    assert settings.read_env(path) == {"A": "one", "B": "two"}


def test_ignore_env_adds_the_rule_once_in_a_git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert settings.ignore_env(tmp_path) == (True, True)
    assert settings.git_ignores(tmp_path) is True
    assert settings.ignore_env(tmp_path) == (True, False)
    assert (tmp_path / ".gitignore").read_text().count(".env") == 1


def test_ignore_env_outside_git_still_writes_the_rule(tmp_path):
    assert settings.ignore_env(tmp_path) == (True, True)
    assert ".env" in (tmp_path / ".gitignore").read_text()


def test_secret_values_pick_secret_names_long_enough_to_matter():
    found = settings.secret_values(
        {"WANDB_API_KEY": "x" * 40, "DB_PASSWORD": "short", "USER": "y" * 20}
    )
    assert found == ["x" * 40]


def test_project_choice_order(tmp_path, monkeypatch):
    monkeypatch.delenv("WEAVE_PROJECT", raising=False)
    folder = tmp_path / "my agent!"
    folder.mkdir()
    assert settings.resolve_project(None, folder, {}) == "my-agent"
    assert settings.resolve_project(None, folder, {"WEAVE_PROJECT": "team/a"}) == "team/a"
    monkeypatch.setenv("WEAVE_PROJECT", "team/b")
    assert settings.resolve_project(None, folder, {"WEAVE_PROJECT": "team/a"}) == "team/b"
    assert settings.resolve_project("team/c", folder, {}) == "team/c"
