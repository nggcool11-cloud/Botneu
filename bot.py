"""
Clipper Community Bot (slim version)
Uses your EXISTING channels. On every start the bot checks all embeds/panels
and posts the ones that are missing. Roles, verification, tickets, applications,
streamer requests, suggestions with threads, honeypot channel and anti-raid.
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
from collections import deque
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
    "ticket_log": (["ticket-log"], []),
    "app_review": (["application-review", "app-review"], []),
    "raid_alerts": (["raid-alert"], []),
}

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
DEFAULTS = {"channels": {}, "roles": {}, "messages": {}, "jobs": {}, "job_counter": 0, "suggestions": {}}


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
        owner_id = int(topic.split(":")[1]) if topic.startswith("ticket:") else None
        if not (is_staff(interaction.user) or interaction.user.id == owner_id):
            return await interaction.response.send_message("Only the ticket owner or staff can close this.", ephemeral=True)
        await interaction.response.send_message("🔒 Closing this ticket in 5 seconds…")
        lines = []
        async for m in channel.history(limit=1000, oldest_first=True):
            lines.append(f"[{m.created_at:%Y-%m-%d %H:%M}] {m.author}: {m.clean_content}")
        file = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"{channel.name}.txt")
        e = make_embed("Ticket Closed", color="bad")
        e.add_field(name="Ticket", value=f"`{channel.name}`")
        e.add_field(name="Closed by", value=interaction.user.mention)
        if owner_id:
            e.add_field(name="Owner", value=f"<@{owner_id}>")
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
        e = make_embed("🎥 New Clipper Search Request", color="brand")
        e.set_author(name=str(user), icon_url=user.display_avatar.url)
        e.set_thumbnail(url=user.display_avatar.url)
        e.add_field(name="Streamer", value=f"{user.mention}\n`{user.id}`")
        e.add_field(name="Platform", value=self.platform)
        e.add_field(name="Streaming topic", value=self.topic.value)
        e.add_field(name="Social media", value=truncate(self.social.value, 200), inline=False)
        e.add_field(name="About", value=truncate(self.about.value, 1000), inline=False)
        e.add_field(name="Payment / rewards for clippers", value=truncate(self.rewards.value, 1000), inline=False)
        e.add_field(name="Further requirements", value=truncate(self.requirements.value or "None", 1000), inline=False)
        e.set_footer(text=f"User ID: {user.id}")
        await ch.send(embed=e)
        streamer_cooldown[user.id] = time.time()
        await interaction.response.send_message("✅ Your request was sent! Our team will take a look at it.", ephemeral=True)


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
                  SuggestionView(), StreamerPanelView()):
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
            await ensure_all_panels(g)
        try:  # bot profile "About Me"
            await bot.http.request(discord.http.Route("PATCH", "/applications/@me"), json={"description": BOT_ABOUT})
        except Exception as e:
            print("Could not set the bot's About Me automatically. Set it in the Developer Portal instead:", e)
    print(f"Logged in as {bot.user} ({len(bot.guilds)} server(s))")


@bot.event
async def on_guild_join(guild):
    await sync_guild(guild)
    await ensure_all_panels(guild)


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
# EVENTS
# ============================================================
@bot.event
async def on_member_join(member):
    guild = member.guild
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
        return

    sugg = get_chan(guild, "suggestions")
    if sugg and message.channel.id == sugg.id:
        await handle_suggestion(message)


if __name__ == "__main__":
    bot.run(os.environ["DISCORD_TOKEN"])
