from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

docx_stub = SimpleNamespace(
    Document=object,
    opc=SimpleNamespace(exceptions=SimpleNamespace(PackageNotFoundError=Exception)),
)
sys.modules.setdefault("docx", docx_stub)
sys.modules.setdefault("docx.opc", docx_stub.opc)
sys.modules.setdefault("docx.opc.exceptions", docx_stub.opc.exceptions)

openpyxl_stub = SimpleNamespace(Workbook=object, load_workbook=lambda *args, **kwargs: None)
sys.modules.setdefault("openpyxl", openpyxl_stub)

pptx_stub = SimpleNamespace(Presentation=object, util=SimpleNamespace(Inches=lambda value: value))
sys.modules.setdefault("pptx", pptx_stub)
sys.modules.setdefault("pptx.util", pptx_stub.util)

import config
from intent_router import AgentSession, IntentRouter
from tools.app_indexer import AppIndexer
from tools import apps as apps_module


def _fake_load_config(name: str) -> dict:
    if name == "paths":
        return {"whitelist": []}
    if name == "apps":
        return {"apps": {}}
    return {}


def test_scan_collects_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    common_dir = tmp_path / "ProgramData"
    user_dir = tmp_path / "UserData"
    common_dir.mkdir()
    user_dir.mkdir()

    target_exe = tmp_path / "apps" / "cool" / "CoolApp.exe"
    target_exe.parent.mkdir(parents=True, exist_ok=True)
    target_exe.write_text("run")

    game_target = tmp_path / "apps" / "game" / "Game.exe"
    game_target.parent.mkdir(parents=True, exist_ok=True)
    game_target.write_text("run")

    (common_dir / "Cool App (x64).lnk").write_text("lnk")
    (user_dir / "Game - ссылка.lnk").write_text("lnk")
    (common_dir / "Broken.lnk").write_text("lnk")

    url_path = user_dir / "Site.url"
    url_path.write_text("[InternetShortcut]\nURL=https://example.com\n")

    appref_path = user_dir / "Tool.appref-ms"
    appref_path.write_text("deploymentProviderUrl=http://example.com/tool\n")

    missing_target = tmp_path / "missing" / "gone.exe"

    def fake_resolve_lnk(shortcut: Path) -> tuple[str, str]:
        name = shortcut.name
        if "Cool App" in name:
            return str(target_exe), "--flag"
        if "Game" in name:
            return str(game_target), ""
        return str(missing_target), ""

    monkeypatch.setattr(AppIndexer, "resolve_lnk", staticmethod(fake_resolve_lnk))

    indexer = AppIndexer(
        cache_dir=tmp_path / "cache",
        start_menu_dirs={"common": common_dir, "user": user_dir},
    )

    items = indexer.scan()
    assert len(items) == 4
    names = {item["name"]: item for item in items}
    assert "Cool App" in names
    assert names["Cool App"]["args"] == "--flag"
    assert names["Cool App"]["source"] == "common"
    assert names["Game"]["source"] == "user"
    assert names["Site"]["path"] == "https://example.com"
    assert names["Tool"]["path"] == "http://example.com/tool"
    assert "Broken" not in names


