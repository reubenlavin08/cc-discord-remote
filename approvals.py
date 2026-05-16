"""Per-tool Discord-button approval flow.

When Claude wants to use a mutating tool (Edit, Write, Bash, …), this module
posts an embed with Approve / Deny buttons in the channel and blocks until the
authorised user clicks one, the timeout fires, or the bot restarts.
"""

import asyncio
import json
from typing import Optional

import discord

APPROVAL_TIMEOUT_SECONDS = 300  # 5 min — long enough to read, short enough to fail safe.


def _format_input(tool_name: str, tool_input: dict) -> str:
    """Render a tool-input dict into something readable in a Discord embed."""
    # Pull out the field that matters most for each common tool.
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return f"```bash\n{cmd[:1500]}\n```"
    if tool_name in ("Write", "Edit"):
        path = tool_input.get("file_path", "?")
        if tool_name == "Edit":
            old = tool_input.get("old_string", "")[:400]
            new = tool_input.get("new_string", "")[:400]
            return f"**path** `{path}`\n**old**\n```\n{old}\n```\n**new**\n```\n{new}\n```"
        content = tool_input.get("content", "")[:1200]
        return f"**path** `{path}`\n```\n{content}\n```"
    # Fallback: pretty-printed JSON, trimmed.
    blob = json.dumps(tool_input, indent=2, default=str)
    return f"```json\n{blob[:1500]}\n```"


class _ApprovalView(discord.ui.View):
    def __init__(self, future: asyncio.Future, authorised_user_id: int):
        super().__init__(timeout=APPROVAL_TIMEOUT_SECONDS)
        self.future = future
        self.authorised_user_id = authorised_user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Reject clicks from anyone but the user who triggered the turn.
        if interaction.user.id != self.authorised_user_id:
            await interaction.response.send_message(
                "Only the user who started this turn can approve tools.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.future.done():
            self.future.set_result(True)
        await self._lock_buttons(interaction, "✅ Approved")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="🚫")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.future.done():
            self.future.set_result(False)
        await self._lock_buttons(interaction, "🚫 Denied")

    async def _lock_buttons(self, interaction: discord.Interaction, label: str):
        for child in self.children:
            child.disabled = True
        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.set_footer(text=f"{label} by {interaction.user.display_name}")
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    async def on_timeout(self):
        if not self.future.done():
            self.future.set_result(False)


async def request_approval(
    channel: discord.abc.Messageable,
    authorised_user_id: int,
    tool_name: str,
    tool_input: dict,
) -> bool:
    """Post an approval embed and await the user's click. Returns True if approved."""
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    view = _ApprovalView(future, authorised_user_id)

    embed = discord.Embed(
        title=f"Claude wants to use `{tool_name}`",
        description=_format_input(tool_name, tool_input),
        color=discord.Color.orange(),
    )
    embed.set_footer(text=f"Times out in {APPROVAL_TIMEOUT_SECONDS}s")
    # Ping the authorised user so their phone notifies — they're the only one who can approve.
    await channel.send(content=f"<@{authorised_user_id}>", embed=embed, view=view)

    try:
        return await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_SECONDS + 5)
    except asyncio.TimeoutError:
        return False
