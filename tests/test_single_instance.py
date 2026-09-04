from pathlib import Path

import pytest

from cryptomathxbot.single_instance import (
    AlreadyRunningError,
    InstanceLockError,
    SingleInstanceLock,
    _windows_mutex_name,
)


def test_second_instance_cannot_acquire_lock(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"

    with SingleInstanceLock(path):
        with pytest.raises(AlreadyRunningError):
            with SingleInstanceLock(path):
                pass

    with SingleInstanceLock(path):
        assert path.read_text(encoding="ascii").isdigit()


def test_invalid_lock_path_is_not_reported_as_another_process(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"
    path.mkdir()

    with pytest.raises(InstanceLockError):
        with SingleInstanceLock(path):
            pass


def test_windows_mutex_uses_cross_session_namespace(tmp_path: Path) -> None:
    name = _windows_mutex_name((tmp_path / "bot.lock").resolve())

    assert name.startswith("Global\\CryptoMathXBot-")
