"""
ClipForge – Discord server setup bot (roles, categories, channels, permissions).

Single-file version. Run with:  python main.py
Needs the environment variable DISCORD_TOKEN (optional: GUILD_ID).
"""

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from functools import reduce
from operator import or_

import discord
from discord import app_commands

try:  # optional: load a .env file if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ----------------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------------
ADMIN_ROLE_HAS_ADMINISTRATOR = False   # True = role "Administrator" gets the real Discord Administrator permission
UPDATE_EXISTING_ROLES = True           # True = permissions/colour of already existing ClipForge roles are synced
SETUP_PERMISSION = "administrator"     # permission the caller of /setup_server needs (server owner always allowed)
REASON = "ClipForge setup"

log = logging.getLogger("clipforge")

# ----------------------------------------------------------------------------
# ROLES (highest -> lowest)
# ----------------------------------------------------------------------------
FOUNDER = "👑 Founder"
MANAGEMENT = "🛡️ Management"
ADMIN = "🔧 Administrator"
MOD = "🔨 Moderator"
SUPPORT = "🎫 Support Team"
PARTNER = "🤝 Partner"
STREAMER = "🎥 Streamer"
CLIPPER = "✂️ Clipper"
EDITOR = "🎬 Video Editor"
VIP = "💎 VIP"
ACTIVE = "🌟 Active Member"
ANNOUNCE = "🔔 Announcements"
MEMBER = "👤 Member"
NEWMEMBER = "🆕 New Member"
BOT = "🤖 Bot"

P = discord.Permissions
C = discord.Colour


@dataclass(frozen=True)
class RoleSpec:
    name: str
    colour: discord.Colour
    permissions: discord.Permissions
    hoist: bool = False
    mentionable: bool = False


STAFF_BASE = dict(
    manage_messages=True, manage_threads=True, moderate_members=True,
    mute_members=True, deafen_members=True, move_members=True,
    manage_nicknames=True, view_audit_log=True, create_instant_invite=True,
)

ROLE_SPECS = [
    RoleSpec(FOUNDER, C.gold(),
             P(manage_guild=True, manage_roles=True, manage_channels=True, kick_members=True,
               ban_members=True, mention_everyone=True, **STAFF_BASE), hoist=True),
    RoleSpec(MANAGEMENT, C.red(),
             P(manage_guild=True, manage_roles=True, manage_channels=True, kick_members=True,
               ban_members=True, mention_everyone=True, **STAFF_BASE), hoist=True),
    RoleSpec(ADMIN, C.purple(),
             P(administrator=ADMIN_ROLE_HAS_ADMINISTRATOR, manage_channels=True, kick_members=True,
               mention_everyone=True, **STAFF_BASE), hoist=True),
    RoleSpec(MOD, C.blue(), P(**STAFF_BASE), hoist=True),
    RoleSpec(SUPPORT, C.green(), P(manage_threads=True), hoist=True),
    RoleSpec(PARTNER, C.teal(), P(), hoist=True),
    RoleSpec(STREAMER, C.orange(), P(), hoist=True),
    RoleSpec(CLIPPER, C.from_rgb(0, 188, 212), P(), hoist=True),
    RoleSpec(EDITOR, C.magenta(), P(), hoist=True),
    RoleSpec(VIP, C.from_rgb(255, 200, 60), P(), hoist=True),
    RoleSpec(ACTIVE, C.from_rgb(144, 238, 144), P()),
    RoleSpec(ANNOUNCE, C.light_grey(), P(), mentionable=True),
    RoleSpec(MEMBER, C.default(), P()),
    RoleSpec(NEWMEMBER, C.from_rgb(189, 195, 199), P()),
    RoleSpec(BOT, C.dark_grey(), P()),
]

# permission groups used for channel access
STAFF_ALL = (FOUNDER, MANAGEMENT, ADMIN, MOD, SUPPORT)
STAFF_TOP = (FOUNDER, MANAGEMENT, ADMIN)
STAFF_MOD = (FOUNDER, MANAGEMENT, ADMIN, MOD)
STAFF_APPS = (FOUNDER, MANAGEMENT, ADMIN, SUPPORT)

