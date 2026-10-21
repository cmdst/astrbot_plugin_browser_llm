"""tools/migrate_config_v3.py 的 --to 目标写入（S2b2a 临时版导入用）。

约束：--to 省略时行为与旧版逐字节一致（默认目标 = --config）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "migrate_config_v3", ROOT / "tools" / "migrate_config_v3.py"
)
mig = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mig)

from core.config_map import FLAT_KEYS  # noqa: E402


def flat_config() -> dict:
    return {key: f"value-{i}" for i, key in enumerate(FLAT_KEYS)}


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8-sig")


def test_to_dry_run_does_not_write(tmp_path: Path) -> None:
    src = tmp_path / "old.json"
    dst = tmp_path / "new.json"
    _write(src, flat_config())

    assert mig.main(["--config", str(src), "--to", str(dst), "--dry-run"]) == 0
    assert not dst.exists()
    assert json.loads(src.read_text(encoding="utf-8-sig")) == flat_config()


def test_to_apply_writes_target_and_keeps_source(tmp_path: Path) -> None:
    src = tmp_path / "old.json"
    dst = tmp_path / "new.json"
    _write(src, flat_config())

    assert mig.main(["--config", str(src), "--to", str(dst), "--apply"]) == 0

    dst_data = json.loads(dst.read_text(encoding="utf-8-sig"))
    assert set(dst_data) == set(mig.GROUP_ORDER)
    flat, _unknown = mig.flatten(dst_data)
    assert flat == flat_config()

    # 源文件保持扁平、逐值不变
    src_data = json.loads(src.read_text(encoding="utf-8-sig"))
    assert set(src_data) == set(FLAT_KEYS)
    assert src_data == flat_config()


def test_to_verify_uses_target(tmp_path: Path) -> None:
    src = tmp_path / "old.json"
    dst = tmp_path / "new.json"
    _write(src, flat_config())
    assert mig.main(["--config", str(src), "--to", str(dst), "--apply"]) == 0

    assert mig.main(
        ["--config", str(src), "--to", str(dst), "--verify", "--against", str(src)]
    ) == 0

    tampered = flat_config()
    tampered[FLAT_KEYS[0]] = "changed"
    _write(dst, mig.migrate(tampered))  # 键不变、值变
    assert mig.main(
        ["--config", str(src), "--to", str(dst), "--verify", "--against", str(src)]
    ) == 3
