from pathlib import Path

from lap_build_agent.pipeline import cleanup_stale_workspaces


def test_startup_cleanup_removes_only_agent_workspaces(tmp_path: Path) -> None:
    workspaces = tmp_path / "workspaces"
    stale = workspaces / "build-00000000-0000-4000-8000-000000000001-old"
    unrelated = workspaces / "build-part-one-smoke"
    stale.mkdir(parents=True)
    unrelated.mkdir()
    (stale / "artifact").write_text("temporary")

    cleanup_stale_workspaces(tmp_path)

    assert not stale.exists()
    assert unrelated.exists()
