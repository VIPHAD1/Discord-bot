"""
MoonV4.py
Moon's Reflection: multi-server habit tracking and productivity bot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import re
import shlex
import shutil
import signal
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import aiosqlite
import discord
import pytz
from discord.ext import commands
from dotenv import load_dotenv


# -----------------------------------------------------------------------------
# Environment and constants
# -----------------------------------------------------------------------------
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
BOT_PREFIX = os.getenv("BOT_PREFIX", "!")
DEFAULT_DB_PATH = "moons_reflection.db"
LEGACY_DB_PATH = "grokbot43.db"
DB_PATH = os.getenv("DB_PATH", "").strip()
if not DB_PATH:
    if Path(LEGACY_DB_PATH).exists() and not Path(DEFAULT_DB_PATH).exists():
        DB_PATH = LEGACY_DB_PATH
    else:
        DB_PATH = DEFAULT_DB_PATH
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Asia/Karachi")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

SCHEDULER_INTERVAL = 20
POLL_DURATION_HOURS = 24
LEADERBOARD_REFRESH_SECONDS = 300
STREAK_REFRESH_SECONDS = 300
BACKUP_INTERVAL_SECONDS = 6 * 3600
MAX_HABITS_PER_CATEGORY = 25

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


MIN_PRAYERS_FOR_STREAK = max(0, _env_int("MIN_PRAYERS_FOR_STREAK", 0))
MIN_HABITS_FOR_STREAK = max(0, _env_int("MIN_HABITS_FOR_STREAK", 1))

LOG_DIR = Path("logs")
BACKUP_DIR = Path("backups")
LOG_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

EVENTS_LOG = LOG_DIR / "events.log"
ERRORS_LOG = LOG_DIR / "errors.log"

LEADERBOARD_TITLE = "All-Time Leaderboard"
STREAK_TITLE = "All-Time Streak Leaderboard"
WEEKLY_TITLE = "Weekly Streak Summary"
SUMMARY_TITLE = "Daily Summary"

LEADERBOARD_FOOTER_TAG = "LEADERBOARD_MOON_V4"
STREAK_FOOTER_TAG = "STREAK_MOON_V4"
WEEKLY_FOOTER_TAG = "WEEKLY_MOON_V4"
SUMMARY_FOOTER_TAG = "SUMMARY_MOON_V4"

POLL_TYPE_ORDER = ("prayers", "general", "islamic")
POLL_TYPE_TO_SLOT = {"prayers": "poll1", "general": "poll2", "islamic": "poll3"}
SLOT_TO_POLL_TYPE = {"poll1": "prayers", "poll2": "general", "poll3": "islamic"}
POLL_ALIASES = {
    "poll1": "prayers",
    "poll2": "general",
    "poll3": "islamic",
    "prayer": "prayers",
    "prayers": "prayers",
    "salah": "prayers",
    "salat": "prayers",
    "daily": "general",
    "habit": "general",
    "habits": "general",
    "general": "general",
    "islamic": "islamic",
    "deen": "islamic",
    "iman": "islamic",
}
DEFAULT_POLL_LABELS = {
    "prayers": "Prayer Poll",
    "general": "Daily Habits Poll",
    "islamic": "Islamic Poll",
}
POLL_COLORS = {"prayers": 0x1ABC9C, "general": 0xE67E22, "islamic": 0x2ECC71}
POLL_ICONS = {"prayers": "P1", "general": "P2", "islamic": "P3"}
STREAK_CATCHUP_MAX_DAYS = 400
PRAYER_KEY_ALIASES = {
    "zuhr": "dhuhr",
}


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
def build_logger() -> logging.Logger:
    logger = logging.getLogger("moons_reflection")
    logger.handlers.clear()
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    console = logging.StreamHandler()
    console.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    console.setFormatter(fmt)
    logger.addHandler(console)

    events = logging.handlers.RotatingFileHandler(
        EVENTS_LOG, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    events.setLevel(logging.INFO)
    events.setFormatter(fmt)
    logger.addHandler(events)

    errors = logging.handlers.RotatingFileHandler(
        ERRORS_LOG, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    errors.setLevel(logging.ERROR)
    errors.setFormatter(fmt)
    logger.addHandler(errors)
    return logger


LOGGER = build_logger()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(tz=pytz.UTC).isoformat()


def utc_ts() -> int:
    return int(time.time())


def tz_now(tz_name: str) -> datetime:
    return datetime.now(pytz.timezone(tz_name))


def date_str_local(tz_name: str) -> str:
    return tz_now(tz_name).strftime("%Y-%m-%d")


def date_prev(date_str: str) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def date_next(date_str: str) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def parse_timezone(value: str) -> Optional[str]:
    try:
        pytz.timezone(value)
        return value
    except Exception:
        return None


def safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def level_from_total_xp(total_xp: int) -> int:
    if total_xp <= 0:
        return 0
    return min(100, int((total_xp / 150.0) ** 0.56))


def slugify(text: str) -> str:
    raw = text.strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "_", raw)
    raw = re.sub(r"_+", "_", raw).strip("_")
    return raw or "habit"


def rank_with_ties(items: List[Tuple[int, int]]) -> List[Tuple[int, int, int]]:
    out: List[Tuple[int, int, int]] = []
    last_score: Optional[int] = None
    rank = 0
    idx = 0
    for uid, score in items:
        idx += 1
        if score != last_score:
            rank = idx
            last_score = score
        out.append((rank, uid, score))
    return out


def rank_badge(rank: int) -> str:
    return f"{rank}."


def normalize_poll_type(value: str) -> Optional[str]:
    return POLL_ALIASES.get(value.strip().lower())


def poll_slot_for_type(poll_type: str) -> str:
    return POLL_TYPE_TO_SLOT.get(poll_type, "poll2")


def poll_field_for_type(poll_type: str) -> str:
    return f"{poll_slot_for_type(poll_type)}_name"


def resolve_poll_label(cfg: Optional["GuildConfig"], poll_type: str) -> str:
    if cfg:
        value = {
            "prayers": cfg.poll1_name,
            "general": cfg.poll2_name,
            "islamic": cfg.poll3_name,
        }.get(poll_type)
        if value and value.strip():
            return value.strip()
    return DEFAULT_POLL_LABELS.get(poll_type, "Poll")


def streak_thresholds(cfg: Optional["GuildConfig"]) -> Tuple[int, int]:
    prayers = MIN_PRAYERS_FOR_STREAK
    habits = MIN_HABITS_FOR_STREAK
    if cfg:
        if cfg.streak_min_prayers is not None:
            prayers = max(0, cfg.streak_min_prayers)
        if cfg.streak_min_habits is not None:
            habits = max(0, cfg.streak_min_habits)
    return prayers, habits


def qualifies_for_streak(prayers_count: int, total_habits: int, cfg: Optional["GuildConfig"]) -> bool:
    if prayers_count <= 0 and total_habits <= 0:
        return False
    min_prayers, min_habits = streak_thresholds(cfg)
    return prayers_count >= min_prayers and total_habits >= min_habits


def parse_iso_date(raw: str) -> Optional[str]:
    txt = raw.strip()
    try:
        return datetime.strptime(txt, "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return None


def progress_bar(done: int, total: int, width: int = 14) -> str:
    if total <= 0:
        return "[--------------] 0%"
    ratio = max(0.0, min(1.0, done / total))
    filled = int(round(ratio * width))
    return f"[{'#' * filled}{'-' * (width - filled)}] {int(ratio * 100)}%"


def parse_addhabit(raw: str) -> Tuple[str, int, str]:
    text = raw.strip()
    if not text:
        raise ValueError("Habit name required.")
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    if not parts:
        raise ValueError("Habit name required.")

    category: Optional[str] = None
    xp = 10
    changed = True
    while changed and parts:
        changed = False
        maybe_category = normalize_poll_type(parts[-1])
        if maybe_category and category is None:
            category = maybe_category
            parts = parts[:-1]
            changed = True
            continue
        if re.fullmatch(r"\d{1,3}", parts[-1]):
            xp = int(parts[-1])
            parts = parts[:-1]
            changed = True
            continue
    name = " ".join(parts).strip()
    if not name:
        raise ValueError("Habit name required.")
    category = category or "general"
    if xp < 1 or xp > 200:
        raise ValueError("XP must be in range 1..200.")
    return name, xp, category


def parse_addhabits(raw: str) -> Tuple[str, List[str], int]:
    text = raw.strip()
    if not text:
        raise ValueError("Usage: addhabits <poll> <habit1> <habit2> ... <xp>")
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    if len(parts) < 3:
        raise ValueError("Usage: addhabits <poll> <habit1> <habit2> ... <xp>")
    poll_type = normalize_poll_type(parts[0])
    if not poll_type:
        raise ValueError("Poll must be poll1, poll2, poll3, prayers, general, or islamic.")
    if not re.fullmatch(r"\d{1,3}", parts[-1]):
        raise ValueError("XP must be a number at the end of the command.")
    xp = int(parts[-1])
    if xp < 1 or xp > 200:
        raise ValueError("XP must be in range 1..200.")
    names = [p.strip() for p in parts[1:-1] if p.strip()]
    if not names:
        raise ValueError("At least one habit name is required.")
    return poll_type, names, xp


# -----------------------------------------------------------------------------
# Default content
# -----------------------------------------------------------------------------
DEFAULT_PRAYERS = [
    {"key": "fajr", "label": "Fajr", "xp": 15},
    {"key": "dhuhr", "label": "Dhuhr", "xp": 15},
    {"key": "asr", "label": "Asr", "xp": 15},
    {"key": "maghrib", "label": "Maghrib", "xp": 15},
    {"key": "isha", "label": "Isha", "xp": 15},
    {"key": "jummah", "label": "Jummah", "xp": 50},
]

DEFAULT_GENERAL_HABITS = [
    ("combat_training", "Combat Training", 30),
    ("work", "Work or Job Tasks", 30),
    ("personal_projects", "Personal Projects", 25),
    ("academics", "Academics and Studying", 25),
    ("human_nature", "Learning Human Nature", 25),
    ("workout", "Workout or Gym", 20),
    ("sports", "Sport", 20),
    ("programming", "Programming", 20),
    ("content_creation", "Content Creation", 20),
    ("one_hard_thing", "One Difficult Task", 30),
    ("creative_writing", "Creative Writing or Mind Map", 20),
    ("deep_work", "Deep Work Session", 30),
    ("editing", "Editing", 15),
    ("chess", "Chess", 15),
    ("healthy_diet", "Healthy Diet", 15),
    ("quality_sleep", "Quality Sleep", 15),
    ("journaling", "Journaling", 10),
    ("reading", "Reading", 10),
]

DEFAULT_ISLAMIC_HABITS = [
    ("quran_sunnah", "Quran and Sunnah", 30),
    ("dhikr", "Dhikr or Tasbeeh", 10),
    ("daily_reflection", "Daily Reflection", 25),
    ("parents", "Respect for Parents", 15),
    ("truthfulness", "Truthfulness", 15),
    ("modesty", "Modesty", 10),
    ("patience", "Patience and Forgiveness", 10),
    ("speaking_good", "Speak Good or Stay Silent", 10),
    ("gratitude", "Gratitude", 10),
]


# -----------------------------------------------------------------------------
# Data models
# -----------------------------------------------------------------------------
@dataclass
class GuildConfig:
    guild_id: int
    poll_channel_id: Optional[int] = None
    leaderboard_channel_id: Optional[int] = None
    streak_channel_id: Optional[int] = None
    timezone: str = DEFAULT_TIMEZONE
    admin_role_id: Optional[int] = None
    last_poll_date: Optional[str] = None
    last_processed_date: Optional[str] = None
    leaderboard_message_id: Optional[int] = None
    streak_message_id: Optional[int] = None
    weekly_message_id: Optional[int] = None
    last_weekly_week_id: Optional[str] = None
    poll1_name: Optional[str] = None
    poll2_name: Optional[str] = None
    poll3_name: Optional[str] = None
    streak_min_prayers: Optional[int] = MIN_PRAYERS_FOR_STREAK
    streak_min_habits: Optional[int] = MIN_HABITS_FOR_STREAK

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "GuildConfig":
        return cls(
            guild_id=safe_int(row["guild_id"]),
            poll_channel_id=safe_int(row["poll_channel_id"]) or None,
            leaderboard_channel_id=safe_int(row["leaderboard_channel_id"]) or None,
            streak_channel_id=safe_int(row["streak_channel_id"]) or None,
            timezone=row["timezone"] or DEFAULT_TIMEZONE,
            admin_role_id=safe_int(row["admin_role_id"]) or None,
            last_poll_date=row["last_poll_date"],
            last_processed_date=row["last_processed_date"],
            leaderboard_message_id=safe_int(row["leaderboard_message_id"]) or None,
            streak_message_id=safe_int(row["streak_message_id"]) or None,
            weekly_message_id=safe_int(row["weekly_message_id"]) or None,
            last_weekly_week_id=row["last_weekly_week_id"],
            poll1_name=row["poll1_name"] if "poll1_name" in row.keys() else None,
            poll2_name=row["poll2_name"] if "poll2_name" in row.keys() else None,
            poll3_name=row["poll3_name"] if "poll3_name" in row.keys() else None,
            streak_min_prayers=safe_int(row["streak_min_prayers"], MIN_PRAYERS_FOR_STREAK)
            if "streak_min_prayers" in row.keys()
            else MIN_PRAYERS_FOR_STREAK,
            streak_min_habits=safe_int(row["streak_min_habits"], MIN_HABITS_FOR_STREAK)
            if "streak_min_habits" in row.keys()
            else MIN_HABITS_FOR_STREAK,
        )


@dataclass
class Habit:
    guild_id: int
    key: str
    name: str
    category: str
    xp: int
    active: int

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Habit":
        return cls(
            guild_id=safe_int(row["guild_id"]),
            key=row["habit_key"],
            name=row["habit_name"],
            category=row["category"],
            xp=safe_int(row["xp"]),
            active=safe_int(row["active"]),
        )


@dataclass
class PollGroup:
    group_id: str
    guild_id: int
    poll_date: str
    timezone: str
    channel_id: int
    prayers_message_id: Optional[int]
    general_message_id: Optional[int]
    islamic_message_id: Optional[int]
    created_ts: int
    expires_ts: int
    processed: int
    send_error: int
    prayers_snapshot_json: str
    general_snapshot_json: str
    islamic_snapshot_json: str

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "PollGroup":
        return cls(
            group_id=row["group_id"],
            guild_id=safe_int(row["guild_id"]),
            poll_date=row["poll_date"],
            timezone=row["timezone"],
            channel_id=safe_int(row["channel_id"]),
            prayers_message_id=safe_int(row["prayers_message_id"]) or None,
            general_message_id=safe_int(row["general_message_id"]) or None,
            islamic_message_id=safe_int(row["islamic_message_id"]) or None,
            created_ts=safe_int(row["created_ts"]),
            expires_ts=safe_int(row["expires_ts"]),
            processed=safe_int(row["processed"]),
            send_error=safe_int(row["send_error"]),
            prayers_snapshot_json=row["prayers_snapshot_json"] or "[]",
            general_snapshot_json=row["general_snapshot_json"] or "[]",
            islamic_snapshot_json=row["islamic_snapshot_json"] or "[]",
        )

    @property
    def prayers_snapshot(self) -> List[Dict[str, Any]]:
        return json.loads(self.prayers_snapshot_json)

    @property
    def general_snapshot(self) -> List[Dict[str, Any]]:
        return json.loads(self.general_snapshot_json)

    @property
    def islamic_snapshot(self) -> List[Dict[str, Any]]:
        return json.loads(self.islamic_snapshot_json)


# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------
class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None
        self.lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.conn is not None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path, timeout=60)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL;")
        await self.conn.execute("PRAGMA synchronous=NORMAL;")
        await self.conn.execute("PRAGMA foreign_keys=ON;")
        await self._create_schema()
        await self.conn.commit()
        LOGGER.info("Database connected: %s", self.path)

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None
            LOGGER.info("Database closed")

    async def _create_schema(self) -> None:
        assert self.conn is not None
        script = """
        CREATE TABLE IF NOT EXISTS guild_config(
            guild_id INTEGER PRIMARY KEY,
            poll_channel_id INTEGER,
            leaderboard_channel_id INTEGER,
            streak_channel_id INTEGER,
            timezone TEXT NOT NULL DEFAULT 'Asia/Karachi',
            admin_role_id INTEGER,
            last_poll_date TEXT,
            last_processed_date TEXT,
            leaderboard_message_id INTEGER,
            streak_message_id INTEGER,
            weekly_message_id INTEGER,
            last_weekly_week_id TEXT,
            poll1_name TEXT,
            poll2_name TEXT,
            poll3_name TEXT,
            streak_min_prayers INTEGER NOT NULL DEFAULT 0,
            streak_min_habits INTEGER NOT NULL DEFAULT 1,
            created_ts TEXT NOT NULL,
            updated_ts TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS habits(
            guild_id INTEGER NOT NULL,
            habit_key TEXT NOT NULL,
            habit_name TEXT NOT NULL,
            category TEXT NOT NULL,
            xp INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_by INTEGER NOT NULL DEFAULT 0,
            created_ts TEXT NOT NULL,
            PRIMARY KEY(guild_id, habit_key)
        );
        CREATE INDEX IF NOT EXISTS idx_habits ON habits(guild_id, category, active);

        CREATE TABLE IF NOT EXISTS poll_groups(
            group_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            poll_date TEXT NOT NULL,
            timezone TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            prayers_message_id INTEGER,
            general_message_id INTEGER,
            islamic_message_id INTEGER,
            created_ts INTEGER NOT NULL,
            expires_ts INTEGER NOT NULL,
            processed INTEGER NOT NULL DEFAULT 0,
            send_error INTEGER NOT NULL DEFAULT 0,
            prayers_snapshot_json TEXT NOT NULL,
            general_snapshot_json TEXT NOT NULL,
            islamic_snapshot_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_polls ON poll_groups(guild_id, poll_date, processed);

        CREATE TABLE IF NOT EXISTS poll_answers(
            group_id TEXT NOT NULL,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            poll_type TEXT NOT NULL,
            selections_json TEXT NOT NULL,
            updated_ts TEXT NOT NULL,
            PRIMARY KEY(group_id, user_id, poll_type)
        );

        CREATE TABLE IF NOT EXISTS daily_entries(
            guild_id INTEGER NOT NULL,
            entry_date TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            prayers_count INTEGER NOT NULL DEFAULT 0,
            general_count INTEGER NOT NULL DEFAULT 0,
            islamic_count INTEGER NOT NULL DEFAULT 0,
            total_habits INTEGER NOT NULL DEFAULT 0,
            xp_earned INTEGER NOT NULL DEFAULT 0,
            source_group_id TEXT NOT NULL,
            processed INTEGER NOT NULL DEFAULT 0,
            updated_ts TEXT NOT NULL,
            PRIMARY KEY(guild_id, entry_date, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_daily ON daily_entries(guild_id, entry_date, processed);

        CREATE TABLE IF NOT EXISTS xp_totals(
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            total_xp INTEGER NOT NULL DEFAULT 0,
            updated_ts TEXT NOT NULL,
            PRIMARY KEY(guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS user_streaks(
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            current_streak INTEGER NOT NULL DEFAULT 0,
            longest_streak INTEGER NOT NULL DEFAULT 0,
            last_qualified_date TEXT,
            updated_ts TEXT NOT NULL,
            PRIMARY KEY(guild_id, user_id)
        );
        """
        await self.conn.executescript(script)
        await self._migrate_schema()

    async def _migrate_schema(self) -> None:
        assert self.conn is not None
        async with self.conn.execute("PRAGMA table_info(guild_config)") as cur:
            rows = await cur.fetchall()
        columns = {r["name"] for r in rows}
        to_add = [
            ("poll1_name", "TEXT"),
            ("poll2_name", "TEXT"),
            ("poll3_name", "TEXT"),
            ("streak_min_prayers", f"INTEGER NOT NULL DEFAULT {MIN_PRAYERS_FOR_STREAK}"),
            ("streak_min_habits", f"INTEGER NOT NULL DEFAULT {MIN_HABITS_FOR_STREAK}"),
        ]
        for name, ctype in to_add:
            if name not in columns:
                await self.conn.execute(f"ALTER TABLE guild_config ADD COLUMN {name} {ctype}")
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_poll_answers_lookup ON poll_answers(guild_id, group_id, user_id)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_user_date ON daily_entries(guild_id, user_id, entry_date)"
        )

    async def backup(self) -> None:
        if not self.conn:
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = BACKUP_DIR / f"moons_reflection_{stamp}.db"
        await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        await self.conn.commit()
        shutil.copy2(self.path, target)
        LOGGER.info("Backup created: %s", target)

    async def ensure_guild(self, guild_id: int) -> GuildConfig:
        cfg = await self.get_config(guild_id)
        if cfg:
            await self.seed_habits_if_empty(guild_id)
            await self.seed_prayers_if_missing(guild_id)
            return cfg
        now = now_iso()
        async with self.lock:
            assert self.conn is not None
            await self.conn.execute(
                """
                INSERT INTO guild_config(guild_id, timezone, created_ts, updated_ts)
                VALUES(?, ?, ?, ?)
                """,
                (guild_id, DEFAULT_TIMEZONE, now, now),
            )
            await self.conn.commit()
        await self.seed_habits_if_empty(guild_id)
        await self.seed_prayers_if_missing(guild_id)
        cfg = await self.get_config(guild_id)
        assert cfg is not None
        return cfg

    async def get_config(self, guild_id: int) -> Optional[GuildConfig]:
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)) as cur:
                row = await cur.fetchone()
        return GuildConfig.from_row(row) if row else None

    async def update_config(self, guild_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "poll_channel_id",
            "leaderboard_channel_id",
            "streak_channel_id",
            "timezone",
            "admin_role_id",
            "last_poll_date",
            "last_processed_date",
            "leaderboard_message_id",
            "streak_message_id",
            "weekly_message_id",
            "last_weekly_week_id",
            "poll1_name",
            "poll2_name",
            "poll3_name",
            "streak_min_prayers",
            "streak_min_habits",
        }
        data = {k: v for k, v in fields.items() if k in allowed}
        if not data:
            return
        data["updated_ts"] = now_iso()
        clause = ", ".join([f"{k}=?" for k in data.keys()])
        params = list(data.values()) + [guild_id]
        async with self.lock:
            assert self.conn is not None
            await self.conn.execute(f"UPDATE guild_config SET {clause} WHERE guild_id = ?", params)
            await self.conn.commit()

    async def count_habits(self, guild_id: int, category: Optional[str] = None, active_only: bool = True) -> int:
        where = ["guild_id = ?"]
        params: List[Any] = [guild_id]
        if category:
            where.append("category = ?")
            params.append(category)
        if active_only:
            where.append("active = 1")
        sql = f"SELECT COUNT(*) AS c FROM habits WHERE {' AND '.join(where)}"
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(sql, params) as cur:
                row = await cur.fetchone()
        return safe_int(row["c"]) if row else 0

    async def seed_habits_if_empty(self, guild_id: int) -> None:
        if await self.count_habits(guild_id, active_only=False) > 0:
            return
        now = now_iso()
        async with self.lock:
            assert self.conn is not None
            for item in DEFAULT_PRAYERS:
                await self.conn.execute(
                    """
                    INSERT INTO habits(guild_id, habit_key, habit_name, category, xp, active, created_by, created_ts)
                    VALUES(?, ?, ?, 'prayers', ?, 1, 0, ?)
                    """,
                    (guild_id, item["key"], item["label"], safe_int(item["xp"]), now),
                )
            for key, name, xp in DEFAULT_GENERAL_HABITS:
                await self.conn.execute(
                    """
                    INSERT INTO habits(guild_id, habit_key, habit_name, category, xp, active, created_by, created_ts)
                    VALUES(?, ?, ?, 'general', ?, 1, 0, ?)
                    """,
                    (guild_id, key, name, xp, now),
                )
            for key, name, xp in DEFAULT_ISLAMIC_HABITS:
                await self.conn.execute(
                    """
                    INSERT INTO habits(guild_id, habit_key, habit_name, category, xp, active, created_by, created_ts)
                    VALUES(?, ?, ?, 'islamic', ?, 1, 0, ?)
                    """,
                    (guild_id, key, name, xp, now),
                )
            await self.conn.commit()

    async def seed_prayers_if_missing(self, guild_id: int) -> None:
        if await self.count_habits(guild_id, category="prayers", active_only=False) > 0:
            return
        now = now_iso()
        async with self.lock:
            assert self.conn is not None
            for item in DEFAULT_PRAYERS:
                await self.conn.execute(
                    """
                    INSERT INTO habits(guild_id, habit_key, habit_name, category, xp, active, created_by, created_ts)
                    VALUES(?, ?, ?, 'prayers', ?, 1, 0, ?)
                    """,
                    (guild_id, item["key"], item["label"], safe_int(item["xp"]), now),
                )
            await self.conn.commit()

    async def list_habits(self, guild_id: int, category: Optional[str] = None, active_only: bool = True) -> List[Habit]:
        where = ["guild_id = ?"]
        params: List[Any] = [guild_id]
        if category:
            where.append("category = ?")
            params.append(category)
        if active_only:
            where.append("active = 1")
        sql = (
            "SELECT guild_id, habit_key, habit_name, category, xp, active FROM habits "
            f"WHERE {' AND '.join(where)} ORDER BY category ASC, habit_name COLLATE NOCASE ASC"
        )
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [Habit.from_row(r) for r in rows]

    async def get_habit(self, guild_id: int, habit_key: str) -> Optional[Habit]:
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(
                "SELECT guild_id, habit_key, habit_name, category, xp, active FROM habits WHERE guild_id=? AND habit_key=?",
                (guild_id, habit_key),
            ) as cur:
                row = await cur.fetchone()
        return Habit.from_row(row) if row else None

    async def find_habit(self, guild_id: int, query: str) -> Optional[Habit]:
        q = query.strip().lower()
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(
                """
                SELECT guild_id, habit_key, habit_name, category, xp, active
                FROM habits WHERE guild_id = ? AND (lower(habit_key)=? OR lower(habit_name)=?)
                ORDER BY active DESC LIMIT 1
                """,
                (guild_id, q, q),
            ) as cur:
                row = await cur.fetchone()
        return Habit.from_row(row) if row else None

    async def add_habit(
        self, guild_id: int, key: str, name: str, category: str, xp: int, created_by: int
    ) -> Habit:
        async with self.lock:
            assert self.conn is not None
            await self.conn.execute(
                """
                INSERT INTO habits(guild_id, habit_key, habit_name, category, xp, active, created_by, created_ts)
                VALUES(?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (guild_id, key, name, category, xp, created_by, now_iso()),
            )
            await self.conn.commit()
        habit = await self.get_habit(guild_id, key)
        assert habit is not None
        return habit

    async def deactivate_habit(self, guild_id: int, key: str) -> bool:
        async with self.lock:
            assert self.conn is not None
            cur = await self.conn.execute(
                "UPDATE habits SET active=0 WHERE guild_id=? AND habit_key=? AND active=1",
                (guild_id, key),
            )
            await self.conn.commit()
        return cur.rowcount > 0

    async def reactivate_habit(
        self, guild_id: int, key: str, name: str, category: str, xp: int
    ) -> Optional[Habit]:
        async with self.lock:
            assert self.conn is not None
            cur = await self.conn.execute(
                """
                UPDATE habits
                SET habit_name=?, category=?, xp=?, active=1
                WHERE guild_id=? AND habit_key=?
                """,
                (name, category, xp, guild_id, key),
            )
            await self.conn.commit()
        if cur.rowcount <= 0:
            return None
        return await self.get_habit(guild_id, key)

    async def deactivate_habits_in_category(self, guild_id: int, category: str) -> int:
        async with self.lock:
            assert self.conn is not None
            cur = await self.conn.execute(
                "UPDATE habits SET active=0 WHERE guild_id=? AND category=? AND active=1",
                (guild_id, category),
            )
            await self.conn.commit()
        return cur.rowcount

    async def create_poll_group(
        self,
        guild_id: int,
        poll_date: str,
        timezone_name: str,
        channel_id: int,
        prayers_snapshot: List[Dict[str, Any]],
        general_snapshot: List[Dict[str, Any]],
        islamic_snapshot: List[Dict[str, Any]],
        expires_ts: int,
    ) -> PollGroup:
        gid = uuid.uuid4().hex
        created = utc_ts()
        async with self.lock:
            assert self.conn is not None
            await self.conn.execute(
                """
                INSERT INTO poll_groups(
                    group_id, guild_id, poll_date, timezone, channel_id,
                    prayers_message_id, general_message_id, islamic_message_id,
                    created_ts, expires_ts, processed, send_error,
                    prayers_snapshot_json, general_snapshot_json, islamic_snapshot_json
                ) VALUES(?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, 0, 0, ?, ?, ?)
                """,
                (
                    gid,
                    guild_id,
                    poll_date,
                    timezone_name,
                    channel_id,
                    created,
                    expires_ts,
                    json.dumps(prayers_snapshot, ensure_ascii=False),
                    json.dumps(general_snapshot, ensure_ascii=False),
                    json.dumps(islamic_snapshot, ensure_ascii=False),
                ),
            )
            await self.conn.commit()
        group = await self.get_poll_group(gid)
        assert group is not None
        return group

    async def get_poll_group(self, group_id: str) -> Optional[PollGroup]:
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute("SELECT * FROM poll_groups WHERE group_id = ?", (group_id,)) as cur:
                row = await cur.fetchone()
        return PollGroup.from_row(row) if row else None

    async def poll_exists_for_date(self, guild_id: int, poll_date: str) -> bool:
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(
                """
                SELECT 1 FROM poll_groups
                WHERE guild_id=? AND poll_date=? AND send_error=0
                LIMIT 1
                """,
                (guild_id, poll_date),
            ) as cur:
                row = await cur.fetchone()
        return row is not None

    async def list_open_poll_groups(self, guild_id: int) -> List[PollGroup]:
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(
                "SELECT * FROM poll_groups WHERE guild_id=? AND processed=0 ORDER BY created_ts ASC",
                (guild_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [PollGroup.from_row(r) for r in rows]

    async def list_all_open_poll_groups(self) -> List[PollGroup]:
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(
                "SELECT * FROM poll_groups WHERE processed=0 ORDER BY created_ts ASC"
            ) as cur:
                rows = await cur.fetchall()
        return [PollGroup.from_row(r) for r in rows]

    async def count_open_poll_groups(self) -> int:
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute("SELECT COUNT(*) AS c FROM poll_groups WHERE processed=0") as cur:
                row = await cur.fetchone()
        return safe_int(row["c"]) if row else 0

    async def set_poll_message_id(self, group_id: str, poll_type: str, message_id: int) -> None:
        col = {
            "prayers": "prayers_message_id",
            "general": "general_message_id",
            "islamic": "islamic_message_id",
        }.get(poll_type)
        if not col:
            return
        async with self.lock:
            assert self.conn is not None
            await self.conn.execute(f"UPDATE poll_groups SET {col}=? WHERE group_id=?", (message_id, group_id))
            await self.conn.commit()

    async def mark_poll_group(self, group_id: str, processed: bool, send_error: Optional[bool] = None) -> None:
        fields = ["processed=?"]
        params: List[Any] = [1 if processed else 0]
        if send_error is not None:
            fields.append("send_error=?")
            params.append(1 if send_error else 0)
        params.append(group_id)
        sql = f"UPDATE poll_groups SET {', '.join(fields)} WHERE group_id=?"
        async with self.lock:
            assert self.conn is not None
            await self.conn.execute(sql, params)
            await self.conn.commit()

    async def save_poll_answer(
        self, group_id: str, guild_id: int, user_id: int, poll_type: str, selections: List[str]
    ) -> None:
        payload = json.dumps(sorted(set(selections)), ensure_ascii=False)
        async with self.lock:
            assert self.conn is not None
            await self.conn.execute(
                """
                INSERT INTO poll_answers(group_id, guild_id, user_id, poll_type, selections_json, updated_ts)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id, poll_type) DO UPDATE SET
                  selections_json=excluded.selections_json,
                  updated_ts=excluded.updated_ts
                """,
                (group_id, guild_id, user_id, poll_type, payload, now_iso()),
            )
            await self.conn.commit()

    async def load_user_answers(self, group_id: str, guild_id: int, user_id: int) -> Dict[str, List[str]]:
        out = {"prayers": [], "general": [], "islamic": []}
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(
                "SELECT poll_type, selections_json FROM poll_answers WHERE group_id=? AND guild_id=? AND user_id=?",
                (group_id, guild_id, user_id),
            ) as cur:
                rows = await cur.fetchall()
        for r in rows:
            try:
                out[r["poll_type"]] = list(json.loads(r["selections_json"]))
            except Exception:
                out[r["poll_type"]] = []
        return out

    async def get_daily_entry(self, guild_id: int, date_str: str, user_id: int) -> Optional[aiosqlite.Row]:
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(
                "SELECT * FROM daily_entries WHERE guild_id=? AND entry_date=? AND user_id=?",
                (guild_id, date_str, user_id),
            ) as cur:
                return await cur.fetchone()

    async def upsert_daily_entry(
        self,
        guild_id: int,
        date_str: str,
        user_id: int,
        prayers_count: int,
        general_count: int,
        islamic_count: int,
        total_habits: int,
        xp_earned: int,
        source_group_id: str,
    ) -> None:
        async with self.lock:
            assert self.conn is not None
            await self.conn.execute(
                """
                INSERT INTO daily_entries(
                    guild_id, entry_date, user_id,
                    prayers_count, general_count, islamic_count, total_habits, xp_earned,
                    source_group_id, processed, updated_ts
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(guild_id, entry_date, user_id) DO UPDATE SET
                    prayers_count=excluded.prayers_count,
                    general_count=excluded.general_count,
                    islamic_count=excluded.islamic_count,
                    total_habits=excluded.total_habits,
                    xp_earned=excluded.xp_earned,
                    source_group_id=excluded.source_group_id,
                    updated_ts=excluded.updated_ts
                """,
                (
                    guild_id,
                    date_str,
                    user_id,
                    prayers_count,
                    general_count,
                    islamic_count,
                    total_habits,
                    xp_earned,
                    source_group_id,
                    now_iso(),
                ),
            )
            await self.conn.commit()

    async def list_daily_entries(self, guild_id: int, date_str: str) -> List[aiosqlite.Row]:
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(
                "SELECT * FROM daily_entries WHERE guild_id=? AND entry_date=? ORDER BY xp_earned DESC, user_id ASC",
                (guild_id, date_str),
            ) as cur:
                return await cur.fetchall()

    async def list_unprocessed_dates(self, guild_id: int, upto: Optional[str]) -> List[str]:
        sql = "SELECT DISTINCT entry_date FROM daily_entries WHERE guild_id=? AND processed=0"
        params: List[Any] = [guild_id]
        if upto:
            sql += " AND entry_date <= ?"
            params.append(upto)
        sql += " ORDER BY entry_date ASC"
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [r["entry_date"] for r in rows]

    async def mark_date_processed(self, guild_id: int, date_str: str) -> None:
        async with self.lock:
            assert self.conn is not None
            await self.conn.execute(
                "UPDATE daily_entries SET processed=1, updated_ts=? WHERE guild_id=? AND entry_date=?",
                (now_iso(), guild_id, date_str),
            )
            await self.conn.commit()

    async def adjust_xp(self, guild_id: int, user_id: int, delta: int) -> int:
        async with self.lock:
            assert self.conn is not None
            await self.conn.execute(
                "INSERT OR IGNORE INTO xp_totals(guild_id, user_id, total_xp, updated_ts) VALUES(?, ?, 0, ?)",
                (guild_id, user_id, now_iso()),
            )
            await self.conn.execute(
                """
                UPDATE xp_totals
                SET total_xp = CASE WHEN total_xp + ? < 0 THEN 0 ELSE total_xp + ? END,
                    updated_ts = ?
                WHERE guild_id=? AND user_id=?
                """,
                (delta, delta, now_iso(), guild_id, user_id),
            )
            async with self.conn.execute(
                "SELECT total_xp FROM xp_totals WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ) as cur:
                row = await cur.fetchone()
            await self.conn.commit()
        return safe_int(row["total_xp"]) if row else 0

    async def top_xp(self, guild_id: int, limit: int = 100) -> List[Tuple[int, int]]:
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(
                """
                SELECT user_id, total_xp FROM xp_totals
                WHERE guild_id=? AND total_xp>0
                ORDER BY total_xp DESC, user_id ASC
                LIMIT ?
                """,
                (guild_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [(safe_int(r["user_id"]), safe_int(r["total_xp"])) for r in rows]

    async def get_streak(self, guild_id: int, user_id: int) -> Dict[str, Any]:
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(
                "SELECT * FROM user_streaks WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ) as cur:
                row = await cur.fetchone()
        if row:
            return dict(row)
        return {
            "guild_id": guild_id,
            "user_id": user_id,
            "current_streak": 0,
            "longest_streak": 0,
            "last_qualified_date": None,
        }

    async def upsert_streak(
        self, guild_id: int, user_id: int, current_streak: int, longest_streak: int, last_qualified_date: Optional[str]
    ) -> None:
        async with self.lock:
            assert self.conn is not None
            await self.conn.execute(
                """
                INSERT INTO user_streaks(guild_id, user_id, current_streak, longest_streak, last_qualified_date, updated_ts)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                  current_streak=excluded.current_streak,
                  longest_streak=excluded.longest_streak,
                  last_qualified_date=excluded.last_qualified_date,
                  updated_ts=excluded.updated_ts
                """,
                (guild_id, user_id, current_streak, longest_streak, last_qualified_date, now_iso()),
            )
            await self.conn.commit()

    async def list_streaks(self, guild_id: int) -> List[aiosqlite.Row]:
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(
                "SELECT * FROM user_streaks WHERE guild_id=? ORDER BY current_streak DESC, longest_streak DESC, user_id ASC",
                (guild_id,),
            ) as cur:
                return await cur.fetchall()

    async def list_streak_user_ids(self, guild_id: int) -> List[int]:
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(
                "SELECT user_id FROM user_streaks WHERE guild_id=?",
                (guild_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [safe_int(r["user_id"]) for r in rows]

    async def list_user_entries(self, guild_id: int, user_id: int, start: str, end: str) -> List[aiosqlite.Row]:
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(
                """
                SELECT * FROM daily_entries
                WHERE guild_id=? AND user_id=? AND entry_date>=? AND entry_date<=?
                ORDER BY entry_date ASC
                """,
                (guild_id, user_id, start, end),
            ) as cur:
                return await cur.fetchall()

    async def reset_streak_state(self, guild_id: int) -> None:
        async with self.lock:
            assert self.conn is not None
            await self.conn.execute("DELETE FROM user_streaks WHERE guild_id=?", (guild_id,))
            await self.conn.execute(
                "UPDATE daily_entries SET processed=0, updated_ts=? WHERE guild_id=?",
                (now_iso(), guild_id),
            )
            await self.conn.execute(
                "UPDATE guild_config SET last_processed_date=NULL, updated_ts=? WHERE guild_id=?",
                (now_iso(), guild_id),
            )
            await self.conn.commit()

    async def weekly_qualified_counts(
        self,
        guild_id: int,
        start: str,
        end: str,
        min_prayers: int,
        min_habits: int,
    ) -> Dict[int, int]:
        async with self.lock:
            assert self.conn is not None
            async with self.conn.execute(
                """
                SELECT user_id, COUNT(*) AS c FROM daily_entries
                WHERE guild_id=?
                  AND entry_date >= ?
                  AND entry_date <= ?
                  AND (prayers_count > 0 OR total_habits > 0)
                  AND prayers_count >= ?
                  AND total_habits >= ?
                GROUP BY user_id
                """,
                (guild_id, start, end, max(0, min_prayers), max(0, min_habits)),
            ) as cur:
                rows = await cur.fetchall()
        return {safe_int(r["user_id"]): safe_int(r["c"]) for r in rows}

    async def reset_guild(self, guild_id: int) -> None:
        async with self.lock:
            assert self.conn is not None
            await self.conn.execute("DELETE FROM poll_answers WHERE guild_id=?", (guild_id,))
            await self.conn.execute("DELETE FROM poll_groups WHERE guild_id=?", (guild_id,))
            await self.conn.execute("DELETE FROM daily_entries WHERE guild_id=?", (guild_id,))
            await self.conn.execute("DELETE FROM xp_totals WHERE guild_id=?", (guild_id,))
            await self.conn.execute("DELETE FROM user_streaks WHERE guild_id=?", (guild_id,))
            await self.conn.execute("DELETE FROM habits WHERE guild_id=?", (guild_id,))
            await self.conn.execute(
                """
                UPDATE guild_config
                SET last_poll_date=NULL, last_processed_date=NULL,
                    leaderboard_message_id=NULL, streak_message_id=NULL,
                    weekly_message_id=NULL, last_weekly_week_id=NULL,
                    updated_ts=?
                WHERE guild_id=?
                """,
                (now_iso(), guild_id),
            )
            await self.conn.commit()
        await self.seed_habits_if_empty(guild_id)

# -----------------------------------------------------------------------------
# Rate limit queue
# -----------------------------------------------------------------------------
class OutboundQueue:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Optional[Tuple[str, Callable[[], Awaitable[Any]], asyncio.Future]]] = asyncio.Queue()
        self.running = False
        self.worker: Optional[asyncio.Task] = None
        self.last_sent = 0.0
        self.min_interval = 0.9

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.worker = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        await self.queue.put(None)
        if self.worker:
            await asyncio.gather(self.worker, return_exceptions=True)
        self.worker = None

    async def submit(self, label: str, op: Callable[[], Awaitable[Any]]) -> Any:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await self.queue.put((label, op, fut))
        return await fut

    async def _loop(self) -> None:
        while True:
            item = await self.queue.get()
            if item is None:
                self.queue.task_done()
                break
            label, op, fut = item
            try:
                elapsed = time.monotonic() - self.last_sent
                if elapsed < self.min_interval:
                    await asyncio.sleep(self.min_interval - elapsed)
                result = await self._retry(label, op)
                self.last_sent = time.monotonic()
                if not fut.cancelled():
                    fut.set_result(result)
            except Exception as exc:
                if not fut.cancelled():
                    fut.set_exception(exc)
                LOGGER.exception("Queue op failed: %s | %s", label, exc)
            finally:
                self.queue.task_done()

    async def _retry(self, label: str, op: Callable[[], Awaitable[Any]]) -> Any:
        last: Optional[Exception] = None
        for attempt in range(1, 7):
            try:
                return await op()
            except discord.Forbidden:
                raise
            except discord.NotFound:
                raise
            except discord.HTTPException as exc:
                last = exc
                if getattr(exc, "status", None) == 429:
                    wait = float(getattr(exc, "retry_after", 2.0))
                    await asyncio.sleep(wait)
                    continue
                if 500 <= getattr(exc, "status", 0) < 600:
                    await asyncio.sleep(min(2 ** attempt, 15))
                    continue
                raise
            except asyncio.TimeoutError as exc:
                last = exc
                await asyncio.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"Operation failed after retries: {label} | {last}")

    @property
    def pending(self) -> int:
        return self.queue.qsize()


# -----------------------------------------------------------------------------
# Bot runtime
# -----------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents, help_command=None)

db = Database(DB_PATH)
outbox = OutboundQueue()

config_cache: Dict[int, GuildConfig] = {}
leaderboard_dirty: set[int] = set()
streak_dirty: set[int] = set()
scheduler_task: Optional[asyncio.Task] = None
scheduler_running = False
started_once = False
last_backup_monotonic = 0.0
last_lb_refresh: Dict[int, float] = {}
last_st_refresh: Dict[int, float] = {}


def mark_lb_dirty(guild_id: int) -> None:
    leaderboard_dirty.add(guild_id)


def mark_st_dirty(guild_id: int) -> None:
    streak_dirty.add(guild_id)


async def ensure_config(guild_id: int) -> GuildConfig:
    cfg = await db.ensure_guild(guild_id)
    config_cache[guild_id] = cfg
    return cfg


async def refresh_config(guild_id: int) -> Optional[GuildConfig]:
    cfg = await db.get_config(guild_id)
    if cfg:
        config_cache[guild_id] = cfg
    return cfg


async def get_config(guild_id: int) -> Optional[GuildConfig]:
    cached = config_cache.get(guild_id)
    if cached:
        return cached
    return await refresh_config(guild_id)


async def is_admin_or_authorized(ctx: commands.Context) -> bool:
    if ctx.guild is None or not isinstance(ctx.author, discord.Member):
        return False
    if ctx.author.guild_permissions.administrator or ctx.author.guild_permissions.manage_guild:
        return True
    cfg = await get_config(ctx.guild.id)
    if cfg and cfg.admin_role_id:
        return any(r.id == cfg.admin_role_id for r in ctx.author.roles)
    return False


def admin_guard() -> Callable:
    async def predicate(ctx: commands.Context) -> bool:
        return await is_admin_or_authorized(ctx)

    return commands.check(predicate)


async def send_message(channel: discord.abc.Messageable, **kwargs: Any) -> Optional[discord.Message]:
    async def _op() -> Optional[discord.Message]:
        return await channel.send(**kwargs)

    try:
        return await outbox.submit(f"send:{getattr(channel, 'id', 0)}", _op)
    except Exception:
        return None


async def safe_edit(msg: discord.Message, **kwargs: Any) -> bool:
    async def _op() -> discord.Message:
        return await msg.edit(**kwargs)

    try:
        await outbox.submit(f"edit:{msg.id}", _op)
        return True
    except Exception:
        return False


async def safe_pin(msg: discord.Message) -> bool:
    async def _op() -> None:
        await msg.pin()

    try:
        await outbox.submit(f"pin:{msg.id}", _op)
        return True
    except Exception:
        return False


def ui_embed(title: str, description: str, color: int = 0x3498DB, footer: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(
        title=title[:256],
        description=description[:4096] if description else "",
        color=color,
        timestamp=datetime.now(tz=pytz.UTC),
    )
    if bot.user:
        embed.set_author(name=bot.user.display_name, icon_url=bot.user.display_avatar.url)
    if footer:
        embed.set_footer(text=footer[:2048])
    return embed


async def reply_embed(
    ctx: commands.Context,
    title: str,
    description: str,
    color: int = 0x3498DB,
    footer: Optional[str] = None,
) -> None:
    await ctx.reply(embed=ui_embed(title, description, color=color, footer=footer), mention_author=False)


def poll_preview(snapshot: List[Dict[str, Any]], cap: int = 8) -> str:
    if not snapshot:
        return "No options configured yet."
    lines: List[str] = []
    for idx, item in enumerate(snapshot[:cap], 1):
        lines.append(f"{idx}. {item.get('label', 'Option')} ({safe_int(item.get('xp'), 0)} XP)")
    if len(snapshot) > cap:
        lines.append(f"...and {len(snapshot) - cap} more")
    return "\n".join(lines)


def build_poll_embed(
    poll_type: str,
    poll_label: str,
    poll_date: str,
    snapshot: List[Dict[str, Any]],
    expires_ts: int,
    group_id: str,
) -> discord.Embed:
    icon = POLL_ICONS.get(poll_type, "P")
    color = POLL_COLORS.get(poll_type, 0x3498DB)
    embed = ui_embed(
        title=f"{icon} {poll_label} | {poll_date}",
        description=(
            "Mark completed items from the dropdown below.\n"
            "You can update your response multiple times before the poll closes."
        ),
        color=color,
        footer=f"group={group_id}",
    )
    embed.add_field(name="Checklist", value=poll_preview(snapshot), inline=False)
    embed.add_field(name="Time Remaining", value=f"<t:{expires_ts}:R>", inline=True)
    embed.add_field(name="Total Items", value=str(len(snapshot)), inline=True)
    embed.add_field(name="Tip", value="Small daily wins build long-term streaks.", inline=False)
    return embed


def snapshot_to_options(snapshot: List[Dict[str, Any]]) -> List[discord.SelectOption]:
    if not snapshot:
        return [discord.SelectOption(label="No options configured", value="__none__", description="Add habits first")]
    out: List[discord.SelectOption] = []
    for item in snapshot[:MAX_HABITS_PER_CATEGORY]:
        label = str(item.get("label", "Option"))[:95]
        value = str(item.get("key", "option"))
        xp = safe_int(item.get("xp"), 0)
        out.append(discord.SelectOption(label=label, value=value, description=f"{xp} XP"))
    return out


class PollView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        group_id: str,
        poll_type: str,
        snapshot: List[Dict[str, Any]],
        poll_label: Optional[str] = None,
    ) -> None:
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.group_id = group_id
        self.poll_type = poll_type
        self.poll_label = poll_label or DEFAULT_POLL_LABELS.get(poll_type, "Poll")
        options = snapshot_to_options(snapshot)
        select = discord.ui.Select(
            placeholder=f"{self.poll_label}: select completed items",
            min_values=0,
            max_values=min(len(options), 25),
            options=options,
            custom_id=f"poll:{guild_id}:{group_id}:{poll_type}",
        )
        select.callback = self.on_select  # type: ignore[assignment]
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("Poll belongs to another server.", ephemeral=True)
            return
        if interaction.user.bot:
            return
        values = interaction.data.get("values", []) if interaction.data else []
        values = [v for v in values if v != "__none__"]
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        msg = await handle_selection(self.guild_id, self.group_id, self.poll_type, interaction.user.id, values)
        await interaction.followup.send(msg, ephemeral=True)


async def compute_prayer_xp_and_count(selected: List[str], date_str: str, snapshot: List[Dict[str, Any]]) -> Tuple[int, int]:
    pool = {str(i["key"]): i for i in snapshot}
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    friday = dt.weekday() == 4
    xp = 0
    count = 0
    for k in sorted(set(selected)):
        item = pool.get(k)
        if not item:
            continue
        if k == "jummah" and not friday:
            continue
        xp += safe_int(item.get("xp"), 0)
        count += 1
    return xp, count


async def compute_habit_xp_and_count(selected: List[str], snapshot: List[Dict[str, Any]]) -> Tuple[int, int]:
    pool = {str(i["key"]): i for i in snapshot}
    xp = 0
    count = 0
    for k in sorted(set(selected)):
        item = pool.get(k)
        if not item:
            continue
        xp += safe_int(item.get("xp"), 0)
        count += 1
    return xp, count


async def handle_selection(
    guild_id: int, group_id: str, poll_type: str, user_id: int, selected: List[str]
) -> str:
    group = await db.get_poll_group(group_id)
    if not group:
        return "Poll no longer exists."
    if group.guild_id != guild_id:
        return "Invalid poll context."
    if group.processed:
        return "Poll already closed."

    await db.save_poll_answer(group_id, guild_id, user_id, poll_type, selected)
    answers = await db.load_user_answers(group_id, guild_id, user_id)
    p_sel = answers.get("prayers", [])
    g_sel = answers.get("general", [])
    i_sel = answers.get("islamic", [])

    p_xp, p_count = await compute_prayer_xp_and_count(p_sel, group.poll_date, group.prayers_snapshot)
    g_xp, g_count = await compute_habit_xp_and_count(g_sel, group.general_snapshot)
    i_xp, i_count = await compute_habit_xp_and_count(i_sel, group.islamic_snapshot)
    new_xp = p_xp + g_xp + i_xp
    total_habits = g_count + i_count

    old = await db.get_daily_entry(guild_id, group.poll_date, user_id)
    old_xp = safe_int(old["xp_earned"]) if old else 0
    delta = new_xp - old_xp

    await db.upsert_daily_entry(
        guild_id,
        group.poll_date,
        user_id,
        p_count,
        g_count,
        i_count,
        total_habits,
        new_xp,
        group.group_id,
    )
    if delta != 0:
        await db.adjust_xp(guild_id, user_id, delta)
        mark_lb_dirty(guild_id)
    cfg = await get_config(guild_id)
    qualified = qualifies_for_streak(p_count, total_habits, cfg)
    p_label = resolve_poll_label(cfg, "prayers")
    g_label = resolve_poll_label(cfg, "general")
    i_label = resolve_poll_label(cfg, "islamic")

    p_total = len(group.prayers_snapshot)
    g_total = len(group.general_snapshot)
    i_total = len(group.islamic_snapshot)
    completed = p_count + g_count + i_count
    total = p_total + g_total + i_total

    return (
        f"Saved for {group.poll_date}\n"
        f"XP: {new_xp} ({delta:+d})\n"
        f"Progress: {completed}/{total} {progress_bar(completed, total)}\n"
        f"{p_label}: {p_count}/{p_total}\n"
        f"{g_label}: {g_count}/{g_total}\n"
        f"{i_label}: {i_count}/{i_total}\n"
        f"Streak qualified today: {'Yes' if qualified else 'No'}"
    )


async def post_daily_polls(guild: discord.Guild, force_date: Optional[str] = None, force: bool = False) -> Optional[PollGroup]:
    cfg = await get_config(guild.id)
    if not cfg or not cfg.poll_channel_id:
        return None
    channel = guild.get_channel(cfg.poll_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return None

    poll_date = force_date or date_str_local(cfg.timezone)
    if not force and await db.poll_exists_for_date(guild.id, poll_date):
        return None

    await db.seed_prayers_if_missing(guild.id)
    prayers = await db.list_habits(guild.id, category="prayers", active_only=True)
    general = await db.list_habits(guild.id, category="general", active_only=True)
    islamic = await db.list_habits(guild.id, category="islamic", active_only=True)

    prayers_snapshot = [{"key": h.key, "label": h.name, "xp": h.xp} for h in prayers][:MAX_HABITS_PER_CATEGORY]
    general_snapshot = [{"key": h.key, "label": h.name, "xp": h.xp} for h in general][:MAX_HABITS_PER_CATEGORY]
    islamic_snapshot = [{"key": h.key, "label": h.name, "xp": h.xp} for h in islamic][:MAX_HABITS_PER_CATEGORY]
    expires_ts = int(
        (tz_now(cfg.timezone).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=POLL_DURATION_HOURS)).timestamp()
    )

    group = await db.create_poll_group(
        guild.id,
        poll_date,
        cfg.timezone,
        channel.id,
        prayers_snapshot,
        general_snapshot,
        islamic_snapshot,
        expires_ts,
    )

    p_label = resolve_poll_label(cfg, "prayers")
    g_label = resolve_poll_label(cfg, "general")
    i_label = resolve_poll_label(cfg, "islamic")

    p_view = PollView(guild.id, group.group_id, "prayers", prayers_snapshot, poll_label=p_label)
    g_view = PollView(guild.id, group.group_id, "general", general_snapshot, poll_label=g_label)
    i_view = PollView(guild.id, group.group_id, "islamic", islamic_snapshot, poll_label=i_label)

    p_embed = build_poll_embed("prayers", p_label, poll_date, prayers_snapshot, expires_ts, group.group_id)
    g_embed = build_poll_embed("general", g_label, poll_date, general_snapshot, expires_ts, group.group_id)
    i_embed = build_poll_embed("islamic", i_label, poll_date, islamic_snapshot, expires_ts, group.group_id)

    p_msg = await send_message(channel, embed=p_embed, view=p_view)
    g_msg = await send_message(channel, embed=g_embed, view=g_view)
    i_msg = await send_message(channel, embed=i_embed, view=i_view)
    if p_msg:
        await db.set_poll_message_id(group.group_id, "prayers", p_msg.id)
    if g_msg:
        await db.set_poll_message_id(group.group_id, "general", g_msg.id)
    if i_msg:
        await db.set_poll_message_id(group.group_id, "islamic", i_msg.id)

    if not (p_msg or g_msg or i_msg):
        await db.mark_poll_group(group.group_id, True, send_error=True)
        return None

    bot.add_view(p_view)
    bot.add_view(g_view)
    bot.add_view(i_view)
    await db.update_config(guild.id, last_poll_date=poll_date)
    await refresh_config(guild.id)
    LOGGER.info(
        "Poll created guild=%s date=%s group=%s labels=(%s,%s,%s)",
        guild.id,
        poll_date,
        group.group_id,
        p_label,
        g_label,
        i_label,
    )
    return await db.get_poll_group(group.group_id)


async def restore_persistent_views() -> int:
    groups = await db.list_all_open_poll_groups()
    restored = 0
    for group in groups:
        if group.send_error:
            continue
        cfg = await get_config(group.guild_id)
        bot.add_view(
            PollView(
                group.guild_id,
                group.group_id,
                "prayers",
                group.prayers_snapshot,
                poll_label=resolve_poll_label(cfg, "prayers"),
            )
        )
        bot.add_view(
            PollView(
                group.guild_id,
                group.group_id,
                "general",
                group.general_snapshot,
                poll_label=resolve_poll_label(cfg, "general"),
            )
        )
        bot.add_view(
            PollView(
                group.guild_id,
                group.group_id,
                "islamic",
                group.islamic_snapshot,
                poll_label=resolve_poll_label(cfg, "islamic"),
            )
        )
        restored += 1
    return restored


async def announce_and_close_group(guild: discord.Guild, group: PollGroup) -> None:
    if group.processed:
        return
    cfg = await get_config(guild.id)
    channel: Optional[discord.TextChannel] = None
    if cfg and cfg.poll_channel_id:
        ch = guild.get_channel(cfg.poll_channel_id)
        if isinstance(ch, discord.TextChannel):
            channel = ch
    if channel is None:
        ch = guild.get_channel(group.channel_id)
        if isinstance(ch, discord.TextChannel):
            channel = ch
    if channel is None:
        await db.mark_poll_group(group.group_id, True, send_error=True)
        return

    rows = await db.list_daily_entries(guild.id, group.poll_date)
    if rows:
        lines: List[str] = []
        total_xp = 0
        for idx, r in enumerate(rows[:30], start=1):
            uid = safe_int(r["user_id"])
            member = guild.get_member(uid)
            name = member.display_name if member else f"user:{uid}"
            earned = safe_int(r["xp_earned"])
            total_xp += earned
            lines.append(
                f"{idx}. {name} | XP {earned} | prayers {safe_int(r['prayers_count'])} | habits {safe_int(r['total_habits'])}"
            )
        avg = total_xp // max(1, len(rows))
        desc = "\n".join(lines)
    else:
        desc = "No entries submitted for this day."

    embed = ui_embed(
        title=f"{SUMMARY_TITLE} | {group.poll_date}",
        description=desc,
        color=0x3498DB,
        footer=f"{SUMMARY_FOOTER_TAG} | group={group.group_id}",
    )
    if rows:
        embed.add_field(name="Participants", value=str(len(rows)), inline=True)
        embed.add_field(name="Avg XP", value=str(avg), inline=True)
    msg = await send_message(channel, embed=embed)
    if msg:
        await safe_pin(msg)
    await db.mark_poll_group(group.group_id, True, send_error=False)
    mark_lb_dirty(guild.id)
    mark_st_dirty(guild.id)


async def close_expired_groups(guild: discord.Guild) -> int:
    now_epoch = utc_ts()
    groups = await db.list_open_poll_groups(guild.id)
    count = 0
    for g in groups:
        if now_epoch >= g.expires_ts:
            await announce_and_close_group(guild, g)
            count += 1
    return count


async def get_or_create_leaderboard_message(channel: discord.TextChannel, cfg: GuildConfig) -> Optional[discord.Message]:
    if cfg.leaderboard_message_id:
        try:
            return await channel.fetch_message(cfg.leaderboard_message_id)
        except Exception:
            pass
    embed = ui_embed(
        title=LEADERBOARD_TITLE,
        description="Initializing...",
        color=0xF1C40F,
        footer=f"{LEADERBOARD_FOOTER_TAG} | created",
    )
    msg = await send_message(channel, embed=embed)
    if msg:
        await db.update_config(cfg.guild_id, leaderboard_message_id=msg.id)
        await refresh_config(cfg.guild_id)
        await safe_pin(msg)
    return msg


async def get_or_create_streak_message(channel: discord.TextChannel, cfg: GuildConfig) -> Optional[discord.Message]:
    if cfg.streak_message_id:
        try:
            return await channel.fetch_message(cfg.streak_message_id)
        except Exception:
            pass
    embed = ui_embed(
        title=STREAK_TITLE,
        description="Initializing...",
        color=0xF1C40F,
        footer=f"{STREAK_FOOTER_TAG} | created",
    )
    msg = await send_message(channel, embed=embed)
    if msg:
        await db.update_config(cfg.guild_id, streak_message_id=msg.id)
        await refresh_config(cfg.guild_id)
        await safe_pin(msg)
    return msg


async def refresh_leaderboard(guild: discord.Guild, force: bool = False) -> None:
    cfg = await get_config(guild.id)
    if not cfg or not cfg.leaderboard_channel_id:
        return
    ch = guild.get_channel(cfg.leaderboard_channel_id)
    if not isinstance(ch, discord.TextChannel):
        return

    if not force and time.monotonic() - last_lb_refresh.get(guild.id, 0.0) < 20:
        return
    last_lb_refresh[guild.id] = time.monotonic()

    rows = await db.top_xp(guild.id, 100)
    if rows:
        sorted_rows = sorted(rows, key=lambda x: (-x[1], x[0]))
        ranked = rank_with_ties(sorted_rows)
        lines = []
        for rank, uid, xp in ranked[:25]:
            member = guild.get_member(uid)
            name = member.display_name if member else f"user:{uid}"
            badge = rank_badge(rank)
            lvl = level_from_total_xp(xp)
            lines.append(f"{badge} {name}  |  {xp} XP  |  Level {lvl}")
        desc = "\n".join(lines)
    else:
        desc = "No XP data yet."

    embed = ui_embed(
        title=LEADERBOARD_TITLE,
        description=desc,
        color=0xF1C40F,
        footer=f"{LEADERBOARD_FOOTER_TAG} | {tz_now(cfg.timezone).strftime('%Y-%m-%d %H:%M %Z')}",
    )
    msg = await get_or_create_leaderboard_message(ch, cfg)
    if msg:
        await safe_edit(msg, embed=embed)


async def refresh_streak_board(guild: discord.Guild, force: bool = False) -> None:
    cfg = await get_config(guild.id)
    if not cfg or not cfg.streak_channel_id:
        return
    ch = guild.get_channel(cfg.streak_channel_id)
    if not isinstance(ch, discord.TextChannel):
        return

    if not force and time.monotonic() - last_st_refresh.get(guild.id, 0.0) < 20:
        return
    last_st_refresh[guild.id] = time.monotonic()

    rows = await db.list_streaks(guild.id)
    if rows:
        lines = []
        for i, r in enumerate(rows[:25], 1):
            uid = safe_int(r["user_id"])
            member = guild.get_member(uid)
            name = member.display_name if member else f"user:{uid}"
            cur = safe_int(r["current_streak"])
            longest = safe_int(r["longest_streak"])
            lines.append(
                f"{rank_badge(i)} {name}  |  current {cur}  |  best {longest}"
            )
        desc = "\n".join(lines)
    else:
        desc = "No streak data yet."

    embed = ui_embed(
        title=STREAK_TITLE,
        description=desc,
        color=0xF1C40F,
        footer=f"{STREAK_FOOTER_TAG} | {tz_now(cfg.timezone).strftime('%Y-%m-%d %H:%M %Z')}",
    )
    msg = await get_or_create_streak_message(ch, cfg)
    if msg:
        await safe_edit(msg, embed=embed)


async def process_streaks_up_to(guild: discord.Guild, upto_date: str) -> int:
    dates = await db.list_unprocessed_dates(guild.id, upto_date)
    cfg = await get_config(guild.id)
    synthetic: List[str] = []
    if cfg and cfg.last_processed_date:
        cursor = date_next(cfg.last_processed_date)
        guard = 0
        existing = set(dates)
        while cursor <= upto_date and guard < STREAK_CATCHUP_MAX_DAYS:
            if cursor not in existing:
                synthetic.append(cursor)
            cursor = date_next(cursor)
            guard += 1
    plan = sorted(set(dates + synthetic))
    if not plan:
        return 0
    for d in plan:
        await process_streak_date(guild, d, cfg=cfg)
    return len(plan)


async def process_streak_date(guild: discord.Guild, date_str: str, cfg: Optional[GuildConfig] = None) -> None:
    if cfg is None:
        cfg = await get_config(guild.id)
    rows = await db.list_daily_entries(guild.id, date_str)
    entries = {safe_int(r["user_id"]): r for r in rows}
    prev = date_prev(date_str)
    user_ids = set(entries.keys())
    user_ids.update(await db.list_streak_user_ids(guild.id))
    for uid in sorted(user_ids):
        row = entries.get(uid)
        prayers = safe_int(row["prayers_count"]) if row else 0
        total_habits = safe_int(row["total_habits"]) if row else 0
        qualifies = qualifies_for_streak(prayers, total_habits, cfg)
        streak = await db.get_streak(guild.id, uid)
        cur = safe_int(streak["current_streak"])
        longest = safe_int(streak["longest_streak"])
        last_date = streak["last_qualified_date"]
        if qualifies:
            if last_date == prev:
                cur += 1
            else:
                cur = 1
            longest = max(longest, cur)
            last_date = date_str
        else:
            cur = 0
            last_date = date_str
        await db.upsert_streak(guild.id, uid, cur, longest, last_date)
    await db.mark_date_processed(guild.id, date_str)
    await db.update_config(guild.id, last_processed_date=date_str)
    await refresh_config(guild.id)
    mark_st_dirty(guild.id)


async def post_weekly_summary(guild: discord.Guild, week_id: str) -> None:
    cfg = await get_config(guild.id)
    if not cfg or not cfg.streak_channel_id:
        return
    ch = guild.get_channel(cfg.streak_channel_id)
    if not isinstance(ch, discord.TextChannel):
        return

    now = tz_now(cfg.timezone)
    end_dt = (now - timedelta(days=1)).date()
    start_dt = end_dt - timedelta(days=6)
    start = start_dt.strftime("%Y-%m-%d")
    end = end_dt.strftime("%Y-%m-%d")
    min_prayers, min_habits = streak_thresholds(cfg)
    counts = await db.weekly_qualified_counts(guild.id, start, end, min_prayers, min_habits)
    rows = [(m, counts.get(m.id, 0)) for m in [x for x in guild.members if not x.bot]]
    rows.sort(key=lambda x: (-x[1], x[0].display_name.lower()))
    lines = [f"{rank_badge(idx)} {m.display_name} | {c}/7 qualified days" for idx, (m, c) in enumerate(rows[:50], 1)]

    embed = ui_embed(
        title=f"{WEEKLY_TITLE} ({start} to {end})",
        description="\n".join(lines) or "No data",
        color=0x9B59B6,
        footer=f"{WEEKLY_FOOTER_TAG} | week={week_id}",
    )
    msg = await send_message(ch, embed=embed)
    if msg:
        await db.update_config(guild.id, weekly_message_id=msg.id, last_weekly_week_id=week_id)
        await refresh_config(guild.id)


async def scheduler_loop() -> None:
    global scheduler_running, last_backup_monotonic
    await bot.wait_until_ready()
    scheduler_running = True
    while scheduler_running and not bot.is_closed():
        try:
            for guild in bot.guilds:
                cfg = await get_config(guild.id)
                if not cfg:
                    continue
                today = date_str_local(cfg.timezone)
                yesterday = date_prev(today)

                await close_expired_groups(guild)
                if cfg.poll_channel_id and not await db.poll_exists_for_date(guild.id, today):
                    await post_daily_polls(guild, force_date=today, force=False)
                await process_streaks_up_to(guild, yesterday)

                now_mono = time.monotonic()
                if guild.id in leaderboard_dirty or now_mono - last_lb_refresh.get(guild.id, 0.0) >= LEADERBOARD_REFRESH_SECONDS:
                    await refresh_leaderboard(guild, force=True)
                    leaderboard_dirty.discard(guild.id)
                if guild.id in streak_dirty or now_mono - last_st_refresh.get(guild.id, 0.0) >= STREAK_REFRESH_SECONDS:
                    await refresh_streak_board(guild, force=True)
                    streak_dirty.discard(guild.id)

                local_now = tz_now(cfg.timezone)
                if local_now.weekday() == 0 and local_now.hour == 0 and local_now.minute < 15:
                    week_id = local_now.strftime("%G-W%V")
                    if cfg.last_weekly_week_id != week_id:
                        await post_weekly_summary(guild, week_id)

            if time.monotonic() - last_backup_monotonic >= BACKUP_INTERVAL_SECONDS:
                await db.backup()
                last_backup_monotonic = time.monotonic()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            LOGGER.exception("Scheduler error: %s", exc)
        await asyncio.sleep(SCHEDULER_INTERVAL)


# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------
@bot.command(name="help")
@commands.guild_only()
async def cmd_help(ctx: commands.Context) -> None:
    txt = (
        f"Prefix: `{BOT_PREFIX}`\n\n"
        f"`{BOT_PREFIX}setup #poll #leaderboard #streak [timezone]`\n"
        f"`{BOT_PREFIX}setpollchannel #channel`\n"
        f"`{BOT_PREFIX}setleaderboardchannel #channel`\n"
        f"`{BOT_PREFIX}setstreakchannel #channel`\n"
        f"`{BOT_PREFIX}settimezone <tz>`\n"
        f"`{BOT_PREFIX}setstreakrules <min_prayers> <min_habits>`\n"
        f"`{BOT_PREFIX}setadminrole @role` / `{BOT_PREFIX}clearadminrole`\n"
        f"`{BOT_PREFIX}renamepoll <poll1|poll2|poll3> <new name>`\n"
        f"`{BOT_PREFIX}addhabit <habit_name> <poll1|poll2|poll3> [xp]`\n"
        f"`{BOT_PREFIX}addhabits <poll1|poll2|poll3> <habit1> <habit2> ... <xp>`\n"
        f"`{BOT_PREFIX}removehabit <name_or_key>`\n"
        f"`{BOT_PREFIX}removeallhabits <poll1|poll2|poll3>`\n"
        f"`{BOT_PREFIX}listhabits [poll1|poll2|poll3|general|islamic|prayers]`\n"
        f"`{BOT_PREFIX}pollnow` / `{BOT_PREFIX}closetoday`\n"
        f"`{BOT_PREFIX}leaderboard` / `{BOT_PREFIX}streaks`\n"
        f"`{BOT_PREFIX}rebuildstreaks`\n"
        f"`{BOT_PREFIX}myprogress [YYYY-MM-DD]`\n"
        f"`{BOT_PREFIX}status`\n"
        f"`{BOT_PREFIX}resetsystem confirm`\n"
    )
    await reply_embed(ctx, "Moon's Reflection Commands", txt, color=0x4B7BEC)


@bot.command(name="setup")
@commands.guild_only()
@admin_guard()
async def cmd_setup(
    ctx: commands.Context,
    poll: discord.TextChannel,
    leaderboard: discord.TextChannel,
    streak: discord.TextChannel,
    timezone: Optional[str] = None,
) -> None:
    assert ctx.guild is not None
    tz = timezone or DEFAULT_TIMEZONE
    ok = parse_timezone(tz)
    if not ok:
        await reply_embed(ctx, "Setup Failed", "Invalid timezone. Example: `Asia/Karachi`.", color=0xE74C3C)
        return
    await db.update_config(
        ctx.guild.id,
        poll_channel_id=poll.id,
        leaderboard_channel_id=leaderboard.id,
        streak_channel_id=streak.id,
        timezone=ok,
    )
    await refresh_config(ctx.guild.id)
    await db.seed_habits_if_empty(ctx.guild.id)
    await db.seed_prayers_if_missing(ctx.guild.id)
    cfg = await get_config(ctx.guild.id)
    min_prayers, min_habits = streak_thresholds(cfg)
    await reply_embed(
        ctx,
        "Setup Complete",
        (
            f"Poll channel: {poll.mention}\n"
            f"Leaderboard channel: {leaderboard.mention}\n"
            f"Streak channel: {streak.mention}\n"
            f"Timezone: `{ok}`\n"
            f"Streak rule: prayers>={min_prayers}, habits>={min_habits}"
        ),
        color=0x2ECC71,
    )


@bot.command(name="setpollchannel")
@commands.guild_only()
@admin_guard()
async def cmd_setpoll(ctx: commands.Context, channel: discord.TextChannel) -> None:
    assert ctx.guild is not None
    await db.update_config(ctx.guild.id, poll_channel_id=channel.id)
    await refresh_config(ctx.guild.id)
    await reply_embed(ctx, "Poll Channel Updated", f"Poll channel set to {channel.mention}", color=0x2ECC71)


@bot.command(name="setleaderboardchannel")
@commands.guild_only()
@admin_guard()
async def cmd_setboard(ctx: commands.Context, channel: discord.TextChannel) -> None:
    assert ctx.guild is not None
    await db.update_config(ctx.guild.id, leaderboard_channel_id=channel.id)
    await refresh_config(ctx.guild.id)
    await reply_embed(ctx, "Leaderboard Channel Updated", f"Leaderboard channel set to {channel.mention}", color=0x2ECC71)


@bot.command(name="setstreakchannel")
@commands.guild_only()
@admin_guard()
async def cmd_setstreak(ctx: commands.Context, channel: discord.TextChannel) -> None:
    assert ctx.guild is not None
    await db.update_config(ctx.guild.id, streak_channel_id=channel.id)
    await refresh_config(ctx.guild.id)
    await reply_embed(ctx, "Streak Channel Updated", f"Streak channel set to {channel.mention}", color=0x2ECC71)


@bot.command(name="settimezone")
@commands.guild_only()
@admin_guard()
async def cmd_settimezone(ctx: commands.Context, timezone: str) -> None:
    assert ctx.guild is not None
    ok = parse_timezone(timezone)
    if not ok:
        await reply_embed(ctx, "Invalid Timezone", "Please provide a valid timezone name.", color=0xE74C3C)
        return
    await db.update_config(ctx.guild.id, timezone=ok)
    await refresh_config(ctx.guild.id)
    await reply_embed(ctx, "Timezone Updated", f"Timezone set to `{ok}`", color=0x2ECC71)


@bot.command(name="setadminrole")
@commands.guild_only()
@admin_guard()
async def cmd_setadminrole(ctx: commands.Context, role: discord.Role) -> None:
    assert ctx.guild is not None
    await db.update_config(ctx.guild.id, admin_role_id=role.id)
    await refresh_config(ctx.guild.id)
    await reply_embed(ctx, "Admin Role Updated", f"Authorized role set: {role.mention}", color=0x2ECC71)


@bot.command(name="setstreakrules")
@commands.guild_only()
@admin_guard()
async def cmd_setstreakrules(ctx: commands.Context, min_prayers: int, min_habits: int) -> None:
    assert ctx.guild is not None
    if min_prayers < 0 or min_habits < 0:
        await reply_embed(ctx, "Invalid Rules", "Values must be zero or higher.", color=0xE74C3C)
        return
    if min_prayers > 10 or min_habits > 50:
        await reply_embed(ctx, "Invalid Rules", "Use practical limits: prayers<=10, habits<=50.", color=0xE74C3C)
        return
    await db.update_config(
        ctx.guild.id,
        streak_min_prayers=min_prayers,
        streak_min_habits=min_habits,
    )
    await db.reset_streak_state(ctx.guild.id)
    cfg = await refresh_config(ctx.guild.id)
    assert cfg is not None
    await process_streaks_up_to(ctx.guild, date_prev(date_str_local(cfg.timezone)))
    await refresh_streak_board(ctx.guild, force=True)
    mark_st_dirty(ctx.guild.id)
    await reply_embed(
        ctx,
        "Streak Rules Updated",
        (
            f"Rule: prayers>={min_prayers}, habits>={min_habits}\n"
            f"Current poll labels: {resolve_poll_label(cfg, 'prayers')} | {resolve_poll_label(cfg, 'general')} | {resolve_poll_label(cfg, 'islamic')}"
        ),
        color=0x2ECC71,
    )


@bot.command(name="clearadminrole")
@commands.guild_only()
@admin_guard()
async def cmd_clearadminrole(ctx: commands.Context) -> None:
    assert ctx.guild is not None
    await db.update_config(ctx.guild.id, admin_role_id=None)
    await refresh_config(ctx.guild.id)
    await reply_embed(ctx, "Admin Role Updated", "Authorized role cleared.", color=0x2ECC71)


@bot.command(name="renamepoll")
@commands.guild_only()
@admin_guard()
async def cmd_renamepoll(ctx: commands.Context, poll: str, *, new_name: str) -> None:
    assert ctx.guild is not None
    poll_type = normalize_poll_type(poll)
    if not poll_type:
        await reply_embed(
            ctx,
            "Invalid Poll",
            "Use one of: `poll1`, `poll2`, `poll3` (or `prayers`, `general`, `islamic`).",
            color=0xE74C3C,
        )
        return
    cleaned = new_name.strip().strip('"').strip()
    if not cleaned:
        await reply_embed(ctx, "Invalid Name", "Poll name cannot be empty.", color=0xE74C3C)
        return
    if len(cleaned) > 80:
        await reply_embed(ctx, "Invalid Name", "Poll name must be 80 characters or fewer.", color=0xE74C3C)
        return

    await db.update_config(ctx.guild.id, **{poll_field_for_type(poll_type): cleaned})
    cfg = await refresh_config(ctx.guild.id)
    assert cfg is not None
    lines = [
        f"poll1: {resolve_poll_label(cfg, 'prayers')}",
        f"poll2: {resolve_poll_label(cfg, 'general')}",
        f"poll3: {resolve_poll_label(cfg, 'islamic')}",
    ]
    await reply_embed(ctx, "Poll Name Updated", "\n".join(lines), color=0x2ECC71)


@bot.command(name="addhabit")
@commands.guild_only()
@admin_guard()
async def cmd_addhabit(ctx: commands.Context, *, raw: str) -> None:
    assert ctx.guild is not None
    try:
        name, xp, category = parse_addhabit(raw)
    except ValueError as exc:
        await reply_embed(
            ctx,
            "Add Habit Failed",
            f"{exc}\nUsage: `{BOT_PREFIX}addhabit <habit_name> <poll1|poll2|poll3> [xp]`",
            color=0xE74C3C,
        )
        return

    active_count = await db.count_habits(ctx.guild.id, category=category, active_only=True)
    if active_count >= MAX_HABITS_PER_CATEGORY:
        await reply_embed(
            ctx,
            "Category Limit Reached",
            f"{resolve_poll_label(await get_config(ctx.guild.id), category)} reached limit {MAX_HABITS_PER_CATEGORY}.",
            color=0xE67E22,
        )
        return

    key_base = slugify(name)
    key = key_base
    existing = await db.get_habit(ctx.guild.id, key)
    if existing and existing.active == 0:
        rec = await db.reactivate_habit(ctx.guild.id, key, name, category, xp)
        if rec:
            cfg = await get_config(ctx.guild.id)
            await reply_embed(
                ctx,
                "Habit Restored",
                (
                    f"Name: `{rec.name}`\n"
                    f"Poll: {resolve_poll_label(cfg, rec.category)} ({poll_slot_for_type(rec.category)})\n"
                    f"Key: `{rec.key}`\n"
                    f"XP: `{rec.xp}`"
                ),
                color=0x2ECC71,
            )
            return
    idx = 1
    while existing:
        idx += 1
        key = f"{key_base}_{idx}"
        existing = await db.get_habit(ctx.guild.id, key)
    rec = await db.add_habit(ctx.guild.id, key, name, category, xp, ctx.author.id)
    cfg = await get_config(ctx.guild.id)
    await reply_embed(
        ctx,
        "Habit Added",
        (
            f"Name: `{rec.name}`\n"
            f"Poll: {resolve_poll_label(cfg, rec.category)} ({poll_slot_for_type(rec.category)})\n"
            f"Key: `{rec.key}`\n"
            f"XP: `{rec.xp}`"
        ),
        color=0x2ECC71,
    )


@bot.command(name="addhabits")
@commands.guild_only()
@admin_guard()
async def cmd_addhabits(ctx: commands.Context, *, raw: str) -> None:
    assert ctx.guild is not None
    try:
        category, names, xp = parse_addhabits(raw)
    except ValueError as exc:
        await reply_embed(
            ctx,
            "Add Habits Failed",
            f"{exc}\nUsage: `{BOT_PREFIX}addhabits poll1 math physics chemistry 30`",
            color=0xE74C3C,
        )
        return

    active_count = await db.count_habits(ctx.guild.id, category=category, active_only=True)
    added: List[str] = []
    restored: List[str] = []
    skipped: List[str] = []
    limited: List[str] = []

    for idx, name in enumerate(names):
        if active_count >= MAX_HABITS_PER_CATEGORY:
            limited.extend(names[idx:])
            break
        key_base = slugify(name)
        if not key_base:
            skipped.append(name)
            continue
        existing = await db.get_habit(ctx.guild.id, key_base)
        if existing and existing.active == 0:
            rec = await db.reactivate_habit(ctx.guild.id, key_base, name, category, xp)
            if rec:
                restored.append(rec.name)
                active_count += 1
            else:
                skipped.append(name)
            continue
        if existing and existing.active == 1:
            key = key_base
            suffix = 1
            while existing:
                suffix += 1
                key = f"{key_base}_{suffix}"
                existing = await db.get_habit(ctx.guild.id, key)
            rec = await db.add_habit(ctx.guild.id, key, name, category, xp, ctx.author.id)
            added.append(rec.name)
            active_count += 1
            continue
        rec = await db.add_habit(ctx.guild.id, key_base, name, category, xp, ctx.author.id)
        added.append(rec.name)
        active_count += 1

    cfg = await get_config(ctx.guild.id)
    summary = [
        f"Poll: {resolve_poll_label(cfg, category)} ({poll_slot_for_type(category)})",
        f"XP per habit: {xp}",
        f"Added: {len(added)}",
        f"Restored: {len(restored)}",
        f"Skipped: {len(skipped)}",
    ]
    if limited:
        summary.append(f"Limit reached. Remaining not added: {len(limited)}")
    details = []
    if added:
        details.append(f"Added: {', '.join(added[:10])}{'...' if len(added) > 10 else ''}")
    if restored:
        details.append(f"Restored: {', '.join(restored[:10])}{'...' if len(restored) > 10 else ''}")
    if skipped:
        details.append(f"Skipped: {', '.join(skipped[:10])}{'...' if len(skipped) > 10 else ''}")
    body = "\n".join(summary + details)
    await reply_embed(ctx, "Habits Added", body, color=0x2ECC71)


@bot.command(name="removehabit")
@commands.guild_only()
@admin_guard()
async def cmd_removehabit(ctx: commands.Context, *, query: str) -> None:
    assert ctx.guild is not None
    q = query.strip().lower()
    if q in PRAYER_KEY_ALIASES:
        query = PRAYER_KEY_ALIASES[q]
    rec = await db.find_habit(ctx.guild.id, query)
    if not rec or rec.active == 0:
        await reply_embed(ctx, "Habit Not Found", "No active habit matched that name/key.", color=0xE74C3C)
        return
    if rec.category in {"general", "islamic"} and await db.count_habits(ctx.guild.id, category=rec.category, active_only=True) <= 1:
        await reply_embed(
            ctx,
            "Removal Blocked",
            f"Cannot remove last active habit in {resolve_poll_label(await get_config(ctx.guild.id), rec.category)}.",
            color=0xE67E22,
        )
        return
    ok = await db.deactivate_habit(ctx.guild.id, rec.key)
    if ok:
        await reply_embed(ctx, "Habit Removed", f"Removed `{rec.name}` from active habits.", color=0x2ECC71)
    else:
        await reply_embed(ctx, "Removal Failed", "Could not remove habit.", color=0xE74C3C)


@bot.command(name="removeallhabits")
@commands.guild_only()
@admin_guard()
async def cmd_removeallhabits(ctx: commands.Context, poll: str) -> None:
    assert ctx.guild is not None
    poll_type = normalize_poll_type(poll)
    if not poll_type:
        await reply_embed(
            ctx,
            "Invalid Poll",
            "Use one of: `poll1`, `poll2`, `poll3` (or `prayers`, `general`, `islamic`).",
            color=0xE74C3C,
        )
        return
    count = await db.deactivate_habits_in_category(ctx.guild.id, poll_type)
    if count <= 0:
        await reply_embed(ctx, "Nothing Removed", "No active habits to remove.", color=0x95A5A6)
        return
    await reply_embed(
        ctx,
        "Habits Removed",
        f"Removed `{count}` habit(s) from {resolve_poll_label(await get_config(ctx.guild.id), poll_type)}.",
        color=0x2ECC71,
    )


@bot.command(name="listhabits")
@commands.guild_only()
async def cmd_listhabits(ctx: commands.Context, category: Optional[str] = None) -> None:
    assert ctx.guild is not None
    cfg = await get_config(ctx.guild.id)
    cat = normalize_poll_type(category) if category else None
    if category and not cat:
        await reply_embed(
            ctx,
            "Invalid Category",
            "Use `poll1`, `poll2`, `poll3`, `prayers`, `general`, or `islamic`.",
            color=0xE74C3C,
        )
        return
    rows = await db.list_habits(ctx.guild.id, category=cat, active_only=True)

    desc_lines: List[str] = []
    if rows:
        for h in rows:
            desc_lines.append(f"[{h.category}] {h.name} | key={h.key} | xp={h.xp}")

    if not desc_lines:
        await reply_embed(ctx, "Habits", "No active habits.", color=0x95A5A6)
        return

    body = "\n".join(desc_lines)
    if len(body) > 3900:
        body = body[:3900] + "\n...truncated..."
    title = "Habits"
    if cat:
        title = f"Habits - {resolve_poll_label(cfg, cat)}"
    await reply_embed(ctx, title, body, color=0x3498DB)


@bot.command(name="pollnow")
@commands.guild_only()
@admin_guard()
async def cmd_pollnow(ctx: commands.Context) -> None:
    assert ctx.guild is not None
    cfg = await get_config(ctx.guild.id)
    if not cfg or not cfg.poll_channel_id:
        await reply_embed(ctx, "Poll Channel Missing", "Run setup first or set a poll channel.", color=0xE74C3C)
        return
    today = date_str_local(cfg.timezone)
    if await db.poll_exists_for_date(ctx.guild.id, today):
        await reply_embed(ctx, "Poll Exists", f"A poll already exists for `{today}`.", color=0xE67E22)
        return
    g = await post_daily_polls(ctx.guild, force_date=today, force=True)
    if g:
        await reply_embed(ctx, "Poll Posted", f"Created polls for `{today}`.", color=0x2ECC71)
    else:
        await reply_embed(ctx, "Poll Failed", "Could not post poll. Check channel permissions and logs.", color=0xE74C3C)


@bot.command(name="closetoday")
@commands.guild_only()
@admin_guard()
async def cmd_closetoday(ctx: commands.Context) -> None:
    assert ctx.guild is not None
    cfg = await get_config(ctx.guild.id)
    if not cfg:
        await reply_embed(ctx, "Config Missing", "Server config not found.", color=0xE74C3C)
        return
    today = date_str_local(cfg.timezone)
    groups = await db.list_open_poll_groups(ctx.guild.id)
    n = 0
    for g in groups:
        if g.poll_date == today:
            await announce_and_close_group(ctx.guild, g)
            n += 1
    if n:
        await reply_embed(ctx, "Polls Closed", f"Closed `{n}` open poll group(s) for `{today}`.", color=0x2ECC71)
    else:
        await reply_embed(ctx, "Nothing To Close", "No open groups for today.", color=0x95A5A6)


@bot.command(name="leaderboard")
@commands.guild_only()
async def cmd_leaderboard(ctx: commands.Context) -> None:
    assert ctx.guild is not None
    await refresh_leaderboard(ctx.guild, force=True)
    rows = await db.top_xp(ctx.guild.id, 10)
    if not rows:
        await reply_embed(ctx, "Leaderboard", "No leaderboard data yet.", color=0x95A5A6)
        return
    lines = []
    for i, (uid, xp) in enumerate(rows, 1):
        m = ctx.guild.get_member(uid)
        name = m.display_name if m else f"user:{uid}"
        lines.append(f"{rank_badge(i)} {name} | {xp} XP | L{level_from_total_xp(xp)}")
    await reply_embed(ctx, LEADERBOARD_TITLE, "\n".join(lines), color=0xF1C40F)


@bot.command(name="streaks")
@commands.guild_only()
async def cmd_streaks(ctx: commands.Context) -> None:
    assert ctx.guild is not None
    await refresh_streak_board(ctx.guild, force=True)
    rows = await db.list_streaks(ctx.guild.id)
    if not rows:
        await reply_embed(ctx, "Streaks", "No streak data.", color=0x95A5A6)
        return
    lines = []
    for i, r in enumerate(rows[:10], 1):
        uid = safe_int(r["user_id"])
        m = ctx.guild.get_member(uid)
        name = m.display_name if m else f"user:{uid}"
        lines.append(
            f"{rank_badge(i)} {name} | current {safe_int(r['current_streak'])} | longest {safe_int(r['longest_streak'])}"
        )
    await reply_embed(ctx, STREAK_TITLE, "\n".join(lines), color=0xF1C40F)


@bot.command(name="rebuildstreaks")
@commands.guild_only()
@admin_guard()
async def cmd_rebuildstreaks(ctx: commands.Context) -> None:
    assert ctx.guild is not None
    cfg = await get_config(ctx.guild.id)
    if not cfg:
        await reply_embed(ctx, "Rebuild Failed", "Config missing.", color=0xE74C3C)
        return
    await db.reset_streak_state(ctx.guild.id)
    rebuilt = await process_streaks_up_to(ctx.guild, date_prev(date_str_local(cfg.timezone)))
    await refresh_streak_board(ctx.guild, force=True)
    mark_st_dirty(ctx.guild.id)
    await reply_embed(
        ctx,
        "Streaks Rebuilt",
        f"Reprocessed `{rebuilt}` date bucket(s) using current streak rules.",
        color=0x2ECC71,
    )


@bot.command(name="myprogress")
@commands.guild_only()
async def cmd_myprogress(ctx: commands.Context, date_str: Optional[str] = None) -> None:
    assert ctx.guild is not None
    assert isinstance(ctx.author, discord.Member)
    cfg = await get_config(ctx.guild.id)
    if not cfg:
        await reply_embed(ctx, "Progress", "Config missing.", color=0xE74C3C)
        return

    target_date = parse_iso_date(date_str) if date_str else date_str_local(cfg.timezone)
    if not target_date:
        await reply_embed(ctx, "Invalid Date", "Use format `YYYY-MM-DD`.", color=0xE74C3C)
        return

    row = await db.get_daily_entry(ctx.guild.id, target_date, ctx.author.id)
    prayers = safe_int(row["prayers_count"]) if row else 0
    general = safe_int(row["general_count"]) if row else 0
    islamic = safe_int(row["islamic_count"]) if row else 0
    total_habits = safe_int(row["total_habits"]) if row else 0
    xp = safe_int(row["xp_earned"]) if row else 0
    qualified = qualifies_for_streak(prayers, total_habits, cfg)

    streak = await db.get_streak(ctx.guild.id, ctx.author.id)
    cur = safe_int(streak["current_streak"])
    longest = safe_int(streak["longest_streak"])

    end_dt = datetime.strptime(target_date, "%Y-%m-%d").date()
    start_dt = end_dt - timedelta(days=6)
    window_rows = await db.list_user_entries(
        ctx.guild.id,
        ctx.author.id,
        start_dt.strftime("%Y-%m-%d"),
        end_dt.strftime("%Y-%m-%d"),
    )
    q7 = 0
    xp7 = 0
    min_prayers, min_habits = streak_thresholds(cfg)
    for r in window_rows:
        xp7 += safe_int(r["xp_earned"])
        if qualifies_for_streak(safe_int(r["prayers_count"]), safe_int(r["total_habits"]), cfg):
            q7 += 1

    rank_txt = "N/A"
    board = await db.top_xp(ctx.guild.id, 2000)
    for idx, (uid, _) in enumerate(board, 1):
        if uid == ctx.author.id:
            rank_txt = str(idx)
            break

    desc = (
        f"Date: `{target_date}`\n"
        f"XP: `{xp}`\n"
        f"Prayers: `{prayers}`\n"
        f"Habits: `{total_habits}` (general `{general}` + islamic `{islamic}`)\n"
        f"Qualified Today: `{'Yes' if qualified else 'No'}`\n"
        f"Streak: current `{cur}` | longest `{longest}`\n"
        f"7-Day Qualified: `{q7}/7` {progress_bar(q7, 7)}\n"
        f"Rule: prayers>={min_prayers}, habits>={min_habits}\n"
        f"7-Day XP: `{xp7}`\n"
        f"Leaderboard Rank: `{rank_txt}`"
    )
    await reply_embed(ctx, f"{ctx.author.display_name} Progress", desc, color=0x5865F2)


@bot.command(name="status")
@commands.guild_only()
async def cmd_status(ctx: commands.Context) -> None:
    assert ctx.guild is not None
    cfg = await get_config(ctx.guild.id)
    if not cfg:
        await reply_embed(ctx, "Status", "Config missing.", color=0xE74C3C)
        return
    unprocessed = await db.list_unprocessed_dates(ctx.guild.id, None)
    open_jobs = await db.list_open_poll_groups(ctx.guild.id)
    ph = await db.count_habits(ctx.guild.id, "prayers", True)
    gh = await db.count_habits(ctx.guild.id, "general", True)
    ih = await db.count_habits(ctx.guild.id, "islamic", True)
    min_prayers, min_habits = streak_thresholds(cfg)
    lines = [
        f"Timezone: {cfg.timezone}",
        f"Poll Channel: {cfg.poll_channel_id}",
        f"Leaderboard Channel: {cfg.leaderboard_channel_id}",
        f"Streak Channel: {cfg.streak_channel_id}",
        f"Admin Role: {cfg.admin_role_id}",
        f"Last Poll Date: {cfg.last_poll_date}",
        f"Last Processed Date: {cfg.last_processed_date}",
        f"Poll Names: poll1={resolve_poll_label(cfg, 'prayers')} | poll2={resolve_poll_label(cfg, 'general')} | poll3={resolve_poll_label(cfg, 'islamic')}",
        f"Open Poll Jobs: {len(open_jobs)}",
        f"Unprocessed Dates: {len(unprocessed)}",
        f"Habits: poll1={ph}, poll2={gh}, poll3={ih}",
        f"Streak Rule: prayers>={min_prayers}, habits>={min_habits}",
        f"Queue Pending: {outbox.pending}",
        f"Scheduler Running: {'Yes' if scheduler_running else 'No'}",
    ]
    await reply_embed(ctx, "System Status", "\n".join(lines), color=0x34495E)


@bot.command(name="resetsystem")
@commands.guild_only()
@admin_guard()
async def cmd_resetsystem(ctx: commands.Context, confirm: str = "") -> None:
    assert ctx.guild is not None
    if confirm.lower() != "confirm":
        await reply_embed(ctx, "Confirmation Required", f"Use `{BOT_PREFIX}resetsystem confirm`.", color=0xE67E22)
        return
    await db.reset_guild(ctx.guild.id)
    await refresh_config(ctx.guild.id)
    await reply_embed(ctx, "System Reset", "System reset complete.", color=0x2ECC71)


# -----------------------------------------------------------------------------
# Bot events
# -----------------------------------------------------------------------------
@bot.event
async def on_command(ctx: commands.Context) -> None:
    if ctx.guild:
        LOGGER.info(
            "command guild=%s user=%s cmd=%s raw=%s",
            ctx.guild.id,
            ctx.author.id,
            ctx.command.qualified_name if ctx.command else "unknown",
            ctx.message.content,
        )


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CheckFailure):
        await reply_embed(ctx, "Permission Denied", "You are not allowed to run this command.", color=0xE74C3C)
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await reply_embed(ctx, "Missing Argument", f"Missing argument: `{error.param.name}`", color=0xE67E22)
        return
    if isinstance(error, commands.BadArgument):
        await reply_embed(ctx, "Invalid Argument", "Could not parse one of the arguments.", color=0xE67E22)
        return
    LOGGER.exception("Command error: %s", error)
    await reply_embed(ctx, "Command Failed", "Check `logs/errors.log` for details.", color=0xE74C3C)


@bot.event
async def on_ready() -> None:
    global started_once, scheduler_task, scheduler_running, last_backup_monotonic
    LOGGER.info("Bot ready as %s", bot.user)
    if started_once:
        return
    started_once = True

    await db.connect()
    await outbox.start()
    for g in bot.guilds:
        await ensure_config(g.id)
    restored = await restore_persistent_views()
    LOGGER.info("Restored persistent poll views: %s", restored)

    for g in bot.guilds:
        cfg = await get_config(g.id)
        if not cfg:
            continue
        today = date_str_local(cfg.timezone)
        await close_expired_groups(g)
        if cfg.poll_channel_id and not await db.poll_exists_for_date(g.id, today):
            await post_daily_polls(g, force_date=today, force=False)
        await process_streaks_up_to(g, date_prev(today))
        mark_lb_dirty(g.id)
        mark_st_dirty(g.id)

    scheduler_running = True
    last_backup_monotonic = time.monotonic()
    scheduler_task = asyncio.create_task(scheduler_loop())

    poll_jobs = await db.count_open_poll_groups()
    print("Bot Online")
    print(f"Connected Servers: {len(bot.guilds)}")
    print(f"Database Connected: {'Yes' if db.connected else 'No'}")
    print(f"Scheduler Running: {'Yes' if scheduler_running else 'No'}")
    print(f"Poll Jobs Loaded: {poll_jobs}")


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    await ensure_config(guild.id)
    text = (
        "Welcome to Moon's Reflection!\n"
        f"Start by running: `{BOT_PREFIX}setup #poll #leaderboard #streak [timezone]`.\n"
        f"Then manage habits with: `{BOT_PREFIX}addhabit <habit> <poll1|poll2|poll3>`, `{BOT_PREFIX}addhabits`, "
        f"`{BOT_PREFIX}removehabit`, `{BOT_PREFIX}removeallhabits`, `{BOT_PREFIX}listhabits`.\n"
        f"Rename poll categories with: `{BOT_PREFIX}renamepoll poll1 \"Prayer Tracker\"`.\n"
        f"Adjust streak strictness with: `{BOT_PREFIX}setstreakrules 0 0`."
    )
    sent = False
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        await send_message(guild.system_channel, content=text)
        sent = True
    if not sent:
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                await send_message(ch, content=text)
                sent = True
                break
    if not sent and guild.owner:
        try:
            await guild.owner.send(text)
        except Exception:
            pass


@bot.event
async def on_guild_remove(guild: discord.Guild) -> None:
    config_cache.pop(guild.id, None)


# -----------------------------------------------------------------------------
# Shutdown and entry point
# -----------------------------------------------------------------------------
async def shutdown() -> None:
    global scheduler_running
    scheduler_running = False
    if scheduler_task:
        scheduler_task.cancel()
        await asyncio.gather(scheduler_task, return_exceptions=True)
    await outbox.stop()
    await db.close()
    await bot.close()


def setup_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    def _handler() -> None:
        asyncio.create_task(shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            signal.signal(sig, lambda *_: asyncio.create_task(shutdown()))


async def main() -> None:
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN not found.")
    loop = asyncio.get_running_loop()
    setup_signal_handlers(loop)
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
