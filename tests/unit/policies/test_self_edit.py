from pathlib import Path

import pytest

from policies.self_edit import SelfEditPolicy


def test_new_file_is_authored_and_then_updatable(tmp_path: Path) -> None:
    policy = SelfEditPolicy(tmp_path / "repo", tmp_path / "mutations")
    root = tmp_path / "repo"
    root.mkdir()
    path = root / "generated.py"

    mutation = policy.begin(path, "create")
    path.write_text("v1", encoding="utf-8")
    policy.commit(mutation)

    update = policy.begin(path, "update")
    path.write_text("v2", encoding="utf-8")
    policy.commit(update)

    assert policy.revert(update.id)
    assert path.read_text(encoding="utf-8") == "v1"


def test_user_authored_file_cannot_be_updated(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    path = root / "user.py"
    path.write_text("user", encoding="utf-8")

    policy = SelfEditPolicy(root, tmp_path / "mutations")

    with pytest.raises(PermissionError, match="was not created by North"):
        policy.begin(path, "update")


@pytest.mark.parametrize(
    "relative",
    ["approval/policy.py", "tools/_path.py", "ledger/main.db", "inference/router.py", "config/settings.py", ".env"],
)
def test_protected_paths_are_always_frozen(tmp_path: Path, relative: str) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    policy = SelfEditPolicy(root, tmp_path / "mutations")

    with pytest.raises(PermissionError, match="frozen"):
        policy.begin(path, "create")
