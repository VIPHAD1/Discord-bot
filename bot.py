# bot.py

# Daily system for 2 polls (Prayers + Habits) using select menus.
# 00:00 Asia/Karachi: post polls; next 00:00: compute + announce + pin/unpin + reset.
# Robust catch-up: if the bot wasn't up at midnight, it posts today's polls as soon as it boots.
# Daily summary in poll channel; all-time leaderboard in a separate channel (pinned & updated daily).

import discord
from discord.ext import commands, tasks
import asyncio, json, os, time, sqlite3, threading
from datetime import datetime
from collections import defaultdict
import pytz
from math import floor

# ---------------- CONFIG ----------------

TOKEN = "MTQwNDQxMDUwMDIyOTQ5Njg4Mg.GKcPus.Epg7rz7zMpRXQyHLJHmCpCBfO7Aud419vE5eJE"  # <-- REPLACE with your token string 
CHANNEL_ID = 1403752939709010051             # <--- poll channel id (int, NO quotes)
LEADERBOARD_CHANNEL_ID = 1403754754085093466 # <--- leaderboard channel id (int, NO quotes)

TIMEZONE = "Asia/Karachi"

POLL_DURATION_SECONDS = 24 * 3600

SEND_IMMEDIATELY_ON_START = True  # catch-up logic below already posts when needed; keep False to avoid duplicates

# Files (kept as logical keys for compatibility)
GROUPS_FILE = "active_groups.json"
XP_TOTALS_FILE = "xp_totals.json"
CONFIG_FILE = "config.json"

# SQLite DB path
DB_PATH = "habits.db"

# Pin markers (to find/unpin/update)
SUMMARY_TITLE = "📊 Daily XP Summary"
SUMMARY_FOOTER_TAG = "XP_SUMMARY_V1"

LEADERBOARD_TITLE = "🏆 All-Time Leaderboard (Levels)"
LEADERBOARD_FOOTER_TAG = "LEADERBOARD_V1"

# --------------- LEVEL CURVE ---------------
# Harder curve: only monsters reach Level 100
# Level = min(100, floor( (Total_XP / 150) ** 0.56 ))

def level_from_total_xp(total_xp: int) -> int:
    if total_xp <= 0:
        return 0
    return min(100, floor((total_xp / 150.0) ** 0.56))

# ---------------- UTIL ----------------

def epoch_now() -> int:
    return int(time.time())

def now_local() -> datetime:
    return datetime.now(pytz.timezone(TIMEZONE))

def midnight_local(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)

# ---------------- DB init & helpers ----------------
# We use a tiny KV table (k TEXT PRIMARY KEY, v TEXT) and store JSON blobs.
# This preserves the rest of your code that expects load_json/save_json to exist.

_db_conn = None
_db_thread_lock = threading.Lock()

