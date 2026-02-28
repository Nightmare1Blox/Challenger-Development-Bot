import asyncio
import datetime
import logging

import aiosqlite
import discord
from discord.ext import commands

# ==========================
# CONFIG
# ==========================

STAFF_ROLE_ID = 1475926935560257710      # still used for roles, NOT for perms
DENIED_ROLE_ID = 1476605634194571467
BLACKLISTED_ROLE_ID = 1476605812100038910

# Applications ALWAYS go here:
REVIEW_CHANNEL_ID = 1476226286841106694

COOLDOWN_SECONDS = 2 * 24 * 60 * 60
DATABASE_PATH = "database.db"

intents = discord.Intents.default()
intents.members = True
intents.message_content = False

bot = commands.Bot(command_prefix="!", intents=intents)
bot.db: aiosqlite.Connection | None = None

logging.basicConfig(level=logging.INFO)
# ==========================
# DATABASE HELPERS
# ==========================

async def init_db():
    bot.db = await aiosqlite.connect(DATABASE_PATH)
    await bot.db.execute(
        """
        CREATE TABLE IF NOT EXISTS blacklist (
            user_id INTEGER PRIMARY KEY,
            reason TEXT,
            added_at INTEGER
        )
        """
    )
    await bot.db.execute(
        """
        CREATE TABLE IF NOT EXISTS cooldowns (
            user_id INTEGER PRIMARY KEY,
            last_applied_at INTEGER
        )
        """
    )
    await bot.db.execute(
        """
        CREATE TABLE IF NOT EXISTS applications (
            applicant_id INTEGER PRIMARY KEY,
            message_id INTEGER,
            channel_id INTEGER
        )
        """
    )
    await bot.db.commit()


async def is_blacklisted(user_id: int) -> bool:
    async with bot.db.execute(
        "SELECT 1 FROM blacklist WHERE user_id = ?", (user_id,)
    ) as cursor:
        return await cursor.fetchone() is not None


async def add_to_blacklist(user_id: int, reason: str | None = None):
    now_ts = int(datetime.datetime.utcnow().timestamp())
    await bot.db.execute(
        """
        INSERT INTO blacklist (user_id, reason, added_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            reason = excluded.reason,
            added_at = excluded.added_at
        """,
        (user_id, reason or "No reason provided.", now_ts),
    )
    await bot.db.commit()


async def remove_from_blacklist(user_id: int):
    await bot.db.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
    await bot.db.commit()