# ----------------------------------------------------------------------------
# CATEGORIES + CHANNELS
# mode: "read"  = everyone reads, only `posters` write
#       "chat"  = everyone reads and writes
#       "media" = like chat, plus attachments / embeds
#       "voice" = public voice
#       "private" = only `viewers` see it, only `posters` write / speak
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    mode: str = "chat"
    voice: bool = False
    viewers: tuple = ()
    posters: tuple = STAFF_MOD
    topic: str = None


@dataclass(frozen=True)
class CategorySpec:
    name: str
    channels: tuple
    private_viewers: tuple = None  # set => whole category is private


CATEGORIES = [
    CategorySpec("WELCOME", (
        ChannelSpec("🛬︱welcome", "read"),
        ChannelSpec("📘︱rules", "read"),
        ChannelSpec("🎭︱roles", "read"),
        ChannelSpec("🎫︱support", "read", topic="Support – Ticket-System folgt."),
        ChannelSpec("📣︱announcements", "read"),
        ChannelSpec("📋︱applications", "read", topic="Bewerbungen – Ticket-System folgt."),
        ChannelSpec("📌︱server-info", "read"),
    )),
    CategorySpec("COMMUNITY", (
        ChannelSpec("💬︱general-chat", "chat"),
        ChannelSpec("📸︱media", "media"),
        ChannelSpec("🤖︱bot-cmds", "chat"),
        ChannelSpec("💡︱suggestions", "chat"),
    )),
    CategorySpec("CLIPPER HUB", (
        ChannelSpec("🔎︱search-clipper", "chat", topic="Streamer suchen Clipper."),
        ChannelSpec("🎬︱search-streamer", "chat", topic="Clipper suchen Streamer."),
        ChannelSpec("📂︱clip-showcase", "media", topic="Eure fertigen Clips und Edits."),
        ChannelSpec("🏆︱best-clips", "read", topic="Vom Team ausgewählte Top-Clips."),
        ChannelSpec("💡︱tips", "chat"),
        ChannelSpec("📚︱editing-resources", "chat"),
        ChannelSpec("🧰︱creator-tools", "chat"),
        ChannelSpec("🤝︱collaboration", "chat"),
    )),
    CategorySpec("PARTNERS", (
        ChannelSpec("🌐︱our-partners", "read"),
        ChannelSpec("📣︱partner-announcements", "read"),
        ChannelSpec("🤝︱partnership-requests", "chat"),
    )),
    CategorySpec("PUBLIC VOICE", (
        ChannelSpec("🔊︱voice-1", "voice", voice=True),
        ChannelSpec("🔊︱voice-2", "voice", voice=True),
        ChannelSpec("🔊︱voice-3", "voice", voice=True),
    )),
    CategorySpec("STAFF AREA", private_viewers=STAFF_ALL, channels=(
        ChannelSpec("💬︱staff-chat", "private", viewers=STAFF_ALL, posters=STAFF_ALL),
        ChannelSpec("📣︱staff-announcements", "private", viewers=STAFF_ALL, posters=STAFF_TOP),
        ChannelSpec("📝︱staff-log", "private", viewers=STAFF_TOP, posters=()),
        ChannelSpec("🎫︱support-team", "private", viewers=STAFF_ALL, posters=STAFF_ALL),
        ChannelSpec("📋︱application-review", "private", viewers=STAFF_APPS, posters=STAFF_APPS),
        ChannelSpec("🚨︱reports", "private", viewers=STAFF_MOD, posters=STAFF_MOD),
        ChannelSpec("🔊︱staff-voice", "private", voice=True, viewers=STAFF_ALL, posters=STAFF_ALL),
    )),
]

# ----------------------------------------------------------------------------
# PERMISSION HELPERS
# ----------------------------------------------------------------------------
PO = discord.PermissionOverwrite

# What the bot itself must hold to create everything and to grant overwrites.
REQUIRED_BOT_PERMS = P(
    view_channel=True, manage_roles=True, manage_channels=True, send_messages=True,
    send_messages_in_threads=True, read_message_history=True, attach_files=True,
    embed_links=True, connect=True, speak=True,
)

