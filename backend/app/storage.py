from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from .config import AppConfig
from .models import Asset, uid
from .services import AppError


def register_file(
    session: Session,
    config: AppConfig,
    source: Path,
    *,
    kind: str,
    content_id: str | None,
    mime_type: str,
    filename: str | None = None,
    input_hash: str | None = None,
) -> Asset:
    if not source.is_file() or source.stat().st_size == 0:
        raise AppError("OUTPUT_MISSING", "处理完成后未找到有效输出文件", 409)
    if shutil.disk_usage(config.data_dir).free < source.stat().st_size + 50 * 1024 * 1024:
        raise AppError("DISK_FULL", "剩余磁盘空间不足，无法保存产物", 409)
    identifier = uid()
    target = config.data_dir / "assets" / f"{identifier}{source.suffix.lower()}"
    temporary = target.with_suffix(target.suffix + ".part")
    hasher = hashlib.sha256()
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            while block := incoming.read(1024 * 1024):
                hasher.update(block)
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        temporary.replace(target)
        asset = Asset(
            id=identifier,
            content_id=content_id,
            kind=kind,
            filename=Path(filename or source.name).name[:200],
            mime_type=mime_type,
            relative_path=str(target.relative_to(config.data_dir)),
            size=target.stat().st_size,
            sha256=hasher.hexdigest(),
            input_hash=input_hash,
        )
        session.add(asset)
        session.flush()
        return asset
    except BaseException:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise
