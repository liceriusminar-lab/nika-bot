"""Тест ежедневного бэкапа: копия рабочая, хранятся 7 последних."""

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot.backup import backup_if_missing, make_backup
from bot.memory.store import Store

NOW = datetime(2026, 10, 26, 4, 30, tzinfo=ZoneInfo("Asia/Vladivostok"))


async def test_backups_are_valid_and_rotated(tmp_path):
    store = Store(tmp_path / "nika.db")
    await store.open()
    await store.add_message("user", "привет", NOW)
    backups = tmp_path / "backups"

    for day in range(9):
        await make_backup(store, backups, NOW + timedelta(days=day))
    files = sorted(p.name for p in backups.glob("nika-*.db"))
    assert len(files) == 7 and files[0] == "nika-2026-10-28.db" and files[-1] == "nika-2026-11-03.db"
    assert not list(backups.glob("*.tmp"))

    with sqlite3.connect(backups / files[-1]) as copy:   # копия открывается и в ней есть данные
        assert copy.execute("SELECT content FROM messages").fetchall() == [("привет",)]

    await backup_if_missing(store, backups, NOW + timedelta(days=8))   # за этот день уже есть — не трогаем
    assert len(list(backups.glob("nika-*.db"))) == 7
    await store.close()
