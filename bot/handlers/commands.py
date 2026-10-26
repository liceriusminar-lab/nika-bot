"""Служебные команды (отвечают не «в образе»)."""

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import Settings
from bot.handlers.chat import Replier
from bot.humanize import split_parts
from bot.images import KEY_ENABLED as KEY_IMAGES
from bot.images import ImageService
from bot.llm.router import LLMRouter
from bot.memory.facts import CATEGORIES, CATEGORY_TITLES
from bot.memory.store import Store
from bot.memory.updater import MemoryUpdater
from bot.proactive import KEY_ENABLED, KEY_PER_DAY, Proactive
from bot.relationship import KEY_ERP, STAGE_ORDER, RelationshipTracker, stage_label
from bot.relationship import KEY_FIXED as KEY_FIXED_STAGE
from bot.roleplay import (
    END_NOTE,
    RESUME_NOTE,
    START_SCENE_NOTE,
    STOP_TEXT,
    RoleplayManager,
)
from bot.sleep import SleepTracker

router = Router(name="commands")


# ---------- Память ----------

@router.message(Command("memory"))
async def on_memory(message: Message, store: Store, memory: MemoryUpdater) -> None:
    await memory.run(force=True)  # чтобы в списке были и самые свежие факты
    facts = await store.all_facts()
    if not facts:
        await message.answer("пока я ничего особенного о тебе не запомнила — расскажи о себе)")
        return

    lines = ["🧠 что я о тебе помню (номер — для /forget):"]
    for category in CATEGORIES:
        group = [f for f in facts if f.category == category]
        if group:
            lines.append(f"\n{CATEGORY_TITLES[category]}:")
            lines += [f"{f.id}. {f.text}{' ⭐' if f.importance >= 4 else ''}" for f in group]

    followups = await store.open_followups()
    if followups:
        lines.append("\n📌 хочу потом спросить:")
        lines += [f"• {f.due_at:%d.%m %H:%M} — {f.text}" for f in followups]

    for part in split_parts("\n".join(lines), mode="rp"):
        await message.answer(part)


@router.message(Command("forget"))
async def on_forget(message: Message, command: CommandObject, store: Store) -> None:
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await message.answer("напиши номер факта из /memory, например: /forget 12")
        return
    fact = await store.delete_fact(int(arg))
    if fact is None:
        await message.answer(f"факта с номером {arg} нет — посмотри номера в /memory")
    else:
        await message.answer(f"🗑 забыла: {fact.text}")


# ---------- Сброс ----------

RESET_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="стереть переписку", callback_data="reset:chat")],
    [InlineKeyboardButton(text="стереть всё, включая память", callback_data="reset:all")],
    [InlineKeyboardButton(text="отмена", callback_data="reset:cancel")],
])


@router.message(Command("reset"))
async def on_reset(message: Message) -> None:
    await message.answer(
        "⚠️ что стереть?\n\n"
        "• переписку — Ника забудет разговор, но факты о тебе (/memory) и свою «жизнь» сохранит\n"
        "• всё — как будто вы не знакомы. расходы (/stats) сохранятся\n\n"
        "отменить это нельзя",
        reply_markup=RESET_KEYBOARD,
    )


@router.callback_query(F.data.startswith("reset:"))
async def on_reset_choice(callback: CallbackQuery, store: Store) -> None:
    choice = callback.data.split(":", 1)[1]
    if choice == "chat":
        await store.reset_conversation()
        text = "🧹 переписка стёрта. факты о тебе на месте"
    elif choice == "all":
        await store.reset_all()
        text = "🧹 всё стёрто. можно начать знакомство заново: /start"
    else:
        text = "ок, ничего не трогаю"
    await callback.message.edit_text(text)
    await callback.answer()


# ---------- Сон ----------

SLEEP_NOTE = "Он написал, что идёт спать. Пожелай спокойной ночи — коротко и тепло."
WAKE_NOTE = "Он только что проснулся и написал тебе об этом. Поздоровайся по-утреннему, коротко."


@router.message(Command("sleep"))
async def on_sleep(message: Message, sleep: SleepTracker, replier: Replier) -> None:
    await sleep.go_to_sleep(replier.dialogue.now())
    await replier.reply(SLEEP_NOTE)


@router.message(Command("wake"))
async def on_wake(message: Message, sleep: SleepTracker, replier: Replier) -> None:
    await sleep.wake_up(replier.dialogue.now())
    await replier.reply(WAKE_NOTE)


# ---------- Настройки ----------

SHORT_STAGE = {"acquaintances": "знаком.", "friends": "друзья", "close_friends": "близкие", "romance": "роман."}