async def get_last_application_ts(user_id: int) -> int | None:
    async with bot.db.execute(
        "SELECT last_applied_at FROM cooldowns WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_last_application_ts(user_id: int):
    now_ts = int(datetime.datetime.utcnow().timestamp())
    await bot.db.execute(
        """
        INSERT INTO cooldowns (user_id, last_applied_at)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            last_applied_at = excluded.last_applied_at
        """,
        (user_id, now_ts),
    )
    await bot.db.commit()


async def get_cooldown_remaining(user_id: int) -> int:
    last_ts = await get_last_application_ts(user_id)
    if last_ts is None:
        return 0
    now_ts = int(datetime.datetime.utcnow().timestamp())
    return max(0, COOLDOWN_SECONDS - (now_ts - last_ts))


async def save_application_message(applicant_id: int, message_id: int, channel_id: int):
    await bot.db.execute(
        """
        INSERT INTO applications (applicant_id, message_id, channel_id)
        VALUES (?, ?, ?)
        ON CONFLICT(applicant_id) DO UPDATE SET
            message_id = excluded.message_id,
            channel_id = excluded.channel_id
        """,
        (applicant_id, message_id, channel_id),
    )
    await bot.db.commit()


async def get_application_message(applicant_id: int):
    async with bot.db.execute(
        "SELECT message_id, channel_id FROM applications WHERE applicant_id = ?",
        (applicant_id,),
    ) as cursor:
        row = await cursor.fetchone()
        return row if row else None
# ==========================
# EMBED HELPERS
# ==========================

def make_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Staff Applications",
        description=(
            "Press the button below to start your staff application.\n\n"
            "You will receive a **DM** with the questions.\n"
            "Make sure your DMs are open."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Challenger Staff Applications")
    return embed


def make_blacklisted_embed() -> discord.Embed:
    return discord.Embed(
        title="⛔ You Have Been Blacklisted",
        description=(
            "Your application has been **blacklisted**.\n"
            "You may not apply again."
        ),
        color=discord.Color.dark_red(),
    )


def make_cooldown_embed(remaining_seconds: int) -> discord.Embed:
    now = datetime.datetime.utcnow()
    target = now + datetime.timedelta(seconds=remaining_seconds)
    unix_ts = int(target.timestamp())
    human = target.strftime("%Y-%m-%d %H:%M UTC")

    return discord.Embed(
        title="You recently applied",
        description=(
            f"You may reapply on:\n"
            f"• `{human}`\n"
            f"• <t:{unix_ts}:R>"
        ),
        color=discord.Color.orange(),
    )


def make_application_embed(member: discord.Member, answers: dict[str, str]) -> discord.Embed:
    embed = discord.Embed(
        title="New Staff Application",
        color=discord.Color.green(),
        timestamp=datetime.datetime.utcnow(),
    )

    for key, label in [
        ("username", "Discord Username"),
        ("display_name", "Display Name"),
        ("discord_id", "Discord ID"),
        ("age", "Age"),
        ("timezone", "Time Zone"),
        ("q_why_staff", "Why do you want to join the staff team?"),
        ("q_experience", "Previous staff experience"),
        ("q_activity", "Daily activity"),
        ("q_toxic", "Handling toxic users"),
        ("q_spammer", "Handling spammers"),
        ("q_fit", "Why are you a good fit?"),
        ("q_understand", "Understands abuse = removal"),
    ]:
        embed.add_field(name=label, value=answers[key], inline=False)

    embed.add_field(name="Applicant", value=f"{member.mention} (`{member.id}`)", inline=False)
    embed.set_footer(text="Use the buttons below to review this application.")
    return embed
# ==========================
# DM QUESTION FLOW
# ==========================

QUESTIONS = [
    ("username", "Discord Username:"),
    ("display_name", "Display Name:"),
    ("discord_id", "Discord ID:"),
    ("age", "Age:"),
    ("timezone", "Time Zone:"),
    ("q_why_staff", "Why do you want to join the staff team?"),
    ("q_experience", "Do you have any previous staff experience?\n(If yes, explain where and what you did)"),
    ("q_activity", "How active can you be daily?"),
    ("q_toxic", "How would you handle a toxic user?"),
    ("q_spammer", "How would you handle a spammer?"),
    ("q_fit", "What makes you a good fit for our staff team?"),
    ("q_understand", "Do you understand that abusing power results in instant removal?"),
]


async def run_dm_application_flow(member: discord.Member, origin_channel):
    answers = {}

    try:
        dm = await member.create_dm()
    except discord.Forbidden:
        await origin_channel.send(
            f"{member.mention}, I couldn't DM you. Enable DMs and try again.",
            delete_after=10,
        )
        return

    await dm.send(
        "Hello! Let's begin your **staff application**.\n"
        "Answer each question in a separate message.\n"
        "Type `cancel` to stop."
    )

    def check(m):
        return m.author.id == member.id and m.channel.id == dm.id

    total = len(QUESTIONS)

    for index, (key, question) in enumerate(QUESTIONS, start=1):
        await dm.send(f"({index}/{total}) — {question}")

        try:
            msg = await bot.wait_for("message", check=check, timeout=300)
        except asyncio.TimeoutError:
            await dm.send("You took too long. Application cancelled.")
            return

        content = msg.content.strip()
        if content.lower() == "cancel":
            await dm.send("Your application has been cancelled.")
            return

        answers[key] = content or "No answer provided."

    await set_last_application_ts(member.id)

    guild = member.guild
    if guild is None:
        await dm.send("Error: You are no longer in the server.")
        return

    channel = guild.get_channel(REVIEW_CHANNEL_ID)

    embed = make_application_embed(member, answers)
    view = ReviewView(member.id)
    message = await channel.send(embed=embed, view=view)

    # Save application message for later status editing
    await save_application_message(member.id, message.id, channel.id)

    await dm.send("Your application has been submitted successfully!")
# ==========================
# PANEL BUTTON
# ==========================

class ApplyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Apply for Staff (DM)",
            style=discord.ButtonStyle.primary,
            custom_id="apply_for_staff_dm",
        )

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user

        # Blacklist check
        if await is_blacklisted(member.id):
            # DM to user
            await interaction.response.send_message(
                embed=make_blacklisted_embed(),
                ephemeral=True,
            )

            # Log attempt in review channel
            guild = interaction.guild
            review_channel = guild.get_channel(REVIEW_CHANNEL_ID)

            if review_channel is not None:
                blacklisted_attempt_embed = discord.Embed(
                    title="⛔ Blacklisted User Attempted to Apply",
                    description=(
                        f"{member.mention} (`{member.id}`) attempted to apply but is **blacklisted**."
                    ),
                    color=discord.Color.dark_red(),
                )
                await review_channel.send(embed=blacklisted_attempt_embed)
            return

        # Cooldown check
        remaining = await get_cooldown_remaining(member.id)
        if remaining > 0:
            await interaction.response.send_message(
                embed=make_cooldown_embed(remaining),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Check your DMs — your application has started.",
            ephemeral=True,
        )

        await run_dm_application_flow(member, interaction.channel)


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ApplyButton())
# ==========================
# REVIEW BUTTONS
# ==========================

