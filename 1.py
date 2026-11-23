import asyncio
import logging
import os
import csv
import io
import aiosqlite
import random
import urllib.parse
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    BufferedInputFile, FSInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession

# ============ КОНФИГ ============
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 7878666092
ADMIN_ID_2 = 5171953150
DB_PATH = "data/bazashell.sql"
CONTACT = "@shellwo"

SPONSOR_CHECK_CHANNEL = "@tut_botes"
SPONSOR_CHECK_URL = "https://t.me/tut_botes"
SPONSOR_SOFT_CHANNEL_URL = "https://t.me/+7QWa9WvKsF5kOTg6"

PRICES = {"Неделя": 200, "2 недели": 350, "1 месяц": 650, "3 месяца": 1250, "Навсегда": 2500}
VIP_PRICES = {"1 месяц": 1500, "3 месяца": 3500, "Навсегда": 7000}

# Реферальные milestone-бонусы: кол-во рефералов -> (тип, значение)
REF_MILESTONES = {
    1:  ("minutes", 30),
    3:  ("minutes", 60 * 24),        # 1 день
    5:  ("minutes", 60 * 24 * 3),    # 3 дня
    10: ("minutes", 60 * 24 * 7),    # неделя
    20: ("vip_days", 30),            # месяц VIP
}

DAILY_MIN_MINUTES = 5
DAILY_MIN_STARS = 0.1
DAILY_LUCK_CHANCE = 0.10
DAILY_LUCK_MINUTES = 30

MAX_REPORTS_STANDARD = 10
MAX_REPORTS_VIP = 30

logging.basicConfig(level=logging.INFO)

def now(): return datetime.now(timezone.utc)
def to_utc(dt):
    if dt is None: return None
    if dt.tzinfo is None: return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
def parse_iso_utc(s):
    if not s: return None
    try: return to_utc(datetime.fromisoformat(s))
    except Exception: return None
def format_msk(dt_str):
    dt = parse_iso_utc(dt_str) if isinstance(dt_str, str) else to_utc(dt_str)
    if dt is None: return "—"
    return (dt + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M МСК")

LINE = "━━━━━━━━━━━━━━━━━━━━"

# ============ БАЗА ============
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
            language_code TEXT, is_premium INTEGER DEFAULT 0, referred_by INTEGER,
            sub_until TEXT, snoops_count INTEGER DEFAULT 0, created_at TEXT
        )""")
        for col_sql in [
            "ALTER TABLE users ADD COLUMN vip_until TEXT", "ALTER TABLE users ADD COLUMN balance_stars REAL DEFAULT 0.0",
            "ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'ru'", "ALTER TABLE users ADD COLUMN last_daily_at TEXT",
            "ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0", "ALTER TABLE users ADD COLUMN notify_sub_sent INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN extra_sub INTEGER DEFAULT 0", "ALTER TABLE users ADD COLUMN last_ref_reminder TEXT",
            "ALTER TABLE users ADD COLUMN support_banned INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN first_seen TEXT", "ALTER TABLE users ADD COLUMN last_seen TEXT", "ALTER TABLE users ADD COLUMN is_left INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN sponsor_checked INTEGER DEFAULT 0"
        ]:
            try: await db.execute(col_sql)
            except Exception: pass
        await db.execute("CREATE TABLE IF NOT EXISTS referrals (id INTEGER PRIMARY KEY AUTOINCREMENT, inviter_id INTEGER, invited_id INTEGER UNIQUE, created_at TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS snoop_history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, target TEXT, reason TEXT, count INTEGER, mode TEXT, created_at TEXT)")
        try: await db.execute("ALTER TABLE snoop_history ADD COLUMN mode TEXT")
        except Exception: pass
        await db.execute("""
        CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY, bonus_minutes INTEGER DEFAULT 0, bonus_stars REAL DEFAULT 0.0,
            bonus_days_vip INTEGER DEFAULT 0, max_uses INTEGER DEFAULT 0, used_count INTEGER DEFAULT 0,
            expires_at TEXT, created_at TEXT
        )""")
        await db.execute("CREATE TABLE IF NOT EXISTS promo_activations (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, user_id INTEGER, used_at TEXT, UNIQUE(code, user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS action_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT, details TEXT, created_at TEXT)")
        await db.commit()

async def get_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return await cur.fetchone()

async def get_user_by_username(username):
    username = username.lstrip('@')
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE username=?", (username,))
        return await cur.fetchone()

async def add_user(tg_user, referred_by=None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM users WHERE user_id=?", (tg_user.id,))
        if await cur.fetchone():
            await db.execute("UPDATE users SET username=?, first_name=?, last_name=?, last_seen=?, is_left=0 WHERE user_id=?",
                             (tg_user.username, tg_user.first_name, tg_user.last_name, now().isoformat(), tg_user.id))
            await db.commit(); return False
        await db.execute("INSERT INTO users(user_id, username, first_name, last_name, language_code, is_premium, referred_by, created_at, language, first_seen, last_seen, sponsor_checked) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tg_user.id, tg_user.username, tg_user.first_name, tg_user.last_name, tg_user.language_code, 1 if getattr(tg_user, "is_premium", False) else 0, referred_by, now().isoformat(), "ru", now().isoformat(), now().isoformat(), 0))
        await db.commit(); return True

async def log_action(user_id, action, details=""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO action_logs(user_id, action, details, created_at) VALUES(?,?,?,?)", (user_id, action, details, now().isoformat()))
        await db.commit()

async def add_referral(inviter_id, invited_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO referrals(inviter_id, invited_id, created_at) VALUES(?,?,?)", (inviter_id, invited_id, now().isoformat()))
        await db.commit()

async def add_subscription_minutes(user_id, minutes):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT sub_until FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row: return None
        base = now()
        if row["sub_until"]:
            existing = parse_iso_utc(row["sub_until"])
            if existing and existing > base: base = existing
        new_until = base + timedelta(minutes=minutes)
        await db.execute("UPDATE users SET sub_until=?, notify_sub_sent=0 WHERE user_id=?", (new_until.isoformat(), user_id))
        await db.commit(); return new_until

async def add_vip_days(user_id, days):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT vip_until, sub_until FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row: return None
        base_vip = now()
        if row["vip_until"]:
            existing_vip = parse_iso_utc(row["vip_until"])
            if existing_vip and existing_vip > base_vip: base_vip = existing_vip
        new_vip = base_vip + timedelta(days=days)
        base_sub = now()
        if row["sub_until"]:
            existing_sub = parse_iso_utc(row["sub_until"])
            if existing_sub and existing_sub > base_sub: base_sub = existing_sub
        new_sub = max(base_sub, new_vip)
        await db.execute("UPDATE users SET vip_until=?, sub_until=?, notify_sub_sent=0 WHERE user_id=?", (new_vip.isoformat(), new_sub.isoformat(), user_id))
        await db.commit(); return new_vip

async def add_stars_balance(user_id, stars: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance_stars = balance_stars + ? WHERE user_id=?", (stars, user_id))
        await db.commit()

async def is_vip(user_id):
    u = await get_user(user_id)
    if not u or not u["vip_until"]: return False
    return bool(parse_iso_utc(u["vip_until"]) and parse_iso_utc(u["vip_until"]) > now())

async def is_subscribed(user_id):
    u = await get_user(user_id)
    if not u: return False
    n = now()
    if parse_iso_utc(u["sub_until"]) and parse_iso_utc(u["sub_until"]) > n: return True
    if parse_iso_utc(u["vip_until"]) and parse_iso_utc(u["vip_until"]) > n: return True
    return False

async def has_access(user_id): return await is_subscribed(user_id) or await is_vip(user_id)

async def is_banned(user_id):
    u = await get_user(user_id)
    return bool(u and "is_banned" in u.keys() and u["is_banned"])

async def inc_snoops(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET snoops_count = snoops_count + 1 WHERE user_id=?", (user_id,))
        await db.commit()

async def log_snoop(user_id, target, reason, count, mode):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO snoop_history(user_id, target, reason, count, mode, created_at) VALUES(?,?,?,?,?,?)", (user_id, target, reason, count, mode, now().isoformat()))
        await db.commit()

async def get_snoop_history(user_id, limit=15):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT target, reason, count, mode, created_at FROM snoop_history WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
        return await cur.fetchall()

async def get_top_referrals(limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT u.user_id, u.username, u.first_name, COUNT(r.id) AS cnt FROM referrals r JOIN users u ON u.user_id = r.inviter_id GROUP BY u.user_id ORDER BY cnt DESC LIMIT ?", (limit,))
        return await cur.fetchall()

async def stats():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        users = (await (await db.execute("SELECT COUNT(*) c FROM users")).fetchone())["c"]
        active_today = (await (await db.execute("SELECT COUNT(*) c FROM users WHERE last_seen >= date('now')")).fetchone())["c"]
        left = (await (await db.execute("SELECT COUNT(*) c FROM users WHERE is_left=1")).fetchone())["c"]
        refs = (await (await db.execute("SELECT COUNT(*) c FROM referrals")).fetchone())["c"]
        snoops = (await (await db.execute("SELECT COUNT(*) c FROM snoop_history")).fetchone())["c"]
        promos = (await (await db.execute("SELECT COUNT(*) c FROM promocodes")).fetchone())["c"]
        subs = 0; vips = 0; n = now()
        cur = await db.execute("SELECT sub_until, vip_until FROM users")
        for r in await cur.fetchall():
            sub_dt = parse_iso_utc(r["sub_until"]); vip_dt = parse_iso_utc(r["vip_until"])
            if (sub_dt and sub_dt > n) or (vip_dt and vip_dt > n): subs += 1
            if vip_dt and vip_dt > n: vips += 1
    return users, refs, subs, snoops, vips, promos, active_today, left

async def all_user_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        return [r[0] for r in await cur.fetchall()]

async def set_ban(user_id, banned):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_banned=? WHERE user_id=?", (1 if banned else 0, user_id))
        await db.commit()

async def set_sponsor_checked(user_id, val=1):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET sponsor_checked=? WHERE user_id=?", (val, user_id))
        await db.commit()

async def export_users_csv():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users")
        rows = await cur.fetchall()
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["user_id", "username", "first_name", "last_name", "sub_until", "vip_until", "balance_stars", "snoops_count", "created_at"])
    for r in rows: w.writerow([r["user_id"], r["username"], r["first_name"], r["last_name"], r["sub_until"], r["vip_until"], r["balance_stars"], r["snoops_count"], r["created_at"]])
    return out.getvalue().encode("utf-8")

async def check_expiring_subscriptions(bot: Bot):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT user_id, sub_until, notify_sub_sent FROM users WHERE sub_until IS NOT NULL")
        rows = await cur.fetchall()
    n = now()
    for r in rows:
        dt = parse_iso_utc(r["sub_until"])
        if not dt: continue
        delta = (dt - n).total_seconds()
        if 0 < delta <= 24 * 3600 and not r["notify_sub_sent"]:
            try: await bot.send_message(r["user_id"], f"Ваша подписка истекает через 24 часа.\n{LINE}\nДо: {format_msk(r['sub_until'])}\nПродлить: {CONTACT}", parse_mode="HTML")
            except Exception: pass
            async with aiosqlite.connect(DB_PATH) as db2:
                await db2.execute("UPDATE users SET notify_sub_sent=1 WHERE user_id=?", (r["user_id"],))
                await db2.commit()

async def check_ref_reminders(bot: Bot):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT u.user_id, u.last_ref_reminder, COUNT(r.id) as cnt FROM users u JOIN referrals r ON r.inviter_id = u.user_id GROUP BY u.user_id HAVING cnt >= 5")
        rows = await cur.fetchall()
    n = now()
    for r in rows:
        last = parse_iso_utc(r["last_ref_reminder"])
        if not last or (n - last).total_seconds() >= 10 * 3600:
            try:
                await bot.send_message(r["user_id"], f"Видим что ты приглашаешь друзей.\n{LINE}\nГотовы давать за это деньги, за каждого друга <b>0.10$</b>\nДействуй бро.", parse_mode="HTML")
            except Exception: pass
            async with aiosqlite.connect(DB_PATH) as db2:
                await db2.execute("UPDATE users SET last_ref_reminder=? WHERE user_id=?", (n.isoformat(), r["user_id"]))
                await db2.commit()

async def background_loop(bot: Bot):
    while True:
        try:
            await check_expiring_subscriptions(bot)
            await check_ref_reminders(bot)
        except Exception as e: logging.error(f"bg loop: {e}")
        await asyncio.sleep(3600)

async def check_sponsor_subscription(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=SPONSOR_CHECK_CHANNEL, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logging.error(f"check_sponsor: {e}")
        return False

async def notify_admins_new_user(bot: Bot, tg_user, referred_by):
    ref_text = f"@{referred_by}" if isinstance(referred_by, str) else (f"<code>{referred_by}</code>" if referred_by else "нет")
    text = (
        f"<b>Новый пользователь в боте</b>\n{LINE}\n"
        f"Имя: <b>{tg_user.first_name or '—'}</b>\n"
        f"Username: @{tg_user.username or '—'}\n"
        f"ID: <code>{tg_user.id}</code>\n"
        f"Реф: {ref_text}\n"
        f"Время: {format_msk(now().isoformat())}"
    )
    for admin in {ADMIN_ID, ADMIN_ID_2}:
        try: await bot.send_message(admin, text, parse_mode="HTML")
        except Exception: pass

router = Router()

# ============ КЛАВИАТУРЫ ============
# Главное меню — остаётся Reply (клавиатурным), как и просили
def main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Снести аккаунт")],
        [KeyboardButton(text="Услуги"), KeyboardButton(text="Подписка")],
        [KeyboardButton(text="Бонусы"), KeyboardButton(text="Настройки")],
        [KeyboardButton(text="Техподдержка")],
    ], resize_keyboard=True)

# Всё остальное — инлайн
def settings_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Личный кабинет", callback_data="set:profile")],
        [InlineKeyboardButton(text="Моя статистика", callback_data="set:stats")],
        [InlineKeyboardButton(text="История сносов", callback_data="set:history")],
        [InlineKeyboardButton(text="Топ рефералов", callback_data="set:top")],
        [InlineKeyboardButton(text="Язык", callback_data="set:lang")],
        [InlineKeyboardButton(text="О боте", callback_data="set:about")],
    ])

def cancel_inline_kb(callback_data="cancel:global"):
    """Инлайн-кнопка отмены для диалогов."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отмена", callback_data=callback_data)]
    ])

