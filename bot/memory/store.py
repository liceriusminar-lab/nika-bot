"""База данных SQLite: сообщения, долгая память, расходы, настройки.

Файл базы — data/nika.db. Таблицы создаются автоматически при запуске.
Остальные таблицы из плана (сцены, отношения, активность...) добавятся на следующих этапах.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    role              TEXT NOT NULL,              -- user | assistant
    content           TEXT NOT NULL,
    mode              TEXT NOT NULL DEFAULT 'chat', -- chat | rp
    scene_id          INTEGER,
    has_image         INTEGER NOT NULL DEFAULT 0,
    image_description TEXT,
    created_at        TEXT NOT NULL               -- ISO-время с часовым поясом
);

-- Сцены ролевых игр. status: active (идёт) | paused (на паузе) | ended (закончена)
CREATE TABLE IF NOT EXISTS rp_scenes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    setting          TEXT NOT NULL DEFAULT '',
    characters       TEXT NOT NULL DEFAULT '',
    summary          TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'active',
    summarized_until INTEGER NOT NULL DEFAULT 0,  -- до какого сообщения сцена свёрнута в summary
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- Факты обо мне. category: обо_мне | предпочтения | люди | события | наши_моменты
CREATE TABLE IF NOT EXISTS facts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    category     TEXT NOT NULL,
    text         TEXT NOT NULL,
    importance   INTEGER NOT NULL DEFAULT 3,      -- 1 (мелочь) … 5 (самое важное)
    created_at   TEXT NOT NULL,
    last_used_at TEXT
);

-- События и подробности «жизни» Ники, которые она уже рассказала — чтобы не противоречить себе
CREATE TABLE IF NOT EXISTS her_life (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Сводки старой переписки. Каждая новая включает в себя предыдущую
CREATE TABLE IF NOT EXISTS summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    text            TEXT NOT NULL,
    last_message_id INTEGER NOT NULL,             -- до какого сообщения включительно свёрнуто
    created_at      TEXT NOT NULL
);

-- Что спросить у меня потом («как прошло собеседование?»)
CREATE TABLE IF NOT EXISTS followups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    due_at     TEXT NOT NULL,                     -- когда уместно спросить
    done       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Полнотекстовый поиск (FTS5) по фактам и её жизни. Индексы обновляются триггерами сами
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text, content='facts', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE OF text ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS her_life_fts USING fts5(text, content='her_life', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS her_life_ai AFTER INSERT ON her_life BEGIN
    INSERT INTO her_life_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS her_life_ad AFTER DELETE ON her_life BEGIN
    INSERT INTO her_life_fts(her_life_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

-- Моя активность: kind = message (написал) | sleep (ушёл спать) | wake (проснулся)
CREATE TABLE IF NOT EXISTS user_activity (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    at   TEXT NOT NULL
);

-- Когда и что Ника писала первой. reason: morning | evening | followup | silence | share | missing
CREATE TABLE IF NOT EXISTS proactive_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL,
    text   TEXT NOT NULL,
    at     TEXT NOT NULL
);

-- История изменений близости: на сколько изменилась, каким стал уровень и почему
CREATE TABLE IF NOT EXISTS relationship_history (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    delta  REAL NOT NULL,
    level  REAL NOT NULL,
    reason TEXT NOT NULL,
    at     TEXT NOT NULL
);

-- Расход токенов и денег: одна строка на (день, модель)
CREATE TABLE IF NOT EXISTS usage (
    day           TEXT NOT NULL,                  -- YYYY-MM-DD по моему часовому поясу
    model         TEXT NOT NULL,
    requests      INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_rub      REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model)
);

-- Настройки и служебные отметки вида ключ → значение
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class StoredMessage:
    id: int
    role: str
    content: str
    mode: str
    created_at: datetime
    has_image: bool = False
    image_description: str | None = None

    def as_text(self) -> str:
        """Текст для нейросети. Картинка превращается в её описание в квадратных скобках."""
        if not self.has_image:
            return self.content
        if self.role == "user":
            what = f"[он прислал фото: {self.image_description}]" if self.image_description \
                else "[он прислал фото, но оно у тебя не открылось]"
        else:
            # Та же метка, которой Ника сама просит картинку (см. images.py) — модель видит свой формат
            what = f"[[картинка: {self.image_description}]]"
        return f"{what}\n{self.content}".strip()


@dataclass
class Scene:
    id: int
    title: str
    setting: str
    characters: str
    summary: str
    status: str
    summarized_until: int
    updated_at: datetime


@dataclass
class Fact:
    id: int
    category: str
    text: str
    importance: int
    created_at: datetime


@dataclass
class HerEvent:
    id: int
    text: str
    created_at: datetime


@dataclass
class Summary:
    id: int
    text: str
    last_message_id: int


@dataclass
class Followup:
    id: int
    text: str
    due_at: datetime


@dataclass
class ProactiveEntry:
    reason: str
    text: str
    at: datetime


@dataclass
class ModelUsage:
    model: str
    requests: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_rub: float


def _msg(r) -> StoredMessage:
    return StoredMessage(r["id"], r["role"], r["content"], r["mode"], datetime.fromisoformat(r["created_at"]),
                         bool(r["has_image"]), r["image_description"])


def _scene(r) -> Scene:
    return Scene(r["id"], r["title"], r["setting"], r["characters"], r["summary"], r["status"],
                 r["summarized_until"], datetime.fromisoformat(r["updated_at"]))


def _fact(r) -> Fact:
    return Fact(r["id"], r["category"], r["text"], r["importance"], datetime.fromisoformat(r["created_at"]))


def _event(r) -> HerEvent:
    return HerEvent(r["id"], r["text"], datetime.fromisoformat(r["created_at"]))


def _not_in(ids: list[int]) -> tuple[str, list[int]]:
    """Кусок SQL «AND id NOT IN (...)» и параметры к нему."""
    if not ids:
        return "", []
    return f" AND id NOT IN ({','.join('?' * len(ids))})", list(ids)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None

    async def _all(self, sql: str, params=()) -> list:
        cur = await self.db.execute(sql, params)
        return await cur.fetchall()

    # ---------- Сообщения ----------

    async def add_message(self, role: str, content: str, now: datetime, mode: str = "chat",
                          has_image: bool = False, image_description: str | None = None,
                          scene_id: int | None = None) -> int:
        cur = await self.db.execute(
            "INSERT INTO messages (role, content, mode, scene_id, has_image, image_description, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (role, content, mode, scene_id, int(has_image), image_description, now.isoformat()),
        )
        await self.db.commit()
        return cur.lastrowid

    async def recent_messages(self, limit: int) -> list[StoredMessage]:
        """Последние limit сообщений в хронологическом порядке (старые сверху)."""
        rows = await self._all("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))
        return [_msg(r) for r in reversed(rows)]

    async def last_message(self) -> StoredMessage | None:
        messages = await self.recent_messages(1)
        return messages[0] if messages else None

    async def last_message_by(self, role: str) -> StoredMessage | None:
        rows = await self._all("SELECT * FROM messages WHERE role = ? ORDER BY id DESC LIMIT 1", (role,))
        return _msg(rows[0]) if rows else None

    async def messages_between(self, after_id: int, up_to_id: int | None = None, limit: int = 200) -> list[StoredMessage]:
        """Сообщения с id > after_id (и <= up_to_id, если задан), по порядку."""
        if up_to_id is None:
            rows = await self._all("SELECT * FROM messages WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit))
        else:
            rows = await self._all("SELECT * FROM messages WHERE id > ? AND id <= ? ORDER BY id LIMIT ?",
                                   (after_id, up_to_id, limit))
        return [_msg(r) for r in rows]

    async def messages_before(self, before_id: int, limit: int) -> list[StoredMessage]:
        """limit сообщений перед before_id — для контекста, по порядку."""
        rows = await self._all("SELECT * FROM messages WHERE id < ? ORDER BY id DESC LIMIT ?", (before_id, limit))
        return [_msg(r) for r in reversed(rows)]

    # ---------- Факты ----------

    async def add_fact(self, category: str, text: str, importance: int, now: datetime) -> int:
        cur = await self.db.execute(
            "INSERT INTO facts (category, text, importance, created_at) VALUES (?, ?, ?, ?)",
            (category, text, importance, now.isoformat()),
        )
        await self.db.commit()
        return cur.lastrowid

    async def update_fact(self, fact_id: int, text: str, importance: int) -> bool:
        cur = await self.db.execute("UPDATE facts SET text = ?, importance = ? WHERE id = ?",
                                    (text, importance, fact_id))
        await self.db.commit()
        return cur.rowcount > 0

    async def delete_fact(self, fact_id: int) -> Fact | None:
        rows = await self._all("SELECT * FROM facts WHERE id = ?", (fact_id,))
        if not rows:
            return None
        await self.db.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        await self.db.commit()
        return _fact(rows[0])

    async def all_facts(self) -> list[Fact]:
        return [_fact(r) for r in await self._all("SELECT * FROM facts ORDER BY id")]

    async def important_facts(self, min_importance: int, limit: int) -> list[Fact]:
        rows = await self._all(
            "SELECT * FROM facts WHERE importance >= ? ORDER BY importance DESC, id DESC LIMIT ?",
            (min_importance, limit),
        )
        return [_fact(r) for r in rows]

    async def search_facts(self, match: str, limit: int, exclude_ids: list[int] = ()) -> list[Fact]:
        """Поиск по FTS5. match — готовое выражение (см. facts.build_match_query)."""
        if not match:
            return []
        extra, params = _not_in(list(exclude_ids))
        rows = await self._all(
            f"SELECT * FROM facts WHERE id IN (SELECT rowid FROM facts_fts WHERE facts_fts MATCH ? "
            f"ORDER BY bm25(facts_fts) LIMIT ?){extra}",
            [match, limit + len(params), *params],
        )
        return [_fact(r) for r in rows][:limit]

    async def touch_facts(self, ids: list[int], now: datetime) -> None:
        """Отмечает, что факты были использованы в разговоре."""
        if ids:
            await self.db.execute(f"UPDATE facts SET last_used_at = ? WHERE id IN ({','.join('?' * len(ids))})",
                                  [now.isoformat(), *ids])
            await self.db.commit()

    # ---------- Её жизнь ----------

    async def add_her_event(self, text: str, now: datetime) -> int:
        cur = await self.db.execute("INSERT INTO her_life (text, created_at) VALUES (?, ?)", (text, now.isoformat()))
        await self.db.commit()
        return cur.lastrowid

    async def recent_her_events(self, limit: int) -> list[HerEvent]:
        rows = await self._all("SELECT * FROM her_life ORDER BY id DESC LIMIT ?", (limit,))
        return [_event(r) for r in reversed(rows)]

    async def search_her_events(self, match: str, limit: int, exclude_ids: list[int] = ()) -> list[HerEvent]:
        if not match:
            return []
        extra, params = _not_in(list(exclude_ids))
        rows = await self._all(
            f"SELECT * FROM her_life WHERE id IN (SELECT rowid FROM her_life_fts WHERE her_life_fts MATCH ? "
            f"ORDER BY bm25(her_life_fts) LIMIT ?){extra} ORDER BY id",
            [match, limit + len(params), *params],
        )
        return [_event(r) for r in rows][:limit]

    # ---------- Сводки ----------

    async def add_summary(self, text: str, period_start: datetime, period_end: datetime,
                          last_message_id: int, now: datetime) -> None:
        await self.db.execute(
            "INSERT INTO summaries (period_start, period_end, text, last_message_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (period_start.isoformat(), period_end.isoformat(), text, last_message_id, now.isoformat()),
        )
        await self.db.commit()

    async def latest_summary(self) -> Summary | None:
        rows = await self._all("SELECT id, text, last_message_id FROM summaries ORDER BY id DESC LIMIT 1")
        return Summary(rows[0]["id"], rows[0]["text"], rows[0]["last_message_id"]) if rows else None

    # ---------- Что спросить потом ----------

    async def add_followup(self, text: str, due_at: datetime, now: datetime) -> int:
        cur = await self.db.execute("INSERT INTO followups (text, due_at, created_at) VALUES (?, ?, ?)",
                                    (text, due_at.isoformat(), now.isoformat()))
        await self.db.commit()
        return cur.lastrowid

    async def open_followups(self) -> list[Followup]:
        rows = await self._all("SELECT id, text, due_at FROM followups WHERE done = 0 ORDER BY due_at")
        return [Followup(r["id"], r["text"], datetime.fromisoformat(r["due_at"])) for r in rows]

    async def due_followups(self, now: datetime) -> list[Followup]:
        """Невыполненные followups, срок которых уже наступил."""
        return [f for f in await self.open_followups() if f.due_at <= now]

    async def mark_followup_done(self, followup_id: int) -> None:
        await self.db.execute("UPDATE followups SET done = 1 WHERE id = ?", (followup_id,))
        await self.db.commit()

    # ---------- Активность и «пишет первой» ----------

    async def add_activity(self, kind: str, now: datetime) -> None:
        await self.db.execute("INSERT INTO user_activity (kind, at) VALUES (?, ?)", (kind, now.isoformat()))
        await self.db.commit()

    async def activity_since(self, since: datetime) -> list[tuple[str, datetime]]:
        rows = await self._all("SELECT kind, at FROM user_activity WHERE at >= ? ORDER BY id", (since.isoformat(),))
        return [(r["kind"], datetime.fromisoformat(r["at"])) for r in rows]

    async def add_proactive(self, reason: str, text: str, now: datetime) -> None:
        await self.db.execute("INSERT INTO proactive_log (reason, text, at) VALUES (?, ?, ?)",
                              (reason, text, now.isoformat()))
        await self.db.commit()

    async def proactive_since(self, since: datetime) -> list[ProactiveEntry]:
        rows = await self._all("SELECT reason, text, at FROM proactive_log WHERE at >= ? ORDER BY id",
                               (since.isoformat(),))
        return [ProactiveEntry(r["reason"], r["text"], datetime.fromisoformat(r["at"])) for r in rows]

    async def last_proactive(self) -> ProactiveEntry | None:
        rows = await self._all("SELECT reason, text, at FROM proactive_log ORDER BY id DESC LIMIT 1")
        return ProactiveEntry(rows[0]["reason"], rows[0]["text"], datetime.fromisoformat(rows[0]["at"])) if rows else None

    # ---------- Ролевые сцены ----------

    async def create_scene(self, title: str, setting: str, characters: str, now: datetime) -> int:
        cur = await self.db.execute(
            "INSERT INTO rp_scenes (title, setting, characters, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', ?, ?)",
            (title, setting, characters, now.isoformat(), now.isoformat()),
        )
        await self.db.commit()
        return cur.lastrowid

    async def get_scene(self, scene_id: int) -> Scene | None:
        rows = await self._all("SELECT * FROM rp_scenes WHERE id = ?", (scene_id,))
        return _scene(rows[0]) if rows else None

    async def update_scene(self, scene_id: int, now: datetime, **fields) -> None:
        """Обновляет поля сцены: status, summary, summarized_until и т.п."""
        sets = ", ".join(f"{k} = ?" for k in fields)
        await self.db.execute(f"UPDATE rp_scenes SET {sets}, updated_at = ? WHERE id = ?",
                              [*fields.values(), now.isoformat(), scene_id])
        await self.db.commit()

    async def list_scenes(self, limit: int = 10) -> list[Scene]:
        rows = await self._all("SELECT * FROM rp_scenes ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [_scene(r) for r in rows]

    async def scene_messages(self, scene_id: int, after_id: int = 0, limit: int = 20,
                             last: bool = True) -> list[StoredMessage]:
        """Сообщения сцены по порядку. last=True — последние limit штук, иначе первые после after_id."""
        order = "DESC" if last else "ASC"
        rows = await self._all(
            f"SELECT * FROM messages WHERE scene_id = ? AND mode = 'rp' AND id > ? ORDER BY id {order} LIMIT ?",
            (scene_id, after_id, limit),
        )
        messages = [_msg(r) for r in rows]
        return list(reversed(messages)) if last else messages

    # ---------- Отношения ----------

    async def add_relationship_change(self, delta: float, level: float, reason: str, now: datetime) -> None:
        await self.db.execute("INSERT INTO relationship_history (delta, level, reason, at) VALUES (?, ?, ?, ?)",
                              (delta, level, reason, now.isoformat()))
        await self.db.commit()

    async def relationship_changes_since(self, since: datetime) -> list[tuple[float, float, str, datetime]]:
        """[(изменение, уровень после, причина, когда)] по порядку."""
        rows = await self._all("SELECT delta, level, reason, at FROM relationship_history WHERE at >= ? ORDER BY id",
                               (since.isoformat(),))
        return [(r["delta"], r["level"], r["reason"], datetime.fromisoformat(r["at"])) for r in rows]

    # ---------- Сброс ----------

    async def reset_conversation(self) -> None:
        """Стирает переписку, сводки и сцены. Факты, её жизнь и отношения остаются."""
        await self.db.executescript("DELETE FROM messages; DELETE FROM summaries; DELETE FROM proactive_log; "
                                    "DELETE FROM rp_scenes; DELETE FROM settings WHERE key LIKE 'rp:%';")
        await self.db.commit()

    async def reset_all(self) -> None:
        """Стирает всё, кроме учёта расходов и настроек."""
        await self.db.executescript(
            "DELETE FROM messages; DELETE FROM summaries; DELETE FROM facts; "
            "DELETE FROM her_life; DELETE FROM followups; DELETE FROM proactive_log; DELETE FROM user_activity; "
            "DELETE FROM relationship_history; DELETE FROM rp_scenes; "
            "DELETE FROM settings WHERE key LIKE 'relationship:%' OR key LIKE 'rp:%';"
        )
        await self.db.commit()

    # ---------- Расходы ----------

    async def add_usage(self, day: str, model: str, input_tokens: int, output_tokens: int,
                        cached_tokens: int, cost_rub: float) -> None:
        await self.db.execute(
            """INSERT INTO usage (day, model, requests, input_tokens, output_tokens, cached_tokens, cost_rub)
               VALUES (?, ?, 1, ?, ?, ?, ?)
               ON CONFLICT(day, model) DO UPDATE SET
                   requests = requests + 1,
                   input_tokens = input_tokens + excluded.input_tokens,
                   output_tokens = output_tokens + excluded.output_tokens,
                   cached_tokens = cached_tokens + excluded.cached_tokens,
                   cost_rub = cost_rub + excluded.cost_rub""",
            (day, model, input_tokens, output_tokens, cached_tokens, cost_rub),
        )
        await self.db.commit()

    async def usage_since(self, first_day: str) -> list[ModelUsage]:
        """Расход по моделям начиная с дня first_day (YYYY-MM-DD) включительно."""
        rows = await self._all(
            """SELECT model, SUM(requests) AS requests, SUM(input_tokens) AS input_tokens,
                      SUM(output_tokens) AS output_tokens, SUM(cached_tokens) AS cached_tokens,
                      SUM(cost_rub) AS cost_rub
               FROM usage WHERE day >= ? GROUP BY model ORDER BY cost_rub DESC""",
            (first_day,),
        )
        return [ModelUsage(**dict(r)) for r in rows]

    async def cost_since(self, first_day: str) -> float:
        rows = await self._all("SELECT COALESCE(SUM(cost_rub), 0) FROM usage WHERE day >= ?", (first_day,))
        return rows[0][0]

    # ---------- Настройки ----------

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        rows = await self._all("SELECT value FROM settings WHERE key = ?", (key,))
        return rows[0][0] if rows else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()
