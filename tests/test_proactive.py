"""Сон и «пишет первой». Время подставляется вручную — ждать часами не нужно."""

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.proactive import NORMAL, TEST, Proactive
from bot.sleep import SleepTracker, learn_window
from tests.helpers import ScriptedProvider, make_router

TZ = ZoneInfo("Asia/Vladivostok")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


class Rng:
    """Подставной генератор случайностей: always=True — любой повод срабатывает."""

    def __init__(self, always: bool) -> None:
        self.value = 0.0 if always else 0.999

    def random(self) -> float:
        return self.value


class FakeReplier:
    def __init__(self) -> None:
        self.notes: list[str] = []

    async def reply(self, extra_note=None, silent_errors=False, ooc=False):
        self.notes.append(extra_note)
        return "приветик) как ты?"


def make_proactive(store, always=True, gate="YES", timings=NORMAL, per_day=4):
    provider = ScriptedProvider(*[gate] * 10)
    replier = FakeReplier()
    p = Proactive(store, SleepTracker(store), make_router(store, provider), replier, per_day, timings, Rng(always))
    return p, replier, provider


# ---------- Сон ----------

async def test_default_window(store):
    sleep = SleepTracker(store)
    assert await sleep.is_asleep(at(26, 3))
    assert not await sleep.is_asleep(at(26, 12))


async def test_sleep_phrase_and_waking(store):
    sleep = SleepTracker(store)
    assert await sleep.on_user_message("ну всё, я спать", at(25, 23)) == "sleep"
    assert await sleep.is_asleep(at(25, 23, 40))           # до окна 01–09, но сказал «я спать»
    assert await sleep.on_user_message("ну пока)", at(25, 23, 20)) is None  # прощание — ещё не проснулся
    assert await sleep.is_asleep(at(26, 7, 30))            # 8,5 ч сна, но ещё ночное окно
    assert not await sleep.is_asleep(at(26, 9, 30))        # выспался и окно кончилось


async def test_message_after_sleep_wakes(store):
    sleep = SleepTracker(store)
    await sleep.go_to_sleep(at(26, 0))
    assert await sleep.on_user_message("не спится что-то", at(26, 2)) == "wake"
    assert not await sleep.is_asleep(at(26, 2, 30))        # пишу посреди ночи — значит не сплю
    assert await sleep.on_user_message("доброе утро", at(26, 8)) == "wake"


def test_learn_window_from_messages():
    activity = [("message", at(day, hour, 15)) for day in range(10, 20) for hour in [*range(11, 24), 0]]
    assert learn_window(activity) == (1, 11)
    assert learn_window(activity[:20]) is None             # данных мало


def test_learn_window_from_signals():
    activity = [("sleep", at(20, 23, 30)), ("sleep", at(21, 0, 30)), ("sleep", at(22, 0, 0)),
                ("wake", at(21, 8, 0)), ("wake", at(22, 8, 30)), ("wake", at(23, 7, 30))]
    assert learn_window(activity) == (0, 8)


# ---------- Жёсткие правила ----------

async def test_does_not_write_while_asleep_or_disabled(store):
    p, _, _ = make_proactive(store)
    assert await p.decide(at(26, 3)) is None
    await store.set_setting("proactive:enabled", "0")
    assert await p.decide(at(26, 14)) is None


async def test_does_not_interrupt_conversation(store):
    p, _, _ = make_proactive(store)
    await store.add_message("user", "привет", at(26, 13, 40))
    assert await p.decide(at(26, 14)) is None


async def test_min_gap_and_daily_limit(store):
    p, _, _ = make_proactive(store, per_day=2)
    await store.add_message("user", "привет", at(26, 8))
    await store.add_proactive("morning", "утро)", at(26, 10))
    await store.add_message("user", "ответ", at(26, 10, 5))
    assert await p.decide(at(26, 12)) is None              # прошло меньше 2,5 ч
    await store.add_proactive("share", "смотри)", at(26, 13))
    await store.add_message("user", "ответ", at(26, 13, 5))
    assert await p.decide(at(26, 18)) is None              # лимит 2 в день исчерпан


async def test_unanswered_then_one_missing_message(store):
    p, _, _ = make_proactive(store)
    await store.add_message("user", "пока", at(25, 12))
    await store.add_proactive("silence", "ты как?)", at(25, 18))
    assert await p.decide(at(26, 10)) is None              # её сообщение без ответа — молчит
    decision = await p.decide(at(26, 13))                  # я молчу больше суток
    assert decision.reason == "missing"
    await store.add_proactive("missing", "ты как? пропал куда-то", at(26, 13))
    assert await p.decide(at(27, 14)) is None              # «пропал» — только один раз


# ---------- Поводы ----------

async def test_followup_has_priority_and_is_marked_done(store):
    p, replier, _ = make_proactive(store)
    await store.add_message("user", "пока", at(26, 9))
    await store.add_followup("спросить про собеседование", at(26, 18), at(25, 10))
    await store.add_followup("спросить про старое", at(20, 18), at(19, 10))
    text = await p.check(at(26, 19))
    assert text and "собеседование" in replier.notes[0]
    assert await store.open_followups() == []              # и свежий, и протухший закрыты


async def test_morning_after_usual_wake(store):
    p, _, _ = make_proactive(store)
    await store.add_message("user", "спокойной", at(25, 23))
    assert (await p.decide(at(26, 9, 40))).reason == "morning"


async def test_morning_not_if_i_already_wrote(store):
    p, _, _ = make_proactive(store, always=True)
    await store.add_message("user", "доброе", at(26, 9, 10))
    decision = await p.decide(at(26, 10, 30))
    assert decision is None or decision.reason != "morning"


async def test_evening_and_silence(store):
    p, _, _ = make_proactive(store)
    await store.add_message("user", "ок", at(26, 18))
    assert (await p.decide(at(26, 20))).reason == "evening"
    await store.add_proactive("evening", "как день?", at(26, 20))
    await store.add_message("user", "норм", at(26, 20, 10))
    assert (await p.decide(at(27, 16))).reason == "silence"   # молчу почти сутки, но днём


async def test_nothing_fires_without_luck(store):
    p, _, _ = make_proactive(store, always=False)
    await store.add_message("user", "ок", at(26, 12))
    assert await p.decide(at(26, 20)) is None


# ---------- Полный цикл ----------

async def test_gate_skip_prevents_sending(store):
    p, replier, _ = make_proactive(store, gate="SKIP")
    await store.add_message("user", "я на совещании, напишу позже", at(26, 9))
    assert await p.check(at(26, 20)) is None and replier.notes == []


async def test_check_sends_and_logs(store):
    p, replier, provider = make_proactive(store)
    await store.add_message("user", "ок", at(26, 12))
    assert await p.check(at(26, 20)) == "приветик) как ты?"
    assert (await store.last_proactive()).reason == "evening"
    assert provider.prompts[0][1]["content"].startswith("Сейчас 26.10 20:00")  # проверка YES/SKIP


async def test_test_mode_uses_minutes(store):
    p, _, _ = make_proactive(store, timings=TEST)
    await store.add_message("user", "ок", at(26, 14, 0))
    assert await p.decide(at(26, 14, 1)) is None           # меньше минуты — разговор идёт
    assert (await p.decide(at(26, 14, 5))).reason == "silence"
