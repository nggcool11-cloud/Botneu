"""
Clipper Community Bot (slim version)
Uses your EXISTING channels. On every start the bot checks all embeds/panels
and posts the ones that are missing. Roles, verification, tickets, applications,
streamer requests, suggestions with threads, honeypot channel, moderation commands,
auto-moderation, logs and anti-raid.
Requires: discord.py >= 2.4   |   Env variable: DISCORD_TOKEN
"""
import asyncio
import io
import json
import os
import random
import re
import time
import traceback
from collections import defaultdict, deque
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

# ============================================================
# CONFIG
# ============================================================
DATA_FILE = "data.json"

STREAMER_PANEL_CHANNEL_ID = 1561361883695681577   # button panel for streamers
STREAMER_SUBMIT_CHANNEL_ID = 1561361894663524422  # where the finished requests are posted

BOT_ABOUT = "Creates edits"   # bot profile text (About Me)
BOT_ACTIVITY = "CapCut"       # shown as "Playing CapCut"

# Optional: force a channel by ID instead of name matching, e.g. {"verify": 123456789}
CHANNEL_IDS = {}

# How the bot finds your existing channels by name: key -> (must contain any of, must NOT contain)
CHANNEL_NAMES = {
    "welcome": (["welcome"], []),
    "verify": (["verify", "verifiz"], []),
    "support": (["support"], []),
    "serverinfo": (["serverinfo", "server-info"], []),
    "roles": (["roles"], []),
    "applications": (["application"], ["review"]),
    "suggestions": (["suggestion"], []),
    "honeypot": (["do-not-write", "honeypot", "dont-write"], []),
    "general": (["general"], []),
    "mod_log": (["mod-log"], ["automod"]),
    "automod_log": (["automod-log"], []),
    "member_log": (["member-log"], []),
    "message_log": (["message-log"], []),
    "ticket_log": (["ticket-log"], []),
    "app_review": (["application-review", "app-review"], []),
    "raid_alerts": (["raid-alert"], []),
}

# Auto-moderation
LINK_TIMEOUT = timedelta(hours=3)
INSULT_TIMEOUT = timedelta(hours=3)
SPAM_TIMEOUT = timedelta(hours=3)
ALLOWED_DOMAINS = {"tenor.com", "giphy.com"}   # links from these domains are allowed
SPAM_MESSAGES = 5        # this many messages ...
SPAM_WINDOW = 6          # ... within this many seconds
DUPLICATE_LIMIT = 3      # same message this many times ...
DUPLICATE_WINDOW = 20    # ... within this many seconds
MENTION_LIMIT = 6        # mentions in one message

# Mass kick/ban alarm (someone kicks/bans several people in a row)
RAID_ACTION_LIMIT = 3
RAID_WINDOW = 60
AUTO_STRIP_ROLES = False  # True = bot removes dangerous roles from the person who triggered the alarm

# Warning escalation: number of warnings -> automatic timeout
WARN_ESCALATION = {3: timedelta(hours=6), 5: timedelta(hours=24)}

INSULTS = [
    "fuck you", "fuck off", "kys", "kill yourself", "go die", "idiot", "moron",
    "retard", "retarded", "bitch", "cunt", "whore", "slut", "faggot", "fag",
    "nigger", "nigga", "dumbass", "bastard", "asshole", "dickhead", "shithead",
    "hurensohn", "hure", "wichser", "fotze", "arschloch", "missgeburt", "spast",
    "schwuchtel", "behindert", "vollidiot",
]

# Streamer <-> clipper chats: can staff see these private chats? (recommended for safety)
CONTACT_STAFF_CAN_SEE = True

# Anti-raid
JOIN_LIMIT = 8                # this many joins ...
JOIN_WINDOW = 30              # ... within this many seconds trigger raid mode
RAID_MODE_MINUTES = 10        # how long raid mode stays on
RAID_KICK_ACCOUNT_DAYS = 3    # during raid mode, accounts younger than this are kicked

COLORS = {"brand": 0x5865F2, "ok": 0x57F287, "warn": 0xFEE75C,
          "bad": 0xED4245, "info": 0x3498DB, "gold": 0xF1C40F}

# ============================================================
# DATA
# ============================================================
DEFAULTS = {"channels": {}, "roles": {}, "messages": {}, "jobs": {}, "job_counter": 0, "suggestions": {},
            "warnings": {}, "case_counter": 0}


