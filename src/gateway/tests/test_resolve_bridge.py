from __future__ import annotations

import pytest

from gateway.resolve.bridge import ResolveBridge, _find_rendered_output


class FakeProject:
    def __init__(self, name: str):
        self._name = name

    def GetName(self) -> str:  # noqa: N802 (matching Resolve API casing)
        return self._name

    def GetMediaPool(self):  # pragma: no cover - not used in these tests
        return None

    def GetCurrentTimeline(self):  # pragma: no cover - not used in these tests
        return None


class FakeProjectManager:
    def __init__(self, current=None, to_load=None, to_create=None):
        self.current = current
        self.to_load = to_load
        self.to_create = to_create
        self.load_calls = 0
        self.create_calls = 0

    def GetCurrentProject(self):  # noqa: N802
        return self.current

    def LoadProject(self, name):  # noqa: N802
        self.load_calls += 1
        return self.to_load

    def CreateProject(self, name):  # noqa: N802
        self.create_calls += 1
        return self.to_create


class FakeResolve:
    def __init__(self, manager):
        self._manager = manager

    def GetProjectManager(self):  # noqa: N802
        return self._manager


def test_ensure_project_prefers_current_project():
    manager = FakeProjectManager(current=FakeProject("demo"))
    bridge = ResolveBridge(project_name="demo")
    bridge._resolve = FakeResolve(manager)

    project = bridge._ensure_project_loaded()

    assert project is manager.current
    assert manager.load_calls == 0
    assert manager.create_calls == 0


def test_ensure_project_loads_or_creates_when_missing():
    manager = FakeProjectManager(
        current=None,
        to_load=None,
        to_create=FakeProject("demo"),
    )
    bridge = ResolveBridge(project_name="demo")
    bridge._resolve = FakeResolve(manager)

    project = bridge._ensure_project_loaded()

    assert project.GetName() == "demo"
    assert manager.load_calls == 1
    assert manager.create_calls == 1


def test_ensure_project_raises_when_unavailable():
    manager = FakeProjectManager(current=None, to_load=None, to_create=None)
    bridge = ResolveBridge(project_name="demo")
    bridge._resolve = FakeResolve(manager)

    with pytest.raises(RuntimeError):
        bridge._ensure_project_loaded()


def test_find_rendered_output_accepts_resolve_changed_extension(tmp_path):
    requested = tmp_path / "proxy.mp4"
    actual = tmp_path / "proxy.mov"
    actual.write_text("render", encoding="utf-8")

    assert _find_rendered_output(requested) == actual
