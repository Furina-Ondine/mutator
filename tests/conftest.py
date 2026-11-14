import pytest


@pytest.fixture
def can_symlink(tmp_path):
    target = tmp_path / "symlink-target"
    target.write_text("probe", encoding="utf-8")
    link = tmp_path / "symlink-probe"
    try:
        link.symlink_to(target)
    except OSError:
        return False
    link.unlink()
    target.unlink()
    return True