# Used for the invite link printed on startup (no Administrator!).
INVITE_PERMS = P(reduce(or_, [s.permissions.value for s in ROLE_SPECS], REQUIRED_BOT_PERMS.value)
                 & ~P(administrator=True).value)


def perm_names(value: int) -> str:
    return ", ".join(name for name, on in P(value) if on)


def channel_overwrites(guild: discord.Guild, roles: dict, spec: ChannelSpec) -> dict:
    """Central place that defines who may do what in a channel."""
    everyone = guild.default_role
    me = guild.me
    ows = {}

    if spec.mode == "private":
        if spec.voice:
            ows[everyone] = PO(view_channel=False, connect=False)
            for name in spec.viewers:
                if name in roles:
                    ows[roles[name]] = PO(view_channel=True, connect=True, speak=name in spec.posters)
            ows[me] = PO(view_channel=True, connect=True)
        else:
            ows[everyone] = PO(view_channel=False, read_message_history=False, send_messages=False,
                               send_messages_in_threads=False)
            for name in spec.viewers:
                if name in roles:
                    can_write = name in spec.posters
                    ows[roles[name]] = PO(view_channel=True, read_message_history=True,
                                          send_messages=can_write, send_messages_in_threads=can_write)
            ows[me] = PO(view_channel=True, read_message_history=True, send_messages=True,
                         send_messages_in_threads=True)

    elif spec.mode == "read":
        ows[everyone] = PO(view_channel=True, read_message_history=True, send_messages=False,
                           send_messages_in_threads=False, create_public_threads=False,
                           create_private_threads=False)
        for name in spec.posters:
            if name in roles:
                ows[roles[name]] = PO(send_messages=True, send_messages_in_threads=True)
        ows[me] = PO(view_channel=True, send_messages=True, send_messages_in_threads=True)

    elif spec.mode in ("chat", "media"):
        values = dict(view_channel=True, read_message_history=True, send_messages=True)
        if spec.mode == "media":
            values.update(attach_files=True, embed_links=True)
        ows[everyone] = PO(**values)

    elif spec.mode == "voice":
        ows[everyone] = PO(view_channel=True, connect=True, speak=True)

    return ows


def category_overwrites(guild: discord.Guild, roles: dict, spec: CategorySpec) -> dict:
    if spec.private_viewers is None:
        return {}
    ows = {guild.default_role: PO(view_channel=False, connect=False),
           guild.me: PO(view_channel=True, connect=True)}
    for name in spec.private_viewers:
        if name in roles:
            ows[roles[name]] = PO(view_channel=True, connect=True)
    return ows


def merged(current: discord.PermissionOverwrite, desired: discord.PermissionOverwrite):
    """Keep unrelated flags of an existing overwrite, change only the ones we control."""
    values = {k: v for k, v in current if v is not None}
    values.update({k: v for k, v in desired if v is not None})
    return PO(**values)


async def apply_overwrites(target, desired: dict) -> bool:
    changed = False
    for who, wanted in desired.items():
        current = target.overwrites_for(who)
        new = merged(current, wanted)
        if new != current:
            await target.set_permissions(who, overwrite=new, reason=REASON)
            changed = True
    return changed


# ----------------------------------------------------------------------------
# SETUP LOGIC
# ----------------------------------------------------------------------------
@dataclass
class Report:
    created: list = field(default_factory=list)
    updated: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