def load_data():
    d = {k: (v.copy() if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                d.update(json.load(f))
        except Exception as e:
            print("Could not load data file:", e)
    return d


data = load_data()


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ============================================================
# HELPERS
# ============================================================
def make_embed(title=None, desc=None, color="brand"):
    return discord.Embed(title=title, description=desc, color=COLORS[color], timestamp=discord.utils.utcnow())


def get_chan(guild, key):
    """Finds an existing channel: by ID override, otherwise by name."""
    if key in CHANNEL_IDS:
        return guild.get_channel(CHANNEL_IDS[key])
    spec = CHANNEL_NAMES.get(key)
    if spec:
        include, exclude = spec
        for c in guild.text_channels:
            n = c.name.lower()
            if any(w in n for w in include) and not any(w in n for w in exclude):
                return c
    cid = data["channels"].get(key)
    return guild.get_channel(cid) if cid else None


def get_role(guild, key):
    name = next((n for k, n, _ in ROLE_SPECS if k == key), None)
    if name:
        return next((r for r in guild.roles if r.name.lower() == name.lower()), None)
    return None


def is_staff(member):
    p = member.guild_permissions
    if p.administrator or p.manage_messages or p.moderate_members:
        return True
    r = get_role(member.guild, "staff")
    return bool(r and r in member.roles)


def truncate(text, n):
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


async def send_to(guild, key, emb, **kwargs):
    c = get_chan(guild, key)
    if c:
        try:
            await c.send(embed=emb, **kwargs)
        except discord.HTTPException:
            pass


async def try_dm(user, emb):
    try:
        await user.send(embed=emb)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


def codeblock(text, n=900):
    return "```" + truncate(text.replace("```", "'''"), n) + "```"


def next_case():
    data["case_counter"] += 1
    save_data()
    return data["case_counter"]


def fmt_td(td):
    s = int(td.total_seconds())
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = [f"{v}{u}" for v, u in ((d, "d"), (h, "h"), (m, "m"), (s, "s")) if v]
    return " ".join(parts) or "0s"


def parse_duration(text):
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", text.lower())
    if not m:
        return None
    unit = m.group(2) or "m"
    td = timedelta(seconds=int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])
    if td < timedelta(seconds=1) or td > timedelta(days=28):
        return None
    return td


def case_embed(title, color, case, target, moderator_text, reason, extra=None):
    e = make_embed(title, color=color)
    e.add_field(name="User", value=f"{target.mention}\n`{target.id}`")
    e.add_field(name="Moderator", value=moderator_text)
    e.add_field(name="Reason", value=truncate(reason, 1000), inline=False)
    for name, value in extra or []:
        e.add_field(name=name, value=value)
    e.set_footer(text=f"Case #{case}")
    return e


def dm_embed(guild, action, reason, extra=None):
    e = make_embed(f"You were {action} in {guild.name}", color="bad")
    e.add_field(name="Reason", value=truncate(reason, 1000), inline=False)
    if extra:
        e.add_field(name="Details", value=extra, inline=False)
    return e


def hierarchy_error(guild, moderator, target):
    if target.id == moderator.id:
        return "You can't do that to yourself."
    if target.id == guild.owner_id:
        return "You can't target the server owner."
    if target.id == bot.user.id:
        return "I won't do that to myself."
    if isinstance(target, discord.Member):
        if moderator.id != guild.owner_id and target.top_role >= moderator.top_role:
            return "That member has the same or a higher role than you."
        if target.top_role >= guild.me.top_role:
            return "I can't moderate that member (their role is higher than mine)."
    return None


async def find_audit(guild, action, target_id, seconds=15):
    try:
        await asyncio.sleep(1.5)
        async for entry in guild.audit_logs(limit=10, action=action):
            age = (discord.utils.utcnow() - entry.created_at).total_seconds()
            if entry.target is not None and entry.target.id == target_id and age < seconds:
                return entry
    except (discord.Forbidden, discord.HTTPException):
        pass
    return None


def is_private_channel(channel):
    cat = getattr(channel, "category_id", None)
    return cat is not None and cat in {data["channels"].get("staff_category"),
                                      data["channels"].get("tickets_category")}


background_tasks = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    background_tasks.add(t)
    t.add_done_callback(background_tasks.discard)


# ============================================================
# ROLES
# ============================================================
ROLE_SPECS = [
    ("unverified", "Unverified", {"colour": discord.Colour.dark_grey()}),
    ("verified", "Verified", {"colour": discord.Colour.green()}),
    ("high_team", "High Team", {"colour": discord.Colour.gold(), "hoist": True, "mentionable": True,
                                "permissions": discord.Permissions(kick_members=True, ban_members=True,
                                                                   moderate_members=True, manage_messages=True,
                                                                   view_audit_log=True)}),
    ("staff", "Staff", {"colour": discord.Colour.blue(), "hoist": True, "mentionable": True,
                        "permissions": discord.Permissions(kick_members=True, moderate_members=True,
                                                           manage_messages=True)}),
    ("streamer", "Streamer", {"colour": discord.Colour(0x9146FF), "hoist": True}),
    ("clipper", "Clipper", {"colour": discord.Colour.orange(), "hoist": True}),
    ("partner", "Partner", {"colour": discord.Colour.teal(), "hoist": True}),
    ("announcements", "Announcements", {"mentionable": True}),
    ("search_clipper", "Search Clipper", {"mentionable": True}),
    ("tips", "Tips", {"mentionable": True}),
    ("application_ping", "Application Ping", {"mentionable": True}),
]


async def ensure_roles(guild):
    """Creates every role that doesn't exist yet (matched by name, case-insensitive)."""
    created, existing = [], []
    for key, name, kwargs in ROLE_SPECS:
        role = next((r for r in guild.roles if r.name.lower() == name.lower()), None)
        if role is None:
            role = await guild.create_role(name=name, reason="Role setup", **kwargs)
            created.append(name)
        else:
            existing.append(name)
        data["roles"][key] = role.id
    save_data()
    return created, existing


# ============================================================
# PANELS (automatic check on every start)
# ============================================================
async def ensure_panel(channel, emb, view=None):
    """Edits the bot's existing panel (same title) or posts it if it's missing."""
    emb.timestamp = None
    try:
        async for m in channel.history(limit=50):
            if m.author.id == bot.user.id and m.embeds and m.embeds[0].title == emb.title:
                await m.edit(embed=emb, view=view)
                return "updated"
        await channel.send(embed=emb, view=view)
        return "posted"
    except discord.Forbidden:
        return "NO PERMISSION"
    except discord.HTTPException as e:
        return f"error ({e})"


async def ensure_tickets_category(guild):
    cat = next((c for c in guild.categories if c.name.lower() == "tickets"), None)
    if cat is None:
        P = discord.PermissionOverwrite
        ow = {guild.default_role: P(view_channel=False),
              guild.me: P(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)}
        for r in (get_role(guild, "staff"), get_role(guild, "high_team")):
            if r:
                ow[r] = P(view_channel=True, send_messages=True, read_message_history=True)
        cat = await guild.create_category("TICKETS", overwrites=ow)
    data["channels"]["tickets_category"] = cat.id
    save_data()
    return cat


PANELS = {
    "verify": lambda: (make_embed(
        "✅ Verification",
        "Welcome! To unlock the server, press the button below. You'll receive a **4-digit code** "
        "that you have to enter to verify yourself.\n-# A new code is generated every time you press the button.",
        "ok"), VerifyView()),
    "roles": lambda: (make_embed(
        "🎭 Self Roles",
        "Choose which notifications you want. Press a button again to remove the role.\n\n"
        "📢 **Announcements** – server news\n🔎 **Search Clipper** – clipper requests\n"
        "💡 **Tips** – tips & tricks\n📝 **Application Ping** – new open positions", "brand"), RolesView()),
    "support": lambda: (make_embed(
        "🎫 Support",
        "Need help? Pick a category from the dropdown menu below and a private ticket will be created for you.\n\n"
        "💬 **General Support** – questions and general help\n🐛 **Bug Report** – something isn't working\n"
        "👑 **High Team Ticket** – sensitive matters for the High Team", "brand"), SupportView()),
    "serverinfo": lambda: (make_embed("ℹ️ Server Info", "## Coming soon…", "info"), None),
    "honeypot": lambda: (make_embed(
        "🚫 DO NOT WRITE HERE",
        "This channel is a security trap.\n**Anyone who sends a message here is kicked automatically.**\n"
        "-# Hacked accounts and bots usually can't resist writing in every channel.", "bad"), None),
    "applications": lambda: (build_apply_embed(), build_apply_view()),
}


async def ensure_all_panels(guild):
    """Checks every panel in your existing channels and posts missing ones."""
    try:
        await ensure_tickets_category(guild)
    except discord.HTTPException as e:
        print(f"[{guild.name}] Could not create TICKETS category: {e}")
    for key, factory in PANELS.items():
        ch = get_chan(guild, key)
        if not ch:
            print(f"[{guild.name}] {key}: channel not found (skipped)")
            continue
        emb, view = factory()
        print(f"[{guild.name}] {key} -> #{ch.name}: {await ensure_panel(ch, emb, view)}")
    streamer_ch = guild.get_channel(STREAMER_PANEL_CHANNEL_ID)
    if streamer_ch:
        emb = make_embed(
            "🎥 Looking for Clippers?",
            "Are you a streamer and want clippers for your content? Press the button below, answer a few "
            "questions and your request will be posted for our clippers.\n\n"
            "**You will be asked for:**\n• Your streaming platform\n• Your streaming topic\n• Something about you\n"
            "• Your social media link\n• Payment / rewards for clippers\n• Further requirements\n"
            "-# Your answers are only visible to you until you send them.", "brand")
        print(f"[{guild.name}] streamer -> #{streamer_ch.name}: {await ensure_panel(streamer_ch, emb, StreamerPanelView())}")
    else:
        print(f"[{guild.name}] streamer panel channel {STREAMER_PANEL_CHANNEL_ID} not found")


# ============================================================
# ANTI-RAID
# ============================================================
join_times = deque()
raid_mode_until = 0.0


def raid_mode_active():
    return time.time() < raid_mode_until


async def raid_ping(guild):
    return " ".join(r.mention for r in (get_role(guild, "staff"), get_role(guild, "high_team")) if r)


async def enable_raid_mode(guild, reason):
    global raid_mode_until
    already = raid_mode_active()
    raid_mode_until = time.time() + RAID_MODE_MINUTES * 60
    if not already:
        e = make_embed("🚨 RAID MODE ENABLED", f"{reason}\n\n• Verification is paused\n"
                       f"• New accounts younger than **{RAID_KICK_ACCOUNT_DAYS} days** are kicked\n"
                       f"• Raid mode ends automatically in **{RAID_MODE_MINUTES} minutes**\n"
                       "-# Use /raidmode to turn it off earlier.", "bad")
        await send_to(guild, "raid_alerts", e, content=await raid_ping(guild),
                      allowed_mentions=discord.AllowedMentions(roles=True))


# ============================================================
# MASS KICK/BAN ALARM
# ============================================================
action_log = defaultdict(deque)


async def record_action(guild, executor, group, description):
    if executor is None or executor.id == bot.user.id:
        return
    now = time.time()
    dq = action_log[(executor.id, group)]
    dq.append((now, description))
    while dq and now - dq[0][0] > RAID_WINDOW:
        dq.popleft()
    if len(dq) >= RAID_ACTION_LIMIT:
        items = [d for _, d in dq]
        dq.clear()
        await mass_action_alert(guild, executor, group, items)


async def mass_action_alert(guild, executor, group, items):
    e = make_embed("🚨 RAID ALERT: Mass Action Detected", color="bad")
    e.add_field(name="Responsible", value=f"{executor.mention}\n`{executor.id}`")
    e.add_field(name="Type", value=group.title())
    e.add_field(name="Actions", value=truncate("\n".join(f"• {i}" for i in items), 1000), inline=False)
    e.add_field(name="Window", value=f"{len(items)} actions in under {RAID_WINDOW}s")
    result = "No automatic action taken."
    member = guild.get_member(executor.id)
    if AUTO_STRIP_ROLES and member and member.id != guild.owner_id:
        risky = [r for r in member.roles if not r.is_default() and not r.managed and r < guild.me.top_role
                 and (r.permissions.administrator or r.permissions.kick_members or r.permissions.ban_members
                      or r.permissions.manage_channels or r.permissions.manage_roles)]
        try:
            if risky:
                await member.remove_roles(*risky, reason="Raid alert: mass action detected")
                result = "Removed roles: " + ", ".join(r.name for r in risky)
        except discord.HTTPException:
            result = "Tried to remove roles but failed."
    e.add_field(name="Automatic action", value=result, inline=False)
    await send_to(guild, "raid_alerts", e, content=await raid_ping(guild),
                  allowed_mentions=discord.AllowedMentions(roles=True, users=True))
    await send_to(guild, "mod_log", e)


# ============================================================
# AUTO-MODERATION
# ============================================================
BARE_LINK_RE = re.compile(
    r"\b((?:[a-z0-9-]+\.)+(?:com|net|org|gg|io|xyz|co|tv|me|ly|link|click|ru|de|info|shop|site|online|"
    r"app|dev|to|cc|ws|top|club|live|stream|store|fun|icu|vip|pw|tk|ml|ga|cf|gq|ink|page|cloud))\b", re.I)
URL_RE = re.compile(r"https?://([^\s/]+)", re.I)
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})


def _build_pattern(words):
    parts = [r"\s+".join(map(re.escape, w.split())) for w in words]
    return re.compile(r"(?<![a-z])(?:" + "|".join(parts) + r")(?:s|es)?(?![a-z])")


INSULT_RE = _build_pattern(INSULTS)


def has_blocked_link(text):
    hosts = [m.group(1).lower() for m in URL_RE.finditer(text)]
    hosts += [m.group(1).lower() for m in BARE_LINK_RE.finditer(text)]
    for h in hosts:
        if not any(h == d or h.endswith("." + d) for d in ALLOWED_DOMAINS):
            return True
    return False