async def _settings_view(proactive: Proactive, sleep: SleepTracker, images: ImageService,
                         relationship: RelationshipTracker, now) -> tuple[str, InlineKeyboardMarkup]:
    enabled = await proactive.enabled()
    per_day = await proactive.per_day()
    start, end, learned = await sleep.window(now)
    fixed = await relationship.fixed_stage()
    stage = await relationship.stage()
    erp_from = await relationship.erp_min_stage()
    text = (
        "⚙️ настройки\n\n"
        f"✉️ пишет первой: {'вкл' if enabled else 'выкл'}, до {per_day} раз в день\n"
        f"🖼 её картинки: {'вкл' if await images.enabled() else 'выкл'}, сегодня ещё {await images.left_today()}\n"
        f"😴 твой сон: {start:02d}:00–{end:02d}:00 "
        f"({'по твоей активности' if learned else 'по умолчанию, пока мало данных'}), "
        f"сейчас {'спишь' if await sleep.is_asleep(now) else 'не спишь'}\n"
        f"💞 стадия: {stage_label(stage)} ({'зафиксирована' if fixed else 'меняется сама'})\n"
        f"🔞 флирт и ERP — со стадии «{stage_label(erp_from)}»\n\n"
        "ряды кнопок: сообщения первой в день · стадия (авто = сама) · ERP со стадии"
    )
    rows = [
        [InlineKeyboardButton(text="выключить сообщения первой" if enabled else "включить сообщения первой",
                              callback_data="set:proactive:toggle")],
        [InlineKeyboardButton(text=f"{'• ' if n == per_day else ''}{n}", callback_data=f"set:per_day:{n}")
         for n in range(1, 7)],
        [InlineKeyboardButton(text="выключить её картинки" if await images.enabled() else "включить её картинки",
                              callback_data="set:images:toggle")],
        [InlineKeyboardButton(text=f"{'• ' if fixed is None else ''}авто", callback_data="set:stage:auto")]
        + [InlineKeyboardButton(text=f"{'• ' if fixed == s else ''}{SHORT_STAGE[s]}", callback_data=f"set:stage:{s}")
           for s in STAGE_ORDER],
        [InlineKeyboardButton(text=f"{'• ' if erp_from == s else ''}ERP: {SHORT_STAGE[s]}", callback_data=f"set:erp:{s}")
         for s in STAGE_ORDER[1:]],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("settings"))
async def on_settings(message: Message, proactive: Proactive, sleep: SleepTracker, images: ImageService,
                      relationship: RelationshipTracker, replier: Replier) -> None:
    text, keyboard = await _settings_view(proactive, sleep, images, relationship, replier.dialogue.now())
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("set:"))
async def on_settings_change(callback: CallbackQuery, store: Store, proactive: Proactive, sleep: SleepTracker,
                             images: ImageService, relationship: RelationshipTracker, replier: Replier) -> None:
    _, key, value = callback.data.split(":", 2)
    if key == "proactive":
        await store.set_setting(KEY_ENABLED, "0" if await proactive.enabled() else "1")
    elif key == "per_day" and value.isdigit():
        await store.set_setting(KEY_PER_DAY, value)
    elif key == "images":
        await store.set_setting(KEY_IMAGES, "0" if await images.enabled() else "1")
    elif key == "stage" and (value == "auto" or value in STAGE_ORDER):
        await store.set_setting(KEY_FIXED_STAGE, "" if value == "auto" else value)
    elif key == "erp" and value in STAGE_ORDER:
        await store.set_setting(KEY_ERP, value)
    text, keyboard = await _settings_view(proactive, sleep, images, relationship, replier.dialogue.now())
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        pass  # "message is not modified" — нажали ту же кнопку
    await callback.answer("сохранено")


# ---------- Ролевые игры ----------

RP_STARTED_NOTE = "Он только что предложил поиграть в текстовую ролевую игру."
OOC_STARTED_NOTE = "Он вышел из роли, чтобы поболтать."
STATUS_NAMES = {"active": "идёт", "paused": "на паузе", "ended": "закончена"}


@router.message(Command("rp"))
async def on_rp(message: Message, command: CommandObject, roleplay: RoleplayManager, replier: Replier) -> None:
    rp = await roleplay.get()
    store, now = replier.dialogue.store, replier.dialogue.now()
    idea = (command.args or "").strip()

    if rp.state == "ooc":
        scene = await roleplay.back_to_scene()
        await replier.reply(RESUME_NOTE.format(title=scene.title))
    elif rp.state == "scene":
        await message.answer(f"🎭 мы уже в сцене «{rp.scene.title}»\n"
                             "/ooc — выйти из роли, /end — закончить, /stop или «стоп» — остановить")
    elif rp.state == "setup":  # /rp во время обсуждения = «начинай уже»
        if idea:
            await store.add_message("user", idea, now)
        created = await roleplay.check_setup(force=True)
        if created is None:
            await message.answer("ой, что-то не получилось начать(( попробуй ещё раз")
            return
        scene, opening = created
        await replier.reply(START_SCENE_NOTE.format(title=scene.title, setting=scene.setting,
                                                    characters=scene.characters, opening=opening))
    else:
        await roleplay.start_setup()
        if idea:
            await store.add_message("user", f"давай поиграем в ролевую: {idea}", now)
            replier.debouncer.poke()
        else:
            await replier.reply(RP_STARTED_NOTE)