def _init_db():
    global _db_conn
    if _db_conn is not None:
        return
    # check_same_thread=False so sqlite can be used from asyncio callbacks reasonably.
    _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = _db_conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS kv (
        k TEXT PRIMARY KEY,
        v TEXT
    )
    """)
    _db_conn.commit()

def load_json(path, default):
    """
    Backwards-compatible loader: reads JSON-like blobs from KV table keyed by 'path'.
    Falls back to file if DB is unavailable or content missing.
    """
    try:
        _init_db()
        with _db_thread_lock:
            c = _db_conn.cursor()
            c.execute("SELECT v FROM kv WHERE k = ?", (path,))
            row = c.fetchone()
            if row and row[0] is not None:
                try:
                    return json.loads(row[0])
                except Exception:
                    # corrupted JSON in DB -> fallback
                    pass
    except Exception:
        # fall through to file fallback
        pass

    # file fallback (legacy)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass

    return default

def save_json(path, data):
    """
    Backwards-compatible saver: stores JSON into kv table with key==path.
    Uses a thread lock to keep sqlite writes safe and avoids any asyncio loop shenanigans.
    Falls back to atomic file write if DB fails.
    """
    dump = None
    try:
        dump = json.dumps(data, indent=2, ensure_ascii=False)
    except Exception:
        try:
            dump = json.dumps(data, ensure_ascii=False)
        except Exception:
            dump = str(data)

    # Try DB-backed save (preferred)
    try:
        _init_db()
        with _db_thread_lock:
            c = _db_conn.cursor()
            # Use UPSERT (INSERT ... ON CONFLICT DO UPDATE)
            c.execute(
                "INSERT INTO kv(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (path, dump)
            )
            _db_conn.commit()
        return
    except Exception:
        # fallback to safe file write
        pass

    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(dump)
        os.replace(tmp, path)
    except Exception:
        # As last resort do nothing (original code swallowed errors)
        pass

# Persistent stores (in-memory mirrors)
active_groups = load_json(GROUPS_FILE, [])
xp_totals = load_json(XP_TOTALS_FILE, {})  # { user_id: int total_xp }

# ---------------- ITEMS & XP ----------------

# Prayers (boosted to 15 each; Jummah 50 only if Friday)
PRAYERS = [
    ("💖", "Fajr",    "fajr",    15),
    ("💛", "Dhuhr",   "dhuhr",   15),
    ("💚", "Asr",     "asr",     15),
    ("💙", "Maghrib", "maghrib", 15),
    ("🤍", "Isha",    "isha",    15),
    ("💎", "Jummah",  "jummah",  50),  # Friday only
]

# Habits (importance/effort weighted)
# NOTE: exactly <=25 items allowed by Discord select menu; Daily Reflection must appear as the last
HABITS = [
    # High-core (25–30)
    ("🥊", "Boxing / Any Other Combat Training",          "boxing",                30),
    ("💎", "One Hard Thing Daily",                        "one_hard_thing",        30),
    ("📜", "Quran & Sunnah Deep Dives",                   "quran_sunnah_deep",     30),
    ("🚀", "Personal Projects",                           "personal_projects",     25),
    ("🧠", "Learning Human Nature",                       "human_nature",          25),

    # Core growth (15–20)
    ("💪", "Workout / Gym",                               "workout",               20),
    ("⚽", "Football / Any Other Sport",                  "sports",                20),
    ("📖", "Quran Notes / Reading / Memorizing",          "quran_notes",           20),
    ("💻", "Programming",                                 "programming",           20),
    ("🎥", "Content Creation",                            "content_creation",      20),
    ("✍️", "Creative Writing / Thinking / Mind Map",      "creative_writing",      20),
    ("🕵️", "Detective-like Observation & Log",            "detective_observation", 20),

    ("🎓", "Academics",                                   "academics",             15),
    ("🎬", "Editing",                                     "editing",               15),
    ("🎨", "Painting / Sketching",                        "painting",              15),
    ("♟️", "Chess",                                       "chess",                 15),
    ("🗂️", "Spaced Repetition (Anki)",                    "anki",                  15),
    ("🥗", "Healthy Diet",                                "healthy_diet",          15),
    ("😴", "Quality Sleep (7–8 hrs)",                     "quality_sleep",         15),

    # Foundations (5–10)
    ("📔", "Journaling",                                  "journaling",            10),
    ("📚", "Reading (Fiction / Non-Fiction)",             "reading",               10),
    ("📝", "Planning a To-Do List",                       "todo_planning",         10),
    ("✨", "Dhikr / Tasbeeh",                             "dhikr",                 10),
    ("💧", "Good Water Intake (7 Glass)",                 "water_intake",           5),

    # Explicitly placed last so it appears in the habit poll
    ("📝", "Daily Reflection",                            "daily_reflection",      25),
]

PRAYER_MAP = {k: xp for _, _, k, xp in PRAYERS}
HABIT_MAP  = {k: xp for _, _, k, xp in HABITS}

# ---------------- DISCORD ----------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- Select Menus ----------

def to_select_options(items):
    opts = []
    for emoji, label, key, xp in items:
        opts.append(discord.SelectOption(
            label=f"{label} ({xp} XP)",
            value=key,
            emoji=emoji
        ))
    return opts

class PollView(discord.ui.View):
    """Reusable view for a single select menu (multi-select)."""
    def __init__(self, poll_key: str, group_id: str, options: list, max_select: int):
        super().__init__(timeout=None)
        self.poll_key = poll_key
        self.group_id = group_id
        # ensure max_select <= 25 (Discord limit)
        safe_max = max(1, min(max_select, 25))
        self.select = discord.ui.Select(
            placeholder="Select all that you completed today…",
            min_values=0,
            max_values=safe_max,
            options=options,
            custom_id=f"poll:{poll_key}:{group_id}",
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        # Respond fast to avoid "This interaction failed"
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        user_id = str(interaction.user.id)

        # get selected values robustly
        try:
            values = interaction.data.get("values", []) if isinstance(interaction.data, dict) else list(self.select.values)
        except Exception:
            values = list(self.select.values)

        values = list(dict.fromkeys(values))  # unique preserve order

        # Save into today's active group
        changed = False
        group = None
        for g in active_groups:
            if g.get("group_id") == self.group_id and not g.get("processed", False):
                g.setdefault("selections", {}).setdefault(self.poll_key, {})
                g["selections"][self.poll_key][user_id] = values
                group = g
                changed = True
                break

        if changed:
            # Persist selections first
            save_json(GROUPS_FILE, active_groups)

            # --- NEW: idempotent XP awarding per-user for this group ---
            try:
                group_dt_local = datetime.fromtimestamp(group.get("created_at", epoch_now()), pytz.timezone(TIMEZONE))
                p_keys = group.get("selections", {}).get("prayers", {}).get(user_id, [])
                h_keys = group.get("selections", {}).get("habits", {}).get(user_id, [])
                new_daily_total = award_for_prayers(p_keys, group_dt_local) + award_for_habits(h_keys)

                prev_awarded = int(group.setdefault("awarded", {}).get(user_id, 0))
                delta = int(new_daily_total) - int(prev_awarded)

                if delta != 0:
                    xp_totals[user_id] = max(0, int(xp_totals.get(user_id, 0)) + int(delta))
                    group.setdefault("awarded", {})[user_id] = int(new_daily_total)
                    save_json(XP_TOTALS_FILE, xp_totals)
                    save_json(GROUPS_FILE, active_groups)

                    # FIXED: Removed silent exception handling that was hiding leaderboard update errors
                    # This was the main cause of the leaderboard not updating issue
                    if interaction.guild:
                        await update_all_time_leaderboard(interaction.guild)
            except Exception:
                pass

            # ----------------- STREAKS: immediate upsert & provisional apply -----------------
            try:
                # compute date string & counts for this group's created date
                group_dt_local = datetime.fromtimestamp(group.get("created_at", epoch_now()), pytz.timezone(TIMEZONE))
                date_str = group_dt_local.strftime("%Y-%m-%d")

                # compute prayer & habit counts for this user (use helper functions defined later)
                p_count = compute_prayer_count_from_keys(p_keys, group_dt_local)
                h_count = compute_habit_count_from_keys(h_keys)

                # upsert daily streak row (processed stays 0 until authoritative pass)
                try:
                    upsert_daily_streak(date_str, user_id, p_count, h_count, processed=0)
                except Exception:
                    pass

                # apply immediate provisional streak update (optimistic, will be corrected at midnight if needed)
                try:
                    apply_immediate_provisional_streak(user_id, date_str)
                except Exception:
                    pass

                # schedule an immediate edit of the all-time streak embed so users see it
                try:
                    if interaction.guild:
                        await edit_alltime_embed(interaction.guild)
                except Exception:
                    # best-effort; ignore failures here
                    pass
            except Exception:
                pass
            # -------------------------------------------------------------------------------

        pretty = ", ".join(values) if values else "none"

        try:
            await interaction.followup.send(
                f"✅ Saved your selections for **{self.poll_key}**: `{pretty}`",
                ephemeral=True
            )
        except Exception:
            pass

# ---------- Posting Polls ----------

async def post_todays_polls(channel: discord.TextChannel):
    """Post both polls and register active group."""
    group_id = str(epoch_now() + POLL_DURATION_SECONDS)
    end_ts   = epoch_now() + POLL_DURATION_SECONDS

    # Create group early and save to avoid duplicate postings if send partially fails
    group = {
        "group_id": group_id,
        "channel_id": channel.id,
        "prayers_msg_id": None,
        "habits_msg_id": None,
        "created_at": epoch_now(),
        "end_time": end_ts,
        "processed": False,
        "send_error": False,
        "selections": {
            "prayers": {},   # user_id -> [keys]
            "habits":  {},   # user_id -> [keys]
        }
    }

    active_groups.append(group)
    save_json(GROUPS_FILE, active_groups)

    # Prayers
    try:
        p_opts = to_select_options(PRAYERS)
        p_view = PollView("prayers", group_id, p_opts, max_select=len(p_opts))
        p_embed = discord.Embed(
            title="🕌 Daily Prayers — Select What You Prayed",
            description="Every completed habit earns you XP — **All entries are tracked per-user and shown in your daily summary**",
            color=0x1ABC9C
        )
        prayers_msg = await channel.send(embed=p_embed, view=p_view)
        for g in active_groups:
            if g.get("group_id") == group_id:
                g["prayers_msg_id"] = prayers_msg.id
                save_json(GROUPS_FILE, active_groups)
                break
    except Exception as e:
        print("Error sending prayers poll:", e)
        for g in active_groups:
            if g.get("group_id") == group_id:
                g["send_error"] = True
                g["processed"] = True
                save_json(GROUPS_FILE, active_groups)
                break
        return None

    # Habits
    try:
        h_opts = to_select_options(HABITS)
        h_view = PollView("habits", group_id, h_opts, max_select=len(h_opts))
        h_embed = discord.Embed(
            title="🔥 Daily Habits — Select What You Completed",
            description="Every completed habit earns you XP — **All entries are tracked per-user and shown in your daily summary**",
            color=0xE67E22
        )
        habits_msg = await channel.send(embed=h_embed, view=h_view)
        for g in active_groups:
            if g.get("group_id") == group_id:
                g["habits_msg_id"] = habits_msg.id
                save_json(GROUPS_FILE, active_groups)
                break
    except Exception as e:
        print("Error sending habits poll:", e)
        for g in active_groups:
            if g.get("group_id") == group_id:
                g["send_error"] = True
                g["processed"] = True
                save_json(GROUPS_FILE, active_groups)
                break
        return None

    return group

# ---------- XP Calculation ----------

def award_for_prayers(keys, date_local: datetime) -> int:
    if not keys:
        return 0
    total = 0
    is_friday = (date_local.weekday() == 4)  # Monday=0
    for k in set(keys):
        if k == "jummah" and not is_friday:
            continue
        total += PRAYER_MAP.get(k, 0)
    return total

def award_for_habits(keys) -> int:
    return sum(HABIT_MAP.get(k, 0) for k in set(keys or []))

def rank_with_ties(sorted_pairs):
    result = []
    prev_xp = None
    rank = 0
    i = 0
    for uid, xp in sorted_pairs:
        i += 1
        if xp != prev_xp:
            rank = i
            prev_xp = xp
        result.append((rank, uid, xp))
    return result

# ---------- Pins & Leaderboard ----------

async def unpin_previous_summary(channel: discord.TextChannel):
    try:
        pins = await channel.pins()
    except Exception:
        return
    for msg in pins:
        if msg.author == bot.user and msg.embeds:
            e = msg.embeds[0]
            if e.title == SUMMARY_TITLE and e.footer and e.footer.text and SUMMARY_FOOTER_TAG in e.footer.text:
                try:
                    await msg.unpin()
                except Exception:
                    pass

async def get_or_create_leaderboard_message(channel: discord.TextChannel):
    try:
        pins = await channel.pins()
    except Exception:
        pins = []
    for msg in pins:
        if msg.author == bot.user and msg.embeds:
            e = msg.embeds[0]
            if e.title == LEADERBOARD_TITLE and e.footer and e.footer.text and LEADERBOARD_FOOTER_TAG in e.footer.text:
                return msg

    embed = discord.Embed(
        title=LEADERBOARD_TITLE,
        description="Initializing leaderboard...",
        color=0xFFD700
    )
    embed.set_footer(text=f"{LEADERBOARD_FOOTER_TAG} • Created {now_local().strftime('%Y-%m-%d %H:%M %Z')}")
    msg = await channel.send(embed=embed)
    try:
        await msg.pin()
    except Exception:
        pass

    for m in pins:
        try:
            if m.author == bot.user and m.embeds and m.embeds[0].title == LEADERBOARD_TITLE and m.id != msg.id:
                await m.unpin()
        except Exception:
            pass

    return msg

async def update_all_time_leaderboard(guild):
    channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
    if not channel:
        print("Leaderboard channel not found or bot lacks access. Check LEADERBOARD_CHANNEL_ID and bot permissions.")
        return

    members = {str(m.id): m for m in guild.members if not m.bot}
    entries = []
    for uid, total in xp_totals.items():
        if uid in members:
            entries.append((uid, int(total)))

    if not entries:
        msg = await get_or_create_leaderboard_message(channel)
        embed = discord.Embed(
            title=LEADERBOARD_TITLE,
            description="No data yet — complete habits to climb the board!",
            color=0xFFD700
        )
        embed.set_footer(text=f"{LEADERBOARD_FOOTER_TAG} • Updated {now_local().strftime('%Y-%m-%d %H:%M %Z')}")
        try:
            await msg.edit(embed=embed)
        except Exception as e:
            print("Failed to edit/create leaderboard message:", e)
        return

    def name_of(uid):
        m = members.get(uid)
        return (m.display_name or m.name).lower() if m else ""
    entries.sort(key=lambda kv: (-kv[1], name_of(kv[0])))

    lines = []
    for i, (uid, total) in enumerate(entries, start=1):
        m = members.get(uid)
        name = m.mention if m else f"<@{uid}>"
        lv = level_from_total_xp(total)
        prefix = "🥇 " if i == 1 else "🥈 " if i == 2 else "🥉 " if i == 3 else ""
        lines.append(f"**{i}.** {prefix}{name} — **{total} XP** • L{lv}")
        if i >= 100:
            lines.append(f"...and {len(entries) - 100} more")
            break

    desc = "\n".join(lines)
    msg = await get_or_create_leaderboard_message(channel)
    embed = discord.Embed(
        title=LEADERBOARD_TITLE,
        description=desc,
        color=0xFFD700
    )
    embed.set_footer(text=f"{LEADERBOARD_FOOTER_TAG} • Updated {now_local().strftime('%Y-%m-%d %H:%M %Z')}")
    try:
        await msg.edit(embed=embed)
    except Exception as e:
        print("Failed to update leaderboard message:", e)

# ---------- Announce + Reset ----------

async def announce_daily_results_and_reset(group: dict):
    if group.get("processed"):
        return
    channel = bot.get_channel(group["channel_id"])
    if not channel:
        print("announce_daily_results_and_reset: channel not found for group", group.get("group_id"))
        return

    sel = group.get("selections", {})
    prayers_sel = sel.get("prayers", {})
    habits_sel  = sel.get("habits", {})

    group_dt_local = datetime.fromtimestamp(group["created_at"], pytz.timezone(TIMEZONE))
    date_label = group_dt_local.strftime("%Y-%m-%d")

    daily_xp = defaultdict(int)
    guild = channel.guild
    members = [m for m in guild.members if not m.bot]

    all_voters = set(prayers_sel.keys()) | set(habits_sel.keys())
    for uid in all_voters:
        p_keys = prayers_sel.get(uid, [])
        h_keys = habits_sel.get(uid, [])
        daily_xp[uid] += award_for_prayers(p_keys, group_dt_local)
        daily_xp[uid] += award_for_habits(h_keys)

    for m in members:
        uid = str(m.id)
        daily_xp.setdefault(uid, 0)

    names = {str(m.id): (m.display_name or m.name) for m in members}
    sorted_pairs = sorted(daily_xp.items(), key=lambda kv: (-kv[1], names.get(kv[0], "").lower()))
    ranked = rank_with_ties(sorted_pairs)

    awarded_map = group.get("awarded", {}) or {}
    for uid, gained in daily_xp.items():
        if str(uid) in awarded_map:
            continue
        xp_totals[uid] = int(xp_totals.get(uid, 0)) + int(gained)

    save_json(XP_TOTALS_FILE, xp_totals)

    lines = []
    for rank, uid, xp_today in ranked:
        name = names.get(uid, f"User {uid}")
        prefix = "🥇 " if rank == 1 else "🥈 " if rank == 2 else "🥉 " if rank == 3 else ""
        total = int(xp_totals.get(uid, 0))
        lv = level_from_total_xp(total)
        lines.append(f"**{rank}.** {prefix}<@{uid}> — **{xp_today} XP today** • Total **{total}** • L{lv}")

    desc = "\n".join(lines) if lines else "No participants."

    try:
        await unpin_previous_summary(channel)
    except Exception:
        pass

    embed = discord.Embed(
        title=SUMMARY_TITLE + f" — {date_label}",
        description=desc,
        color=0x5865F2
    )
    embed.set_footer(text=f"{SUMMARY_FOOTER_TAG} • Updated {now_local().strftime('%Y-%m-%d %H:%M %Z')}")

    try:
        msg = await channel.send(content="@everyone", embed=embed)
        try:
            await msg.pin()
        except Exception:
            pass
    except Exception as e:
        print("Failed to send/ pin daily summary:", e)

    group["processed"] = True
    save_json(GROUPS_FILE, active_groups)

    try:
        await update_all_time_leaderboard(guild)
    except Exception as e:
        print("Leaderboard update error:", e)

# ---------- Helpers ----------

def polls_exist_since(ts_cutoff: int) -> bool:
    for g in active_groups:
        if not g.get("processed", False) and g.get("created_at", 0) >= ts_cutoff:
            return True
    return False

def cleanup_old_processed_groups(retain_days: int = 10):
    global active_groups
    now_ts = epoch_now()
    keep = []
    for g in active_groups:
        if not g.get("processed", False):
            keep.append(g)
        else:
            if now_ts - g.get("end_time", now_ts) <= retain_days * 86400:
                keep.append(g)
    if len(keep) != len(active_groups):
        active_groups = keep
        save_json(GROUPS_FILE, active_groups)

# ---------- Schedulers ----------

@tasks.loop(minutes=1)
async def midnight_cycle():
    now = now_local()
    if now.hour == 0 and now.minute == 0:
        ts = epoch_now()
        changed = False
        for g in list(active_groups):
            if not g.get("processed", False) and ts >= g.get("end_time", 0):
                try:
                    await announce_daily_results_and_reset(g)
                except Exception as e:
                    print("Error processing group:", e)
                changed = True
        if changed:
            save_json(GROUPS_FILE, active_groups)

        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            cutoff = int(midnight_local(now).timestamp())
            async with bot._posting_lock:
                if not polls_exist_since(cutoff):
                    await post_todays_polls(channel)

        cleanup_old_processed_groups(retain_days=10)

@tasks.loop(minutes=30)
async def daytime_failsafe():
    now = now_local()
    cutoff = int(midnight_local(now).timestamp())
    if not polls_exist_since(cutoff):
        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            try:
                async with bot._posting_lock:
                    if not polls_exist_since(cutoff):
                        await post_todays_polls(channel)
                        print("⚡ Failsafe posted today's polls.")
            except Exception as e:
                print("Failsafe error:", e)

# ---------- Commands ----------

@bot.command(name="pollnow")
@commands.has_permissions(manage_guild=True)
async def cmd_pollnow(ctx: commands.Context):
    cutoff = int(midnight_local(now_local()).timestamp())
    if polls_exist_since(cutoff):
        await ctx.reply("A poll for today already exists.")
        return
    async with bot._posting_lock:
        if polls_exist_since(cutoff):
            await ctx.reply("A poll for today already exists.")
            return
        group = await post_todays_polls(ctx.channel)
        if group:
            await ctx.reply(f"✅ Posted today's polls (group {group['group_id']}).")
        else:
            await ctx.reply("❌ Failed to post today's polls — check bot console for errors.")

@bot.command(name="closetoday")
@commands.has_permissions(manage_guild=True)
async def cmd_closetoday(ctx: commands.Context):
    ts = epoch_now()
    closed_any = False
    for g in list(active_groups):
        if not g.get("processed", False):
            try:
                await announce_daily_results_and_reset(g)
                closed_any = True
            except Exception as e:
                print("Error force-closing group:", e)
    if closed_any:
        try:
            await update_all_time_leaderboard(ctx.guild)
        except Exception as e:
            print("Error updating leaderboard after closetoday:", e)
        await ctx.reply("🔒 Closed and announced today's results.")
    else:
        await ctx.reply("No active group found.")

@bot.command(name="leaderboard")
async def cmd_leaderboard(ctx: commands.Context):
    await update_all_time_leaderboard(ctx.guild)
    members = {str(m.id): m for m in ctx.guild.members if not m.bot}
    entries = [(uid, int(total)) for uid, total in xp_totals.items() if uid in members]
    if not entries:
        await ctx.reply("No data yet — complete habits to climb the board!")
        return
    entries.sort(key=lambda kv: (-kv[1], (members[kv[0]].display_name or members[kv[0]].name).lower()))
    top10 = entries[:10]
    lines = []
    for i, (uid, total) in enumerate(top10, start=1):
        m = members[uid]
        lv = level_from_total_xp(total)
        prefix = "🥇 " if i == 1 else "🥈 " if i == 2 else "🥉 " if i == 3 else ""
        lines.append(f"**{i}.** {prefix}{m.mention} — **{total} XP** • L{lv}")
    await ctx.reply("\n".join(lines))

# ---------- Ready ----------

@bot.event
async def on_ready():
    print(f"✅ Bot ready as {bot.user} (ID {bot.user.id})")
    if not hasattr(bot, "_posting_lock") or bot._posting_lock is None:
        bot._posting_lock = asyncio.Lock()
    if not midnight_cycle.is_running():
        midnight_cycle.start()
    if not daytime_failsafe.is_running():
        daytime_failsafe.start()

    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    today_midnight = midnight_local(now)

    channel = bot.get_channel(CHANNEL_ID)
    if channel and now > today_midnight:
        cutoff = int(today_midnight.timestamp())
        async with bot._posting_lock:
            if not polls_exist_since(cutoff):
                await post_todays_polls(channel)
                print("⚡ Missed midnight poll -> posted catch-up poll now!")

    if SEND_IMMEDIATELY_ON_START:
        if channel:
            cutoff = int(today_midnight.timestamp())
            async with bot._posting_lock:
                if not polls_exist_since(cutoff):
                    await post_todays_polls(channel)




# -------------------------------------------------------------------------

# ------------------------ STREAK SYSTEM (ADDED) --------------------------

# -------------------------------------------------------------------------

# This block is appended below all existing logic and does not modify

# any of your pre-existing functions. It provides:

#  - SQLite-backed streak tables (user_streaks & daily_streaks)

#  - Real-time upserts of daily entries (scans active_groups)

#  - Immediate provisional updates when a user qualifies

#  - Midnight/catch-up authoritative processing (rebuilds/resolves streaks)

#  - Weekly summary message (posted once per week; Monday 00:00, Asia/Karachi)

#  - All-time leaderboard message (single message edited in-place)

#

# Channel used for both streak messages:

STREAK_CHANNEL_ID = 1403775276344807474

STREAK_ALLTIME_TITLE = "🔥 All-Time Streak Leaderboard"

STREAK_ALLTIME_FOOTER_TAG = "STREAKS_ALLTIME_V1"

STREAK_WEEKLY_TITLE = "📅 Weekly Streak Summary"

STREAK_WEEKLY_FOOTER_TAG = "STREAKS_WEEKLY_V1"



# streak criteria

MIN_PRAYERS_FOR_STREAK = 5     # 5 or 6 counts

MIN_HABITS_FOR_STREAK = 5      # changed to 5 per your requirement (>= 5)



# imports required by streak subsystem

from datetime import timedelta



# initialize new DB tables for streaks in same habits.db

def ensure_streak_tables():

    _init_db()

    with _db_thread_lock:

        c = _db_conn.cursor()

        c.execute("""

        CREATE TABLE IF NOT EXISTS user_streaks (

            user_id TEXT PRIMARY KEY,

            current_streak INTEGER DEFAULT 0,

            longest_streak INTEGER DEFAULT 0,

            last_date TEXT

        )

        """)

        c.execute("""

        CREATE TABLE IF NOT EXISTS daily_streaks (

            date TEXT,

            user_id TEXT,

            prayers INTEGER,

            habits INTEGER,

            processed INTEGER DEFAULT 0,

            PRIMARY KEY (date, user_id)

        )

        """)

        _db_conn.commit()



# helper: convert epoch created_at from group to date string in timezone

def _date_str_from_group_created_at(group):

    try:

        ts = int(group.get("created_at", epoch_now()))

        dt = datetime.fromtimestamp(ts, pytz.timezone(TIMEZONE))

        return dt.strftime("%Y-%m-%d")

    except Exception:

        return now_local().strftime("%Y-%m-%d")



# DB operations for daily_streaks and user_streaks

def upsert_daily_streak(date_str, user_id, prayers_count, habits_count, processed=0):

    """Insert or update a daily_streak row for (date, user_id)."""

    ensure_streak_tables()

    with _db_thread_lock:

        c = _db_conn.cursor()

        c.execute("""

        INSERT INTO daily_streaks(date, user_id, prayers, habits, processed)

        VALUES (?, ?, ?, ?, ?)

        ON CONFLICT(date, user_id) DO UPDATE SET

          prayers=excluded.prayers,

          habits=excluded.habits,

          processed=CASE WHEN excluded.processed=1 THEN 1 ELSE daily_streaks.processed END

        """, (date_str, str(user_id), int(prayers_count), int(habits_count), int(processed)))

        _db_conn.commit()



def get_daily_streak_entry(date_str, user_id):

    ensure_streak_tables()

    with _db_thread_lock:

        c = _db_conn.cursor()

        c.execute("SELECT prayers, habits, processed FROM daily_streaks WHERE date=? AND user_id=?", (date_str, str(user_id)))

        row = c.fetchone()

        if row:

            return {"prayers": row[0], "habits": row[1], "processed": int(row[2])}

        return None



def mark_daily_processed(date_str, user_id):

    ensure_streak_tables()

    with _db_thread_lock:

        c = _db_conn.cursor()

        c.execute("UPDATE daily_streaks SET processed=1 WHERE date=? AND user_id=?", (date_str, str(user_id)))

        _db_conn.commit()



def get_unprocessed_dates(upto_date=None):

    """Return sorted list of distinct dates (strings) with processed=0 and optionally <= upto_date"""

    ensure_streak_tables()

    with _db_thread_lock:

        c = _db_conn.cursor()

        if upto_date:

            c.execute("SELECT DISTINCT date FROM daily_streaks WHERE processed=0 AND date <= ? ORDER BY date ASC", (upto_date,))

        else:

            c.execute("SELECT DISTINCT date FROM daily_streaks WHERE processed=0 ORDER BY date ASC")

        rows = c.fetchall()

        return [r[0] for r in rows]



def get_all_user_streak(user_id):

    ensure_streak_tables()

    with _db_thread_lock:

        c = _db_conn.cursor()

        c.execute("SELECT current_streak, longest_streak, last_date FROM user_streaks WHERE user_id=?", (str(user_id),))

        r = c.fetchone()

        if r:

            return {"current_streak": int(r[0]), "longest_streak": int(r[1]), "last_date": r[2]}

        else:

            return {"current_streak": 0, "longest_streak": 0, "last_date": None}



def set_user_streak(user_id, current_streak, longest_streak, last_date):

    ensure_streak_tables()

    with _db_thread_lock:

        c = _db_conn.cursor()

        c.execute("""

        INSERT INTO user_streaks(user_id, current_streak, longest_streak, last_date)

        VALUES (?, ?, ?, ?)

        ON CONFLICT(user_id) DO UPDATE SET

            current_streak=excluded.current_streak,

            longest_streak=excluded.longest_streak,

            last_date=excluded.last_date

        """, (str(user_id), int(current_streak), int(longest_streak), last_date))

        _db_conn.commit()



def list_all_user_streaks():

    ensure_streak_tables()

    with _db_thread_lock:

        c = _db_conn.cursor()

        c.execute("SELECT user_id, current_streak, longest_streak, last_date FROM user_streaks")

        rows = c.fetchall()

        return [{"user_id": r[0], "current_streak": r[1], "longest_streak": r[2], "last_date": r[3]} for r in rows]



def qualify_day(prayers, habits):

    """Return True if a day counts as a streak, per your rule (prayers >= 5 and habits >= 5)."""

    try:

        return int(prayers) >= MIN_PRAYERS_FOR_STREAK and int(habits) >= MIN_HABITS_FOR_STREAK

    except Exception:

        return False



# compute prayer count for a set of prayer keys for a given date (accounts jummah)

def compute_prayer_count_from_keys(keys, date_local):

    if not keys:

        return 0

    # reuse award_for_prayers logic but count entries instead of XP

    is_friday = (date_local.weekday() == 4)

    count = 0

    for k in set(keys):

        if k == "jummah" and not is_friday:

            continue

        # only count if it's a known prayer key

        if k in PRAYER_MAP:

            count += 1

    return count



# compute habit count simply as set size

def compute_habit_count_from_keys(keys):

    if not keys:

        return 0

    return len(set(keys))



# --------------------------

# All-time embed functions

# --------------------------

def _get_streak_kv_key():

    return "streak_alltime_message_id"



def save_alltime_message_id(msg_id):

    save_json(_get_streak_kv_key(), {"msg_id": msg_id})



def load_alltime_message_id():

    v = load_json(_get_streak_kv_key(), {})

    return v.get("msg_id") if isinstance(v, dict) else v



async def get_or_create_alltime_message(channel: discord.TextChannel):

    # Try pins first

    try:

        pins = await channel.pins()

    except Exception:

        pins = []

    # Find pinned message with our footer tag

    for msg in pins:

        if msg.author == bot.user and msg.embeds:

            e = msg.embeds[0]

            if e.title and STREAK_ALLTIME_TITLE in e.title and e.footer and e.footer.text and STREAK_ALLTIME_FOOTER_TAG in e.footer.text:

                return msg

    # If not found, try stored id

    msg_id = load_alltime_message_id()

    if msg_id:

        try:

            msg = await channel.fetch_message(int(msg_id))

            if msg and msg.author == bot.user:

                return msg

        except Exception:

            pass

    # Create a new message

    embed = discord.Embed(

        title=STREAK_ALLTIME_TITLE,

        description="Initializing all-time streaks...",

        color=0xFFD700

    )

    embed.set_footer(text=f"{STREAK_ALLTIME_FOOTER_TAG} • Created {now_local().strftime('%Y-%m-%d %H:%M %Z')}")

    msg = await channel.send(embed=embed)

    try:

        await msg.pin()

    except Exception:

        pass

    save_alltime_message_id(msg.id)

    return msg



async def edit_alltime_embed(guild):

    """Compose and edit the single all-time streak embed (editable message)."""

    channel = bot.get_channel(STREAK_CHANNEL_ID)

    if not channel:

        # try to find channel in guild

        try:

            for ch in guild.text_channels:

                if ch.id == STREAK_CHANNEL_ID:

                    channel = ch

                    break

        except Exception:

            pass

    if not channel:

        # cannot update embed without channel

        return



    # gather human members

    members = [m for m in channel.guild.members if not m.bot]

    members_map = {str(m.id): m for m in members}



    # ensure user_streaks row exists for each human member

    for m in members:

        us = get_all_user_streak(m.id)

        # if no row exists (last_date None and zeros), ensure DB entry

        if us["current_streak"] == 0 and us["longest_streak"] == 0 and us["last_date"] is None:

            set_user_streak(m.id, 0, 0, None)



    # load all rows and sort

    rows = list_all_user_streaks()

    # create lookup for display name sort fallback

    def name_of(uid):

        m = members_map.get(str(uid))

        return (m.display_name or m.name).lower() if m else ""



    # sort by current_streak desc, tie-breaker by name

    rows.sort(key=lambda r: (-int(r["current_streak"]), name_of(r["user_id"])))



    # Build lines. Show top X in main field, rest in truncated section

    lines = []

    MAX_SHOW = 100

    for i, r in enumerate(rows[:MAX_SHOW], start=1):

        uid = r["user_id"]

        current = int(r["current_streak"])

        longest = int(r["longest_streak"])

        m = members_map.get(uid)

        name = m.mention if m else f"<@{uid}>"

        prefix = "🥇 " if i == 1 else "🥈 " if i == 2 else "🥉 " if i == 3 else ""

        lines.append(f"**{i}.** {prefix}{name} — Current: **{current}** 🔥 | Longest: **{longest}** 🏆")



    desc = "\n".join(lines) if lines else "No members yet."



    # edit or create message

    try:

        msg = await get_or_create_alltime_message(channel)

        embed = discord.Embed(

            title=STREAK_ALLTIME_TITLE,

            description=desc,

            color=0xFFD700

        )

        embed.set_footer(text=f"{STREAK_ALLTIME_FOOTER_TAG} • Updated {now_local().strftime('%Y-%m-%d %H:%M %Z')}")

        try:

            await msg.edit(embed=embed)

        except Exception:

            # fallback: send a new message and store id (rare case)

            new_msg = await channel.send(embed=embed)

            save_alltime_message_id(new_msg.id)

    except Exception as e:

        print("Failed to edit/create all-time streak message:", e)



# ------------------------

# Immediate provisional streak application

# ------------------------

def _date_prev(date_str):

    dt = datetime.strptime(date_str, "%Y-%m-%d")

    prev = dt - timedelta(days=1)

    return prev.strftime("%Y-%m-%d")



def apply_immediate_provisional_streak(user_id, date_str):

    """

    Apply immediate provisional streak update: increment (or set to 1) if qualifies based on the

    current daily_streaks row for date_str. This is an optimistic immediate update used when

    the bot is online and a member selects eligible habits/prayers. The authoritative pass at midnight

    can correct differences if necessary.

    """

    entry = get_daily_streak_entry(date_str, user_id)

    if not entry:

        return

    qualified = qualify_day(entry["prayers"], entry["habits"])

    us = get_all_user_streak(user_id)

    # if user had last_date equal to previous day of this date, increment; else set to 1 when qualified

    if qualified:

        prev = _date_prev(date_str)

        if us["last_date"] == prev:

            new_current = us["current_streak"] + 1

        else:

            new_current = 1

        new_longest = max(us["longest_streak"], new_current)

        set_user_streak(user_id, new_current, new_longest, date_str)

    else:

        # not qualified -> reset to 0 and set last_date to this date

        set_user_streak(user_id, 0, us["longest_streak"], date_str)



# --------------------------

# Authoritative per-day processing (catch-up) 

# --------------------------

def process_unprocessed_dates_authoritative(upto_date_str=None, guild=None):

    """

    This function processes all distinct dates in daily_streaks with processed=0 and date <= upto_date_str (if provided).

    For each date, it ensures every human member has a daily_streak row (inserting 0s if absent) then updates user_streaks

    with authoritative logic (no provisional assumptions).

    """

    ensure_streak_tables()

    dates = get_unprocessed_dates(upto_date_str)

    if not dates:

        return

    # process date by date in ascending order to maintain consecutive logic

    for date_str in dates:

        # get guild (if not passed, try to derive from channel)

        local_guild = guild

        if not local_guild:

            ch = bot.get_channel(STREAK_CHANNEL_ID)

            if ch:

                local_guild = ch.guild

        if not local_guild:

            # cannot process without guild members; skip for now

            continue



        # Gather human members list

        members = [m for m in local_guild.members if not m.bot]



        # For any member missing daily_streak row, insert zero row

        for m in members:

            ent = get_daily_streak_entry(date_str, m.id)

            if not ent:

                upsert_daily_streak(date_str, m.id, 0, 0, processed=0)



        # Now authoritative update per user for this date

        # For each member, check daily_streaks entry and update user_streaks accordingly

        for m in members:

            ent = get_daily_streak_entry(date_str, m.id)

            if not ent:

                # should not happen (inserted above), but guard

                prayers = 0

                habits = 0

            else:

                prayers = ent["prayers"]

                habits = ent["habits"]

            qualified = qualify_day(prayers, habits)

            us = get_all_user_streak(m.id)

            prev_date = _date_prev(date_str)

            if qualified:

                # if their last_date is previous date, continue streak; else start at 1

                if us["last_date"] == prev_date:

                    new_current = us["current_streak"] + 1

                else:

                    new_current = 1

                new_longest = max(us["longest_streak"], new_current)

                set_user_streak(m.id, new_current, new_longest, date_str)

            else:

                # not qualified -> reset to 0 and update last_date to this date

                set_user_streak(m.id, 0, us["longest_streak"], date_str)

            # mark daily entry processed

            mark_daily_processed(date_str, m.id)

        # after each date processed, update all-time embed (so changes are visible progressively)

        try:

            # call async edit via scheduling

            async def _edit(g):

                try:

                    await edit_alltime_embed(g)

                except Exception:

                    pass

            if local_guild:

                coro = _edit(local_guild)

                # schedule

                try:

                    asyncio.run_coroutine_threadsafe(coro, bot.loop)

                except Exception:

                    pass

        except Exception:

            pass



# ------------------------

# Weekly summary generator

# ------------------------

async def post_weekly_summary(guild):

    """

    Posts a weekly summary for the previous week (Mon-Sun).

    This function ALWAYS includes all human members (zero for non-participants).

    """

    channel = bot.get_channel(STREAK_CHANNEL_ID)

    if not channel:

        # fallback to searching in given guild

        try:

            for ch in guild.text_channels:

                if ch.id == STREAK_CHANNEL_ID:

                    channel = ch

                    break

        except Exception:

            pass

    if not channel:

        return



    # compute last week's date range: week_end = yesterday (Sunday), week_start = week_end - 6

    now = now_local()

    # We'll use week ending the previous day (i.e., week that just finished)

    # Determine week_end as (today - 1 day) and week_start 6 days before that

    week_end_dt = (now - timedelta(days=1)).date()

    week_start_dt = week_end_dt - timedelta(days=6)

    week_start = week_start_dt.strftime("%Y-%m-%d")

    week_end = week_end_dt.strftime("%Y-%m-%d")



    # For each human member, count the number of qualifying days in the week interval

    ensure_streak_tables()

    members = [m for m in guild.members if not m.bot]

    counts = []

    with _db_thread_lock:

        c = _db_conn.cursor()

        for m in members:

            c.execute("""

            SELECT COUNT(*) FROM daily_streaks

            WHERE user_id=? AND date BETWEEN ? AND ? AND prayers>=? AND habits>=?

            """, (str(m.id), week_start, week_end, MIN_PRAYERS_FOR_STREAK, MIN_HABITS_FOR_STREAK))

            cnt = c.fetchone()[0]

            counts.append((m, cnt))

    # sort descending

    counts.sort(key=lambda x: (-x[1], (x[0].display_name or x[0].name).lower()))

    # compose embed lines; show flames for up to 7 days

    lines = []

    for i, (m, cnt) in enumerate(counts, start=1):

        flames = "🔥" * int(cnt) if cnt > 0 else "—"

        lines.append(f"**{i}.** {m.mention} — {flames} ({cnt}/7 days)")



    embed = discord.Embed(

        title=f"{STREAK_WEEKLY_TITLE} ({week_start} → {week_end})",

        description="Weekly streaks — all human members included (zeros shown for non-participants).",

        color=0x1ABC9C

    )

    if lines:

        embed.add_field(name="Weekly Results", value="\n".join(lines[:50]), inline=False)

    else:

        embed.add_field(name="Weekly Results", value="No participants this week.", inline=False)

    embed.set_footer(text=f"{STREAK_WEEKLY_FOOTER_TAG} • Generated {now_local().strftime('%Y-%m-%d %H:%M %Z')}")



    try:

        await channel.send(embed=embed)

    except Exception as e:

        print("Failed to post weekly streak message:", e)



# ------------------------

# Background manager: realtime upserts + midnight catch-up + weekly posting

# ------------------------

async def streak_background_task():

    await bot.wait_until_ready()

    ensure_streak_tables()

    # On startup catch-up: process any unprocessed dates up to yesterday

    try:

        yesterday = (now_local() - timedelta(days=1)).strftime("%Y-%m-%d")

        process_unprocessed_dates_authoritative(upto_date_str=yesterday, guild=None)

    except Exception as e:

        print("Initial streak catch-up error:", e)



    # Main loop: scan active_groups frequently to upsert live daily_streaks,

    # apply provisional immediate updates, and periodically run authoritative pass and weekly post.

    last_weekly_post_week = None

    while not bot.is_closed():

        try:

            nowt = now_local()

            today_str = nowt.strftime("%Y-%m-%d")



            # 1) Scan active_groups for selections and upsert daily_streak entries.

            #    For each group, compute its date and update rows for users who submitted.

            for g in list(active_groups):

                try:

                    date_str = _date_str_from_group_created_at(g)

                    group_dt_local = datetime.fromtimestamp(g.get("created_at", epoch_now()), pytz.timezone(TIMEZONE))

                    # prayers selections

                    p_sel = g.get("selections", {}).get("prayers", {}) or {}

                    h_sel = g.get("selections", {}).get("habits", {}) or {}

                    # for each user who has any selection, update DB

                    users = set(list(p_sel.keys()) + list(h_sel.keys()))

                    for uid in users:

                        try:

                            p_keys = p_sel.get(uid, [])

                            h_keys = h_sel.get(uid, [])

                            p_count = compute_prayer_count_from_keys(p_keys, group_dt_local)

                            h_count = compute_habit_count_from_keys(h_keys)

                            upsert_daily_streak(date_str, uid, p_count, h_count, processed=0)

                            # Immediate provisional update if it's today's date (and bot online)

                            if date_str == today_str:

                                apply_immediate_provisional_streak(uid, date_str)

                                # schedule async edit of all-time embed

                                try:

                                    guild = None

                                    ch = bot.get_channel(STREAK_CHANNEL_ID)

                                    if ch:

                                        guild = ch.guild

                                    if guild:

                                        asyncio.run_coroutine_threadsafe(edit_alltime_embed(guild), bot.loop)

                                except Exception:

                                    pass

                        except Exception:

                            pass

                except Exception:

                    pass



            # 2) If it's midnight local time, run authoritative processing for unprocessed dates up to yesterday

            if nowt.hour == 0 and nowt.minute == 0:

                # process up to yesterday

                upto = (nowt - timedelta(days=1)).strftime("%Y-%m-%d")

                try:

                    # process with guild context if possible

                    ch = bot.get_channel(STREAK_CHANNEL_ID)

                    guild = ch.guild if ch else None

                    process_unprocessed_dates_authoritative(upto_date_str=upto, guild=guild)

                except Exception as e:

                    print("Error during midnight authoritative streak processing:", e)



            # 3) Weekly post: Post once per week at Monday 00:00 (start of Monday) in Asia/Karachi timezone.

            # We'll post the summary at Monday 00:00 for the week that just finished (Mon-Sun).

            # Use ISO week number to avoid repeats.

            if nowt.weekday() == 0 and nowt.hour == 0 and nowt.minute == 0:

                week_id = nowt.strftime("%G-W%V")

                if last_weekly_post_week != week_id:

                    # post weekly summary

                    try:

                        ch = bot.get_channel(STREAK_CHANNEL_ID)

                        guild = ch.guild if ch else None

                        if guild:

                            await post_weekly_summary(guild)

                            last_weekly_post_week = week_id

                    except Exception as e:

                        print("Error posting weekly summary:", e)



            # Sleep a short time (catch rapid updates), then loop

            await asyncio.sleep(10)

        except asyncio.CancelledError:

            break

        except Exception as e:

            # log but keep the loop alive

            print("streak_background_task loop error:", e)

            await asyncio.sleep(5)



# schedule the background task

try:

    # if event loop is available, create the task so it runs once bot starts

    bot.loop.create_task(streak_background_task())

except Exception:

    # fallback: continue; task will be created when bot starts

    pass



# -------------------------------------------------------------------------

# ------------------------ END OF STREAK SYSTEM ---------------------------

# -------------------------------------------------------------------------





# ---------- Main ----------



if __name__ == "__main__":

    # Ensure defaults exist (these calls now persist into DB-backed KV)

    if not os.path.exists(GROUPS_FILE):

        save_json(GROUPS_FILE, [])

    if not os.path.exists(XP_TOTALS_FILE):

        save_json(XP_TOTALS_FILE, {})

    bot.run(TOKEN)
