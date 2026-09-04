import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher contract")
def test_failed_install_preserves_existing_runtime(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    shutil.copy2(project_root / "start.ps1", tmp_path / "start.ps1")
    (tmp_path / "requirements-bootstrap.txt").write_text("", encoding="utf-8")
    (tmp_path / "requirements-windows.txt").write_text("@@invalid@@\n", encoding="utf-8")
    runtime = tmp_path / ".runtime-venv"
    runtime.mkdir()
    sentinel = runtime / "working-runtime.txt"
    sentinel.write_text("preserve", encoding="utf-8")

    environment = os.environ.copy()
    environment["CRYPTOMATHX_PYTHON"] = sys.executable
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(tmp_path / "start.ps1"),
            "-Install",
        ],
        cwd=tmp_path,
        capture_output=True,
        timeout=120,
        check=False,
        env=environment,
    )

    assert completed.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not (tmp_path / ".runtime-venv.new").exists()
    assert not (tmp_path / ".runtime-venv.previous").exists()