# Оставляем Reply-кнопку "Отмена" только там, где нужен текстовый ввод
def cancel_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)

def is_cancel_text(txt): return txt in ("Отмена", "Cancel", "Скасувати")

# ============ FSM ============
class Snoop(StatesGroup):
    mode = State(); target = State(); reason = State(); count = State(); processing = State()
class Broadcast(StatesGroup):
    waiting_text = State()
class GiveSub(StatesGroup):
    waiting_target = State(); waiting_amount = State()
class GiveVip(StatesGroup):
    waiting_target = State(); waiting_amount = State()
class GiveStars(StatesGroup):
    waiting_target = State(); waiting_amount = State()
class BanUser(StatesGroup):
    waiting_target = State()
class CreatePromo(StatesGroup):
    waiting_code = State(); waiting_minutes = State(); waiting_stars = State()
    waiting_vip = State(); waiting_uses = State(); waiting_expires = State()
class ActivatePromo(StatesGroup):
    waiting_code = State()
class CheckPromo(StatesGroup):
    waiting_code = State()
class Support(StatesGroup):
    waiting_text = State()
class AdminReply(StatesGroup):
    waiting_text = State()
    target_user_id = State()

REASONS = [
    "Спам", "Мошенничество", "Оскорбления", "Фейк-аккаунт",
    "Пропаганда", "Насилие", "Порнография", "Взлом аккаунта",
    "Массовые рассылки", "Нарушение авторских прав", "Импersonation", "Другое",
    "Терроризм", "Экстремизм", "Наркотики", "Оружие",
    "Детская порнография", "Жестокое обращение", "Травля (буллинг)", "Угрозы",
    "Фишинг", "Криптомошенничество", "Продажа личных данных", "Обход блокировок"
]

REPORT_TEMPLATES = {
    "Спам": "This account is engaged in unsolicited spam messaging.",
    "Мошенничество": "This account is involved in fraudulent activity.",
    "Оскорбления": "This user is harassing and insulting other users.",
    "Фейк-аккаунт": "This is a fake account impersonating another person.",
    "Пропаганда": "This account spreads prohibited propaganda content.",
    "Насилие": "This account shares violent content.",
    "Порнография": "This account distributes pornographic content.",
    "Взлом аккаунта": "This account was hacked.",
    "Массовые рассылки": "This account is used for mass broadcasting.",
    "Нарушение авторских прав": "This user distributes copyrighted content.",
    "Импersonation": "This account impersonates another user.",
    "Другое": "This account violates Telegram Terms of Service.",
    "Терроризм": "This account spreads terrorist propaganda.",
    "Экстремизм": "This account promotes extremist views.",
    "Наркотики": "This account promotes drug use.",
    "Оружие": "This account sells weapons.",
    "Детская порнография": "This account contains child pornography.",
    "Жестокое обращение": "This account contains animal cruelty.",
    "Травля (буллинг)": "This user is bullying other users.",
    "Угрозы": "This user is threatening others.",
    "Фишинг": "This account is used for phishing.",
    "Криптомошенничество": "This account scams people with crypto.",
    "Продажа личных данных": "This account sells personal data.",
    "Обход блокировок": "This account helps bypass bans.",
}

def sponsor_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Канал 1 — @tut_botes", url=SPONSOR_CHECK_URL)],
        [InlineKeyboardButton(text="Канал 2 — партнёрский", url=SPONSOR_SOFT_CHANNEL_URL)],
        [InlineKeyboardButton(text="Проверить подписку", callback_data="check_sponsor")]
    ])

# ============ РЕФЕРАЛЫ — MILESTONE ============
def _milestone_text(bonus_type: str, bonus_value: int) -> str:
    if bonus_type == "minutes":
        days = bonus_value // (60 * 24)
        mins = bonus_value % (60 * 24)
        if days > 0:
            return f"+{days} дней подписки"
        return f"+{mins} минут подписки"
    elif bonus_type == "vip_days":
        return f"+{bonus_value} дней VIP"
    return ""

