from pathlib import Path

import pytest

from cryptomathxbot.single_instance import AlreadyRunningError, SingleInstanceLock


def test_second_instance_cannot_acquire_lock(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"

    with SingleInstanceLock(path):
        with pytest.raises(AlreadyRunningError):
            with SingleInstanceLock(path):
                pass

    with SingleInstanceLock(path):
        assert path.read_text(encoding="ascii").isdigit()