class ReviewView(discord.ui.View):
    def __init__(self, applicant_id: int):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id

    async def _get_member(self, interaction: discord.Interaction):
        return interaction.guild.get_member(self.applicant_id)

    async def _ensure_owner(self, interaction: discord.Interaction):
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message(
                "You do not have permission. Only the server owner can review applications.",
                ephemeral=True,
            )
            return False
        return True

    async def _edit_application_status(self, interaction: discord.Interaction, member: discord.Member,
                                       color: discord.Color, status_text: str):
        row = await get_application_message(member.id)
        if not row:
            return  # no stored message

        message_id, channel_id = row
        channel = interaction.guild.get_channel(channel_id)
        if channel is None:
            return

        try:
            msg = await channel.fetch_message(message_id)
        except discord.NotFound:
            return

        if not msg.embeds:
            return

        embed = msg.embeds[0]
        embed.color = color
        embed.add_field(name="Status", value=status_text, inline=False)

        # Buttons stay active (your choice A)
        await msg.edit(embed=embed, view=self)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_owner(interaction):
            return

        member = await self._get_member(interaction)
        if not member:
            await interaction.response.send_message("User left the server.", ephemeral=True)
            return

        staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
        denied_role = interaction.guild.get_role(DENIED_ROLE_ID)
        blacklisted_role = interaction.guild.get_role(BLACKLISTED_ROLE_ID)

        if denied_role or blacklisted_role:
            roles_to_remove = [r for r in (denied_role, blacklisted_role) if r is not None]
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Approved")

        if staff_role:
            await member.add_roles(staff_role, reason="Approved")

        await remove_from_blacklist(member.id)

        # DM embed to applicant
        approved_embed = discord.Embed(
            title="🎉 You Have Been Approved!",
            description=(
                "Welcome to the **Challenger Development Staff Team!**\n\n"
                "**Quick Rules:**\n"
                "• Stay professional\n"
                "• Respect members\n"
                "• Follow staff rules\n"
                "• Ask questions when unsure\n\n"
                "We're excited to have you on the team."
            ),
            color=discord.Color.green(),
        )

        try:
            await member.send(embed=approved_embed)
        except:
            pass

        # Edit application embed status (inside original embed)
        await self._edit_application_status(
            interaction,
            member,
            discord.Color.green(),
            f"✅ Approved by {interaction.user.mention}",
        )

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_owner(interaction):
            return

        member = await self._get_member(interaction)
        if not member:
            await interaction.response.send_message("User left the server.", ephemeral=True)
            return

        denied_role = interaction.guild.get_role(DENIED_ROLE_ID)
        if denied_role:
            await member.add_roles(denied_role, reason="Denied")

        # Calculate reapply time based on cooldown
        now = datetime.datetime.utcnow()
        target = now + datetime.timedelta(seconds=COOLDOWN_SECONDS)
        unix_ts = int(target.timestamp())

        denied_embed = discord.Embed(
            title="❌ Your Application Was Not Approved",
            description=(
                "Thank you for applying.\n"
                "Unfortunately, your application was not approved.\n\n"
                "You may reapply on:\n"
                f"• <t:{unix_ts}:F>\n"
                f"• <t:{unix_ts}:R>"
            ),
            color=discord.Color.red(),
        )

        try:
            await member.send(embed=denied_embed)
        except:
            pass

        # Edit application embed status
        await self._edit_application_status(
            interaction,
            member,
            discord.Color.red(),
            f"❌ Denied by {interaction.user.mention}",
        )

    @discord.ui.button(label="Blacklist", style=discord.ButtonStyle.secondary)
    async def blacklist(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_owner(interaction):
            return

        member = await self._get_member(interaction)
        if not member:
            await interaction.response.send_message("User left the server.", ephemeral=True)
            return

        blacklisted_role = interaction.guild.get_role(BLACKLISTED_ROLE_ID)
        denied_role = interaction.guild.get_role(DENIED_ROLE_ID)
        staff_role = interaction.guild.get_role(STAFF_ROLE_ID)

        roles_to_remove = [r for r in (staff_role, denied_role) if r is not None]
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="Blacklisted")

        if blacklisted_role:
            await member.add_roles(blacklisted_role, reason="Blacklisted")

        await add_to_blacklist(member.id, "Blacklisted via review")

        blacklisted_embed = discord.Embed(
            title="⛔ You Have Been Blacklisted",
            description=(
                "Your application has been **blacklisted**.\n"
                "You may not apply again."
            ),
            color=discord.Color.dark_red(),
        )

        try:
            await member.send(embed=blacklisted_embed)
        except:
            pass

        # Edit application embed status
        await self._edit_application_status(
            interaction,
            member,
            discord.Color.dark_red(),
            f"⛔ Blacklisted by {interaction.user.mention}",
        )