async def ensure_roles(guild: discord.Guild, report: Report) -> dict:
    me = guild.me
    bot_value = me.guild_permissions.value
    roles = {}
    for spec in ROLE_SPECS:
        lost = spec.permissions.value & ~bot_value
        wanted = P(spec.permissions.value & bot_value)
        if lost:
            report.warnings.append(
                f"Rolle {spec.name}: Bot besitzt diese Rechte selbst nicht, nicht vergeben: {perm_names(lost)}")

        role = discord.utils.get(guild.roles, name=spec.name)
        if role is None:
            role = await guild.create_role(
                name=spec.name, colour=spec.colour, permissions=wanted, hoist=spec.hoist,
                mentionable=spec.mentionable, reason=REASON)
            report.created.append(f"Rolle {spec.name}")
        elif role >= me.top_role:
            report.warnings.append(f"Rolle {spec.name} liegt über/gleich der Bot-Rolle – übersprungen.")
        elif UPDATE_EXISTING_ROLES and (role.permissions != wanted or role.colour != spec.colour):
            await role.edit(permissions=wanted, colour=spec.colour, reason=REASON)
            report.updated.append(f"Rolle {spec.name} (Rechte/Farbe)")
        else:
            report.skipped.append(f"Rolle {spec.name}")
        roles[spec.name] = role
    return roles


async def order_roles(guild: discord.Guild, roles: dict, report: Report):
    """Reorder only the ClipForge roles among the slots they already occupy."""
    fresh = {r.id: r for r in await guild.fetch_roles()}
    top = max(fresh[r.id].position for r in guild.me.roles if r.id in fresh)

    movable = [roles[s.name] for s in ROLE_SPECS
               if roles[s.name].id in fresh and fresh[roles[s.name].id].position < top]
    slots = sorted((fresh[r.id].position for r in movable), reverse=True)
    mapping = {r: pos for r, pos in zip(movable, slots) if fresh[r.id].position != pos}

    if len(movable) < len(ROLE_SPECS):
        report.warnings.append("Einige Rollen liegen über der Bot-Rolle und wurden nicht einsortiert. "
                               "Ziehe die Bot-Rolle in den Servereinstellungen ganz nach oben.")
    if mapping:
        await guild.edit_role_positions(positions=mapping, reason=REASON)
        report.updated.append(f"Rollenreihenfolge ({len(mapping)} Rollen verschoben)")
    else:
        report.skipped.append("Rollenreihenfolge")


async def ensure_category(guild, spec: CategorySpec, roles, report: Report):
    desired = category_overwrites(guild, roles, spec)
    cat = discord.utils.find(lambda c: c.name.casefold() == spec.name.casefold(), guild.categories)
    if cat is None:
        cat = await guild.create_category(spec.name, overwrites=desired, reason=REASON)
        report.created.append(f"Kategorie {spec.name}")
    elif await apply_overwrites(cat, desired):
        report.updated.append(f"Kategorie {spec.name} (Berechtigungen)")
    else:
        report.skipped.append(f"Kategorie {spec.name}")
    return cat


async def ensure_channel(guild, category, spec: ChannelSpec, roles, report: Report):
    pool = guild.voice_channels if spec.voice else guild.text_channels
    ch = next((c for c in pool if c.name == spec.name and c.category_id == category.id), None) \
        or discord.utils.get(pool, name=spec.name)
    desired = channel_overwrites(guild, roles, spec)

    if ch is None:
        if spec.voice:
            await guild.create_voice_channel(spec.name, category=category, overwrites=desired, reason=REASON)
        else:
            await guild.create_text_channel(spec.name, category=category, overwrites=desired,
                                            topic=spec.topic, reason=REASON)
        report.created.append(f"Kanal {spec.name}")
        return

    changed = False
    if ch.category_id != category.id:
        await ch.edit(category=category, reason=REASON)
        changed = True
    if await apply_overwrites(ch, desired):
        changed = True
    (report.updated if changed else report.skipped).append(
        f"Kanal {spec.name}" + (" (aktualisiert)" if changed else ""))


async def run_setup(guild: discord.Guild) -> Report:
    report = Report()
    roles = await ensure_roles(guild, report)
    await order_roles(guild, roles, report)
    for cat_spec in CATEGORIES:
        category = await ensure_category(guild, cat_spec, roles, report)
        for ch_spec in cat_spec.channels:
            await ensure_channel(guild, category, ch_spec, roles, report)
    return report


