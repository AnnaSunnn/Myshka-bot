import asyncio
import hashlib
import html
import io
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
import llm
from config import BOT_TOKEN, GROUPS, PLACES
from extract import extract_text

log = logging.getLogger("bot")
router = Router()
# Бот працює лише в особистих повідомленнях.
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
esc = html.escape
KYIV = ZoneInfo("Europe/Kyiv")
LIMIT = 3900  # запас до ліміту Telegram у 4096 символів

OPTIONS = [("forest", "")] + [("group", g) for g in GROUPS] + [("place", p) for p in PLACES]


class Upload(StatesGroup):
    name = State()
    scope = State()
    confirm = State()
    replace = State()


def label(t, v):
    return {"forest": "увесь ліс", "group": f"частина лісу «{v}»", "place": f"рукавичка «{v}»"}[t]


def date_of(d):
    return datetime.fromisoformat(d["uploaded_at"]).astimezone(KYIV).strftime("%d.%m.%Y")


def source_line(d):
    return f"📄 <i>Джерело:</i> «{esc(d['filename'])}» — {esc(d['author'] or 'невідомий автор')}, надійшов {date_of(d)}"


def norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


def scope_kb():
    b = InlineKeyboardBuilder()
    for i, (t, v) in enumerate(OPTIONS):
        b.button(text=label(t, v), callback_data=f"sc:{i}")
    b.adjust(1)
    return b.as_markup()


def valid_scope(res: dict):
    if res.get("multiple_levels"):
        return None
    t, v = res.get("scope_type"), res.get("scope_value") or ""
    if t == "forest":
        return ("forest", "")
    if t == "group" and v in GROUPS:
        return ("group", v)
    if t == "place" and v in PLACES:
        return ("place", v)
    return None


# ---------- команди ----------

@router.message(Command("start", "help"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "Привіт! Я збираю поради звірів і відповідаю на запитання з них.\n\n"
        "📥 <b>Поділитися порадою:</b> надішліть мені файл PDF, DOCX або TXT.\n"
        "❓ <b>Запитати:</b> напишіть запитання й назвіть місце, наприклад: «Скільки внесок під дубом?»\n"
        "🗂 /my — ваші документи (можна видалити)\n\n"
        "Місця: " + ", ".join(PLACES))


@router.message(Command("my"))
async def cmd_my(m: Message):
    docs = db.user_docs(m.from_user.id)
    if not docs:
        await m.answer("Ви ще не надсилали документів.")
        return
    b = InlineKeyboardBuilder()
    lines = []
    for d in docs:
        lines.append(f"• «{esc(d['filename'])}» — {label(d['scope_type'], d['scope_value'])}, {date_of(d)}")
        b.button(text=f"🗑 Видалити «{d['filename'][:30]}»", callback_data=f"del:{d['id']}")
    b.adjust(1)
    await m.answer("Ваші актуальні документи:\n" + "\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("del:"))
async def on_delete(c: CallbackQuery):
    ok = db.delete_doc(int(c.data[4:]), c.from_user.id)
    await c.answer("Видалено" if ok else "Не вдалося видалити", show_alert=not ok)
    if ok:
        await c.message.answer("🗑 Документ видалено, бот більше його не показуватиме.")


# ---------- завантаження документа ----------

@router.message(F.document)
async def on_document(m: Message, state: FSMContext, bot: Bot):
    doc = m.document
    name = doc.file_name or "document"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ("pdf", "docx", "txt"):
        await m.answer("Я приймаю лише PDF, Word (.docx) та текстові (.txt) файли.")
        return
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await m.answer("Файл завеликий (понад 20 МБ). Надішліть, будь ласка, менший.")
        return
    try:
        buf = io.BytesIO()
        await bot.download(doc, destination=buf)
        text = extract_text(buf.getvalue(), ext)
    except Exception:
        log.exception("extract failed")
        text = ""
    if len(text.strip()) < 20:
        await m.answer("Не вдалося прочитати текст із цього файлу. Надішліть, будь ласка, інший файл "
                       "(PDF, DOCX або TXT з текстом). Нічого не збережено.")
        return
    h = hashlib.sha256(norm(text).encode()).hexdigest()
    await state.clear()
    await state.update_data(filename=name, text=text, hash=h, author_id=m.from_user.id)
    if not db.get_user(m.from_user.id):
        await state.set_state(Upload.name)
        await m.answer("Перш ніж зберегти документ, скажіть, будь ласка, ваше справжнє ім'я. "
                       "Я запам'ятаю його й називатиму вас автором порад.")
        return
    await classify_and_continue(m, state)


@router.message(Upload.name, F.text)
async def on_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if not 2 <= len(name) <= 60 or name.startswith("/"):
        await m.answer("Напишіть, будь ласка, ім'я (від 2 до 60 символів).")
        return
    db.set_user(m.from_user.id, name)
    await m.answer(f"Дякую, {esc(name)}! Запам'ятав.")
    await classify_and_continue(m, state)


async def classify_and_continue(m: Message, state: FSMContext):
    d = await state.get_data()
    await m.answer("Читаю документ…")
    try:
        res = await llm.classify_document(d["filename"], d["text"])
    except Exception:
        log.exception("classify failed")
        res = {}
    scope = valid_scope(res)
    if scope:
        await ask_confirm(m, state, scope)
    else:
        await state.set_state(Upload.scope)
        await m.answer("З тексту не зрозуміло, для кого ця порада. Оберіть, будь ласка, одне:",
                       reply_markup=scope_kb())


async def ask_confirm(m: Message, state: FSMContext, scope):
    await state.update_data(scope_type=scope[0], scope_value=scope[1])
    await state.set_state(Upload.confirm)
    d = await state.get_data()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Так", callback_data="cf:yes"),
        InlineKeyboardButton(text="❌ Ні, змінити", callback_data="cf:no")]])
    await m.answer(f"Документ «{esc(d['filename'])}» буде збережено для: <b>{label(*scope)}</b>. Усе правильно?",
                   reply_markup=kb)