# ==========================
# SLASH COMMANDS
# ==========================

@bot.tree.command(name="panel", description="Send the staff application panel.")
@commands.has_permissions(manage_guild=True)
async def panel(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=make_panel_embed(),
        view=PanelView()
    )


@bot.tree.command(name="resetcooldown", description="Reset a user's staff application cooldown.")
@commands.has_permissions(manage_guild=True)
async def resetcooldown(interaction: discord.Interaction, user: discord.Member):
    await bot.db.execute("DELETE FROM cooldowns WHERE user_id = ?", (user.id,))
    await bot.db.commit()

    await interaction.response.send_message(
        f"Cooldown reset for {user.mention}.",
        ephemeral=True
    )
@bot.tree.command(name="unblacklist", description="Remove a user from the blacklist.")
@commands.has_permissions(manage_guild=True)
async def unblacklist(interaction: discord.Interaction, user: discord.Member):
    # Remove from database
    await remove_from_blacklist(user.id)

    # Remove blacklisted role if they have it
    blacklisted_role = interaction.guild.get_role(BLACKLISTED_ROLE_ID)
    if blacklisted_role and blacklisted_role in user.roles:
        await user.remove_roles(blacklisted_role, reason="Unblacklisted")

    await interaction.response.send_message(
        f"{user.mention} has been **unblacklisted** and can apply again.",
        ephemeral=True
    )
# ==========================
# BOT EVENTS
# ==========================

@bot.event
async def on_ready():
    await init_db()
    bot.add_view(PanelView())
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")

import os

# ==========================
# RUN
# ==========================

if __name__ == "__main__":
    bot.run(os.getenv("TOKEN"))