def has_insult(text):
    t = text.lower().translate(LEET)
    t = re.sub(r"(.)\1{2,}", r"\1", t)
    return INSULT_RE.search(t) is not None


spam_tracker = defaultdict(lambda: deque(maxlen=20))
bot_deleted = set()


def check_spam(message):
    now = time.time()
    content = message.content.strip().lower()
    dq = spam_tracker[message.author.id]
    dq.append((now, content, message))
    recent = [x for x in dq if now - x[0] <= SPAM_WINDOW]
    if len(recent) >= SPAM_MESSAGES:
        return f"Spam ({len(recent)} messages in {SPAM_WINDOW} seconds)", [x[2] for x in recent]
    if content:
        dup = [x for x in dq if now - x[0] <= DUPLICATE_WINDOW and x[1] == content]
        if len(dup) >= DUPLICATE_LIMIT:
            return "Spam (repeated messages)", [x[2] for x in dup]
    if len(message.mentions) + len(message.role_mentions) >= MENTION_LIMIT:
        return "Mass mentions", [message]
    return None, []


async def punish(message, reason, label, duration, extra=()):
    member, guild = message.author, message.guild
    for m in {message.id: message, **{x.id: x for x in extra}}.values():
        bot_deleted.add(m.id)
        try:
            await m.delete()
        except discord.HTTPException:
            pass
    spam_tracker.pop(member.id, None)
    timed_out = False
    try:
        await member.timeout(duration, reason=f"AutoMod: {reason}")
        timed_out = True
    except discord.HTTPException:
        pass
    case = next_case()
    note = f"{member.mention}, your message was removed: **{reason}**."
    if timed_out:
        note += f" You have been timed out for **{fmt_td(duration)}**."
    try:
        await message.channel.send(note, delete_after=10)
    except discord.HTTPException:
        pass
    await try_dm(member, dm_embed(guild, "timed out by AutoMod" if timed_out else "warned by AutoMod", reason,
                                  f"Duration: {fmt_td(duration)}" if timed_out else None))
    e = case_embed(f"AutoMod: {label}", "bad", case, member, "AutoMod", reason,
                   [("Channel", message.channel.mention),
                    ("Punishment", f"Timeout {fmt_td(duration)}" if timed_out else "Message removed (timeout failed)")])
    if message.content:
        e.add_field(name="Message", value=codeblock(message.content), inline=False)
    await send_to(guild, "automod_log", e)


async def run_automod(message):
    member = message.author
    if not isinstance(member, discord.Member) or is_staff(member) or is_private_channel(message.channel):
        return False
    text = message.content or ""
    if text and has_blocked_link(text):
        await punish(message, "Links are not allowed", "Link", LINK_TIMEOUT)
        return True
    if text and has_insult(text):
        await punish(message, "Insults / offensive language", "Insult", INSULT_TIMEOUT)
        return True
    reason, msgs = check_spam(message)
    if reason:
        await punish(message, reason, "Spam", SPAM_TIMEOUT, msgs)
        return True
    return False


# ============================================================
# VERIFICATION
# ============================================================
pending_codes = {}


class CodeModal(discord.ui.Modal, title="Verification"):
    code = discord.ui.TextInput(label="Enter the 4-digit code", min_length=4, max_length=4, placeholder="1234")

    async def on_submit(self, interaction: discord.Interaction):
        uid = interaction.user.id
        entry = pending_codes.get(uid)
        if not entry or time.time() > entry["expires"]:
            pending_codes.pop(uid, None)
            return await interaction.response.send_message(
                "Your code expired. Press **Verify** again to get a new one.", ephemeral=True)
        if self.code.value.strip() != entry["code"]:
            entry["tries"] += 1
            if entry["tries"] >= 3:
                pending_codes.pop(uid, None)
                msg = "Too many wrong attempts. Press **Verify** again to get a new code."
            else:
                msg = f"Wrong code. {3 - entry['tries']} attempt(s) left."
            return await interaction.response.send_message(msg, ephemeral=True)
        pending_codes.pop(uid, None)
        guild, member = interaction.guild, interaction.user
        unv, ver = get_role(guild, "unverified"), get_role(guild, "verified")
        try:
            if unv and unv in member.roles:
                await member.remove_roles(unv, reason="Verified")
            if ver:
                await member.add_roles(ver, reason="Verified")
        except discord.Forbidden:
            return await interaction.response.send_message("I couldn't assign your role. Please contact the team.",
                                                           ephemeral=True)
        await interaction.response.send_message("✅ You are verified! Welcome to the server.", ephemeral=True)


class CodeEntryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Enter Code", style=discord.ButtonStyle.primary, emoji="🔢")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CodeModal())


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, emoji="✅", custom_id="verify:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if raid_mode_active():
            return await interaction.response.send_message(
                "🛡️ Verification is temporarily paused because of unusual activity. Please try again in a few minutes.",
                ephemeral=True)
        ver = get_role(interaction.guild, "verified")
        if ver and ver in interaction.user.roles:
            return await interaction.response.send_message("You are already verified.", ephemeral=True)
        code = f"{random.randint(0, 9999):04d}"
        pending_codes[interaction.user.id] = {"code": code, "expires": time.time() + 300, "tries": 0}
        e = make_embed("Your verification code", f"# `{code}`\nPress **Enter Code** and type this number.\n"
                       "-# The code is valid for 5 minutes. Pressing Verify again creates a new one.", "info")
        await interaction.response.send_message(embed=e, view=CodeEntryView(), ephemeral=True)


# ============================================================
# SELF ROLES
# ============================================================
ROLE_BUTTONS = [("announcements", "Announcements", "📢"), ("search_clipper", "Search Clipper", "🔎"),
                ("tips", "Tips", "💡"), ("application_ping", "Application Ping", "📝")]


class RolesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for key, label, emoji in ROLE_BUTTONS:
            btn = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary,
                                    custom_id=f"role:{key}")
            btn.callback = self.make_callback(key)
            self.add_item(btn)

    def make_callback(self, key):
        async def callback(interaction: discord.Interaction):
            role = get_role(interaction.guild, key)
            if not role:
                return await interaction.response.send_message("This role doesn't exist. Ask an admin to run /createroles.",
                                                               ephemeral=True)
            member = interaction.user
            try:
                if role in member.roles:
                    await member.remove_roles(role)
                    msg = f"Removed {role.mention}."
                else:
                    await member.add_roles(role)
                    msg = f"Added {role.mention}."
            except discord.Forbidden:
                msg = "I can't manage that role. Please contact the team."
            await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        return callback


# ============================================================
# SUPPORT TICKETS
# ============================================================
TICKET_TYPES = {"general": ("General Support", "💬"), "bug": ("Bug Report", "🐛"), "high": ("High Team Ticket", "👑")}


async def create_ticket(interaction: discord.Interaction, kind: str):
    await interaction.response.defer(ephemeral=True)
    guild, member = interaction.guild, interaction.user
    cat = get_chan(guild, "tickets_category")
    if not cat:
        return await interaction.followup.send("Tickets aren't ready yet. Please contact the team.", ephemeral=True)
    for c in cat.text_channels:
        if c.topic and c.topic.startswith(f"ticket:{member.id}:"):
            return await interaction.followup.send(f"You already have an open ticket: {c.mention}", ephemeral=True)
    P = discord.PermissionOverwrite
    handler = get_role(guild, "high_team") if kind == "high" else get_role(guild, "staff")
    ow = {guild.default_role: P(view_channel=False),
          member: P(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
          guild.me: P(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)}
    for r in {handler, get_role(guild, "high_team")}:
        if r:
            ow[r] = P(view_channel=True, send_messages=True, read_message_history=True)
    name = re.sub(r"[^a-z0-9]", "", member.name.lower()) or "user"
    channel = await guild.create_text_channel(f"{kind}-{name}"[:90], category=cat, overwrites=ow,
                                              topic=f"ticket:{member.id}:{kind}")
    label, emoji = TICKET_TYPES[kind]
    e = make_embed(f"{emoji} {label}", f"Hello {member.mention}, thanks for reaching out!\n"
                   "Please describe your issue in as much detail as possible. A team member will be with you shortly.\n"
                   "-# Use the button below to close this ticket when you're done.", "brand")
    await channel.send(content=f"{member.mention} {handler.mention if handler else ''}", embed=e, view=TicketView(),
                       allowed_mentions=discord.AllowedMentions(users=True, roles=True))
    await interaction.followup.send(f"Your ticket has been created: {channel.mention}", ephemeral=True)
    le = make_embed("Ticket Opened", color="ok")
    le.add_field(name="User", value=f"{member.mention}\n`{member.id}`")
    le.add_field(name="Type", value=label)
    le.add_field(name="Channel", value=channel.mention)
    await send_to(guild, "ticket_log", le)


class SupportSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="General Support", value="general", emoji="💬", description="Questions and general help"),
            discord.SelectOption(label="Bug Report", value="bug", emoji="🐛", description="Something isn't working"),
            discord.SelectOption(label="High Team Ticket", value="high", emoji="👑", description="Sensitive matters for the High Team"),
        ]
        super().__init__(custom_id="support:select", placeholder="Select a ticket category…", options=options)

    async def callback(self, interaction: discord.Interaction):
        await create_ticket(interaction, self.values[0])
        try:  # reset the dropdown so it can be used again
            await interaction.message.edit(view=SupportView())
        except discord.HTTPException:
            pass


