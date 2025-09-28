from config import load_config, refresh_cache


def test_load_apps_config():
    refresh_cache()
    apps_cfg = load_config("apps")
    assert "apps" in apps_cfg
    assert "notepad" in apps_cfg["apps"]


def test_paths_whitelist():
    refresh_cache()
    paths_cfg = load_config("paths")
    whitelist = paths_cfg.get("whitelist")
    assert isinstance(whitelist, list)
    assert any("Documents" in path for path in whitelist)


def test_paths_username_expansion(monkeypatch):
    refresh_cache()
    monkeypatch.setenv("USERNAME", "TestUser")
    paths_cfg = load_config("paths")
    whitelist = paths_cfg.get("whitelist", [])
    assert any("TestUser" in path for path in whitelist)
    assert paths_cfg.get("default_downloads") == r"C:\Users\TestUser\Downloads"
