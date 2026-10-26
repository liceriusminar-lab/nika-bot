"""Ежедневный бэкап базы в data/backups (7 последних копий).

Через backup API SQLite — копия целая, даже если бот в этот момент пишет в базу."""

import logging
from datetime import datetime
from pathlib import Path

import aiosqlite

from bot.memory.store import Store

logger = logging.getLogger(__name__)

KEEP = 7


async def make_backup(store: Store, backups_dir: Path, now: datetime, keep: int = KEEP) -> Path:
    """Делает копию базы за сегодня (если уже есть — перезаписывает) и удаляет старые сверх keep."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    path = backups_dir / f"nika-{now:%Y-%m-%d}.db"
    tmp = path.with_suffix(".tmp")
    async with aiosqlite.connect(tmp) as target:
        await store.db.backup(target)
    tmp.replace(path)  # подменяем только готовую копию — недописанный бэкап не останется

    for old in sorted(backups_dir.glob("nika-*.db"))[:-keep]:
        old.unlink()
    logger.info("Бэкап базы: %s (всего копий: %d)", path.name, len(list(backups_dir.glob("nika-*.db"))))
    return path


async def backup_if_missing(store: Store, backups_dir: Path, now: datetime) -> None:
    """При запуске: если сегодня бэкапа ещё не было — сделать."""
    if not (backups_dir / f"nika-{now:%Y-%m-%d}.db").exists():
        await make_backup(store, backups_dir, now)