@router.message(Command("ooc"))
async def on_ooc(message: Message, command: CommandObject, roleplay: RoleplayManager, replier: Replier) -> None:
    rp = await roleplay.get()
    if rp.state not in ("scene", "ooc"):
        await message.answer("сейчас мы не играем) начать — /rp")
        return
    text = (command.args or "").strip()
    if text:  # /ooc с текстом — одна реплика вне роли
        await replier.dialogue.store.add_message("user", f"(({text}))", replier.dialogue.now())
        replier.debouncer.poke()
        return
    await roleplay.ooc()
    await replier.reply(OOC_STARTED_NOTE)


@router.message(Command("end"))
async def on_end(message: Message, roleplay: RoleplayManager, replier: Replier) -> None:
    scene = await roleplay.end()
    if scene is None:
        await message.answer("сейчас нет активной сцены)")
        return
    await replier.reply(END_NOTE.format(title=scene.title))


@router.message(Command("stop"))
async def on_stop(message: Message, roleplay: RoleplayManager, replier: Replier) -> None:
    if (await roleplay.get()).state == "":
        await message.answer("мы и не играем)")
        return
    await roleplay.stop()
    await message.answer(STOP_TEXT)
    await replier.dialogue.store.add_message("assistant", STOP_TEXT, replier.dialogue.now())


@router.message(Command("scenes"))
async def on_scenes(message: Message, store: Store) -> None:
    scenes = await store.list_scenes(limit=10)
    if not scenes:
        await message.answer("у нас ещё не было ролевых сцен) начать — /rp")
        return
    lines = ["🎭 наши сцены (нажми, чтобы вернуться):"]
    for s in scenes:
        summary = f"\n   {s.summary[:150]}…" if s.summary else ""
        lines.append(f"\n«{s.title}» — {STATUS_NAMES.get(s.status, s.status)}, {s.updated_at:%d.%m}{summary}")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"▶ {s.title[:40]}", callback_data=f"rp:resume:{s.id}")] for s in scenes
    ])
    await message.answer("\n".join(lines), reply_markup=keyboard)


@router.callback_query(F.data.startswith("rp:resume:"))
async def on_scene_resume(callback: CallbackQuery, roleplay: RoleplayManager, replier: Replier) -> None:
    scene = await roleplay.resume(int(callback.data.rsplit(":", 1)[1]))
    await callback.answer()
    if scene is None:
        await callback.message.answer("этой сцены уже нет(")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await replier.reply(RESUME_NOTE.format(title=scene.title))


@router.message(Command("status"))
async def on_status(message: Message, relationship: RelationshipTracker) -> None:
    await message.answer(await relationship.status_text())


# ---------- Расходы ----------

_MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
           "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def _tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} млн"
    if n >= 1000:
        return f"{n / 1000:.0f} тыс."
    return str(n)


@router.message(Command("stats"))
async def on_stats(message: Message, store: Store, llm_router: LLMRouter, settings: Settings,
                   images: ImageService) -> None:
    today = llm_router.today()
    month_start = today.replace(day=1).isoformat()
    spent = await store.cost_since(month_start)
    today_spent = await store.cost_since(today.isoformat())
    budget = settings.monthly_budget_rub

    lines = [
        f"📊 {_MONTHS[today.month - 1]}: {spent:.2f} ₽ из {budget:.0f} ₽ ({spent / budget * 100:.0f}%)"
        if budget else f"📊 за месяц: {spent:.2f} ₽",
        f"сегодня: {today_spent:.2f} ₽",
        "",
    ]
    usage = await store.usage_since(month_start)
    if usage:
        lines.append("по моделям за месяц:")
        for u in usage:
            cache = f", из кэша {u.cached_tokens / u.input_tokens * 100:.0f}%" if u.input_tokens and u.cached_tokens else ""
            name = u.model.split('/')[-1]
            if not u.input_tokens and not u.output_tokens:  # картинки
                lines.append(f"• {name} — {u.requests} картинок — {u.cost_rub:.2f} ₽")
            else:
                lines.append(f"• {name} — {u.requests} запр., {_tokens(u.input_tokens)} вход / "
                             f"{_tokens(u.output_tokens)} выход{cache} — {u.cost_rub:.2f} ₽")
    else:
        lines.append("в этом месяце запросов ещё не было")

    balance = await llm_router.primary.get_balance()
    if balance is not None:
        lines += ["", f"💰 баланс агрегатора: {balance:.2f} ₽"]
    lines.append(f"🖼 её картинок сегодня ещё можно: {await images.left_today()}")
    if llm_router.on_backup:
        lines.append("⚠️ сейчас работаю через резервный агрегатор")

    await message.answer("\n".join(lines))