def test_cache_roundtrip(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    indexer = AppIndexer(cache_dir=cache_dir, start_menu_dirs={}, cache_ttl=3600)
    sample = [
        {
            "name": "Sample",
            "path": "C:/Apps/Sample.exe",
            "args": "--mode",
            "shortcut": "C:/Shortcuts/Sample.lnk",
            "source": "user",
            "score_boost": 5,
        }
    ]
    indexer.save_cache(sample)
    loaded = indexer.load_cache()
    assert loaded == sample


def test_launch_prefers_shortcut(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = apps_module.IndexedEntry(
        name="Shortcut App",
        path="",
        args="",
        shortcut="C:/Links/Shortcut.lnk",
        source="user",
        score=95.0,
    )

    calls: List[str] = []

    monkeypatch.setattr(apps_module._MANAGER, "_match_manual", lambda _: None)
    monkeypatch.setattr(apps_module._MANAGER, "candidates", lambda *args, **kwargs: [entry])
    monkeypatch.setattr(apps_module, "_startfile", lambda value: calls.append(value) or True)

    popen_calls: List[List[str]] = []

    class DummyProc:
        def __init__(self, args: List[str]) -> None:
            self.args = args

    monkeypatch.setattr(apps_module.subprocess, "Popen", lambda args: popen_calls.append(list(args)) or DummyProc(list(args)))

    result = apps_module.launch("shortcut app")

    assert result["ok"] is True
    assert calls == ["C:/Links/Shortcut.lnk"]
    assert popen_calls == []


def test_launch_executes_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe_path = tmp_path / "Editor.exe"
    exe_path.write_text("run")

    entry = apps_module.IndexedEntry(
        name="Editor",
        path=str(exe_path),
        args='--flag "value with space"',
        shortcut="",
        source="user",
        score=92.0,
    )

    monkeypatch.setattr(apps_module._MANAGER, "_match_manual", lambda _: None)
    monkeypatch.setattr(apps_module._MANAGER, "candidates", lambda *args, **kwargs: [entry])
    monkeypatch.setattr(apps_module, "_startfile", lambda value: (_ for _ in ()).throw(AssertionError("startfile not expected")))
    monkeypatch.setattr(apps_module.platform, "system", lambda: "Windows")

    popen_calls: List[List[str]] = []

    class DummyProc:
        def __init__(self, args: List[str]) -> None:
            self.args = args

    monkeypatch.setattr(apps_module.subprocess, "Popen", lambda args: popen_calls.append(list(args)) or DummyProc(list(args)))

    result = apps_module.launch("editor")

    assert result["ok"] is True
    assert len(popen_calls) == 1
    command = popen_calls[0]
    assert command[0] == str(exe_path)
    assert command[1] == "--flag"
    assert command[2].strip('"') == "value with space"


def test_refresh_apps_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load_config", _fake_load_config)
    monkeypatch.setattr("intent_router.load_config", _fake_load_config)

    monkeypatch.setattr(apps_module, "refresh_index", lambda: {"ok": True, "count": 42})

    router = IntentRouter()
    router.apps = apps_module

    response = router.handle_message("пересканируй приложения", AgentSession(), {})

    assert response.get("ok") is True
    assert "42" in response.get("reply", "")
import sys
from types import SimpleNamespace
from pathlib import Path
import pytest

from tools.app_indexer import AppIndexer

def test_resolve_lnk_handles_both_TargetPath_casings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "MyApp.exe"
    target.write_text("run")

    # Вариант 1: TargetPath
    class ShortcutUpper:
        TargetPath = str(target)
        Arguments = "--upper"

    # Вариант 2: Targetpath
    class ShortcutLower:
        Targetpath = str(target)
        Arguments = "--lower"

    class FakeShell:
        def __init__(self, kind: str):
            self.kind = kind
        def CreateShortcut(self, _path: str):
            return ShortcutUpper() if self.kind == "upper" else ShortcutLower()

    class FakeClientModule:
        def __init__(self, kind: str):
            self.kind = kind
        def Dispatch(self, _name: str) -> FakeShell:
            return FakeShell(self.kind)

    # Прогоняем оба варианта регистров
    for kind in ("upper", "lower"):
        fake_module = SimpleNamespace(client=FakeClientModule(kind))
        monkeypatch.setitem(sys.modules, "win32com", fake_module)
        monkeypatch.setitem(sys.modules, "win32com.client", fake_module.client)

        shortcut_path = tmp_path / f"Sample_{kind}.lnk"
        shortcut_path.write_text("lnk")

        result = AppIndexer.resolve_lnk(shortcut_path)
        assert result == (str(target), f"--{kind}")
