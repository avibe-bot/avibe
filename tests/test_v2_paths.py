import pytest

from config import paths


@pytest.mark.uses_real_paths
def test_paths_are_under_home():
    root = paths.get_vibe_remote_dir()
    assert root.name in {".avibe", ".vibe_remote"}
    assert paths.get_config_path().parent == paths.get_config_dir()
    assert paths.get_settings_path().parent == paths.get_state_dir()
    assert paths.get_sessions_path().parent == paths.get_state_dir()
    assert paths.get_discovered_chats_path().parent == paths.get_state_dir()
    assert paths.get_user_preferences_path().parent == paths.get_state_dir()


def test_ensure_data_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".avibe"))
    paths.ensure_data_dirs()
    assert (tmp_path / ".avibe" / "config").exists()
    assert (tmp_path / ".avibe" / "state").exists()
    assert (tmp_path / ".avibe" / "logs").exists()
    assert (tmp_path / ".avibe" / "runtime").exists()
    assert (tmp_path / ".avibe" / "attachments").exists()
    preferences_path = tmp_path / ".avibe" / "state" / "user_preferences.md"
    assert preferences_path.exists()
    text = preferences_path.read_text(encoding="utf-8")
    assert "# User Context and Preferences" in text
    assert "Prefer adding notes under `## Users`." in text
    assert "### platform/user_id" in text
    assert "communicate, work, and make decisions." in text
    assert "free of secrets unless the user explicitly asks." in text


def test_avibe_home_env_sets_custom_home(tmp_path, monkeypatch):
    avibe_home = tmp_path / "custom-avibe"
    monkeypatch.setenv("AVIBE_HOME", str(avibe_home))

    assert paths.get_vibe_remote_dir() == avibe_home.resolve()

    paths.ensure_data_dirs()

    assert (avibe_home / "state").exists()


def test_legacy_env_is_ignored(tmp_path, monkeypatch):
    custom_legacy_home = tmp_path / "explicit-legacy"
    monkeypatch.delenv("AVIBE_HOME", raising=False)
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(custom_legacy_home))
    monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)

    assert paths.get_vibe_remote_dir() == tmp_path / ".avibe"

    paths.ensure_data_dirs()

    assert not custom_legacy_home.exists()
    assert (tmp_path / ".avibe" / "state").exists()


def test_default_prefers_existing_avibe_home(tmp_path, monkeypatch):
    monkeypatch.delenv("AVIBE_HOME", raising=False)
    monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)
    avibe_home = tmp_path / ".avibe"
    avibe_home.mkdir()

    assert paths.get_vibe_remote_dir() == avibe_home

    paths.ensure_data_dirs()

    assert avibe_home.exists()
    assert (tmp_path / ".vibe_remote").is_symlink()
    assert (tmp_path / ".vibe_remote").resolve() == avibe_home.resolve()


def test_default_adopts_old_user_home_without_data_loss(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AVIBE_HOME", raising=False)
    monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)
    old_home = tmp_path / ".vibe_remote"
    old_state = old_home / "state"
    old_state.mkdir(parents=True)
    (old_state / "settings.json").write_text('{"ok": true}', encoding="utf-8")

    assert paths.get_vibe_remote_dir() == old_home

    paths.ensure_data_dirs()

    avibe_home = tmp_path / ".avibe"
    assert avibe_home.is_dir()
    assert (avibe_home / "state" / "settings.json").read_text(encoding="utf-8") == '{"ok": true}'
    assert old_home.is_symlink()
    assert old_home.resolve() == avibe_home.resolve()
    notice_path = avibe_home / paths.HOME_MIGRATION_NOTICE_PATH
    assert notice_path.exists()
    assert "Migrated runtime home" in notice_path.read_text(encoding="utf-8")
    assert "Migrated runtime home" in capsys.readouterr().err

    paths.ensure_data_dirs()

    assert capsys.readouterr().err == ""
    assert notice_path.read_text(encoding="utf-8").count("Migrated runtime home") == 1


def test_existing_avibe_and_legacy_real_dirs_do_not_clobber(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AVIBE_HOME", raising=False)
    monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)
    avibe_home = tmp_path / ".avibe"
    legacy_home = tmp_path / ".vibe_remote"
    avibe_home.mkdir()
    legacy_home.mkdir()
    (legacy_home / "legacy.txt").write_text("legacy", encoding="utf-8")

    assert paths.get_vibe_remote_dir() == avibe_home

    paths.ensure_data_dirs()

    assert avibe_home.is_dir()
    assert legacy_home.is_dir()
    assert not legacy_home.is_symlink()
    assert (legacy_home / "legacy.txt").read_text(encoding="utf-8") == "legacy"
    notice_path = avibe_home / paths.HOME_MIGRATION_NOTICE_PATH
    assert notice_path.exists()
    assert "was not modified" in notice_path.read_text(encoding="utf-8")
    assert "was not modified" in capsys.readouterr().err


def test_symlink_creation_failure_still_uses_avibe_home(tmp_path, monkeypatch):
    monkeypatch.delenv("AVIBE_HOME", raising=False)
    monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)
    avibe_home = tmp_path / ".avibe"
    avibe_home.mkdir()

    def fail_symlink(self, target, target_is_directory=False):
        raise OSError("no symlink permission")

    monkeypatch.setattr(paths.Path, "symlink_to", fail_symlink)

    paths.ensure_data_dirs()

    assert avibe_home.is_dir()
    assert not (tmp_path / ".vibe_remote").exists()


def test_default_new_user_uses_avibe_home(tmp_path, monkeypatch):
    monkeypatch.delenv("AVIBE_HOME", raising=False)
    monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)

    assert paths.get_vibe_remote_dir() == tmp_path / ".avibe"

    paths.ensure_data_dirs()

    assert (tmp_path / ".avibe" / "state").exists()
    assert not (tmp_path / ".vibe_remote").exists()