async def check_ref_milestones(bot: Bot, inviter_id: int):
    """Проверяет и выдаёт milestone-бонус при достижении порога рефералов."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE inviter_id=?",
            (inviter_id,)
        )
        count = (await cur.fetchone())[0]

    # Следующий milestone для подсказки
    def next_milestone_info(current_count: int):
        next_counts = sorted(k for k in REF_MILESTONES if k > current_count)
        if not next_counts:
            return "\nТы достиг максимального уровня! 🏆"
        nc = next_counts[0]
        nt, nv = REF_MILESTONES[nc]
        return f"\nДо следующего бонуса: ещё <b>{nc - current_count}</b> друзей → {_milestone_text(nt, nv)}"

    if count in REF_MILESTONES:
        bonus_type, bonus_value = REF_MILESTONES[count]
        bonus_text = _milestone_text(bonus_type, bonus_value)

        if bonus_type == "minutes":
            await add_subscription_minutes(inviter_id, bonus_value)
        elif bonus_type == "vip_days":
            await add_vip_days(inviter_id, bonus_value)

        await log_action(inviter_id, "ref_milestone", f"count={count} bonus={bonus_text}")

        try:
            await bot.send_message(
                inviter_id,
                f"<b>Milestone за рефералов!</b>\n{LINE}\n"
                f"Друзей приглашено: <b>{count}</b>\n"
                f"Получено: <b>{bonus_text}</b>"
                f"{next_milestone_info(count)}",
                parse_mode="HTML"
            )
        except Exception:
            pass
    else:
        # Просто уведомление о новом реферале без milestone
        next_counts = sorted(k for k in REF_MILESTONES if k > count)
        if next_counts:
            nc = next_counts[0]
            nt, nv = REF_MILESTONES[nc]
            try:
                await bot.send_message(
                    inviter_id,
                    f"<b>Новый реферал!</b>\n{LINE}\n"
                    f"Всего друзей: <b>{count}</b>\n"
                    f"До бонуса: ещё <b>{nc - count}</b> друзей\n"
                    f"Награда: <b>{_milestone_text(nt, nv)}</b>",
                    parse_mode="HTML"
                )
            except Exception:
                pass


async def process_referral(bot: Bot, user_id: int, username: str, referred_by: int):
    await add_referral(referred_by, user_id)
    await log_action(referred_by, "referral_reward", f"invited={user_id}")
    # Теперь milestone вместо фиксированного бонуса
    await check_ref_milestones(bot, referred_by)

# ============ ОТМЕНА ============
@router.message(F.text == "Отмена")
async def global_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is not None:
        await state.clear()
        await message.answer("Действие отменено.", reply_markup=main_kb())
    else:
        await message.answer("Нечего отменять.", reply_markup=main_kb())

@router.callback_query(F.data == "cancel:global")
async def global_cancel_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try: await cb.message.edit_text("Отменено.")
    except Exception: pass
    await cb.message.answer("Вы вернулись в главное меню.", reply_markup=main_kb())
    await cb.answer()

# ============ СТАРТ ============
@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    if await is_banned(user.id): return await message.answer("Вы забанены в боте.")
    referred_by = None
    parts = (message.text or "").split()
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            inviter = int(parts[1][4:])
            if inviter != user.id: referred_by = inviter
        except ValueError: pass
    is_new = await add_user(user, referred_by)

    if referred_by and is_new: await process_referral(message.bot, user.id, user.username, referred_by)

    if is_new:
        await notify_admins_new_user(message.bot, user, referred_by)

    u = await get_user(user.id)
    if not (u and "sponsor_checked" in u.keys() and u["sponsor_checked"]):
        if not await check_sponsor_subscription(message.bot, user.id):
            text = (
                f"<b>Подпишись на наши каналы, чтобы пользоваться ботом.</b>\n{LINE}\n"
                f"1️⃣ <b>@tut_botes</b> — основной канал (проверяется автоматически)\n"
                f"2️⃣ <b>Партнёрский канал</b> — тоже подпишись, будем благодарны\n\n"
                f"После подписки нажми «Проверить подписку»."
            )
            return await message.answer(text, parse_mode="HTML", reply_markup=sponsor_kb())
        else:
            await set_sponsor_checked(user.id, 1)

    name = user.first_name or "друг"
    # Строим текст milestone-прогрессии для /start
    milestones_text = " → ".join(
        f"{k} друг{'а' if k == 3 else 'ей' if k >= 5 else ''} = {_milestone_text(t, v)}"
        for k, (t, v) in sorted(REF_MILESTONES.items())
    )
    text = (
        f"Привет, <b>{name}</b>.\n\n"
        f"Это лучший бот для сноса телеграм-аккаунтов — <b>ShellSnos</b>.\n\n"
        f"Мы делаем всё чётко, быстро и безопасно:\n"
        f"Стандарт (заморозка) — от 2 до 7 недель.\n"
        f"VIP (полный снос) — от 30 минут до 7 часов.\n\n"
        f"<b>Реферальная программа:</b>\n{milestones_text}\n\n"
        f"Жми на кнопки ниже, чтобы начать."
    )
    photo = FSInputFile("start.png")
    await message.answer_photo(photo=photo, caption=text, parse_mode="HTML", reply_markup=main_kb())

@router.callback_query(F.data == "check_sponsor")
async def cb_check_sponsor(cb: CallbackQuery):
    if await check_sponsor_subscription(cb.bot, cb.from_user.id):
        await set_sponsor_checked(cb.from_user.id, 1)
        try: await cb.message.delete()
        except Exception: pass
        name = cb.from_user.first_name or "друг"
        milestones_text = " → ".join(
            f"{k} = {_milestone_text(t, v)}"
            for k, (t, v) in sorted(REF_MILESTONES.items())
        )
        text = (
            f"Привет, <b>{name}</b>.\n\n"
            f"Это лучший бот для сноса телеграм-аккаунтов — <b>ShellSnos</b>.\n\n"
            f"Стандарт (заморозка) — от 2 до 7 недель.\n"
            f"VIP (полный снос) — от 30 минут до 7 часов.\n\n"
            f"<b>Рефералы:</b> {milestones_text}\n\n"
            f"Жми на кнопки ниже, чтобы начать."
        )
        photo = FSInputFile("start.png")
        await cb.message.answer_photo(photo=photo, caption=text, parse_mode="HTML", reply_markup=main_kb())
    else:
        await cb.answer("Вы не подписаны на @tut_botes.", show_alert=True)
    await cb.answer()

# ============ СНОС ============
@router.message(F.text == "Снести аккаунт")
async def snoop_start(message: Message, state: FSMContext):
    await state.clear()
    if await is_banned(message.from_user.id): return await message.answer("Вы забанены в боте.")

    if not await has_access(message.from_user.id):
        me = await message.bot.me()
        ref_link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
        # Прогресс пользователя по milestones
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id=?", (message.from_user.id,))
            my_refs = (await cur.fetchone())[0]
        next_counts = sorted(k for k in REF_MILESTONES if k > my_refs)
        if next_counts:
            nc = next_counts[0]
            nt, nv = REF_MILESTONES[nc]
            progress_text = f"У тебя уже <b>{my_refs}</b> друзей. Ещё <b>{nc - my_refs}</b> → {_milestone_text(nt, nv)}"
        else:
            progress_text = f"У тебя <b>{my_refs}</b> друзей — ты достиг максимального уровня!"

        text = (
            f"<b>У вас нет активной подписки.</b>\n{LINE}\n\n"
            f"<b>Как получить подписку бесплатно:</b>\n"
            f"Приглашай друзей по своей реф-ссылке и накапливай время:\n\n"
            f"1 друг → +30 мин подписки\n"
            f"3 друга → +1 день подписки\n"
            f"5 друзей → +3 дня подписки\n"
            f"10 друзей → +1 неделя подписки\n"
            f"20 друзей → +1 месяц VIP\n\n"
            f"{progress_text}\n\n"
            f"Твоя реф-ссылка:\n<code>{ref_link}</code>\n\n"
            f"<b>Или купи у администратора:</b> {CONTACT}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Купить подписку", url=f"https://t.me/shellwo")],
        ])
        return await message.answer(text, parse_mode="HTML", reply_markup=kb)

    vip = await is_vip(message.from_user.id)
    await state.set_state(Snoop.mode)
    kb_rows = []
    if vip:
        kb_rows.append([InlineKeyboardButton(text="Полный снос (VIP)", callback_data="mode:full")])
    else:
        kb_rows.append([InlineKeyboardButton(text="Полный снос (только VIP)", callback_data="mode:full_locked")])
    kb_rows.append([InlineKeyboardButton(text="Заморозить (стандарт)", callback_data="mode:freeze")])
    kb_rows.append([InlineKeyboardButton(text="Отмена", callback_data="mode:cancel")])
    photo = FSInputFile("snos.png")
    await message.answer_photo(
        photo=photo,
        caption=f"<b>Выберите тип сноса</b>\n{LINE}\nПолный снос — удаление навсегда (VIP, 30 мин – 7 часов).\nЗаморозка — временная блокировка (стандарт, 2–7 недель).\n{LINE}\nВаш статус: <b>{'VIP' if vip else 'Обычный'}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )

@router.callback_query(F.data == "mode:cancel")
async def mode_cancel_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try: await cb.message.edit_text("Отменено.")
    except Exception: pass
    await cb.message.answer("Вы вернулись в главное меню.", reply_markup=main_kb())
    await cb.answer()

@router.callback_query(F.data == "mode:full_locked")
async def mode_full_locked(cb: CallbackQuery):
    await cb.answer("Полный снос доступен только VIP.", show_alert=True)

@router.callback_query(F.data.in_({"mode:full", "mode:freeze"}))
async def mode_selected(cb: CallbackQuery, state: FSMContext):
    if cb.data == "mode:full" and not await is_vip(cb.from_user.id):
        return await cb.answer("Полный снос доступен только VIP.", show_alert=True)
    mode = "Полный снос" if cb.data == "mode:full" else "Заморозка"
    await state.update_data(mode=mode)
    await state.set_state(Snoop.target)
    try:
        await cb.message.edit_caption(
            f"<b>Снос аккаунта</b>\n{LINE}\nРежим: <b>{mode}</b>\n{LINE}\nУкажите username (@name) или ID:",
            parse_mode="HTML"
        )
    except Exception:
        await cb.message.answer(
            f"<b>Снос аккаунта</b>\n{LINE}\nРежим: <b>{mode}</b>\n{LINE}\nУкажите username (@name) или ID:",
            parse_mode="HTML"
        )
    # Reply-клавиатура с "Отмена" нужна для текстового ввода
    await cb.message.answer("Введите цель (или «Отмена»):", reply_markup=cancel_kb())
    await cb.answer()

@router.message(Snoop.target)
async def snoop_target(message: Message, state: FSMContext):
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())

    target = (message.text or "").strip()
    valid = False
    if target.startswith("@") and len(target) >= 5: valid = True
    elif target.isdigit() and 4 <= len(target) <= 11: valid = True

    if not valid:
        return await message.answer("Неверный формат. Укажите @username (мин 4 символа) или Telegram ID (4–11 цифр).")

    await state.update_data(target=target)
    await state.set_state(Snoop.reason)
    rows, row = [], []
    for i, r in enumerate(REASONS, 1):
        row.append(InlineKeyboardButton(text=r, callback_data=f"reason:{i}"))
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="reason:cancel")])
    await message.answer("Выберите причину:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@router.callback_query(F.data == "reason:cancel")
async def reason_cancel_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try: await cb.message.edit_text("Отменено.")
    except Exception: pass
    await cb.message.answer("Вы вернулись в главное меню.", reply_markup=main_kb())
    await cb.answer()

@router.callback_query(F.data.startswith("reason:"))
async def snoop_reason(cb: CallbackQuery, state: FSMContext):
    raw = cb.data.split(":")[1]
    if raw == "cancel": return await cb.answer()
    try: idx = int(raw)
    except ValueError: return await cb.answer("Неверно")
    if idx < 1 or idx > len(REASONS): return await cb.answer("Неверно")
    reason = REASONS[idx - 1]
    await state.update_data(reason=reason)
    await state.set_state(Snoop.count)

    vip = await is_vip(cb.from_user.id)
    max_reports = MAX_REPORTS_VIP if vip else MAX_REPORTS_STANDARD

    try: await cb.message.edit_text(f"<b>Причина:</b> {reason}", parse_mode="HTML")
    except Exception: pass
    await cb.message.answer(
        f"Сколько жалоб? (1–{max_reports})\n<i>Меньше жалоб — быстрее снос.</i>",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )
    await cb.answer()

@router.message(Snoop.count)
async def snoop_count(message: Message, state: FSMContext):
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())

    user_id = message.from_user.id
    vip = await is_vip(user_id)
    max_reports = MAX_REPORTS_VIP if vip else MAX_REPORTS_STANDARD

    try:
        n = int((message.text or "").strip())
        if not 1 <= n <= max_reports: raise ValueError
    except ValueError:
        return await message.answer(f"Введите число от 1 до {max_reports}.")

    data = await state.get_data()
    target = data.get("target"); reason = data.get("reason"); mode = data.get("mode", "Заморозка")
    if not target or not reason: await state.clear(); return await message.answer("Ошибка. Начните заново.", reply_markup=main_kb())

    await state.set_state(Snoop.processing)
    report_text = REPORT_TEMPLATES.get(reason, REPORT_TEMPLATES["Другое"])
    eta = "30 мин – 7 часов" if mode == "Полный снос" else "2–7 недель"

    await message.answer("Принято, запускаем.", reply_markup=main_kb())

    async def flash_msg(text: str, delay: float = 2.0):
        m = await message.answer(text, parse_mode="HTML")
        await asyncio.sleep(delay)
        try: await m.delete()
        except Exception: pass

    await flash_msg(f"<b>Инициализация сноса</b>\n{LINE}\nЦель: <code>{target}</code>\nРежим: {mode}\n{LINE}\nГотовимся к отправке жалоб...", 2.5)
    await flash_msg(f"<b>Инициализация сноса</b>\n{LINE}\nЦель: <code>{target}</code>\nРежим: {mode}\n{LINE}\nГотовим почту для сноса...", 2.0)
    await flash_msg(f"<b>Инициализация сноса</b>\n{LINE}\nЦель: <code>{target}</code>\nРежим: {mode}\n{LINE}\nЗагружаем текст жалобы...\n<blockquote>{report_text}</blockquote>", 2.5)
    await flash_msg(f"<b>Инициализация сноса</b>\n{LINE}\nЦель: <code>{target}</code>\nРежим: {mode}\n{LINE}\nПроверяем данные цели...", 1.5)
    await flash_msg(f"<b>Инициализация сноса</b>\n{LINE}\nЦель: <code>{target}</code>\nРежим: {mode}\n{LINE}\nСессии готовы. Запускаем жалобы...", 1.5)

    success = 0; failed = 0
    for i in range(1, n + 1):
        await asyncio.sleep(1.5)
        if random.random() < 0.85:
            success += 1
            await message.answer(f"<b>Жалоба {i}/{n}</b>\n{LINE}\nЦель: <code>{target}</code>\nПричина: {reason}\nРежим: {mode}\nТекст:\n<blockquote>{report_text}</blockquote>\n✅ Отправлено.", parse_mode="HTML")
        else:
            failed += 1
            await message.answer(f"<b>Жалоба {i}/{n}</b>\n{LINE}\nЦель: <code>{target}</code>\nПричина: {reason}\nРежим: {mode}\n❌ Не дошло (проблемы на стороне сервера). Повторная попытка...", parse_mode="HTML")

    await asyncio.sleep(1.5)
    await message.answer(
        f"<b>Снос завершён</b>\n{LINE}\n"
        f"Цель: <code>{target}</code>\nПричина: {reason}\nРежим: {mode}\n"
        f"Жалоб: {n} (успешно: {success}, неудачно: {failed})\nСрок: {eta}\n{LINE}\n"
        f"Жалобы отправлены.\nОжидайте результата.\n<i>Бывают задержки — проверяйте историю.</i>",
        parse_mode="HTML"
    )
    await inc_snoops(message.from_user.id)
    await log_snoop(message.from_user.id, target, reason, n, mode)
    await log_action(message.from_user.id, "snoop", f"{target} | {reason} | {n} | {mode}")
    await state.clear()

# ============ НАСТРОЙКИ ============
@router.message(F.text == "Настройки")
async def settings_menu(message: Message):
    photo = FSInputFile("settings3.png")
    await message.answer_photo(photo=photo, caption=f"<b>Настройки</b>\nВыберите раздел:", parse_mode="HTML", reply_markup=settings_kb())

@router.callback_query(F.data == "set:profile")
async def set_profile(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    if not u: return await cb.answer("Нажмите /start")
    sub = format_msk(u["sub_until"]); vip_date = format_msk(u["vip_until"]) if u["vip_until"] else "—"
    vip_status = "VIP" if await is_vip(cb.from_user.id) else "Обычный"
    active = "Активна" if await is_subscribed(cb.from_user.id) else "Нет"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id=?", (cb.from_user.id,))
        my_refs = (await cur.fetchone())[0]
    me = await cb.bot.me()
    link = f"https://t.me/{me.username}?start=ref_{cb.from_user.id}"

    # Прогресс по milestones
    next_counts = sorted(k for k in REF_MILESTONES if k > my_refs)
    if next_counts:
        nc = next_counts[0]
        nt, nv = REF_MILESTONES[nc]
        progress = f"До следующего бонуса: ещё <b>{nc - my_refs}</b> друзей → {_milestone_text(nt, nv)}"
    else:
        progress = "Ты достиг максимального реферального уровня!"

    text = (
        f"<b>Личный кабинет</b>\n{LINE}\n"
        f"ID: <code>{u['user_id']}</code>\nИмя: {u['first_name'] or '—'}\n"
        f"Статус: <b>{vip_status}</b> (до {vip_date})\n{LINE}\n"
        f"Баланс: <b>{u['balance_stars']:.2f} звезд</b>\n"
        f"Подписка: {active} (до {sub})\n{LINE}\n"
        f"Друзей приглашено: <b>{my_refs}</b>\n"
        f"{progress}\n\n"
        f"Реф-ссылка:\n<code>{link}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Поделиться ссылкой", url=f"https://t.me/share/url?url={urllib.parse.quote(link)}")],
        [InlineKeyboardButton(text="Назад", callback_data="set:back")]
    ])
    await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data == "set:stats")
async def set_stats(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    if not u: return await cb.answer("Нажмите /start")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id=?", (cb.from_user.id,))
        my_refs = (await cur.fetchone())[0]
    await cb.message.answer(
        f"<b>Моя статистика</b>\n{LINE}\n"
        f"Всего сносов: <b>{u['snoops_count']}</b>\n"
        f"Приглашено друзей: <b>{my_refs}</b>\n"
        f"Баланс звезд: <b>{u['balance_stars']:.2f}</b>",
        parse_mode="HTML"
    )
    await cb.answer()

@router.callback_query(F.data == "set:history")
async def set_history(cb: CallbackQuery):
    history = await get_snoop_history(cb.from_user.id, limit=15)
    photo = FSInputFile("history99.png")
    if not history:
        await cb.message.answer_photo(photo=photo, caption=f"<b>История сносов</b>\n{LINE}\nУ вас пока нет сносов.", parse_mode="HTML")
        return await cb.answer()
    lines = [f"<b>История сносов</b>\n{LINE}"]
    for target, reason, count, mode, created in history:
        lines.append(f"Цель: <code>{target}</code>\nПричина: {reason}\nРежим: {mode or '—'}\nЖалоб: {count}\n{format_msk(created)}\n")
    lines.append(LINE)
    text = "\n".join(lines)
    if len(text) > 1000: text = text[:1000] + "\n...\n(Показаны последние)"
    await cb.message.answer_photo(photo=photo, caption=text, parse_mode="HTML")
    await cb.answer()

@router.callback_query(F.data == "set:top")
async def set_top(cb: CallbackQuery):
    top = await get_top_referrals(10)
    photo = FSInputFile("referals2.png")
    if not top:
        await cb.message.answer_photo(photo=photo, caption="Пока пусто.")
        return await cb.answer()
    lines = [f"<b>Топ по рефералам</b>\n{LINE}"]
    medals = ["1.", "2.", "3."]
    for i, r in enumerate(top, 1):
        medal = medals[i-1] if i <= 3 else f"{i}."
        name = f"@{r['username']}" if r['username'] else r['first_name'] or f"id{r['user_id']}"
        lines.append(f"{medal} {name} — <b>{r['cnt']}</b>")
    lines.append(LINE)
    lines.append(f"<i>Топ-1 получает +1 день VIP каждую неделю.</i>")
    await cb.message.answer_photo(photo=photo, caption="\n".join(lines), parse_mode="HTML")
    await cb.answer()

@router.callback_query(F.data == "set:lang")
async def set_lang_menu(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Русский", callback_data="lang:ru")],
        [InlineKeyboardButton(text="English", callback_data="lang:en")],
        [InlineKeyboardButton(text="Українська", callback_data="lang:ua")]
    ])
    await cb.message.answer("Язык / Language / Мова", reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data.startswith("lang:"))
async def set_lang(cb: CallbackQuery):
    lang = cb.data.split(":")[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET language=? WHERE user_id=?", (lang, cb.from_user.id))
        await db.commit()
    await cb.message.answer("Язык изменён.", reply_markup=main_kb())
    await cb.answer()

@router.callback_query(F.data == "set:about")
async def set_about(cb: CallbackQuery):
    milestones_lines = "\n".join(
        f"— {k} {'друг' if k == 1 else 'друга' if k <= 4 else 'друзей'} → {_milestone_text(t, v)}"
        for k, (t, v) in sorted(REF_MILESTONES.items())
    )
    await cb.message.answer(
        f"<b>О боте</b>\n{LINE}\nShellSnos — сервис сноса телеграм аккаунтов.\n\n"
        f"<b>Возможности:</b>\n— Заморозка аккаунтов (стандарт, 2–7 недель)\n— Полный снос (VIP, 30 мин – 7 часов)\n"
        f"— Ежедневные бонусы и промокоды\n— VIP: полный снос, приоритет\n"
        f"— Топ рефералов, личная статистика, история\n— Мультиязычность: RU / EN / UA\n\n"
        f"<b>Реферальные бонусы:</b>\n{milestones_lines}\n\n"
        f"<b>Гарантия:</b> если результат не достигнут в срок — вернём деньги или повторим бесплатно.\n\n"
        f"Поддержка: {CONTACT}", parse_mode="HTML"
    )
    await cb.answer()

@router.callback_query(F.data == "set:back")
async def set_back(cb: CallbackQuery):
    photo = FSInputFile("settings3.png")
    try:
        await cb.message.answer_photo(photo=photo, caption="<b>Настройки</b>\nВыберите раздел:", parse_mode="HTML", reply_markup=settings_kb())
    except Exception:
        await cb.message.answer("<b>Настройки</b>\nВыберите раздел:", parse_mode="HTML", reply_markup=settings_kb())
    await cb.answer()

# ============ УСЛУГИ ============
SERVICES = [
    {"id": "del_channel",    "name": "Снос канала",                    "price": 499,  "desc": "Удаление Telegram-канала обидчика. Работаем по жалобам и техническим методам."},
    {"id": "del_chat",       "name": "Снос чата/группы",               "price": 399,  "desc": "Удаление Telegram-чата или группы. Быстро и без следов."},
    {"id": "del_tiktok",     "name": "Снос TikTok аккаунта",           "price": 549,  "desc": "Удаление аккаунта TikTok через массовые жалобы на нарушения."},
    {"id": "dox",            "name": "Пробить инфу на человека",        "price": 699,  "desc": "Полный досье: имя, номер телефона, соцсети, геолокация, связанные аккаунты."},
    {"id": "police",         "name": "Донести на человека в полицию",   "price": 350,  "desc": "Составим грамотное обращение в полицию с доказательной базой и поможем подать."},
    {"id": "hack",           "name": "Взломать аккаунт обидчика",       "price": 750,  "desc": "Получение доступа к аккаунту цели (Telegram, ВКонтакте, Instagram). Анонимно."},
    {"id": "del_vk",         "name": "Снос ВКонтакте страницы",        "price": 299,  "desc": "Удаление страницы ВКонтакте через жалобы и модерацию."},
    {"id": "del_inst",       "name": "Снос Instagram аккаунта",         "price": 449,  "desc": "Удаление Instagram аккаунта нарушителя через жалобы."},
    {"id": "neg_review",     "name": "Негативные отзывы на бизнес",    "price": 250,  "desc": "Оставим волну негативных отзывов на бизнес обидчика (2ГИС, Яндекс, Google)."},
    {"id": "block_number",   "name": "Блок номера в Telegram",          "price": 199,  "desc": "Добиваемся блокировки номера телефона в Telegram."},
    {"id": "find_person",    "name": "Вычислить местоположение",        "price": 599,  "desc": "Определение примерного местонахождения человека по его активности."},
    {"id": "spam_attack",    "name": "Спам-атака на аккаунт",           "price": 149,  "desc": "Мощная спам-атака сообщениями на аккаунт цели."},
]

def services_menu_kb():
    rows = []
    row = []
    for svc in SERVICES:
        btn = InlineKeyboardButton(text=svc["name"], callback_data=f"svc:{svc['id']}")
        row.append(btn)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

def service_buy_kb(svc_id: str, svc_name: str = ""):
    buy_text = urllib.parse.quote(
        f"Привет! Хочу заказать услугу \"{svc_name}\" в ShellSnos. Сколько стоит и как оплатить?"
    )
    buy_url = f"https://t.me/{CONTACT.lstrip('@')}?text={buy_text}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Купить у {CONTACT}", url=buy_url)],
        [InlineKeyboardButton(text="Назад к услугам", callback_data="svc:back")],
    ])

@router.message(F.text == "Услуги")
async def services_menu(message: Message):
    text = (
        f"<b>Отдел услуг</b>\n{LINE}\n"
        f"Надоел кто-то? Хочешь не просто снести — а сделать обидчику по-настоящему?\n\n"
        f"Всё анонимно. Гарантия возврата если не сработало.\n\n"
        f"<b>Выбери услугу:</b>"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=services_menu_kb())

@router.callback_query(F.data == "svc:back")
async def services_back(cb: CallbackQuery):
    text = (
        f"<b>Отдел услуг</b>\n{LINE}\n"
        f"Надоел кто-то? Хочешь не просто снести — а сделать обидчику по-настоящему?\n\n"
        f"Всё анонимно. Гарантия возврата если не сработало.\n\n"
        f"<b>Выбери услугу:</b>"
    )
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=services_menu_kb())
    except Exception:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=services_menu_kb())
    await cb.answer()

@router.callback_query(F.data.startswith("svc:"))
async def service_selected(cb: CallbackQuery):
    svc_id = cb.data.split(":", 1)[1]
    if svc_id == "back":
        return
    svc = next((s for s in SERVICES if s["id"] == svc_id), None)
    if not svc:
        return await cb.answer("Услуга не найдена.", show_alert=True)
    text = (
        f"<b>{svc['name']}</b>\n{LINE}\n"
        f"{svc['desc']}\n\n"
        f"<b>Стоимость:</b> {svc['price']} руб.\n\n"
        f"<b>Гарантия:</b> если не сработало — вернём деньги или повторим бесплатно.\n\n"
        f"Для заказа нажми кнопку ниже и напиши менеджеру название услуги."
    )
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=service_buy_kb(svc_id, svc['name']))
    except Exception:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=service_buy_kb(svc_id, svc['name']))
    await cb.answer()

# ============ ТЕХПОДДЕРЖКА ============
@router.message(F.text == "Техподдержка")
async def support_start(message: Message, state: FSMContext):
    await state.set_state(Support.waiting_text)
    photo = FSInputFile("tech12.png")
    text = (
        f"<b>Техподдержка</b>\n{LINE}\n"
        f"Напишите ваш вопрос или проблему. Администратор свяжется с вами.\n\n"
        f"<b>Внимание:</b> Если вы пишете спам, оскорбления или глупые вопросы, "
        f"вы будете заблокированы в поддержке <b>без возврата денег</b>.\n\n"
        f"Пишите только по делу."
    )
    await message.answer_photo(photo=photo, caption=text, parse_mode="HTML", reply_markup=cancel_kb())

@router.message(Support.waiting_text)
async def support_send(message: Message, state: FSMContext):
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    user = message.from_user; await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ответить", callback_data=f"support:reply:{user.id}"),
         InlineKeyboardButton(text="Забанить в поддержке", callback_data=f"support:ban:{user.id}")]
    ])
    try:
        await message.bot.send_message(
            ADMIN_ID,
            f"<b>Обращение в поддержку</b>\n{LINE}\n"
            f"От: @{user.username or user.id} (ID: <code>{user.id}</code>)\n\n"
            f"<b>Сообщение:</b>\n{message.text}",
            parse_mode="HTML",
            reply_markup=kb
        )
        await message.answer("Ваше сообщение отправлено. Ожидайте ответа.", reply_markup=main_kb())
    except Exception:
        await message.answer("Ошибка при отправке. Попробуйте позже.", reply_markup=main_kb())

@router.callback_query(F.data.startswith("support:reply:"))
async def support_reply_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return await cb.answer("Нет доступа", show_alert=True)
    target_id = int(cb.data.split(":")[2])
    await state.update_data(target_user_id=target_id)
    await state.set_state(AdminReply.waiting_text)
    await cb.message.answer(f"Введите ответ для пользователя <code>{target_id}</code>:", reply_markup=cancel_kb())
    await cb.answer()

@router.message(AdminReply.waiting_text)
async def support_reply_send(message: Message, state: FSMContext):
    if message.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    data = await state.get_data()
    target_id = data.get("target_user_id")
    await state.clear()
    if not target_id: return await message.answer("Ошибка: цель не найдена.", reply_markup=main_kb())
    try:
        await message.bot.send_message(target_id, f"<b>Ответ техподдержки</b>\n{LINE}\n{message.text}", parse_mode="HTML")
        await message.answer(f"Ответ отправлен пользователю {target_id}.", reply_markup=main_kb())
    except Exception:
        await message.answer("Не удалось отправить ответ. Пользователь заблокировал бота.", reply_markup=main_kb())

@router.callback_query(F.data.startswith("support:ban:"))
async def support_ban_user(cb: CallbackQuery):
    if cb.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return await cb.answer("Нет доступа", show_alert=True)
    target_id = int(cb.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET support_banned=1 WHERE user_id=?", (target_id,))
        await db.commit()
    try:
        await cb.bot.send_message(target_id, "Вы были заблокированы в техподдержке за нарушение правил.")
    except Exception: pass
    await cb.message.edit_text(f"Пользователь {target_id} заблокирован в поддержке.")
    await cb.answer("Заблокирован.")

# ============ БОНУСЫ ============
@router.message(F.text == "Бонусы")
async def bonus_menu(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ежедневный бонус", callback_data="bonus:daily")],
        [InlineKeyboardButton(text="Активировать промокод", callback_data="bonus:promo")]
    ])
    photo = FSInputFile("bonusi.png")
    await message.answer_photo(
        photo=photo,
        caption=f"<b>Бонусы и промокоды</b>\n{LINE}\nЕжедневный бонус — раз в 24 часа: +{DAILY_MIN_MINUTES} мин подписки, +{DAILY_MIN_STARS} звезды.\nШанс {int(DAILY_LUCK_CHANCE*100)}% на +{DAILY_LUCK_MINUTES} мин.\n\nПромокод — вводите код от админа и получайте бонусы.",
        parse_mode="HTML",
        reply_markup=kb
    )

@router.callback_query(F.data == "bonus:daily")
async def daily_bonus(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    if not u: return await cb.answer("Нажмите /start")
    last = parse_iso_utc(u["last_daily_at"]) if "last_daily_at" in u.keys() else None
    if last and (now() - last).total_seconds() < 24 * 3600:
        wait = 24 * 3600 - int((now() - last).total_seconds())
        h = wait // 3600; m = (wait % 3600) // 60
        return await cb.answer(f"Следующий бонус через {h}ч {m}мин.", show_alert=True)
    bonus_min = DAILY_MIN_MINUTES; bonus_stars = DAILY_MIN_STARS; luck = False
    if random.random() < DAILY_LUCK_CHANCE: bonus_min += DAILY_LUCK_MINUTES; luck = True
    await add_subscription_minutes(cb.from_user.id, bonus_min)
    await add_stars_balance(cb.from_user.id, bonus_stars)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_daily_at=? WHERE user_id=?", (now().isoformat(), cb.from_user.id))
        await db.commit()
    await log_action(cb.from_user.id, "daily_bonus", f"+{bonus_min}min +{bonus_stars}stars luck={luck}")
    text = f"<b>Ежедневный бонус</b>\n{LINE}\nПолучено: +{bonus_min} мин подписки\n+{bonus_stars} звезды\n"
    if luck: text += f"Удача! Бонус увеличен на +{DAILY_LUCK_MINUTES} мин.\n"
    text += f"\nСледующий бонус через 24 часа."
    await cb.message.answer(text, parse_mode="HTML")
    await cb.answer("Бонус получен!")

@router.callback_query(F.data == "bonus:promo")
async def bonus_promo(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ActivatePromo.waiting_code)
    await cb.message.answer("Введите промокод:", reply_markup=cancel_kb())
    await cb.answer()

@router.message(ActivatePromo.waiting_code)
async def activate_promo_msg(message: Message, state: FSMContext):
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    code = (message.text or "").strip().upper()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM promocodes WHERE code=?", (code,))
        p = await cur.fetchone()
        if not p: await state.clear(); return await message.answer("Промокод не найден.")
        expires = parse_iso_utc(p["expires_at"]) if p["expires_at"] else None
        if expires and expires <= now(): await state.clear(); return await message.answer("Промокод истёк.")
        if p["max_uses"] > 0 and p["used_count"] >= p["max_uses"]: await state.clear(); return await message.answer("Лимит активаций исчерпан.")
        cur = await db.execute("SELECT 1 FROM promo_activations WHERE code=? AND user_id=?", (code, message.from_user.id))
        if await cur.fetchone(): await state.clear(); return await message.answer("Вы уже активировали этот промокод.")
        await db.execute("INSERT INTO promo_activations(code, user_id, used_at) VALUES(?,?,?)", (code, message.from_user.id, now().isoformat()))
        await db.execute("UPDATE promocodes SET used_count = used_count + 1 WHERE code=?", (code,))
        await db.commit()
    if p["bonus_minutes"]: await add_subscription_minutes(message.from_user.id, p["bonus_minutes"])
    if p["bonus_stars"]: await add_stars_balance(message.from_user.id, p["bonus_stars"])
    if p["bonus_days_vip"]: await add_vip_days(message.from_user.id, p["bonus_days_vip"])
    await log_action(message.from_user.id, "promo_activated", code)
    await state.clear()
    lines = [f"<b>Промокод активирован</b>\n{LINE}", f"Код: <code>{code}</code>"]
    if p["bonus_minutes"]: lines.append(f"+{p['bonus_minutes']} минут подписки")
    if p["bonus_stars"]: lines.append(f"+{p['bonus_stars']} звезд")
    if p["bonus_days_vip"]: lines.append(f"+{p['bonus_days_vip']} дней VIP")
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_kb())

# ============ ПОДПИСКА ============
@router.message(F.text == "Подписка")
async def subscription(message: Message):
    prices = "\n".join(f"— {k} — {v} RUB" for k, v in PRICES.items())
    vip_prices = "\n".join(f"— {k} — {v} RUB" for k, v in VIP_PRICES.items())
    milestones_lines = "\n".join(
        f"— {k} {'друг' if k == 1 else 'друга' if k <= 4 else 'друзей'} → {_milestone_text(t, v)}"
        for k, (t, v) in sorted(REF_MILESTONES.items())
    )
    text = (
        f"<b>Подписка</b>\n{LINE}\n"
        f"<b>Стандарт</b> (заморозка, 2–7 недель):\n{prices}\n\n"
        f"<b>VIP</b> (полный снос, 30 мин – 7 часов):\n{vip_prices}\n\n"
        f"<b>Бесплатно — реферальная программа:</b>\n{milestones_lines}\n\n"
        f"Купить: {CONTACT}"
    )
    me = await message.bot.me()
    ref_link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    share_text = f"Лучший бот для сноса телеграм аккаунтов!\n\nЗаходи: @{me.username}"
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(ref_link)}&text={urllib.parse.quote(share_text)}"
    buy_text = "Привет! Хочу купить подписку в ShellSnos. Расскажи про варианты и оплату."
    buy_url = f"https://t.me/shellwo?text={urllib.parse.quote(buy_text)}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Поделиться реф-ссылкой", url=share_url)],
        [InlineKeyboardButton(text="Купить подписку", url=buy_url)],
    ])
    photo = FSInputFile("podpiska.png")
    await message.answer_photo(photo=photo, caption=text, parse_mode="HTML", reply_markup=kb)

# ============ АДМИНКА ============
@router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext):
    if message.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return await message.answer("Нет доступа.")
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="Выдать подписку", callback_data="admin:givesub"),
         InlineKeyboardButton(text="Выдать VIP", callback_data="admin:givevip")],
        [InlineKeyboardButton(text="Начислить звезды", callback_data="admin:givestars")],
        [InlineKeyboardButton(text="Создать промокод", callback_data="admin:createpromo"),
         InlineKeyboardButton(text="Промокоды", callback_data="admin:listpromos")],
        [InlineKeyboardButton(text="Активации промокода", callback_data="admin:checkpromo")],
        [InlineKeyboardButton(text="Логи", callback_data="admin:logs")],
        [InlineKeyboardButton(text="Топ рефералов", callback_data="admin:toprefs")],
        [InlineKeyboardButton(text="Забанить/Разбанить", callback_data="admin:ban")],
        [InlineKeyboardButton(text="Экспорт CSV", callback_data="admin:export")],
        [InlineKeyboardButton(text="Рассылка", callback_data="admin:broadcast")],
    ])
    await message.answer(f"<b>Админ-панель</b>\n{LINE}\nВыберите действие:", parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "admin:stats")
async def admin_stats(cb: CallbackQuery):
    if cb.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return await cb.answer("Нет доступа", show_alert=True)
    users, refs, subs, snoops, vips, promos, active_today, left = await stats()
    await cb.message.answer(
        f"<b>Статистика</b>\n{LINE}\n"
        f"Всего пользователей: {users}\n"
        f"Активных сегодня: {active_today}\n"
        f"Покинули бота: {left}\n"
        f"Рефералов: {refs}\n"
        f"Активных подписок: {subs}\n"
        f"VIP: {vips}\n"
        f"Всего сносов: {snoops}\n"
        f"Промокодов: {promos}",
        parse_mode="HTML"
    )
    await cb.answer()

@router.callback_query(F.data == "admin:givesub")
async def admin_givesub_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return await cb.answer("Нет доступа", show_alert=True)
    await state.set_state(GiveSub.waiting_target)
    await cb.message.answer("ID или @username:", reply_markup=cancel_kb())
    await cb.answer()

@router.message(GiveSub.waiting_target)
async def admin_givesub_target(message: Message, state: FSMContext):
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    tt = (message.text or "").strip()
    user = await get_user_by_username(tt) if tt.startswith("@") else await get_user(int(tt)) if tt.isdigit() else None
    if not user: return await message.answer("Не найден. Попробуй ещё или «Отмена».")
    await state.update_data(target_id=user['user_id'], target_name=user['first_name'])
    await state.set_state(GiveSub.waiting_amount)
    await message.answer(f"Найден: {user['first_name']} (@{user['username'] or user['user_id']})\nВведите кол-во минут:")

@router.message(GiveSub.waiting_amount)
async def admin_givesub_amount(message: Message, state: FSMContext):
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    try:
        minutes = int((message.text or "").strip())
        if minutes <= 0: raise ValueError
    except ValueError: return await message.answer("Целое число > 0.")
    data = await state.get_data(); tid = data['target_id']; tname = data['target_name']
    until = await add_subscription_minutes(tid, minutes); await state.clear()
    await log_action(message.from_user.id, "admin_givesub", f"{tid} {minutes}min")
    await message.answer(f"Подписка выдана.\n{tname} ({tid}) — {minutes} мин.\nДо: {format_msk(until.isoformat()) if until else '—'}", reply_markup=main_kb())
    try:
        await message.bot.send_message(tid, f"✅ Вам выдана подписка.\n{LINE}\nСрок: {minutes} мин.\nДо: {format_msk(until.isoformat()) if until else '—'}", parse_mode="HTML")
    except Exception: pass

@router.callback_query(F.data == "admin:givevip")
async def admin_givevip_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return await cb.answer("Нет доступа", show_alert=True)
    await state.set_state(GiveVip.waiting_target)
    await cb.message.answer("ID или @username:", reply_markup=cancel_kb())
    await cb.answer()

@router.message(GiveVip.waiting_target)
async def admin_givevip_target(message: Message, state: FSMContext):
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    tt = (message.text or "").strip()
    user = await get_user_by_username(tt) if tt.startswith("@") else await get_user(int(tt)) if tt.isdigit() else None
    if not user: return await message.answer("Не найден.")
    await state.update_data(target_id=user['user_id'], target_name=user['first_name'])
    await state.set_state(GiveVip.waiting_amount)
    await message.answer(f"Найден: {user['first_name']}. Введите кол-во дней VIP:")

@router.message(GiveVip.waiting_amount)
async def admin_givevip_amount(message: Message, state: FSMContext):
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    try:
        days = int((message.text or "").strip())
        if days <= 0: raise ValueError
    except ValueError: return await message.answer("Целое число > 0.")
    data = await state.get_data(); tid = data['target_id']; tname = data['target_name']
    until = await add_vip_days(tid, days); await state.clear()
    await log_action(message.from_user.id, "admin_givevip", f"{tid} {days}d")
    await message.answer(f"VIP выдан.\n{tname} ({tid}) — {days} дн.\nДо: {format_msk(until.isoformat()) if until else '—'}", reply_markup=main_kb())
    try:
        await message.bot.send_message(tid, f"👑 <b>ВЫ СТАЛИ VIP!</b>\n{LINE}\nСрок: {days} дн.\nАктивен до: {format_msk(until.isoformat()) if until else '—'}", parse_mode="HTML")
    except Exception: pass

@router.callback_query(F.data == "admin:givestars")
async def admin_givestars_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return await cb.answer("Нет доступа", show_alert=True)
    await state.set_state(GiveStars.waiting_target)
    await cb.message.answer("ID или @username:", reply_markup=cancel_kb())
    await cb.answer()

@router.message(GiveStars.waiting_target)
async def admin_givestars_target(message: Message, state: FSMContext):
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    tt = (message.text or "").strip()
    user = await get_user_by_username(tt) if tt.startswith("@") else await get_user(int(tt)) if tt.isdigit() else None
    if not user: return await message.answer("Не найден.")
    await state.update_data(target_id=user['user_id'], target_name=user['first_name'])
    await state.set_state(GiveStars.waiting_amount)
    await message.answer(f"Найден: {user['first_name']}. Введите кол-во звезд (+/-):")

@router.message(GiveStars.waiting_amount)
async def admin_givestars_amount(message: Message, state: FSMContext):
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    try: amount = float((message.text or "").strip())
    except ValueError: return await message.answer("Введите число.")
    data = await state.get_data(); tid = data['target_id']; tname = data['target_name']
    await add_stars_balance(tid, amount)
    await log_action(message.from_user.id, "admin_givestars", f"{tid} {amount}")
    await state.clear()
    await message.answer(f"Готово.\n{tname} ({tid}) — {amount:+.2f} звезд.", reply_markup=main_kb())
    try: await message.bot.send_message(tid, f"Вам начислено {amount:+.2f} звезд.")
    except Exception: pass

@router.callback_query(F.data == "admin:createpromo")
async def admin_createpromo_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return await cb.answer("Нет доступа", show_alert=True)
    await state.set_state(CreatePromo.waiting_code)
    await cb.message.answer("Введите код промокода (например SHELL50):", reply_markup=cancel_kb())
    await cb.answer()

@router.message(CreatePromo.waiting_code)
async def admin_createpromo_code(message: Message, state: FSMContext):
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    code = (message.text or "").strip().upper()
    if not code or len(code) > 32: return await message.answer("Код 1-32 символа.")
    await state.update_data(code=code)
    await state.set_state(CreatePromo.waiting_minutes)
    await message.answer("Бонус минут подписки (0 если не нужен):")

@router.message(CreatePromo.waiting_minutes)
async def admin_createpromo_minutes(message: Message, state: FSMContext):
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    try:
        m = int((message.text or "").strip())
        if m < 0: raise ValueError
    except ValueError: return await message.answer("Число >= 0.")
    await state.update_data(minutes=m)
    await state.set_state(CreatePromo.waiting_stars)
    await message.answer("Бонус звезд (0 если не нужен):")

@router.message(CreatePromo.waiting_stars)
async def admin_createpromo_stars(message: Message, state: FSMContext):
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    try:
        s = float((message.text or "").strip())
        if s < 0: raise ValueError
    except ValueError: return await message.answer("Число >= 0.")
    await state.update_data(stars=s)
    await state.set_state(CreatePromo.waiting_vip)
    await message.answer("Бонус дней VIP (0 если не нужен):")

@router.message(CreatePromo.waiting_vip)
async def admin_createpromo_vip(message: Message, state: FSMContext):
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    try:
        v = int((message.text or "").strip())
        if v < 0: raise ValueError
    except ValueError: return await message.answer("Число >= 0.")
    await state.update_data(vip=v)
    await state.set_state(CreatePromo.waiting_uses)
    await message.answer("Лимит активаций (0 = без лимита):")

@router.message(CreatePromo.waiting_uses)
async def admin_createpromo_uses(message: Message, state: FSMContext):
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    try:
        u = int((message.text or "").strip())
        if u < 0: raise ValueError
    except ValueError: return await message.answer("Число >= 0.")
    await state.update_data(uses=u)
    await state.set_state(CreatePromo.waiting_expires)
    await message.answer("Срок: YYYY-MM-DD (или 0 = бессрочно):")

@router.message(CreatePromo.waiting_expires)
async def admin_createpromo_expires(message: Message, state: FSMContext):
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    expires_at = None; txt = (message.text or "").strip()
    if txt != "0":
        try:
            dt = datetime.strptime(txt, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
            expires_at = dt.isoformat()
        except ValueError: return await message.answer("Формат YYYY-MM-DD или 0.")
    data = await state.get_data()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO promocodes(code, bonus_minutes, bonus_stars, bonus_days_vip, max_uses, used_count, expires_at, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (data["code"], data["minutes"], data["stars"], data["vip"], data["uses"], 0, expires_at, now().isoformat())
            )
            await db.commit()
        await log_action(message.from_user.id, "admin_createpromo", data["code"])
        await message.answer(
            f"Промокод <code>{data['code']}</code> создан.\n"
            f"Бонусы: +{data['minutes']} мин, +{data['stars']} звёзд, +{data['vip']} дн VIP\n"
            f"Лимит: {data['uses'] or 'без лимита'}\n"
            f"Срок: {format_msk(expires_at) if expires_at else 'бессрочно'}",
            parse_mode="HTML", reply_markup=main_kb()
        )
    except Exception as e:
        await message.answer(f"Ошибка: {e}", reply_markup=main_kb())
    await state.clear()

@router.callback_query(F.data == "admin:listpromos")
async def admin_listpromos(cb: CallbackQuery):
    if cb.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return await cb.answer("Нет доступа", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM promocodes ORDER BY created_at DESC")
        promos = await cur.fetchall()
    if not promos: await cb.message.answer("Промокодов нет."); return await cb.answer()
    lines = ["<b>Промокоды</b>\n" + LINE]
    for p in promos[:30]:
        exp = format_msk(p["expires_at"]) if p["expires_at"] else "бессрочно"
        lines.append(f"<code>{p['code']}</code> | {p['used_count']}/{p['max_uses'] or '∞'} | до {exp}\n  +{p['bonus_minutes']}мин +{p['bonus_stars']}зв +{p['bonus_days_vip']}VIP")
    text = "\n".join(lines)
    if len(text) > 4000: text = text[:4000] + "\n..."
    await cb.message.answer(text, parse_mode="HTML")
    await cb.answer()

@router.callback_query(F.data == "admin:checkpromo")
async def admin_checkpromo_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return await cb.answer("Нет доступа", show_alert=True)
    await state.set_state(CheckPromo.waiting_code)
    await cb.message.answer("Введите код для просмотра активаций:", reply_markup=cancel_kb())
    await cb.answer()

@router.message(CheckPromo.waiting_code)
async def admin_checkpromo_code(message: Message, state: FSMContext):
    if message.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    code = (message.text or "").strip().upper()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT pa.user_id, pa.used_at, u.username, u.first_name FROM promo_activations pa LEFT JOIN users u ON u.user_id = pa.user_id WHERE pa.code=? ORDER BY pa.id DESC",
            (code,)
        )
        acts = await cur.fetchall()
    await state.clear()
    if not acts: return await message.answer(f"Активаций по <code>{code}</code> нет.", parse_mode="HTML", reply_markup=main_kb())
    lines = [f"<b>Активации <code>{code}</code></b>\n{LINE}"]
    for a in acts[:50]:
        name = f"@{a['username']}" if a['username'] else a['first_name'] or a['user_id']
        lines.append(f"— {name} ({a['user_id']}) — {format_msk(a['used_at'])}")
    text = "\n".join(lines)
    if len(text) > 4000: text = text[:4000] + "\n..."
    await message.answer(text, parse_mode="HTML", reply_markup=main_kb())

@router.callback_query(F.data == "admin:logs")
async def admin_logs(cb: CallbackQuery):
    if cb.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return await cb.answer("Нет доступа", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT l.user_id, l.action, l.details, l.created_at, u.username FROM action_logs l LEFT JOIN users u ON u.user_id = l.user_id ORDER BY l.id DESC LIMIT 50"
        )
        logs = await cur.fetchall()
    if not logs: await cb.message.answer("Логов нет."); return await cb.answer()
    lines = ["<b>Последние 50 действий</b>\n" + LINE]
    for l in logs:
        uname = f"@{l['username']}" if l['username'] else l['user_id']
        lines.append(f"— {l['created_at'][:16]} | {uname} | {l['action']} | {l['details'] or ''}")
    text = "\n".join(lines)
    if len(text) > 4000: text = text[:4000] + "\n..."
    await cb.message.answer(text, parse_mode="HTML")
    await cb.answer()

@router.callback_query(F.data == "admin:toprefs")
async def admin_toprefs(cb: CallbackQuery):
    if cb.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return await cb.answer("Нет доступа", show_alert=True)
    top = await get_top_referrals(20)
    if not top: await cb.message.answer("Пока пусто."); return await cb.answer()
    lines = ["<b>Топ-20 рефералов</b>\n" + LINE]
    for i, r in enumerate(top, 1):
        uname = f"@{r['username']}" if r['username'] else r['first_name'] or r['user_id']
        lines.append(f"{i}. {uname} ({r['user_id']}) — <b>{r['cnt']}</b>")
    await cb.message.answer("\n".join(lines), parse_mode="HTML")
    await cb.answer()

@router.callback_query(F.data == "admin:ban")
async def admin_ban_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return await cb.answer("Нет доступа", show_alert=True)
    await state.set_state(BanUser.waiting_target)
    await cb.message.answer("ID или @username для бана/разбана:", reply_markup=cancel_kb())
    await cb.answer()

@router.message(BanUser.waiting_target)
async def admin_ban_target(message: Message, state: FSMContext):
    if message.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    tt = (message.text or "").strip()
    user = await get_user_by_username(tt) if tt.startswith("@") else await get_user(int(tt)) if tt.isdigit() else None
    if not user: return await message.answer("Не найден.")
    currently = bool(user["is_banned"]) if "is_banned" in user.keys() else False
    await set_ban(user['user_id'], not currently)
    await log_action(message.from_user.id, "admin_ban_toggle", f"{user['user_id']} -> {not currently}")
    await state.clear()
    await message.answer(f"{'Забанен' if not currently else 'Разбанен'}: {user['first_name']} ({user['user_id']})", reply_markup=main_kb())

@router.callback_query(F.data == "admin:export")
async def admin_export(cb: CallbackQuery):
    if cb.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return await cb.answer("Нет доступа", show_alert=True)
    try:
        data = await export_users_csv()
        f = BufferedInputFile(data, filename=f"users_{now().strftime('%Y%m%d_%H%M')}.csv")
        await cb.message.answer_document(f)
        await log_action(cb.from_user.id, "admin_export", "")
    except Exception as e:
        await cb.message.answer(f"Ошибка: {e}")
    await cb.answer()

@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return await cb.answer("Нет доступа", show_alert=True)
    await state.set_state(Broadcast.waiting_text)
    await cb.message.answer("Текст рассылки (или «Отмена»):", reply_markup=cancel_kb())
    await cb.answer()

@router.message(Broadcast.waiting_text)
async def admin_broadcast_send(message: Message, state: FSMContext):
    if message.from_user.id not in (ADMIN_ID, ADMIN_ID_2): return
    if is_cancel_text(message.text or ""): await state.clear(); return await message.answer("Отменено.", reply_markup=main_kb())
    text = message.text or ""
    await state.clear()
    ids = await all_user_ids()
    ok = fail = 0
    await message.answer(f"Рассылка на {len(ids)}...")
    for uid in ids:
        try: await message.bot.send_message(uid, text); ok += 1
        except Exception: fail += 1
        await asyncio.sleep(0.05)
    await log_action(message.from_user.id, "admin_broadcast", f"ok={ok} fail={fail}")
    await message.answer(f"Готово.\nOK: {ok}\nFail: {fail}", reply_markup=main_kb())

# ============ MAIN ============
async def main():
    await init_db()
    session = AiohttpSession()
    bot = Bot(BOT_TOKEN, session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(background_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
    except Exception as e:
        logging.error(f"Bot crashed: {e}")
