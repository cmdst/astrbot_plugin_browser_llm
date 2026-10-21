"""tools/migrate_config_v3.py：干跑 / 应用 / 校验 / 回滚 / 幂等 / 权限。

迁移器不在包内（tools/ 无 __init__.py），用 importlib 按路径加载。
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

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


def test_flatten_and_nest_roundtrip():
    original = flat_config()
    nested = mig.nest(original)
    flat, unknown = mig.flatten(nested)
    assert flat == original
    assert unknown == []


def test_nest_order_is_declared():
    nested = mig.nest(flat_config())
    assert list(nested) == list(mig.GROUP_ORDER)


def test_migrate_preserves_every_key():
    original = flat_config()
    migrated = mig.migrate(original)
    flat, unknown = mig.flatten(migrated)
    assert unknown == []
    assert set(flat) == set(FLAT_KEYS)
    assert flat == original


def test_unknown_key_refused():
    cfg = flat_config()
    cfg["legacy_key"] = 1
    with pytest.raises(mig.MigrationError):
        mig.migrate(cfg)


def test_unknown_key_allowed_and_preserved():
    cfg = flat_config()
    cfg["legacy_key"] = 1
    migrated = mig.migrate(cfg, allow_unknown=True)
    assert migrated["legacy_key"] == 1


def test_idempotent():
    once = mig.migrate(flat_config())
    assert mig.is_migrated(once) is True
    twice = mig.migrate(once)
    assert twice == once


def test_is_migrated_false_for_flat():
    assert mig.is_migrated(flat_config()) is False


def test_diff_keys_reports_names_only():
    a = flat_config()
    b = dict(a)
    b["browser_type"] = "different"
    diff = mig.diff_keys(a, b)
    assert diff["left_only"] == []
    assert diff["right_only"] == []
    assert "different" not in json.dumps(diff)


def test_cli_dry_run_does_not_write(tmp_path: Path):
    path = tmp_path / "cfg.json"
    original = flat_config()
    path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()
    code = mig.main(["--config", str(path), "--dry-run"])
    assert code == 0
    assert path.read_bytes() == before


def test_cli_apply_writes_bom_and_600_and_backup(tmp_path: Path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(flat_config(), ensure_ascii=False), encoding="utf-8")
    code = mig.main(["--config", str(path), "--apply"])
    assert code == 0
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    backups = list(tmp_path.glob("cfg.json.bak-pre-v3-*"))
    assert len(backups) == 1
    assert stat.S_IMODE(os.stat(backups[0]).st_mode) == 0o600


def test_cli_apply_idempotent(tmp_path: Path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(flat_config(), ensure_ascii=False), encoding="utf-8")
    assert mig.main(["--config", str(path), "--apply"]) == 0
    snapshot = path.read_bytes()
    assert mig.main(["--config", str(path), "--apply"]) == 0
    assert path.read_bytes() == snapshot


def test_cli_verify_detects_key_loss(tmp_path: Path):
    path = tmp_path / "cfg.json"
    baseline = tmp_path / "base.json"
    cfg = flat_config()
    path.write_text(json.dumps(cfg), encoding="utf-8")
    broken = dict(cfg)
    broken.pop("max_links")
    baseline.write_text(json.dumps(broken), encoding="utf-8")
    code = mig.main(["--config", str(path), "--verify", "--against", str(baseline)])
    assert code == 3


def test_cli_verify_ok(tmp_path: Path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(flat_config()), encoding="utf-8")
    assert mig.main(["--config", str(path), "--verify", "--against", str(path)]) == 0


def test_cli_unknown_key_refused_without_allow(tmp_path: Path):
    path = tmp_path / "cfg.json"
    cfg = flat_config()
    cfg["legacy_key"] = 1
    path.write_text(json.dumps(cfg), encoding="utf-8")
    before = path.read_bytes()
    assert mig.main(["--config", str(path), "--apply"]) == 2
    assert path.read_bytes() == before


def test_cli_from_backup_restores(tmp_path: Path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(flat_config()), encoding="utf-8")
    assert mig.main(["--config", str(path), "--apply"]) == 0
    backup = sorted(tmp_path.glob("cfg.json.bak-pre-v3-*"))[0]
    assert mig.main(["--config", str(path), "--from-backup", str(backup)]) == 0
    restored = json.loads(path.read_text(encoding="utf-8-sig"))
    assert set(restored) == set(FLAT_KEYS)  # 恢复成扁平结构
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
