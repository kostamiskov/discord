from __future__ import annotations

import io
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("travian-passion-bot")

SETUP_MARKER = "Travian Is a Passion • managed setup v1"
RULES_BUTTON_ID = "travian-passion:accept-rules:v1"
TRIBE_SELECT_ID = "travian-passion:tribe-select:v1"
SPEED_SELECT_ID = "travian-passion:speed-select:v1"


def csv_env(name: str, default: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, default).split(",") if value.strip())


@dataclass(frozen=True)
class Settings:
    token: str
    guild_id: int | None
    tribe_roles: tuple[str, ...]
    speed_roles: tuple[str, ...]
    staff_roles: tuple[str, ...]
    staff_channels: tuple[str, ...]
    read_only_channels: tuple[str, ...]
    welcome_channel: str
    rules_channel: str
    role_channel: str
    mod_log_channel: str
    member_role: str

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise RuntimeError("DISCORD_TOKEN is missing. Copy .env.example to .env and add the token.")

        guild_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
        return cls(
            token=token,
            guild_id=int(guild_raw) if guild_raw else None,
            tribe_roles=csv_env(
                "TRIBE_ROLES",
                "Romans,Gauls,Teutons,Egyptians,Huns,Spartans,Vikings",
            ),
            speed_roles=csv_env("SERVER_SPEED_ROLES", "x1,x2,x3,x5,x10"),
            staff_roles=csv_env("STAFF_ROLE_NAMES", "Founder,Administrator,Moderator"),
            staff_channels=csv_env("STAFF_CHANNEL_NAMES", "staff-chat,moderation-logs"),
            read_only_channels=csv_env("READ_ONLY_CHANNEL_NAMES", "welcome,rules,announcements"),
            welcome_channel=os.getenv("WELCOME_CHANNEL_NAME", "welcome").strip(),
            rules_channel=os.getenv("RULES_CHANNEL_NAME", "rules").strip(),
            role_channel=os.getenv("ROLE_SELECTOR_CHANNEL_NAME", "welcome").strip(),
            mod_log_channel=os.getenv("MOD_LOG_CHANNEL_NAME", "moderation-logs").strip(),
            member_role=os.getenv("MEMBER_ROLE_NAME", "Member").strip(),
        )


SETTINGS = Settings.from_env()


def canonical(value: str) -> str:
    """Match names despite emoji, spaces, hyphens, underscores, and case."""
    return "".join(character for character in value.casefold() if character.isalnum())


def find_role(guild: discord.Guild, desired_name: str) -> discord.Role | None:
    wanted = canonical(desired_name)
    exact = [role for role in guild.roles if canonical(role.name) == wanted]
    if exact:
        return exact[-1]
    # Existing roles such as "🛡️ Administrator" should also match.
    decorated = [role for role in guild.roles if canonical(role.name).endswith(wanted)]
    return decorated[-1] if decorated else None


def find_channel(guild: discord.Guild, desired_name: str) -> discord.abc.GuildChannel | None:
    wanted = canonical(desired_name)
    return next((channel for channel in guild.channels if canonical(channel.name) == wanted), None)