class SupportView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(SupportSelect())


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        topic = channel.topic or ""
        try:
            if topic.startswith("ticket:"):
                participants = [int(topic.split(":")[1])]
            elif topic.startswith("contact:"):
                participants = [int(x) for x in topic.split(":")[1:3]]
            else:
                participants = []
        except ValueError:
            participants = []
        owner_id = participants[0] if participants else None
        if not (is_staff(interaction.user) or interaction.user.id in participants):
            return await interaction.response.send_message("Only the people in this chat or staff can close it.", ephemeral=True)
        await interaction.response.send_message("🔒 Closing this chat in 5 seconds…")
        lines = []
        async for m in channel.history(limit=1000, oldest_first=True):
            lines.append(f"[{m.created_at:%Y-%m-%d %H:%M}] {m.author}: {m.clean_content}")
        file = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"{channel.name}.txt")
        e = make_embed("Ticket Closed", color="bad")
        e.add_field(name="Ticket", value=f"`{channel.name}`")
        e.add_field(name="Closed by", value=interaction.user.mention)
        if participants:
            e.add_field(name="Participants", value=" ".join(f"<@{p}>" for p in participants))
        await send_to(interaction.guild, "ticket_log", e, file=file)
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            pass


# ============================================================
# APPLICATIONS (team positions)
# ============================================================
QUESTIONS = [
    ("name", "What is your **name**?"),
    ("age", "How **old** are you? (numbers only)"),
    ("why", "**Why do you want to join us specifically?**"),
    ("experience", "What **experience** do you have?"),
    ("strengths", "What are your **strengths and weaknesses**?"),
    ("about", "Write a **text of at least 150 words about yourself**. Make it personal and individual."),
    ("other", "Is there anything **else** you would like to tell us?"),
]
LABELS = {"name": "Name", "age": "Age", "why": "Why us?", "experience": "Experience",
          "strengths": "Strengths & weaknesses", "about": "About them (150+ words)", "other": "Other"}
active_applications = set()


def build_apply_embed():
    if data["jobs"]:
        desc = ("Interested in joining our team? Pick a position below to start your application.\n"
                "-# I'll ask you a few questions in a private channel. Please answer honestly.")
    else:
        desc = ("There are currently **no open positions**.\n"
                "-# Get the Application Ping role in the roles channel to be notified when something opens up.")
    return make_embed("📝 Applications", desc, "brand")


def build_apply_view():
    v = discord.ui.View(timeout=None)
    if data["jobs"]:
        for jid, title in list(data["jobs"].items())[:25]:
            v.add_item(ApplyButton(jid, title))
    else:
        v.add_item(discord.ui.Button(label="Nothing available", style=discord.ButtonStyle.secondary,
                                     disabled=True, custom_id="apply:none"))
    return v


class ApplyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"apply:(?P<job>\d+)"):
    def __init__(self, job_id: str, title: str = "Apply"):
        super().__init__(discord.ui.Button(label=truncate(title, 80), style=discord.ButtonStyle.success,
                                           emoji="📝", custom_id=f"apply:{job_id}"))
        self.job_id = job_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        jid = match["job"]
        return cls(jid, data["jobs"].get(jid, "Apply"))

    async def callback(self, interaction: discord.Interaction):
        await start_application(interaction, self.job_id)


async def start_application(interaction: discord.Interaction, job_id: str):
    job = data["jobs"].get(job_id)
    if not job:
        return await interaction.response.send_message("This position is no longer open.", ephemeral=True)
    member, guild = interaction.user, interaction.guild
    if member.id in active_applications:
        return await interaction.response.send_message("You already have an application in progress.", ephemeral=True)
    cat = get_chan(guild, "tickets_category")
    if not cat:
        return await interaction.response.send_message("Applications aren't set up yet.", ephemeral=True)
    P = discord.PermissionOverwrite
    ow = {guild.default_role: P(view_channel=False),
          member: P(view_channel=True, send_messages=True, read_message_history=True),
          guild.me: P(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)}
    for r in (get_role(guild, "staff"), get_role(guild, "high_team")):
        if r:
            ow[r] = P(view_channel=True, send_messages=True, read_message_history=True)
    name = re.sub(r"[^a-z0-9]", "", member.name.lower()) or "user"
    channel = await guild.create_text_channel(f"application-{name}"[:90], category=cat, overwrites=ow,
                                              topic=f"application:{member.id}")
    active_applications.add(member.id)
    await interaction.response.send_message(f"Your application channel is ready: {channel.mention}", ephemeral=True)
    spawn(run_application(channel, member, job))


async def run_application(channel, member, job):
    answers = {}
    try:
        intro = make_embed(f"📝 Application: {job}", f"Hello {member.mention}! I'll ask you {len(QUESTIONS)} questions, "
                           "one after another. Just type your answer in this channel.\n"
                           "-# You have 15 minutes per question.", "brand")
        await channel.send(content=member.mention, embed=intro)
        for key, question in QUESTIONS:
            while True:
                await channel.send(embed=make_embed(desc=question, color="info"))
                msg = await bot.wait_for("message", timeout=900,
                                         check=lambda m: m.author.id == member.id and m.channel.id == channel.id)
                text = msg.content.strip()
                if not text:
                    await channel.send("Please answer with text.")
                    continue
                if key == "age" and not (text.isdigit() and 13 <= int(text) <= 99):
                    await channel.send("Please enter a valid age (13-99).")
                    continue
                if key == "about":
                    words = len(text.split())
                    if words < 150:
                        await channel.send(f"Your text has **{words}** words. It needs at least **150**. Please try again.")
                        continue
                answers[key] = text
                break
        e = make_embed(f"📝 New Application: {job}", color="info")
        e.set_author(name=str(member), icon_url=member.display_avatar.url)
        e.add_field(name="Applicant", value=f"{member.mention}\n`{member.id}`")
        for key, label in LABELS.items():
            e.add_field(name=label, value=truncate(answers[key], 900), inline=key in ("name", "age"))
        e.set_footer(text=f"Applicant ID: {member.id}")
        full = "\n\n".join(f"== {LABELS[k]} ==\n{answers[k]}" for k in LABELS)
        file = discord.File(io.BytesIO(full.encode("utf-8")), filename=f"application-{member.id}.txt")
        review = get_chan(channel.guild, "app_review")
        if review:
            await review.send(embed=e, file=file, view=ReviewView())
        await channel.send(embed=make_embed("✅ Application submitted",
                                            "Thank you! Our team will review your application and get back to you by DM.\n"
                                            "-# This channel will close in 10 seconds.", "ok"))
        await asyncio.sleep(10)
    except asyncio.TimeoutError:
        try:
            await channel.send("⏰ This application timed out. You can start a new one anytime.")
            await asyncio.sleep(5)
        except discord.HTTPException:
            pass
    except Exception:
        traceback.print_exc()
    finally:
        active_applications.discard(member.id)
        try:
            await channel.delete(reason="Application finished")
        except discord.HTTPException:
            pass


class DecisionModal(discord.ui.Modal):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=900,
                                  placeholder="Explain your decision. The applicant will receive this.")

    def __init__(self, message: discord.Message, accepted: bool):
        super().__init__(title="Accept application" if accepted else "Deny application")
        self.message = message
        self.accepted = accepted

    async def on_submit(self, interaction: discord.Interaction):
        emb = self.message.embeds[0]
        uid = int(emb.footer.text.split(":")[1])
        job = emb.title.split(": ", 1)[1] if ": " in emb.title else "the position"
        reason = self.reason.value
        try:
            user = await bot.fetch_user(uid)
            d = make_embed("Application accepted 🎉" if self.accepted else "Application update",
                           f"Your application for **{job}** on **{interaction.guild.name}** was "
                           + ("**accepted**!" if self.accepted else "**not successful** this time."),
                           "ok" if self.accepted else "bad")
            d.add_field(name="Reason", value=reason, inline=False)
            await try_dm(user, d)
        except discord.HTTPException:
            pass
        emb.color = COLORS["ok"] if self.accepted else COLORS["bad"]
        emb.add_field(name=f"{'✅ Accepted' if self.accepted else '❌ Denied'} by {interaction.user.display_name}",
                      value=reason, inline=False)
        await interaction.response.edit_message(embed=emb, view=None)


class ReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def open_modal(self, interaction: discord.Interaction, accepted: bool):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        await interaction.response.send_modal(DecisionModal(interaction.message, accepted))

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅", custom_id="review:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_modal(interaction, True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌", custom_id="review:deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_modal(interaction, False)


async def refresh_application_panel(guild):
    ch = get_chan(guild, "applications")
    if ch:
        await ensure_panel(ch, build_apply_embed(), build_apply_view())


# ============================================================
# STREAMER REQUESTS ("looking for clippers")
# ============================================================
streamer_cooldown = {}


PLATFORMS = {"Twitch": ("🟣", 0x9146FF), "YouTube": ("🔴", 0xFF0000), "Kick": ("🟢", 0x53FC18),
             "TikTok": ("⚫", 0x25F4EE), "Other": ("⚪", 0x5865F2)}


def normalize_link(text):
    t = (text or "").strip()
    if re.fullmatch(r"https?://\S+\.\S+", t):
        return t[:500]
    if re.fullmatch(r"[\w.-]+\.[a-z]{2,}(/\S*)?", t, re.I):
        return ("https://" + t)[:500]
    return None


class StreamerModal(discord.ui.Modal, title="Clipper Search Request"):
    topic = discord.ui.TextInput(label="Streaming topic", placeholder="e.g. Just Chatting, Valorant, IRL", max_length=100)
    about = discord.ui.TextInput(label="About you", style=discord.TextStyle.paragraph, max_length=1000,
                                 placeholder="Tell us something about yourself and your community.")
    social = discord.ui.TextInput(label="Social media link", placeholder="https://...", max_length=200)
    rewards = discord.ui.TextInput(label="Do you pay clippers? Rewards?", style=discord.TextStyle.paragraph,
                                   max_length=800, placeholder="Payment, revenue share, shoutouts, roles, ...")
    requirements = discord.ui.TextInput(label="Further requirements for clippers", style=discord.TextStyle.paragraph,
                                        max_length=800, required=False,
                                        placeholder="Experience, video length, style, posting schedule, ...")

    def __init__(self, platform: str):
        super().__init__()
        self.platform = platform

    async def on_submit(self, interaction: discord.Interaction):
        ch = interaction.guild.get_channel(STREAMER_SUBMIT_CHANNEL_ID)
        if not ch:
            return await interaction.response.send_message(
                "The submission channel could not be found. Please contact the team.", ephemeral=True)
        user = interaction.user
        emoji, color = PLATFORMS.get(self.platform, PLATFORMS["Other"])
        quote = "\n".join("> " + ln for ln in truncate(self.about.value, 900).splitlines() if ln.strip()) or "> –"
        url = normalize_link(self.social.value)
        e = discord.Embed(title=f"🎥 {user.display_name} is looking for clippers!",
                          description=f"### {emoji} {self.platform}  •  {truncate(self.topic.value, 100)}\n{quote}",
                          color=color, timestamp=discord.utils.utcnow())
        e.set_author(name=str(user), icon_url=user.display_avatar.url)
        e.set_thumbnail(url=user.display_avatar.url)
        e.add_field(name="💰 Payment & rewards", value=truncate(self.rewards.value, 1000), inline=False)
        e.add_field(name="📋 Requirements", value=truncate(self.requirements.value or "None", 1000), inline=False)
        e.add_field(name="🔗 Social media",
                    value=f"[{truncate(self.social.value, 60)}]({url})" if url else truncate(self.social.value, 200))
        e.add_field(name="👤 Streamer", value=user.mention)
        e.set_footer(text=f"Press the button below to get in touch • User ID: {user.id}")
        await ch.send(embed=e, view=ContactView(url))
        streamer_cooldown[user.id] = time.time()
        await interaction.response.send_message("✅ Your request was sent! Clippers can now contact you.", ephemeral=True)


class ContactView(discord.ui.View):
    """Button under every streamer request: opens a private channel for streamer + clipper."""

    def __init__(self, link=None):
        super().__init__(timeout=None)
        if link:
            self.add_item(discord.ui.Button(label="Open Social Media", emoji="🔗", style=discord.ButtonStyle.link, url=link))

    @discord.ui.button(label="Contact Streamer", style=discord.ButtonStyle.success, emoji="🤝", custom_id="streamer:contact")
    async def contact(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, clicker = interaction.guild, interaction.user
        try:
            streamer_id = int(interaction.message.embeds[0].footer.text.rsplit("User ID:", 1)[1].strip())
        except (IndexError, ValueError, AttributeError):
            return await interaction.response.send_message("I couldn't read this request.", ephemeral=True)
        if clicker.id == streamer_id:
            return await interaction.response.send_message("That's your own request. 😉", ephemeral=True)
        streamer = guild.get_member(streamer_id)
        if not streamer:
            return await interaction.response.send_message("This streamer is no longer on the server.", ephemeral=True)
        cat = get_chan(guild, "tickets_category")
        if not cat:
            return await interaction.response.send_message("Contact channels aren't ready yet.", ephemeral=True)
        topic = f"contact:{streamer_id}:{clicker.id}"
        for c in cat.text_channels:
            if c.topic == topic:
                return await interaction.response.send_message(f"You already have a chat with them: {c.mention}",
                                                               ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        P = discord.PermissionOverwrite
        ow = {guild.default_role: P(view_channel=False),
              guild.me: P(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)}
        for m in (streamer, clicker):
            ow[m] = P(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
        if CONTACT_STAFF_CAN_SEE:
            for r in (get_role(guild, "staff"), get_role(guild, "high_team")):
                if r:
                    ow[r] = P(view_channel=True, send_messages=True, read_message_history=True)
        clean = lambda n: re.sub(r"[^a-z0-9]", "", n.lower()) or "user"
        channel = await guild.create_text_channel(f"deal-{clean(streamer.name)}-{clean(clicker.name)}"[:90],
                                                  category=cat, overwrites=ow, topic=topic)
        intro = make_embed("🤝 Streamer ↔ Clipper",
                           f"{streamer.mention} and {clicker.mention}, this private channel is for you two to clarify "
                           "everything: payment, requirements, schedule and more.\n"
                           "-# Be respectful and never share passwords. Use the button below to close this chat.", "ok")
        await channel.send(content=f"{streamer.mention} {clicker.mention}", embeds=[intro, interaction.message.embeds[0]],
                           view=TicketView(), allowed_mentions=discord.AllowedMentions(users=True))
        await interaction.followup.send(f"✅ Your chat is ready: {channel.mention}", ephemeral=True)
        le = make_embed("Contact Channel Opened", color="info")
        le.add_field(name="Streamer", value=streamer.mention)
        le.add_field(name="Clipper", value=clicker.mention)
        le.add_field(name="Channel", value=channel.mention)
        await send_to(guild, "ticket_log", le)


class PlatformSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label="Twitch", emoji="🟣"), discord.SelectOption(label="YouTube", emoji="🔴"),
                   discord.SelectOption(label="Kick", emoji="🟢"), discord.SelectOption(label="TikTok", emoji="⚫"),
                   discord.SelectOption(label="Other", emoji="⚪")]
        super().__init__(placeholder="On which platform do you stream?", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(StreamerModal(self.values[0]))


class PlatformView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(PlatformSelect())


class StreamerPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Find Clippers", style=discord.ButtonStyle.primary, emoji="🎥", custom_id="streamer:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        wait = 300 - (time.time() - streamer_cooldown.get(interaction.user.id, 0))
        if wait > 0:
            return await interaction.response.send_message(
                f"Please wait **{int(wait // 60) + 1} minute(s)** before sending another request.", ephemeral=True)
        e = make_embed("🎥 Step 1/2", "On which platform do you stream? Select it below.\n"
                       "-# Only you can see this message.", "info")
        await interaction.response.send_message(embed=e, view=PlatformView(), ephemeral=True)


# ============================================================
# SUGGESTIONS (with discussion thread)
# ============================================================
class SuggestionView(discord.ui.View):
    def __init__(self, up=0, down=0):
        super().__init__(timeout=None)
        self.up_btn.label = str(up)
        self.down_btn.label = str(down)

    async def vote(self, interaction: discord.Interaction, side: str):
        entry = data["suggestions"].setdefault(str(interaction.message.id), {"up": [], "down": []})
        other = "down" if side == "up" else "up"
        uid = interaction.user.id
        if uid in entry[other]:
            entry[other].remove(uid)
        if uid in entry[side]:
            entry[side].remove(uid)
        else:
            entry[side].append(uid)
        save_data()
        await interaction.response.edit_message(view=SuggestionView(len(entry["up"]), len(entry["down"])))

    @discord.ui.button(emoji="👍", label="0", style=discord.ButtonStyle.success, custom_id="sugg:up")
    async def up_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.vote(interaction, "up")

    @discord.ui.button(emoji="👎", label="0", style=discord.ButtonStyle.danger, custom_id="sugg:down")
    async def down_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.vote(interaction, "down")


async def handle_suggestion(message):
    content = message.content.strip()
    files = []
    try:
        files = [await a.to_file() for a in message.attachments[:4]]
    except discord.HTTPException:
        pass
    try:
        await message.delete()
    except discord.NotFound:
        return  # already handled (e.g. by a second running instance of the bot)
    except discord.HTTPException:
        pass
    if not content and not files:
        return
    e = make_embed("💡 New Suggestion", truncate(content, 4000), "gold")
    e.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
    e.add_field(name="Submitted by", value=message.author.mention)
    e.set_footer(text="Vote with the buttons below • Discuss in the thread")
    if files and files[0].filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        e.set_image(url=f"attachment://{files[0].filename}")
    msg = await message.channel.send(embed=e, files=files, view=SuggestionView(),
                                     allowed_mentions=discord.AllowedMentions.none())
    data["suggestions"][str(msg.id)] = {"up": [], "down": []}
    save_data()
    try:
        thread = await msg.create_thread(name=truncate(f"💬 {content or 'Suggestion'}", 90), auto_archive_duration=1440)
        await thread.send("Discuss this suggestion here. Please stay respectful and constructive!")
    except discord.HTTPException:
        pass


# ============================================================
# AUTO-CREATED CHANNELS (only if missing): honeypot + staff logs
# ============================================================
SPECIAL_CHANNELS = [
    ("mod_log", "🛡️｜mod-log", "staff"),
    ("automod_log", "🤖｜automod-log", "staff"),
    ("member_log", "👥｜member-log", "staff"),
    ("message_log", "💬｜message-log", "staff"),
    ("raid_alerts", "🚨｜raid-alerts", "staff"),
    ("ticket_log", "🎫｜ticket-log", "staff"),
    ("app_review", "📋｜application-review", "staff"),
    ("honeypot", "🚫｜do-not-write", "honeypot"),
]


async def ensure_special_channels(guild):
    P = discord.PermissionOverwrite
    staff, high, ver = get_role(guild, "staff"), get_role(guild, "high_team"), get_role(guild, "verified")
    me = P(view_channel=True, send_messages=True, embed_links=True, manage_messages=True, read_message_history=True,
           manage_channels=True, create_public_threads=True, send_messages_in_threads=True)
    staff_ow = {guild.default_role: P(view_channel=False), guild.me: me}
    for r in (staff, high):
        if r:
            staff_ow[r] = P(view_channel=True, send_messages=True, read_message_history=True)
    staff_cat = next((c for c in guild.categories if c.name.lower() == "staff"), None)
    for key, name, mode in SPECIAL_CHANNELS:
        if get_chan(guild, key):
            continue
        try:
            if mode == "staff":
                if staff_cat is None:
                    staff_cat = await guild.create_category("STAFF", overwrites=staff_ow)
                await guild.create_text_channel(name, category=staff_cat, overwrites=staff_ow)
            else:
                ow = {guild.default_role: P(view_channel=False), guild.me: me}
                if ver:
                    ow[ver] = P(view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
                ref = get_chan(guild, "general")
                await guild.create_text_channel(name, category=ref.category if ref else None, overwrites=ow)
            print(f"[{guild.name}] created channel {name}")
        except discord.HTTPException as e:
            print(f"[{guild.name}] could not create {name}: {e}")
    if staff_cat:
        data["channels"]["staff_category"] = staff_cat.id
        save_data()


async def prepare_guild(guild):
    await ensure_roles(guild)
    await ensure_special_channels(guild)
    await ensure_all_panels(guild)


# ============================================================
# BOT
# ============================================================
class ClipperBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None,
                         activity=discord.Game(name=BOT_ACTIVITY), status=discord.Status.online)
        self.synced = False

    async def setup_hook(self):
        for v in (VerifyView(), RolesView(), SupportView(), TicketView(), ReviewView(),
                  SuggestionView(), StreamerPanelView(), ContactView()):
            self.add_view(v)
        self.add_dynamic_items(ApplyButton)


bot = ClipperBot()


async def sync_guild(guild):
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)


@bot.event
async def on_ready():
    if not bot.synced:
        for g in bot.guilds:
            await sync_guild(g)
        bot.synced = True
        for g in bot.guilds:
            await prepare_guild(g)
        try:  # bot profile "About Me"
            await bot.http.request(discord.http.Route("PATCH", "/applications/@me"), json={"description": BOT_ABOUT})
        except Exception as e:
            print("Could not set the bot's About Me automatically. Set it in the Developer Portal instead:", e)
    print(f"Logged in as {bot.user} ({len(bot.guilds)} server(s))")


@bot.event
async def on_guild_join(guild):
    await sync_guild(guild)
    await prepare_guild(guild)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You don't have permission to use this command."
    else:
        traceback.print_exception(type(error), error, error.__traceback__)
        msg = "Something went wrong while running that command."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ============================================================
# COMMANDS
# ============================================================
@bot.tree.command(name="createroles", description="Create all server roles (existing roles are skipped)")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def createroles(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    created, existing = await ensure_roles(interaction.guild)
    e = make_embed("Roles", color="ok")
    e.add_field(name=f"Created ({len(created)})", value=", ".join(created) or "None", inline=False)
    e.add_field(name=f"Already existed ({len(existing)})", value=", ".join(existing) or "None", inline=False)
    await interaction.followup.send(embed=e, ephemeral=True)


@bot.tree.command(name="addapplication", description="Open a new position for applications")
@app_commands.describe(title="Name of the position, e.g. Clipper, Editor, Moderator")
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def addapplication(interaction: discord.Interaction, title: str):
    guild = interaction.guild
    if not get_chan(guild, "applications"):
        return await interaction.response.send_message("I can't find the applications channel.", ephemeral=True)
    if len(data["jobs"]) >= 25:
        return await interaction.response.send_message("You can't have more than 25 open positions.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    data["job_counter"] += 1
    data["jobs"][str(data["job_counter"])] = truncate(title.strip(), 80)
    save_data()
    await refresh_application_panel(guild)
    role, ch = get_role(guild, "application_ping"), get_chan(guild, "applications")
    if role and ch:
        await ch.send(f"{role.mention} A new position is open: **{title}**",
                      allowed_mentions=discord.AllowedMentions(roles=True), delete_after=3600)
    await interaction.followup.send(f"✅ Position **{title}** is now open.", ephemeral=True)


async def job_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=t, value=jid) for jid, t in data["jobs"].items()
            if current.lower() in t.lower()][:25]


@bot.tree.command(name="removeapplication", description="Close an open position")
@app_commands.describe(position="The position to close")
@app_commands.autocomplete(position=job_autocomplete)
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def removeapplication(interaction: discord.Interaction, position: str):
    title = data["jobs"].pop(position, None)
    if not title:
        return await interaction.response.send_message("Position not found.", ephemeral=True)
    save_data()
    await interaction.response.defer(ephemeral=True)
    await refresh_application_panel(interaction.guild)
    await interaction.followup.send(f"Position **{title}** closed.", ephemeral=True)


@bot.tree.command(name="raidmode", description="Turn raid protection on or off manually")
@app_commands.describe(enabled="True = on, False = off")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def raidmode(interaction: discord.Interaction, enabled: bool):
    global raid_mode_until
    if enabled:
        await interaction.response.send_message(f"🛡️ Raid mode enabled for {RAID_MODE_MINUTES} minutes.", ephemeral=True)
        await enable_raid_mode(interaction.guild, f"Enabled manually by {interaction.user.mention}.")
    else:
        raid_mode_until = 0.0
        await interaction.response.send_message("✅ Raid mode disabled.", ephemeral=True)
        await send_to(interaction.guild, "raid_alerts",
                      make_embed("Raid mode disabled", f"Disabled by {interaction.user.mention}.", "ok"))


# ============================================================
# MODERATION COMMANDS
# ============================================================
def mod_command(name, description, perm):
    def deco(func):
        func = app_commands.guild_only()(func)
        func = app_commands.checks.has_permissions(**{perm: True})(func)
        func = app_commands.default_permissions(**{perm: True})(func)
        return bot.tree.command(name=name, description=description)(func)
    return deco


@mod_command("warn", "Warn a member", "moderate_members")
@app_commands.describe(member="Member to warn", reason="Reason for the warning")
async def warn_cmd(interaction: discord.Interaction, member: discord.Member, reason: str):
    err = hierarchy_error(interaction.guild, interaction.user, member)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    case = next_case()
    warns = data["warnings"].setdefault(str(member.id), [])
    warns.append({"case": case, "reason": reason, "mod": interaction.user.id, "time": int(time.time())})
    save_data()
    count = len(warns)
    await try_dm(member, dm_embed(interaction.guild, "warned", reason, f"Total warnings: {count}"))
    e = case_embed("Member Warned", "warn", case, member, interaction.user.mention, reason,
                   [("Total warnings", str(count))])
    await interaction.response.send_message(embed=e)
    await send_to(interaction.guild, "mod_log", e)
    if count in WARN_ESCALATION:
        dur = WARN_ESCALATION[count]
        try:
            await member.timeout(dur, reason=f"Reached {count} warnings")
            e2 = case_embed("Automatic Timeout", "bad", next_case(), member, "AutoMod",
                            f"Reached {count} warnings", [("Duration", fmt_td(dur))])
            await send_to(interaction.guild, "mod_log", e2)
            await interaction.followup.send(f"⚠️ {member.mention} reached **{count}** warnings and was timed out for "
                                            f"**{fmt_td(dur)}**.")
        except discord.HTTPException:
            pass


@mod_command("warnings", "Show the warnings of a member", "moderate_members")
@app_commands.describe(member="Member to check")
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member):
    warns = data["warnings"].get(str(member.id), [])
    e = make_embed(f"Warnings for {member}", color="warn")
    if not warns:
        e.description = "No warnings."
    for w in warns[-10:]:
        e.add_field(name=f"Case #{w['case']} • <t:{w['time']}:R>",
                    value=f"{truncate(w['reason'], 300)}\n-# by <@{w['mod']}>", inline=False)
    e.set_footer(text=f"Total: {len(warns)}")
    await interaction.response.send_message(embed=e, ephemeral=True)


@mod_command("clearwarnings", "Remove all warnings of a member", "manage_guild")
@app_commands.describe(member="Member whose warnings should be cleared")
async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
    n = len(data["warnings"].pop(str(member.id), []))
    save_data()
    await interaction.response.send_message(f"Cleared {n} warning(s) for {member.mention}.")
    e = make_embed("Warnings Cleared", color="ok")
    e.add_field(name="User", value=f"{member.mention}\n`{member.id}`")
    e.add_field(name="Moderator", value=interaction.user.mention)
    e.add_field(name="Cleared", value=str(n))
    await send_to(interaction.guild, "mod_log", e)


@mod_command("kick", "Kick a member", "kick_members")
@app_commands.describe(member="Member to kick", reason="Reason")
async def kick_cmd(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    err = hierarchy_error(interaction.guild, interaction.user, member)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    case = next_case()
    await try_dm(member, dm_embed(interaction.guild, "kicked", reason))
    try:
        await member.kick(reason=f"[{interaction.user}] {reason}")
    except discord.HTTPException:
        return await interaction.response.send_message("I couldn't kick that member.", ephemeral=True)
    e = case_embed("Member Kicked", "warn", case, member, interaction.user.mention, reason)
    await interaction.response.send_message(embed=e)
    await send_to(interaction.guild, "mod_log", e)
    await record_action(interaction.guild, interaction.user, "member removal", f"Kicked {member} (`{member.id}`)")


@mod_command("ban", "Ban a user", "ban_members")
@app_commands.describe(user="User to ban", reason="Reason", delete_days="Delete their messages of the last X days (0-7)")
async def ban_cmd(interaction: discord.Interaction, user: discord.User, reason: str = "No reason provided",
                  delete_days: app_commands.Range[int, 0, 7] = 0):
    guild = interaction.guild
    member = guild.get_member(user.id)
    err = hierarchy_error(guild, interaction.user, member or user)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    case = next_case()
    await try_dm(user, dm_embed(guild, "banned", reason))
    try:
        await guild.ban(user, reason=f"[{interaction.user}] {reason}", delete_message_seconds=delete_days * 86400)
    except discord.HTTPException:
        return await interaction.response.send_message("I couldn't ban that user.", ephemeral=True)
    e = case_embed("Member Banned", "bad", case, user, interaction.user.mention, reason,
                   [("Messages deleted", f"{delete_days} day(s)")])
    await interaction.response.send_message(embed=e)
    await send_to(guild, "mod_log", e)
    await record_action(guild, interaction.user, "member removal", f"Banned {user} (`{user.id}`)")


@mod_command("unban", "Unban a user by ID", "ban_members")
@app_commands.describe(user_id="The ID of the banned user", reason="Reason")
async def unban_cmd(interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
    if not user_id.isdigit():
        return await interaction.response.send_message("Please provide a valid user ID.", ephemeral=True)
    try:
        await interaction.guild.unban(discord.Object(id=int(user_id)), reason=f"[{interaction.user}] {reason}")
    except discord.NotFound:
        return await interaction.response.send_message("That user is not banned.", ephemeral=True)
    case = next_case()
    try:
        user = await bot.fetch_user(int(user_id))
    except discord.HTTPException:
        user = discord.Object(id=int(user_id))
        user.mention = f"<@{user_id}>"
    e = case_embed("Member Unbanned", "ok", case, user, interaction.user.mention, reason)
    await interaction.response.send_message(embed=e)
    await send_to(interaction.guild, "mod_log", e)


@mod_command("timeout", "Timeout a member", "moderate_members")
@app_commands.describe(member="Member to timeout", duration="e.g. 30m, 2h, 1d (max 28d)", reason="Reason")
async def timeout_cmd(interaction: discord.Interaction, member: discord.Member, duration: str,
                      reason: str = "No reason provided"):
    err = hierarchy_error(interaction.guild, interaction.user, member)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    td = parse_duration(duration)
    if not td:
        return await interaction.response.send_message("Invalid duration. Use e.g. `30m`, `2h` or `1d` (max 28d).",
                                                       ephemeral=True)
    try:
        await member.timeout(td, reason=f"[{interaction.user}] {reason}")
    except discord.HTTPException:
        return await interaction.response.send_message("I couldn't timeout that member.", ephemeral=True)
    case = next_case()
    await try_dm(member, dm_embed(interaction.guild, "timed out", reason, f"Duration: {fmt_td(td)}"))
    e = case_embed("Member Timed Out", "warn", case, member, interaction.user.mention, reason,
                   [("Duration", fmt_td(td))])
    await interaction.response.send_message(embed=e)
    await send_to(interaction.guild, "mod_log", e)


@mod_command("untimeout", "Remove a timeout", "moderate_members")
@app_commands.describe(member="Member to release")
async def untimeout_cmd(interaction: discord.Interaction, member: discord.Member):
    try:
        await member.timeout(None, reason=f"[{interaction.user}] timeout removed")
    except discord.HTTPException:
        return await interaction.response.send_message("I couldn't remove that timeout.", ephemeral=True)
    case = next_case()
    e = case_embed("Timeout Removed", "ok", case, member, interaction.user.mention, "Timeout removed manually")
    await interaction.response.send_message(embed=e)
    await send_to(interaction.guild, "mod_log", e)


@mod_command("purge", "Delete multiple messages", "manage_messages")
@app_commands.describe(amount="How many messages (1-100)", member="Only delete messages of this member")
async def purge_cmd(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100],
                    member: Optional[discord.Member] = None):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(
        limit=amount, check=(lambda m: m.author.id == member.id) if member else (lambda m: True))
    await interaction.followup.send(f"🧹 Deleted {len(deleted)} message(s).", ephemeral=True)
    e = make_embed("Messages Purged", color="warn")
    e.add_field(name="Channel", value=interaction.channel.mention)
    e.add_field(name="Amount", value=str(len(deleted)))
    e.add_field(name="Moderator", value=interaction.user.mention)
    if member:
        e.add_field(name="Filtered by", value=member.mention)
    await send_to(interaction.guild, "mod_log", e)


@mod_command("slowmode", "Set the slowmode of this channel", "manage_channels")
@app_commands.describe(seconds="0 turns slowmode off (max 21600)")
async def slowmode_cmd(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
    await interaction.channel.edit(slowmode_delay=seconds, reason=f"[{interaction.user}] slowmode")
    await interaction.response.send_message(f"⏱️ Slowmode set to **{seconds}s**.")
    e = make_embed("Slowmode Changed", color="info")
    e.add_field(name="Channel", value=interaction.channel.mention)
    e.add_field(name="Delay", value=f"{seconds}s")
    e.add_field(name="Moderator", value=interaction.user.mention)
    await send_to(interaction.guild, "mod_log", e)


async def set_lock(interaction, locked, channel):
    channel = channel or interaction.channel
    ver = get_role(interaction.guild, "verified")
    ow = channel.overwrites_for(ver)
    ow.send_messages = False if locked else True
    await channel.set_permissions(ver, overwrite=ow, reason=f"[{interaction.user}] {'lock' if locked else 'unlock'}")
    await interaction.response.send_message(f"{'🔒 Locked' if locked else '🔓 Unlocked'} {channel.mention}.")
    e = make_embed("Channel Locked" if locked else "Channel Unlocked", color="warn" if locked else "ok")
    e.add_field(name="Channel", value=channel.mention)
    e.add_field(name="Moderator", value=interaction.user.mention)
    await send_to(interaction.guild, "mod_log", e)


@mod_command("lock", "Lock a chat channel for members", "manage_channels")
@app_commands.describe(channel="Channel to lock (default: this one)")
async def lock_cmd(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    await set_lock(interaction, True, channel)


@mod_command("unlock", "Unlock a chat channel for members", "manage_channels")
@app_commands.describe(channel="Channel to unlock (default: this one)")
async def unlock_cmd(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    await set_lock(interaction, False, channel)


@mod_command("userinfo", "Show information about a member", "moderate_members")
@app_commands.describe(member="Member to look up")
async def userinfo_cmd(interaction: discord.Interaction, member: discord.Member):
    e = make_embed(str(member), color="info")
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="ID", value=f"`{member.id}`")
    e.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"))
    e.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "?")
    e.add_field(name="Warnings", value=str(len(data["warnings"].get(str(member.id), []))))
    roles = [r.mention for r in member.roles if not r.is_default()]
    e.add_field(name="Roles", value=truncate(" ".join(roles) or "None", 1000), inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


# ============================================================
# EVENTS
# ============================================================
@bot.event
async def on_member_join(member):
    guild = member.guild
    je = make_embed("Member Joined", f"{member.mention} joined the server.", "ok")
    je.set_thumbnail(url=member.display_avatar.url)
    je.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"))
    je.add_field(name="Member count", value=str(guild.member_count))
    if (discord.utils.utcnow() - member.created_at).days < 7:
        je.add_field(name="⚠️ New account", value="Created less than 7 days ago", inline=False)
    je.set_footer(text=f"ID: {member.id}")
    await send_to(guild, "member_log", je)
    now = time.time()
    join_times.append(now)
    while join_times and now - join_times[0] > JOIN_WINDOW:
        join_times.popleft()
    if len(join_times) >= JOIN_LIMIT and not raid_mode_active():
        await enable_raid_mode(guild, f"**{len(join_times)}** members joined within {JOIN_WINDOW} seconds.")

    if member.bot:
        return
    age_days = (discord.utils.utcnow() - member.created_at).days
    if raid_mode_active() and age_days < RAID_KICK_ACCOUNT_DAYS:
        await try_dm(member, make_embed(f"Welcome to {guild.name}",
                                        "The server is under raid protection right now. Please try joining again "
                                        "in a little while.", "warn"))
        try:
            await member.kick(reason="Raid mode: account too new")
            await send_to(guild, "raid_alerts", make_embed(
                "New account kicked", f"{member.mention} (`{member.id}`) • account age: {age_days} day(s)", "warn"))
        except discord.HTTPException:
            pass
        return

    unv = get_role(guild, "unverified")
    if unv:
        try:
            await member.add_roles(unv, reason="New member")
        except discord.HTTPException:
            pass
    ch, verify = get_chan(guild, "welcome"), get_chan(guild, "verify")
    if ch:
        w = make_embed(f"Welcome to {guild.name}! 🎉",
                       f"Hey {member.mention}, we're happy to have you here!\n"
                       f"Head over to {verify.mention if verify else '#verify'} to verify yourself and unlock the server.\n"
                       f"-# You are member #{guild.member_count} • Verification unlocks all channels", "brand")
        w.set_thumbnail(url=member.display_avatar.url)
        w.set_footer(text=guild.name)
        await ch.send(content=member.mention, embed=w, allowed_mentions=discord.AllowedMentions(users=True))


@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    guild, member = message.guild, message.author

    hp = get_chan(guild, "honeypot")
    if hp and message.channel.id == hp.id:
        if isinstance(member, discord.Member) and is_staff(member):
            return
        bot_deleted.add(message.id)
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        await try_dm(member, make_embed(f"You were kicked from {guild.name}",
                                        "You wrote in the security trap channel. If your account was hacked, "
                                        "secure it and join again.", "bad"))
        result = "Kicked"
        try:
            await guild.kick(member, reason="Wrote in the honeypot channel")
        except discord.HTTPException:
            result = "Kick FAILED (check my role position)"
        e = make_embed("🍯 Honeypot triggered", color="bad")
        e.add_field(name="User", value=f"{member.mention}\n`{member.id}`")
        e.add_field(name="Result", value=result)
        e.add_field(name="Message", value=f"```{truncate(message.content.replace('```', ''), 800) or '(no text)'}```",
                    inline=False)
        await send_to(guild, "raid_alerts", e, content=await raid_ping(guild),
                      allowed_mentions=discord.AllowedMentions(roles=True))
        await send_to(guild, "mod_log", e)
        return

    if await run_automod(message):
        return
    sugg = get_chan(guild, "suggestions")
    if sugg and message.channel.id == sugg.id:
        await handle_suggestion(message)


@bot.event
async def on_member_remove(member):
    guild = member.guild
    e = make_embed("Member Left", f"{member.mention} (`{member}`) left the server.", "warn")
    if member.joined_at:
        e.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R"))
    roles = [r.mention for r in member.roles if not r.is_default()]
    if roles:
        e.add_field(name="Roles", value=truncate(" ".join(roles), 900), inline=False)
    e.set_footer(text=f"ID: {member.id}")
    await send_to(guild, "member_log", e)
    entry = await find_audit(guild, discord.AuditLogAction.kick, member.id)
    if entry and entry.user and entry.user.id != bot.user.id:
        ke = case_embed("Member Kicked", "warn", next_case(), member, entry.user.mention,
                        entry.reason or "No reason provided")
        await send_to(guild, "mod_log", ke)
        await record_action(guild, entry.user, "member removal", f"Kicked {member} (`{member.id}`)")


@bot.event
async def on_member_ban(guild, user):
    entry = await find_audit(guild, discord.AuditLogAction.ban, user.id)
    if entry and entry.user and entry.user.id == bot.user.id:
        return  # already logged by the command
    executor = entry.user if entry else None
    e = case_embed("Member Banned", "bad", next_case(), user, executor.mention if executor else "Unknown",
                   (entry.reason if entry and entry.reason else "No reason provided"))
    await send_to(guild, "mod_log", e)
    if executor:
        await record_action(guild, executor, "member removal", f"Banned {user} (`{user.id}`)")


@bot.event
async def on_member_unban(guild, user):
    entry = await find_audit(guild, discord.AuditLogAction.unban, user.id)
    if entry and entry.user and entry.user.id == bot.user.id:
        return
    e = case_embed("Member Unbanned", "ok", next_case(), user, entry.user.mention if entry else "Unknown",
                   (entry.reason if entry and entry.reason else "No reason provided"))
    await send_to(guild, "mod_log", e)


@bot.event
async def on_member_update(before, after):
    guild = after.guild
    if before.timed_out_until != after.timed_out_until:
        entry = await find_audit(guild, discord.AuditLogAction.member_update, after.id)
        if not (entry and entry.user and entry.user.id == bot.user.id):
            active = after.timed_out_until and after.timed_out_until > discord.utils.utcnow()
            e = case_embed("Member Timed Out" if active else "Timeout Removed", "warn" if active else "ok",
                           next_case(), after, entry.user.mention if entry and entry.user else "Unknown",
                           (entry.reason if entry and entry.reason else "No reason provided"),
                           [("Until", discord.utils.format_dt(after.timed_out_until, "R"))] if active else None)
            await send_to(guild, "mod_log", e)
    ignore = {r.id for k in ("unverified", "verified", "announcements", "search_clipper", "tips", "application_ping")
              if (r := get_role(guild, k))}
    added = [r for r in set(after.roles) - set(before.roles) if r.id not in ignore]
    removed = [r for r in set(before.roles) - set(after.roles) if r.id not in ignore]
    if added or removed:
        e = make_embed("Roles Updated", f"Member: {after.mention}", "info")
        if added:
            e.add_field(name="Added", value=" ".join(r.mention for r in added))
        if removed:
            e.add_field(name="Removed", value=" ".join(r.mention for r in removed))
        e.set_footer(text=f"ID: {after.id}")
        await send_to(guild, "mod_log", e)
    if before.nick != after.nick:
        e = make_embed("Nickname Changed", f"Member: {after.mention}", "info")
        e.add_field(name="Before", value=before.nick or "None")
        e.add_field(name="After", value=after.nick or "None")
        await send_to(guild, "member_log", e)


@bot.event
async def on_message_edit(before, after):
    if after.author.bot or not after.guild:
        return
    if await run_automod(after):
        return
    if before.content == after.content or is_private_channel(after.channel):
        return
    e = make_embed("Message Edited", f"{after.author.mention} in {after.channel.mention} • [Jump]({after.jump_url})", "info")
    e.add_field(name="Before", value=codeblock(before.content or "(unknown)"), inline=False)
    e.add_field(name="After", value=codeblock(after.content or "(empty)"), inline=False)
    await send_to(after.guild, "message_log", e)


@bot.event
async def on_message_delete(message):
    if not message.guild or message.author.bot:
        return
    if message.id in bot_deleted:
        bot_deleted.discard(message.id)
        return
    if is_private_channel(message.channel) or (not message.content and not message.attachments):
        return
    e = make_embed("Message Deleted", f"{message.author.mention} in {message.channel.mention}", "bad")
    if message.content:
        e.add_field(name="Content", value=codeblock(message.content), inline=False)
    if message.attachments:
        e.add_field(name="Attachments", value="\n".join(a.filename for a in message.attachments)[:900], inline=False)
    e.set_footer(text=f"User ID: {message.author.id}")
    await send_to(message.guild, "message_log", e)


@bot.event
async def on_bulk_message_delete(messages):
    if not messages or is_private_channel(messages[0].channel):
        return
    e = make_embed("Bulk Delete", f"**{len(messages)}** messages were deleted in {messages[0].channel.mention}.", "warn")
    await send_to(messages[0].guild, "message_log", e)


async def structure_event(guild, title, color, action, target_id, name_text, group=False):
    entry = await find_audit(guild, action, target_id)
    executor = entry.user if entry else None
    if executor and executor.id == bot.user.id:
        return
    e = make_embed(title, name_text, color)
    e.add_field(name="By", value=executor.mention if executor else "Unknown")
    await send_to(guild, "mod_log", e)
    if group and executor:
        await record_action(guild, executor, "channel/role deletion", name_text)


@bot.event
async def on_guild_channel_create(channel):
    await structure_event(channel.guild, "Channel Created", "ok", discord.AuditLogAction.channel_create,
                          channel.id, f"#{channel.name}")


@bot.event
async def on_guild_channel_delete(channel):
    await structure_event(channel.guild, "Channel Deleted", "bad", discord.AuditLogAction.channel_delete,
                          channel.id, f"#{channel.name}", group=True)


@bot.event
async def on_guild_role_create(role):
    await structure_event(role.guild, "Role Created", "ok", discord.AuditLogAction.role_create,
                          role.id, f"@{role.name}")


@bot.event
async def on_guild_role_delete(role):
    await structure_event(role.guild, "Role Deleted", "bad", discord.AuditLogAction.role_delete,
                          role.id, f"@{role.name}", group=True)


if __name__ == "__main__":
    bot.run(os.environ["DISCORD_TOKEN"])
