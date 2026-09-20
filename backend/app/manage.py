from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from . import __version__
from .api import diagnostics_data
from .config import AppConfig
from .db import make_engine, migrate, seed
from .services import AppError


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def backup(config: AppConfig) -> Path:
    """SQLite's online backup plus immutable, DB-referenced assets; secrets and unreferenced runs excluded."""
    config.prepare_directories()
    database = config.data_dir / "publisher.sqlite3"
    if not database.is_file():
        raise AppError("DATABASE_MISSING", "本地数据库尚未初始化", 409)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    temporary = config.data_dir / "backups" / (stamp + ".partial")
    final = config.data_dir / "backups" / stamp
    temporary.mkdir(mode=0o700)
    try:
        with sqlite3.connect(database) as source, sqlite3.connect(temporary / "publisher.sqlite3") as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise AppError("BACKUP_INVALID", "备份数据库未通过完整性检查", 409)
            assets = target.execute("SELECT relative_path, sha256 FROM assets").fetchall()
            revision = target.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        entries = []
        for relative, expected_hash in assets:
            src = (config.data_dir / relative).resolve()
            if not src.is_relative_to(config.data_dir.resolve() / "assets") or not src.is_file():
                raise AppError("BACKUP_ASSET_MISSING", "备份所需素材缺失或路径非法", 409)
            if sha256(src) != expected_hash:
                raise AppError("BACKUP_ASSET_CHANGED", "素材内容与数据库记录不一致，请先核实文件", 409)
            dst = temporary / relative
            dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(src, dst)
            if sha256(dst) != expected_hash:
                raise AppError("BACKUP_COPY_INVALID", "素材备份校验失败", 409)
            entries.append({"path": relative, "sha256": expected_hash})
        manifest = {
            "format_version": 1,
            "app_version": __version__,
            "schema_revision": revision,
            "created_at": datetime.now(UTC).isoformat(),
            "database_sha256": sha256(temporary / "publisher.sqlite3"),
            "assets": entries,
            "secrets_included": False,
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.rename(final)
        return final
    except BaseException:
        # Only the incomplete directory created by this invocation is removed.
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_backup(path: Path) -> dict:
    path = path.resolve()
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if not (path / "publisher.sqlite3").resolve().is_relative_to(path):
            raise ValueError()
        if (
            manifest["format_version"] != 1
            or sha256(path / "publisher.sqlite3") != manifest["database_sha256"]
        ):
            raise ValueError()
        for entry in manifest["assets"]:
            asset = (path / entry["path"]).resolve()
            if not asset.is_relative_to(path / "assets") or sha256(asset) != entry["sha256"]:
                raise ValueError()
        with sqlite3.connect(f"file:{path / 'publisher.sqlite3'}?mode=ro", uri=True) as database:
            if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError()
    except (OSError, ValueError, KeyError, sqlite3.DatabaseError):
        raise AppError("INVALID_BACKUP", "备份不完整、已改变或路径不合法，未执行恢复", 409) from None
    return manifest


def restore(config: AppConfig, path: Path) -> dict:
    """Caller must stop all services and explicitly confirm; preserve current data before replacement."""
    manifest = verify_backup(path)
    preserved = backup(config) if (config.data_dir / "publisher.sqlite3").exists() else None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    staged = config.data_dir / "runs" / f"restore-{stamp}.sqlite3"
    shutil.copyfile(path / "publisher.sqlite3", staged)
    try:
        with sqlite3.connect(staged) as database:
            database.execute("PRAGMA journal_mode=DELETE")
            # A restored queue describes the past. Never replay it or re-enable schedules automatically.
            database.execute("UPDATE schedules SET enabled=0")
            database.execute(
                "UPDATE jobs SET status='WAITING_USER', error_code='RESTORE_REVIEW_REQUIRED', "
                "error_message='从备份恢复后，请核验服务商和平台实际状态，禁止直接重复执行', "
                "current_step='等待恢复状态核验', lease_owner=NULL, lease_until=NULL "
                "WHERE status IN ('QUEUED','RUNNING','WAITING_USER')"
            )
            database.execute("UPDATE model_calls SET status='UNKNOWN' WHERE status='STARTED'")
            database.execute(
                "UPDATE publications SET status='NEEDS_ATTENTION' WHERE status IN ('PREPARING','WAITING_USER','VERIFYING')"
            )
            database.execute("DELETE FROM worker_heartbeats")
            record = database.execute("SELECT value FROM settings WHERE key='app'").fetchone()
            if record:
                settings_value = json.loads(record[0])
                for model in settings_value["models"].values():
                    model["enabled"] = False
                settings_value.setdefault("publishing", {})["auto_publish_after_approval"] = False
                database.execute(
                    "UPDATE settings SET value=? WHERE key='app'",
                    (json.dumps(settings_value, ensure_ascii=False),),
                )
            database.commit()
        for entry in manifest["assets"]:
            target = (config.data_dir / entry["path"]).resolve()
            if not target.is_relative_to(config.data_dir.resolve() / "assets"):
                raise AppError("INVALID_ASSET_PATH", "恢复目标路径不合法", 409)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if target.exists() and sha256(target) != entry["sha256"]:
                raise AppError(
                    "RESTORE_ASSET_CONFLICT", "同名素材内容发生变化，恢复已暂停；原数据已备份", 409
                )
            if not target.exists():
                shutil.copyfile(path / entry["path"], target)
        live = config.data_dir / "publisher.sqlite3"
        # The stopped database is checkpointed before removing its owned WAL sidecars.
        if live.exists():
            with sqlite3.connect(live) as database:
                database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        staged.replace(live)
        for suffix in ("-wal", "-shm"):
            Path(str(live) + suffix).unlink(missing_ok=True)
        migrate(config)
        return {
            "restored_from": str(path),
            "preserved_current": str(preserved) if preserved else None,
            "message": "恢复完成；自动计划和模型调用已关闭，未完成任务需人工核验后处理",
        }
    finally:
        staged.unlink(missing_ok=True)


def _prompt_device_pin() -> str:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise AppError(
            "DEVICE_PIN_TTY_REQUIRED",
            "保存手机 PIN 必须在交互式终端中执行，禁止通过参数、管道或无回显降级输入",
            409,
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            pin = getpass.getpass("手机解锁 PIN：")
            confirmation = getpass.getpass("再次输入手机解锁 PIN：")
    except (EOFError, getpass.GetPassWarning) as exc:
        raise AppError(
            "DEVICE_PIN_SECURE_INPUT_UNAVAILABLE", "终端无法安全隐藏手机 PIN，未保存凭据", 409
        ) from exc
    except KeyboardInterrupt:
        raise AppError("DEVICE_PIN_INPUT_CANCELED", "已取消保存手机 PIN", 409) from None
    if pin != confirmation:
        raise AppError("DEVICE_PIN_MISMATCH", "两次输入的手机 PIN 不一致，未保存凭据", 422)
    return pin


def _manage_device_pin(action: str, serial: str, config: AppConfig) -> None:
    from .device_credentials import delete_device_pin, device_pin_status, set_device_pin
    from .integrations.device import clear_unlock_failure, unlock_failure_latched

    if action == "set":
        pin = _prompt_device_pin()
        try:
            set_device_pin(serial, pin)
        finally:
            pin = ""
        clear_unlock_failure(serial, config)
        result = {"serial": serial, "stored": True, "unlock_retry_blocked": False}
    elif action == "status":
        result = {
            "serial": serial,
            **device_pin_status(serial),
            "unlock_retry_blocked": unlock_failure_latched(serial, config),
        }
    else:
        deleted = delete_device_pin(serial)
        clear_unlock_failure(serial, config)
        result = {
            "serial": serial,
            "stored": False,
            "deleted": deleted,
            "unlock_retry_blocked": False,
        }
    print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="映序本地数据维护")
    parser.add_argument(
        "action", choices=["init", "doctor", "backup", "verify-backup", "restore", "device-pin"]
    )
    parser.add_argument("device_pin_action", nargs="?", choices=["set", "status", "delete"])
    parser.add_argument("--serial")
    parser.add_argument("--from", dest="source", type=Path)
    parser.add_argument("--confirm-restore", action="store_true")
    args = parser.parse_args()
    if args.action != "device-pin" and args.device_pin_action is not None:
        parser.error("仅 device-pin 操作接受 set、status 或 delete 子命令")
    config = AppConfig()
    config.configure_tools()
    config.prepare_directories()
    if args.action == "init":
        migrate(config)
        engine = make_engine(config)
        seed(engine, config)
        engine.dispose()
        print("本地数据库已就绪；信息源和模板已初始化，模型调用与自动计划默认关闭。")
    elif args.action == "doctor":
        engine = make_engine(config)
        with Session(engine) as session:
            print(json.dumps(diagnostics_data(session, config), ensure_ascii=False, indent=2))
        engine.dispose()
    elif args.action == "backup":
        print(backup(config))
    elif args.action == "device-pin":
        if not args.device_pin_action or not args.serial:
            parser.error("device-pin 需要 set、status 或 delete 子命令及 --serial")
        _manage_device_pin(args.device_pin_action, args.serial, config)
    elif args.action in {"verify-backup", "restore"}:
        if not args.source:
            parser.error("需要 --from 指定已生成的备份目录")
        manifest = verify_backup(args.source)
        if args.action == "verify-backup":
            print(
                json.dumps(
                    {
                        "valid": True,
                        "assets": len(manifest["assets"]),
                        "schema_revision": manifest["schema_revision"],
                    },
                    ensure_ascii=False,
                )
            )
        elif not args.confirm_restore:
            print(
                f"恢复目标：{config.data_dir.resolve()}\n备份来源：{args.source.resolve()}\n"
                "将先备份当前数据，再恢复数据库和素材，并关闭自动执行。请先停止全部服务。\n"
                "确认后通过 scripts/control.py restore --from <备份目录> --confirm-restore 执行。"
            )
        else:
            # Direct entrypoint also checks the worker locks; the supervisor additionally checks API PID ownership.
            import fcntl
            from contextlib import ExitStack

            with ExitStack() as stack:
                for name in ("api.lock", "worker-content.lock", "worker-device.lock"):
                    handle = stack.enter_context((config.data_dir / "runs" / name).open("a"))
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise AppError("SERVICES_RUNNING", "任务进程仍在运行，请先停止服务", 409) from None
                print(json.dumps(restore(config, args.source.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    os.umask(0o077)
    main()