@router.callback_query(Upload.scope, F.data.startswith("sc:"))
async def on_scope(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await ask_confirm(c.message, state, OPTIONS[int(c.data[3:])])


@router.callback_query(Upload.confirm, F.data == "cf:no")
async def on_confirm_no(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(Upload.scope)
    await c.message.answer("Тоді оберіть, для кого ця порада:", reply_markup=scope_kb())


@router.callback_query(Upload.confirm, F.data == "cf:yes")
async def on_confirm_yes(c: CallbackQuery, state: FSMContext, bot: Bot):
    await c.answer()
    d = await state.get_data()
    dup = db.find_active_by_hash_scope(d["hash"], d["scope_type"], d["scope_value"])
    if dup:
        await state.clear()
        await c.message.answer(
            f"Такий самий документ уже є для цього рівня: «{esc(dup['filename'])}», надіслав(ла) "
            f"{esc(dup['author'] or '—')} {date_of(dup)}. Нічого не змінено.")
        return
    existing = db.active_docs(d["scope_type"], d["scope_value"])
    if not existing:
        await save(c.message, state, bot, None)
        return
    await state.set_state(Upload.replace)
    b = InlineKeyboardBuilder()
    for e in existing:
        b.button(text=f"Нова версія «{e['filename'][:40]}»", callback_data=f"rp:{e['id']}")
    b.button(text="Окрема порада", callback_data="rp:0")
    b.adjust(1)
    await c.message.answer(
        f"Для цього місця ({label(d['scope_type'], d['scope_value'])}) уже є актуальні документи. "
        "Це нова версія одного з них (старий перестане діяти) чи окрема порада?",
        reply_markup=b.as_markup())


@router.callback_query(Upload.replace, F.data.startswith("rp:"))
async def on_replace(c: CallbackQuery, state: FSMContext, bot: Bot):
    await c.answer()
    d = await state.get_data()
    rid = int(c.data[3:])
    if rid and rid not in {e["id"] for e in db.active_docs(d["scope_type"], d["scope_value"])}:
        await c.message.answer("Цей документ уже змінився. Надішліть файл ще раз.")
        await state.clear()
        return
    await save(c.message, state, bot, rid or None)


async def save(m: Message, state: FSMContext, bot: Bot, replaces):
    d = await state.get_data()
    old = db.get_doc(replaces) if replaces else None
    db.add_doc(d["author_id"], d["filename"], d["text"], d["hash"], d["scope_type"], d["scope_value"], replaces)
    await state.clear()
    msg = "✅ Документ збережено."
    if old:
        msg += f" Стару версію «{esc(old['filename'])}» замінено, її більше не показуватиму."
    await m.answer(msg)
    if old and old["author_id"] != d["author_id"]:
        author = db.get_user(d["author_id"])
        try:
            await bot.send_message(old["author_id"],
                                   f"Ваш документ «{esc(old['filename'])}» замінено новою версією "
                                   f"від {esc(author['real_name'])}.")
        except Exception:
            log.warning("cannot notify %s", old["author_id"])


# ---------- запитання ----------

def tiers_for(t, v):
    """Рівні пошуку від найконкретнішого до загального."""
    if t == "place":
        return [db.active_docs("place", v),
                [d for g in PLACES[v] for d in db.active_docs("group", g)],
                db.active_docs("forest", "")]
    if t == "group":
        return [db.active_docs("group", v), db.active_docs("forest", "")]
    return [db.active_docs("forest", "")]


def fmt_item(d, ans, quote, with_quote=True):
    p = esc(ans[:600])
    if with_quote and quote:
        p += f"\n<blockquote>{esc(quote[:300])}</blockquote>"
    return p + "\n" + source_line(d)


def compose(found):
    """Збирає відповідь, не розриваючи HTML і вкладаючись у ліміт Telegram."""
    main = found[0]
    parts = [fmt_item(*main[0])]
    if len(main) > 1:
        parts.append("<b>Також на цьому рівні (інші документи):</b>\n"
                     + "\n\n".join(fmt_item(*it, with_quote=False) for it in main[1:]))
    if len(found) > 1:
        parts.append("<b>Додатково, загальніша порада:</b>\n" + fmt_item(*found[1][0], with_quote=False))
    text = ""
    for part in parts:
        if len(text) + len(part) + 2 > LIMIT:
            break
        text += ("\n\n" if text else "") + part
    return text


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def on_question(m: Message):
    q = m.text.strip()
    try:
        target = valid_scope(await llm.extract_target(q))
    except Exception:
        log.exception("extract_target failed")
        await m.answer("Сталася помилка, спробуйте ще раз за хвилину.")
        return
    if not target:
        await m.answer("Вкажіть, будь ласка, про що питаєте: місце (наприклад, «Скільки внесок під дубом?»), "
                       "частину лісу (хвойний, листяний, біля річки) або весь ліс.\n"
                       "Місця: " + ", ".join(PLACES))
        return
    t, v = target
    found = []  # по рівнях: [(doc, answer, quote)], найновіші першими
    try:
        for docs in tiers_for(t, v):
            if not docs:
                continue
            by_id = {d["id"]: d for d in docs}
            items = []
            for a in await llm.answer_from_docs(q, docs, v):
                d = by_id[a["doc_id"]]
                quote = a["quote"] if a["quote"] and norm(a["quote"]) in norm(d["text"]) else ""
                items.append((d, a["answer"], quote))
            if items:
                items.sort(key=lambda it: it[0]["uploaded_at"], reverse=True)
                found.append(items)
            if len(found) == 2:
                break
    except Exception:
        log.exception("answer failed")
        await m.answer("Сталася помилка, спробуйте ще раз за хвилину.")
        return
    if not found:
        await m.answer(f"За цим запитом ({label(t, v)}) у наданих документах поради не знайшлося. "
                       "Я не відповідаю зі своїх здогадок, бо не зміг би назвати джерело.")
        return
    await m.answer(compose(found))


@router.message()
async def on_other(m: Message, state: FSMContext):
    st = await state.get_state()
    if st == Upload.name.state:
        await m.answer("Напишіть, будь ласка, ваше ім'я текстом.")
    elif st:
        await m.answer("Зараз іде завантаження документа: оберіть варіант кнопкою вище "
                       "або надішліть /start, щоб почати спочатку.")
    else:
        await m.answer("Надішліть файл PDF, DOCX або TXT з порадою чи напишіть запитання з назвою місця. "
                       "/start — підказка.")


@router.callback_query(F.data.startswith(("sc:", "cf:", "rp:")))
async def on_stale_button(c: CallbackQuery):
    await c.answer("Ця дія застаріла. Надішліть файл ще раз.", show_alert=True)


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