def format_report(report: Report) -> list:
    def block(title, items):
        if not items:
            return f"**{title}:** –"
        return f"**{title} ({len(items)}):**\n" + "\n".join(f"• {i}" for i in items)

    text = "\n\n".join([
        block("✅ Erstellt", report.created),
        block("🔄 Aktualisiert", report.updated),
        f"⏭️ Unverändert übersprungen: {len(report.skipped)}",
        block("⚠️ Hinweise", report.warnings),
    ])
    parts, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 1900:
            parts.append(current)
            current = ""
        current += line + "\n"
    parts.append(current)
    return parts


# ----------------------------------------------------------------------------
# BOT
# ----------------------------------------------------------------------------
def read_guild_id():
    raw = os.getenv("GUILD_ID", "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        log.error("GUILD_ID muss eine Zahl sein.")
        sys.exit(1)
    return int(raw)


GUILD_ID = read_guild_id()
setup_lock = asyncio.Lock()


class ClipForgeBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash-Commands für Guild %s synchronisiert.", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Slash-Commands global synchronisiert (kann bis zu 1 Stunde dauern).")

    async def on_ready(self):
        log.info("Eingeloggt als %s (%s)", self.user, self.user.id)
        if GUILD_ID and self.get_guild(GUILD_ID) is None:
            log.warning("Der Bot ist nicht auf dem Server mit GUILD_ID=%s.", GUILD_ID)
        url = discord.utils.oauth_url(self.user.id, permissions=INVITE_PERMS,
                                      scopes=("bot", "applications.commands"))
        log.info("Einladungslink: %s", url)


bot = ClipForgeBot()


@bot.tree.command(name="setup_server", description="Erstellt/aktualisiert Rollen, Kategorien und Kanäle für ClipForge.")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def setup_server(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Dieser Befehl funktioniert nur auf einem Server.", ephemeral=True)
        return
    if GUILD_ID and guild.id != GUILD_ID:
        await interaction.response.send_message("Dieser Bot ist für einen anderen Server konfiguriert.", ephemeral=True)
        return

    user = interaction.user
    if user.id != guild.owner_id and not getattr(user.guild_permissions, SETUP_PERMISSION, False):
        await interaction.response.send_message(
            f"Dir fehlt die Berechtigung `{SETUP_PERMISSION}` für diesen Befehl.", ephemeral=True)
        return

    missing = REQUIRED_BOT_PERMS.value & ~guild.me.guild_permissions.value
    if missing:
        await interaction.response.send_message(
            f"Dem Bot fehlen diese Rechte: {perm_names(missing)}", ephemeral=True)
        return

    if setup_lock.locked():
        await interaction.response.send_message("Das Setup läuft gerade bereits.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    async with setup_lock:
        try:
            log.info("Setup gestartet von %s auf %s", user, guild.name)
            report = await run_setup(guild)
        except discord.Forbidden as exc:
            log.exception("Setup: fehlende Berechtigung")
            await interaction.followup.send(
                "❌ Fehlende Berechtigung oder Rollen-Hierarchie-Problem. Ziehe die Bot-Rolle in den "
                f"Servereinstellungen ganz nach oben und starte `/setup_server` erneut.\n`{exc}`", ephemeral=True)
            return
        except discord.HTTPException as exc:
            log.exception("Setup: API-Fehler")
            await interaction.followup.send(
                f"❌ Discord-API-Fehler: `{exc}`\nDu kannst `/setup_server` gefahrlos erneut ausführen.",
                ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Setup: unerwarteter Fehler")
            await interaction.followup.send(
                f"❌ Unerwarteter Fehler: `{exc}`\nDu kannst `/setup_server` gefahrlos erneut ausführen.",
                ephemeral=True)
            return

    log.info("Setup abgeschlossen: %d erstellt, %d aktualisiert, %d übersprungen.",
             len(report.created), len(report.updated), len(report.skipped))
    for part in format_report(report):
        await interaction.followup.send(part, ephemeral=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if not hasattr(discord.Permissions, SETUP_PERMISSION):
        log.error("SETUP_PERMISSION '%s' ist keine gültige Discord-Berechtigung.", SETUP_PERMISSION)
        sys.exit(1)
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        log.error("DISCORD_TOKEN ist nicht gesetzt (Umgebungsvariable oder .env-Datei).")
        sys.exit(1)
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