def human_duration(seconds: float) -> str:
    rounded = max(0, int(round(seconds)))
    days, remainder = divmod(rounded, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


GAME_SPEED_MULTIPLIERS = {"x1": 1.0, "x2": 2.0, "x3": 2.0, "x5": 2.0, "x10": 4.0}


def travel_seconds(
    distance: float,
    unit_speed: float,
    movement_multiplier: float,
    tournament_square: int,
    after_20_bonus_percent: float = 0.0,
    global_bonus_percent: float = 0.0,
    artefact_multiplier: float = 1.0,
) -> float:
    """Calculate duration, applying long-distance bonuses only after 20 fields."""
    effective_speed = (
        unit_speed
        * movement_multiplier
        * artefact_multiplier
        * (1 + global_bonus_percent / 100)
    )
    long_distance_bonus = tournament_square * 0.2 + after_20_bonus_percent / 100
    if distance <= 20 or long_distance_bonus == 0:
        return distance / effective_speed * 3_600
    first_part = 20 / effective_speed
    boosted_speed = effective_speed * (1 + long_distance_bonus)
    second_part = (distance - 20) / boosted_speed
    return (first_part + second_part) * 3_600


def toroidal_distance(x1: int, y1: int, x2: int, y2: int, map_size: int) -> float:
    dx = abs(x2 - x1) % map_size
    dy = abs(y2 - y1) % map_size
    dx = min(dx, map_size - dx)
    dy = min(dy, map_size - dy)
    return math.hypot(dx, dy)


def parse_coordinates(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*\(?\s*(-?\d+)\s*[|,/]\s*(-?\d+)\s*\)?\s*", value)
    if not match:
        raise ValueError("Use coordinates like `32|-30`, `32/-30`, or `32,-30`.")
    return int(match.group(1)), int(match.group(2))


@dataclass
class PermissionChange:
    channel: discord.abc.GuildChannel
    role: discord.Role
    view_channel: bool
    send_messages: bool | None = None
    read_message_history: bool | None = None

    def describe(self) -> str:
        values = [f"View Channel={'allow' if self.view_channel else 'deny'}"]
        if self.send_messages is not None:
            values.append(f"Send Messages={'allow' if self.send_messages else 'deny'}")
        if self.read_message_history is not None:
            values.append(f"Read History={'allow' if self.read_message_history else 'deny'}")
        role_label = "@everyone" if self.role.is_default() else f"@{self.role.name}"
        return f"Permission: #{self.channel.name} — {role_label}: " + ", ".join(values)


@dataclass
class SetupPlan:
    guild: discord.Guild
    roles_to_create: list[str] = field(default_factory=list)
    permissions: list[PermissionChange] = field(default_factory=list)
    post_welcome: discord.TextChannel | None = None
    post_rules: discord.TextChannel | None = None
    post_roles: discord.TextChannel | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def change_count(self) -> int:
        return (
            len(self.roles_to_create)
            + len(self.permissions)
            + int(self.post_welcome is not None)
            + int(self.post_rules is not None)
            + int(self.post_roles is not None)
        )

    def lines(self) -> list[str]:
        result = [f"Server: {self.guild.name} ({self.guild.id})", f"Planned changes: {self.change_count}"]
        result.extend(f"Create role: @{name}" for name in self.roles_to_create)
        result.extend(change.describe() for change in self.permissions)
        if self.post_welcome:
            result.append(f"Post managed welcome message in #{self.post_welcome.name}")
        if self.post_rules:
            result.append(f"Post managed rules message in #{self.post_rules.name}")
        if self.post_roles:
            result.append(f"Post tribe and server-speed selector in #{self.post_roles.name}")
        if not self.change_count:
            result.append("No changes needed.")
        if self.warnings:
            result.append("")
            result.append("Warnings:")
            result.extend(f"- {warning}" for warning in self.warnings)
        result.append("")
        result.append("Nothing is deleted. Existing overwrite values not listed here are preserved.")
        return result


async def has_managed_message(channel: discord.TextChannel, kind: str) -> bool:
    try:
        async for message in channel.history(limit=None):
            for embed in message.embeds:
                if embed.footer and embed.footer.text == f"{SETUP_MARKER} • {kind}":
                    return True
    except discord.Forbidden:
        return False
    return False


async def build_plan(guild: discord.Guild) -> SetupPlan:
    plan = SetupPlan(guild=guild)

    for name in (*SETTINGS.tribe_roles, *SETTINGS.speed_roles):
        if not find_role(guild, name):
            plan.roles_to_create.append(name)

    staff_roles = [role for name in SETTINGS.staff_roles if (role := find_role(guild, name))]
    missing_staff = [name for name in SETTINGS.staff_roles if not find_role(guild, name)]
    if missing_staff:
        plan.warnings.append("Staff roles not found: " + ", ".join(missing_staff))

    writer_roles = list(staff_roles)
    bot_member = guild.me
    if (
        bot_member
        and bot_member.top_role != guild.default_role
        and bot_member.top_role not in writer_roles
    ):
        writer_roles.append(bot_member.top_role)

    for channel_name in SETTINGS.staff_channels:
        channel = find_channel(guild, channel_name)
        if channel is None:
            plan.warnings.append(f"Configured staff channel not found: #{channel_name}")
            continue

        everyone_overwrite = channel.overwrites_for(guild.default_role)
        if everyone_overwrite.view_channel is not False:
            plan.permissions.append(PermissionChange(channel, guild.default_role, view_channel=False))

        for role in writer_roles:
            overwrite = channel.overwrites_for(role)
            if (
                overwrite.view_channel is not True
                or overwrite.send_messages is not True
                or overwrite.read_message_history is not True
            ):
                plan.permissions.append(
                    PermissionChange(
                        channel,
                        role,
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                    )
                )

    for channel_name in SETTINGS.read_only_channels:
        channel = find_channel(guild, channel_name)
        if not isinstance(channel, discord.TextChannel):
            plan.warnings.append(f"Configured read-only text channel not found: #{channel_name}")
            continue

        everyone_overwrite = channel.overwrites_for(guild.default_role)
        if (
            everyone_overwrite.view_channel is not True
            or everyone_overwrite.send_messages is not False
            or everyone_overwrite.read_message_history is not True
        ):
            plan.permissions.append(
                PermissionChange(
                    channel,
                    guild.default_role,
                    view_channel=True,
                    send_messages=False,
                    read_message_history=True,
                )
            )

        for role in writer_roles:
            overwrite = channel.overwrites_for(role)
            if (
                overwrite.view_channel is not True
                or overwrite.send_messages is not True
                or overwrite.read_message_history is not True
            ):
                plan.permissions.append(
                    PermissionChange(
                        channel,
                        role,
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                    )
                )

        explicitly_allowed = [
            role.name
            for role, overwrite in channel.overwrites.items()
            if isinstance(role, discord.Role)
            and role not in writer_roles
            and role != guild.default_role
            and overwrite.send_messages is True
        ]
        if explicitly_allowed:
            plan.warnings.append(
                f"#{channel.name} has non-staff roles explicitly allowed to send: " + ", ".join(explicitly_allowed)
            )

    welcome = find_channel(guild, SETTINGS.welcome_channel)
    if isinstance(welcome, discord.TextChannel):
        if not await has_managed_message(welcome, "welcome"):
            plan.post_welcome = welcome
    else:
        plan.warnings.append(f"Welcome text channel not found: #{SETTINGS.welcome_channel}")

    rules = find_channel(guild, SETTINGS.rules_channel)
    if isinstance(rules, discord.TextChannel):
        if not await has_managed_message(rules, "rules"):
            plan.post_rules = rules
    else:
        plan.warnings.append(f"Rules text channel not found: #{SETTINGS.rules_channel}")

    role_channel = find_channel(guild, SETTINGS.role_channel)
    if isinstance(role_channel, discord.TextChannel):
        if not await has_managed_message(role_channel, "roles"):
            plan.post_roles = role_channel
    else:
        plan.warnings.append(f"Role-selector text channel not found: #{SETTINGS.role_channel}")

    return plan


def welcome_embed(member: discord.Member | None = None) -> discord.Embed:
    mention = member.mention if member else "traveler"
    embed = discord.Embed(
        title="Welcome to Travian Is a Passion",
        description=(
            f"Welcome, {mention}!\n\n"
            "**More than a game. A way of life.**\n"
            "We are an international English-speaking community for Travian players of every tribe, "
            "server speed, and experience level."
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Start here",
        value=(
            "1. Read <#RULES_CHANNEL> and accept the rules.\n"
            "2. Choose your tribe and server-speed roles.\n"
            "3. Introduce yourself and join the conversation."
        ),
        inline=False,
    )
    kind = "member-welcome" if member else "welcome"
    embed.set_footer(text=f"{SETUP_MARKER} • {kind}")
    return embed


def rules_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Community Rules",
        description="Keep this a useful, welcoming home for Travian players worldwide.",
        color=discord.Color.red(),
    )
    rules = (
        "1. **Use English** in public channels so everyone can participate.\n"
        "2. **Respect other players.** No harassment, hate speech, threats, or personal attacks.\n"
        "3. **Keep it Travian-friendly.** Use the correct channels and avoid spam.\n"
        "4. **No cheating tools or account theft.** Do not share bots, exploits, credentials, or stolen data.\n"
        "5. **Protect operational information.** Do not leak alliance plans, reports, or private messages without permission.\n"
        "6. **No unsolicited advertising or recruitment spam.** Ask staff first.\n"
        "7. **Follow Discord's Terms of Service** and staff instructions.\n\n"
        "Press **Agree to Rules** to receive the Member role."
    )
    embed.add_field(name="The rules", value=rules, inline=False)
    embed.set_footer(text=f"{SETUP_MARKER} • rules")
    return embed


def role_selector_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Choose Your Travian Roles",
        description=(
            "Use the menus below to choose your main tribe and gameworld speed.\n\n"
            "You can change either selection later. Choosing a new option automatically removes your old "
            "role from the same group."
        ),
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="Tribe",
        value=" • ".join(SETTINGS.tribe_roles),
        inline=False,
    )
    embed.add_field(name="Gameworld speed", value=" • ".join(SETTINGS.speed_roles), inline=False)
    embed.set_footer(text=f"{SETUP_MARKER} • roles")
    return embed


async def send_mod_log(
    guild: discord.Guild,
    title: str,
    description: str,
    color: discord.Color = discord.Color.blurple(),
) -> None:
    """Best-effort staff log; logging failure must never break the member action."""
    channel = find_channel(guild, SETTINGS.mod_log_channel)
    if not isinstance(channel, discord.TextChannel):
        log.warning("Moderation log channel #%s was not found", SETTINGS.mod_log_channel)
        return
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Could not write to moderation log channel #%s", channel.name)


class RulesView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Agree to Rules",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id=RULES_BUTTON_ID,
    )
    async def agree(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This button only works inside the server.", ephemeral=True)
            return
        role = find_role(interaction.guild, SETTINGS.member_role)
        if role is None:
            await interaction.response.send_message(
                f"The {SETTINGS.member_role} role is missing. Please contact a moderator.", ephemeral=True
            )
            return
        if role in interaction.user.roles:
            await interaction.response.send_message("You have already accepted the rules.", ephemeral=True)
            return
        try:
            await interaction.user.add_roles(role, reason="Accepted community rules")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot assign the Member role. Move my bot role above Member in Server Settings → Roles.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("Rules accepted — welcome to the community!", ephemeral=True)


ROLE_EMOJIS = {
    "romans": "🦅",
    "gauls": "🐎",
    "teutons": "🪓",
    "egyptians": "🏺",
    "huns": "🏹",
    "spartans": "🛡️",
    "vikings": "⚔️",
}


async def update_exclusive_role(
    interaction: discord.Interaction,
    selected_name: str,
    group_names: tuple[str, ...],
    group_label: str,
) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This menu only works inside the server.", ephemeral=True)
        return

    member_role = find_role(interaction.guild, SETTINGS.member_role)
    if (
        member_role
        and member_role not in interaction.user.roles
        and not interaction.user.guild_permissions.manage_guild
    ):
        await interaction.response.send_message("Accept the community rules before choosing roles.", ephemeral=True)
        return

    selected_role = find_role(interaction.guild, selected_name)
    if selected_role is None:
        await interaction.response.send_message(
            f"The {selected_name} role is missing. Please contact a moderator.",
            ephemeral=True,
        )
        return

    bot_member = interaction.guild.me
    if bot_member is None or selected_role >= bot_member.top_role:
        await interaction.response.send_message(
            "I cannot manage that role. Move my bot role above the tribe and speed roles.",
            ephemeral=True,
        )
        return

    previous_roles = [
        role
        for name in group_names
        if (role := find_role(interaction.guild, name))
        and role in interaction.user.roles
        and role != selected_role
    ]

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if selected_role not in interaction.user.roles:
            await interaction.user.add_roles(selected_role, reason=f"Selected {group_label} role")
        if previous_roles:
            await interaction.user.remove_roles(*previous_roles, reason=f"Changed {group_label} role")
    except discord.Forbidden:
        await interaction.followup.send(
            "I cannot update those roles. Move my bot role above all tribe and speed roles.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as error:
        log.exception("Discord API error while updating a self-service role")
        await interaction.followup.send(f"Discord could not update the role: {error}", ephemeral=True)
        return

    await interaction.followup.send(f"Updated: {group_label} → **{selected_role.name}**", ephemeral=True)


class TribeSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(
                label=name,
                value=name,
                emoji=ROLE_EMOJIS.get(canonical(name), "⚔️"),
            )
            for name in SETTINGS.tribe_roles[:25]
        ]
        super().__init__(
            placeholder="Choose your tribe",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=TRIBE_SELECT_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await update_exclusive_role(interaction, self.values[0], SETTINGS.tribe_roles, "tribe")


class SpeedSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label=name, value=name, emoji="⚡")
            for name in SETTINGS.speed_roles[:25]
        ]
        super().__init__(
            placeholder="Choose your server speed",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=SPEED_SELECT_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await update_exclusive_role(interaction, self.values[0], SETTINGS.speed_roles, "server speed")


class RoleSelectionView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(TribeSelect())
        self.add_item(SpeedSelect())


async def apply_plan(plan: SetupPlan) -> list[str]:
    results: list[str] = []
    guild = plan.guild

    for role_name in plan.roles_to_create:
        if find_role(guild, role_name):
            results.append(f"Skipped existing role @{role_name}")
            continue
        await guild.create_role(name=role_name, reason="Confirmed Travian community setup")
        results.append(f"Created role @{role_name}")

    for change in plan.permissions:
        overwrite = change.channel.overwrites_for(change.role)
        overwrite.view_channel = change.view_channel
        if change.send_messages is not None:
            overwrite.send_messages = change.send_messages
        if change.read_message_history is not None:
            overwrite.read_message_history = change.read_message_history
        await change.channel.set_permissions(
            change.role,
            overwrite=overwrite,
            reason="Confirmed staff-channel protection",
        )
        results.append(change.describe())

    if plan.post_welcome and not await has_managed_message(plan.post_welcome, "welcome"):
        embed = welcome_embed()
        rules = find_channel(guild, SETTINGS.rules_channel)
        if rules:
            embed.description = embed.description
            for index, field_value in enumerate(embed.fields):
                if "<#RULES_CHANNEL>" in field_value.value:
                    embed.set_field_at(
                        index,
                        name=field_value.name,
                        value=field_value.value.replace("<#RULES_CHANNEL>", rules.mention),
                        inline=field_value.inline,
                    )
        await plan.post_welcome.send(embed=embed)
        results.append(f"Posted welcome message in #{plan.post_welcome.name}")

    if plan.post_rules and not await has_managed_message(plan.post_rules, "rules"):
        await plan.post_rules.send(embed=rules_embed(), view=RulesView())
        results.append(f"Posted rules message in #{plan.post_rules.name}")

    if plan.post_roles and not await has_managed_message(plan.post_roles, "roles"):
        await plan.post_roles.send(embed=role_selector_embed(), view=RoleSelectionView())
        results.append(f"Posted role selector in #{plan.post_roles.name}")

    return results


def require_manage_guild(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild


class SetupConfirmation(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the administrator who opened this preview can confirm it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm Setup", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("The server context changed. Run `/server setup` again.", ephemeral=True)
            return
        if not require_manage_guild(interaction):
            await interaction.response.send_message("You need Manage Server to confirm setup.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        fresh_plan = await build_plan(interaction.guild)
        if fresh_plan.change_count == 0:
            await interaction.followup.send("No changes are needed; the server already matches the plan.", ephemeral=True)
            self.stop()
            return
        try:
            results = await apply_plan(fresh_plan)
        except discord.Forbidden:
            log.exception("Discord rejected a setup change")
            await interaction.followup.send(
                "Discord rejected a change. Check that the bot has Manage Roles, Manage Channels, View Channels, "
                "Send Messages, Read Message History, and that its role is above roles it manages.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as error:
            log.exception("Discord API error during setup")
            await interaction.followup.send(f"Discord API error: {error}", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass
        result_text = "Setup complete:\n" + "\n".join(f"• {line}" for line in results)
        if len(result_text) > 2_000:
            result_file = discord.File(
                io.BytesIO(result_text.encode("utf-8")),
                filename="travian-setup-results.txt",
            )
            await interaction.followup.send(
                "Setup complete. The full result is attached.",
                file=result_file,
                ephemeral=True,
            )
        else:
            await interaction.followup.send(result_text, ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Setup cancelled. No changes were made.", view=self)
        self.stop()


class TravianBot(commands.Bot):
    async def setup_hook(self) -> None:
        self.add_view(RulesView())
        self.add_view(RoleSelectionView())
        self.tree.add_command(server_group)
        if SETTINGS.guild_id:
            guild_object = discord.Object(id=SETTINGS.guild_id)
            self.tree.copy_global_to(guild=guild_object)
            synced = await self.tree.sync(guild=guild_object)
            log.info("Synced %d commands to guild %s", len(synced), SETTINGS.guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global commands", len(synced))


intents = discord.Intents.default()
intents.members = True
bot = TravianBot(command_prefix=commands.when_mentioned, intents=intents)
server_group = app_commands.Group(name="server", description="Inspect and safely configure this Discord server")


@server_group.command(name="inspect", description="Export the current categories, channels, roles, and key permissions")
@app_commands.check(require_manage_guild)
async def inspect_server(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    guild = interaction.guild
    lines = [f"SERVER: {guild.name}", f"ID: {guild.id}", "", "ROLES (highest first)"]
    lines.extend(f"- {role.name} | id={role.id}" for role in reversed(guild.roles))
    lines.extend(["", "CATEGORIES AND CHANNELS"])

    uncategorized = [channel for channel in guild.channels if channel.category is None and not isinstance(channel, discord.CategoryChannel)]
    if uncategorized:
        lines.append("[Uncategorized]")
        lines.extend(f"- {channel.type}: {channel.name} | id={channel.id}" for channel in uncategorized)

    for category in guild.categories:
        lines.append(f"[{category.name}] | id={category.id}")
        for channel in category.channels:
            everyone = channel.overwrites_for(guild.default_role).view_channel
            public_state = "inherit" if everyone is None else str(everyone).lower()
            lines.append(f"- {channel.type}: {channel.name} | id={channel.id} | @everyone view={public_state}")

    lines.extend(["", "CONFIGURATION MATCHES"])
    for name in SETTINGS.staff_channels:
        channel = find_channel(guild, name)
        lines.append(f"- staff channel {name}: " + (f"#{channel.name} ({channel.id})" if channel else "NOT FOUND"))
    for name in SETTINGS.staff_roles:
        role = find_role(guild, name)
        lines.append(f"- staff role {name}: " + (f"@{role.name} ({role.id})" if role else "NOT FOUND"))
    for name in SETTINGS.read_only_channels:
        channel = find_channel(guild, name)
        lines.append(f"- read-only channel {name}: " + (f"#{channel.name} ({channel.id})" if channel else "NOT FOUND"))

    payload = "\n".join(lines).encode("utf-8")
    await interaction.response.send_message(
        "Current server inspection (read-only):",
        file=discord.File(io.BytesIO(payload), filename="travian-server-inspection.txt"),
        ephemeral=True,
    )


@server_group.command(name="setup", description="Preview safe setup changes, then choose Confirm or Cancel")
@app_commands.check(require_manage_guild)
async def setup_server(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    await interaction.response.defer(ephemeral=True, thinking=True)
    plan = await build_plan(interaction.guild)
    text = "\n".join(plan.lines())
    preview_message = f"**Dry-run preview — no changes made**\n```text\n{text}\n```"
    if len(preview_message) > 2_000:
        preview_file = discord.File(io.BytesIO(text.encode("utf-8")), filename="travian-setup-preview.txt")
        await interaction.followup.send(
            "Dry-run preview attached. Review it, then confirm within five minutes.",
            file=preview_file,
            view=SetupConfirmation(interaction.user.id, interaction.guild.id),
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            preview_message,
            view=SetupConfirmation(interaction.user.id, interaction.guild.id),
            ephemeral=True,
        )


@server_group.command(name="health", description="Check bot permissions, role order, channels, and managed components")
@app_commands.check(require_manage_guild)
async def server_health(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    guild = interaction.guild
    bot_member = guild.me
    if bot_member is None:
        await interaction.response.send_message("Discord has not loaded the bot member yet. Try again shortly.", ephemeral=True)
        return

    required_permissions = {
        "View Channels": bot_member.guild_permissions.view_channel,
        "Send Messages": bot_member.guild_permissions.send_messages,
        "Embed Links": bot_member.guild_permissions.embed_links,
        "Attach Files": bot_member.guild_permissions.attach_files,
        "Read Message History": bot_member.guild_permissions.read_message_history,
        "Manage Roles": bot_member.guild_permissions.manage_roles,
        "Manage Channels": bot_member.guild_permissions.manage_channels,
    }
    managed_role_names = (SETTINGS.member_role, *SETTINGS.tribe_roles, *SETTINGS.speed_roles)
    managed_roles = [role for name in managed_role_names if (role := find_role(guild, name))]
    hierarchy_ok = all(role < bot_member.top_role for role in managed_roles)

    channel_names = {
        *SETTINGS.staff_channels,
        *SETTINGS.read_only_channels,
        SETTINGS.welcome_channel,
        SETTINGS.rules_channel,
        SETTINGS.role_channel,
        SETTINGS.mod_log_channel,
    }
    missing_channels = sorted(name for name in channel_names if not find_channel(guild, name))
    missing_roles = sorted(name for name in managed_role_names if not find_role(guild, name))
    failures = [name for name, allowed in required_permissions.items() if not allowed]
    channel_failures: list[str] = []
    for name in sorted(channel_names):
        channel = find_channel(guild, name)
        if not isinstance(channel, discord.TextChannel):
            continue
        permissions = channel.permissions_for(bot_member)
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.read_message_history
            and permissions.embed_links
        ):
            channel_failures.append(f"#{channel.name}")

    embed = discord.Embed(
        title="Bot Health Check",
        color=(
            discord.Color.green()
            if not (failures or channel_failures or missing_channels or missing_roles or not hierarchy_ok)
            else discord.Color.orange()
        ),
    )
    embed.add_field(
        name="Permissions",
        value="\n".join(f"{'✅' if allowed else '❌'} {name}" for name, allowed in required_permissions.items()),
        inline=False,
    )
    embed.add_field(
        name="Role hierarchy",
        value=(
            f"✅ Bot role is above all {len(managed_roles)} managed roles."
            if hierarchy_ok
            else "❌ Move the bot role above Member, tribe, and speed roles."
        ),
        inline=False,
    )
    embed.add_field(
        name="Channel access",
        value=(
            "✅ Bot can view, send, read history, and embed in every configured text channel."
            if not channel_failures
            else "❌ Incomplete access: " + ", ".join(channel_failures)
        ),
        inline=False,
    )
    embed.add_field(
        name="Configured objects",
        value=(
            ("Missing channels: " + ", ".join(missing_channels) + "\n") if missing_channels else "All configured channels found.\n"
        ) + (("Missing roles: " + ", ".join(missing_roles)) if missing_roles else "All managed roles found."),
        inline=False,
    )
    embed.set_footer(text="Read-only diagnostic; this command changes nothing.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@inspect_server.error
@setup_server.error
@server_health.error
async def server_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CheckFailure):
        message = "You need the Manage Server permission to use this command."
    else:
        log.exception("Server command failed", exc_info=error)
        message = "The command failed. Check the bot logs for details."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="travel", description="Calculate Travian troop travel time")
@app_commands.describe(
    unit_speed="Base unit speed in fields/hour, e.g. 14 for Equites Imperatoris",
    server_speed="Gameworld speed; troop movement scaling is applied automatically",
    tournament_square="Tournament Square level, from 0 to 20",
    distance="Direct distance in fields; omit when using origin and target",
    origin="Optional origin coordinates, e.g. 32|-30",
    target="Optional target coordinates, e.g. -12|-37",
    map_size="Map width for edge wrapping; standard ±200 map is 401",
    round_trip="Include the return journey",
    after_20_bonus_percent="Other movement bonus applying after 20 fields, as a percentage",
    global_bonus_percent="Movement bonus applying to the whole trip, as a percentage",
    artefact_multiplier="Troop-speed artefact multiplier, e.g. 1.5 or 2",
)
@app_commands.choices(
    server_speed=[
        app_commands.Choice(name="x1", value="x1"),
        app_commands.Choice(name="x2", value="x2"),
        app_commands.Choice(name="x3", value="x3"),
        app_commands.Choice(name="x5", value="x5"),
        app_commands.Choice(name="x10", value="x10"),
    ]
)
async def travel(
    interaction: discord.Interaction,
    unit_speed: app_commands.Range[float, 0.1, 100.0],
    server_speed: str = "x1",
    tournament_square: app_commands.Range[int, 0, 20] = 0,
    distance: app_commands.Range[float, 0.001, 1_000.0] | None = None,
    origin: str | None = None,
    target: str | None = None,
    map_size: app_commands.Range[int, 1, 10_001] = 401,
    round_trip: bool = False,
    after_20_bonus_percent: app_commands.Range[float, 0.0, 500.0] = 0.0,
    global_bonus_percent: app_commands.Range[float, 0.0, 500.0] = 0.0,
    artefact_multiplier: app_commands.Range[float, 1.0, 10.0] = 1.0,
) -> None:
    if distance is None:
        if not origin or not target:
            await interaction.response.send_message(
                "Provide either `distance`, or both `origin` and `target` coordinates.", ephemeral=True
            )
            return
        try:
            x1, y1 = parse_coordinates(origin)
            x2, y2 = parse_coordinates(target)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        distance = toroidal_distance(x1, y1, x2, y2, map_size)

    movement_multiplier = GAME_SPEED_MULTIPLIERS[server_speed]
    one_way = travel_seconds(
        distance,
        unit_speed,
        movement_multiplier,
        tournament_square,
        after_20_bonus_percent,
        global_bonus_percent,
        artefact_multiplier,
    )
    duration = one_way * (2 if round_trip else 1)
    arrival = discord.utils.utcnow() + timedelta(seconds=duration)
    effective_base_speed = unit_speed * movement_multiplier * artefact_multiplier * (1 + global_bonus_percent / 100)
    embed = discord.Embed(title="Troop Travel Time", color=discord.Color.blue())
    embed.add_field(name="Distance", value=f"{distance:.2f} fields")
    embed.add_field(
        name="Effective base speed",
        value=f"{effective_base_speed:g} fields/hour ({server_speed} gameworld)",
    )
    embed.add_field(name="Tournament Square", value=f"Level {tournament_square}")
    if after_20_bonus_percent:
        embed.add_field(name="Other after-20 bonus", value=f"{after_20_bonus_percent:g}%")
    if global_bonus_percent or artefact_multiplier != 1:
        embed.add_field(
            name="Whole-trip bonuses",
            value=f"{global_bonus_percent:g}% • artefact ×{artefact_multiplier:g}",
        )
    embed.add_field(name="Journey", value="Round trip" if round_trip else "One way")
    embed.add_field(name="Travel time", value=f"**{human_duration(duration)}**", inline=False)
    embed.add_field(name="Arrival / completion", value=discord.utils.format_dt(arrival, style="F"), inline=False)
    embed.set_footer(text="Tournament Square and other long-distance bonuses apply after the first 20 fields.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="distance", description="Calculate wrapped Travian map distance between two villages")
@app_commands.describe(
    origin="Origin coordinates, e.g. 200|0",
    target="Target coordinates, e.g. -200|0",
    map_size="Map width for edge wrapping; standard ±200 map is 401",
)
async def distance_command(
    interaction: discord.Interaction,
    origin: str,
    target: str,
    map_size: app_commands.Range[int, 1, 10_001] = 401,
) -> None:
    try:
        x1, y1 = parse_coordinates(origin)
        x2, y2 = parse_coordinates(target)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    distance = toroidal_distance(x1, y1, x2, y2, map_size)
    await interaction.response.send_message(
        f"Distance from **({x1}|{y1})** to **({x2}|{y2})**: **{distance:.2f} fields** "
        f"(wrapped map size {map_size})."
    )


@bot.tree.command(name="raid", description="Calculate raid carrying capacity and obtainable loot")
@app_commands.describe(
    unit_count="Number of troops",
    carry_per_unit="Carrying capacity of one troop",
    available_resources="Optional total resources available at the target",
)
async def raid(
    interaction: discord.Interaction,
    unit_count: app_commands.Range[int, 1, 2_000_000_000],
    carry_per_unit: app_commands.Range[int, 0, 2_000_000_000],
    available_resources: app_commands.Range[int, 0, 2_000_000_000] | None = None,
) -> None:
    capacity = unit_count * carry_per_unit
    embed = discord.Embed(title="Raid Capacity", color=discord.Color.orange())
    embed.add_field(name="Troops", value=f"{unit_count:,}")
    embed.add_field(name="Carry per troop", value=f"{carry_per_unit:,}")
    embed.add_field(name="Maximum carry", value=f"**{capacity:,}**", inline=False)
    if available_resources is not None:
        loot = min(capacity, available_resources)
        embed.add_field(name="Available resources", value=f"{available_resources:,}")
        embed.add_field(name="Expected loot", value=f"**{loot:,}**")
        embed.add_field(name="Unused capacity", value=f"{capacity - loot:,}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="upkeep", description="Calculate hourly crop upkeep for up to three troop groups")
@app_commands.describe(
    count_1="Troop count in group 1",
    crop_1="Hourly crop upkeep per troop in group 1",
    count_2="Optional troop count in group 2",
    crop_2="Hourly crop upkeep per troop in group 2",
    count_3="Optional troop count in group 3",
    crop_3="Hourly crop upkeep per troop in group 3",
)
async def upkeep(
    interaction: discord.Interaction,
    count_1: app_commands.Range[int, 0, 2_000_000_000],
    crop_1: app_commands.Range[int, 0, 100],
    count_2: app_commands.Range[int, 0, 2_000_000_000] = 0,
    crop_2: app_commands.Range[int, 0, 100] = 0,
    count_3: app_commands.Range[int, 0, 2_000_000_000] = 0,
    crop_3: app_commands.Range[int, 0, 100] = 0,
) -> None:
    groups = [(count_1, crop_1), (count_2, crop_2), (count_3, crop_3)]
    totals = [count * crop for count, crop in groups]
    total = sum(totals)
    embed = discord.Embed(title="Troop Upkeep", color=discord.Color.dark_green())
    for index, ((count, crop), subtotal) in enumerate(zip(groups, totals), start=1):
        if count or crop or index == 1:
            embed.add_field(
                name=f"Group {index}",
                value=f"{count:,} × {crop} = **{subtotal:,}** crop/hour",
                inline=False,
            )
    embed.add_field(name="Total hourly upkeep", value=f"**{total:,} crop/hour**", inline=False)
    embed.add_field(name="Daily upkeep", value=f"{total * 24:,} crop/day", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="resources", description="Add resources and calculate storage and merchant requirements")
@app_commands.describe(
    wood="Wood amount",
    clay="Clay amount",
    iron="Iron amount",
    crop="Crop amount",
    warehouse_capacity="Optional single Warehouse capacity",
    granary_capacity="Optional single Granary capacity",
    merchant_capacity="Optional carrying capacity per merchant",
)
async def resources(
    interaction: discord.Interaction,
    wood: app_commands.Range[int, 0, 2_000_000_000],
    clay: app_commands.Range[int, 0, 2_000_000_000],
    iron: app_commands.Range[int, 0, 2_000_000_000],
    crop: app_commands.Range[int, 0, 2_000_000_000],
    warehouse_capacity: app_commands.Range[int, 1, 2_000_000_000] | None = None,
    granary_capacity: app_commands.Range[int, 1, 2_000_000_000] | None = None,
    merchant_capacity: app_commands.Range[int, 1, 2_000_000_000] | None = None,
) -> None:
    building = wood + clay + iron
    total = building + crop
    embed = discord.Embed(title="Resource Calculator", color=discord.Color.green())
    embed.description = f"🪵 {wood:,}  •  🧱 {clay:,}  •  ⛓️ {iron:,}  •  🌾 {crop:,}"
    embed.add_field(name="Building resources", value=f"{building:,}")
    embed.add_field(name="Grand total", value=f"**{total:,}**")

    if warehouse_capacity:
        warehouses = math.ceil(max(wood, clay, iron) / warehouse_capacity)
        embed.add_field(name="Warehouses needed", value=str(warehouses), inline=False)
    if granary_capacity:
        granaries = math.ceil(crop / granary_capacity)
        embed.add_field(name="Granaries needed", value=str(granaries), inline=False)
    if merchant_capacity:
        merchants = math.ceil(total / merchant_capacity)
        embed.add_field(name="Merchants needed", value=str(merchants), inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="Show the Travian bot commands and server onboarding steps")
async def help_command(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="Travian Is a Passion — Bot Help",
        description="Community onboarding, calculators, and safe server administration.",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Start here",
        value=(
            "1. Read the rules and press **Agree to Rules**.\n"
            "2. Choose one tribe and one server-speed role in the welcome channel.\n"
            "3. Use the community channels and calculators below."
        ),
        inline=False,
    )
    embed.add_field(
        name="Travian calculators",
        value=(
            "`/distance` — wrapped map distance\n"
            "`/travel` — travel time, bonuses, and arrival\n"
            "`/resources` — totals, storage, and merchants\n"
            "`/raid` — carrying capacity and loot\n"
            "`/upkeep` — hourly and daily crop upkeep"
        ),
        inline=False,
    )
    embed.add_field(
        name="Status",
        value="`/ping` — bot latency",
        inline=False,
    )
    if require_manage_guild(interaction):
        embed.add_field(
            name="Administrator tools",
            value=(
                "`/server inspect` — export current structure\n"
                "`/server setup` — dry-run and confirmed safe setup\n"
                "`/server health` — permissions, channels, and role-order check"
            ),
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ping", description="Check whether the bot is online and responsive")
async def ping(interaction: discord.Interaction) -> None:
    latency_ms = round(bot.latency * 1_000)
    await interaction.response.send_message(f"🏓 Online — gateway latency **{latency_ms} ms**.", ephemeral=True)


@bot.tree.error
async def global_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    log.exception("Application command failed", exc_info=error)
    if isinstance(error, app_commands.CommandOnCooldown):
        message = f"Try again in {error.retry_after:.1f} seconds."
    elif isinstance(error, app_commands.TransformerError):
        message = "One of the values is invalid. Check the command hints and try again."
    elif isinstance(error, app_commands.CheckFailure):
        message = "You do not have permission to use this command."
    else:
        message = "The command could not be completed. Staff can check the Railway logs for details."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        log.exception("Could not send the command error response")


@bot.event
async def on_member_join(member: discord.Member) -> None:
    await send_mod_log(
        member.guild,
        "Member joined",
        f"{member.mention} (`{member}`)\nUser ID: `{member.id}`\nAccount created: {discord.utils.format_dt(member.created_at, style='R')}",
        discord.Color.green(),
    )
    channel = find_channel(member.guild, SETTINGS.welcome_channel)
    if not isinstance(channel, discord.TextChannel):
        return
    embed = welcome_embed(member)
    rules = find_channel(member.guild, SETTINGS.rules_channel)
    if rules:
        for index, field_value in enumerate(embed.fields):
            embed.set_field_at(
                index,
                name=field_value.name,
                value=field_value.value.replace("<#RULES_CHANNEL>", rules.mention),
                inline=field_value.inline,
            )
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        log.warning("Cannot post welcome message in #%s", channel.name)


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    await send_mod_log(
        member.guild,
        "Member left",
        f"**{member}**\nUser ID: `{member.id}`",
        discord.Color.red(),
    )


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    before_ids = {role.id for role in before.roles}
    after_ids = {role.id for role in after.roles}
    added = [role for role in after.roles if role.id not in before_ids]
    removed = [role for role in before.roles if role.id not in after_ids]
    if not added and not removed:
        return
    details: list[str] = [f"Member: {after.mention} (`{after.id}`)"]
    if added:
        details.append("Added: " + ", ".join(f"@{role.name}" for role in added))
    if removed:
        details.append("Removed: " + ", ".join(f"@{role.name}" for role in removed))
    await send_mod_log(after.guild, "Member roles updated", "\n".join(details), discord.Color.blue())


@bot.event
async def on_ready() -> None:
    await bot.change_presence(activity=discord.Game(name="/help • Travian Is a Passion"))
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")


if __name__ == "__main__":
    bot.run(SETTINGS.token, log_handler=None)
