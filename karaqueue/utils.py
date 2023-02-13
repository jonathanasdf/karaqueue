"""Utils."""
import logging
import platform
import subprocess
from typing import Union
import discord


def call(binary: str, cmd: str) -> None:
    """Call a local binary with a command."""
    if platform.system() == 'Windows':
        binary = f'{binary}.exe'
    try:
        subprocess.run(f'{binary} {cmd}', shell=True, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as err:
        logging.error(err.stdout.decode('utf-8'))
        raise


DiscordContext = Union[discord.ApplicationContext, discord.Interaction]
DiscordMessage = Union[discord.Interaction, discord.InteractionMessage, discord.WebhookMessage]


async def respond(ctx: DiscordContext, *args, **kwargs) -> DiscordMessage:
    """Post a response to a discord interaction."""
    interaction: discord.Interaction
    if isinstance(ctx, discord.ApplicationContext):
        interaction = ctx.interaction
    else:
        interaction = ctx
    try:
        if not interaction.response.is_done():
            return await interaction.response.send_message(*args, **kwargs)
        return await interaction.followup.send(*args, **kwargs)
    except discord.errors.InteractionResponded:
        return await interaction.followup.send(*args, **kwargs)


async def edit(ctx: DiscordContext, *args, **kwargs) -> DiscordMessage:
    """Edit the response to a discord interaction."""
    interaction: discord.Interaction
    if isinstance(ctx, discord.ApplicationContext):
        interaction = ctx.interaction
    else:
        interaction = ctx
    return await interaction.edit_original_response(*args, **kwargs)


async def delete(ctx: DiscordContext, *args, **kwargs) -> None:
    """Delete the response to a discord interaction."""
    interaction: discord.Interaction
    if isinstance(ctx, discord.ApplicationContext):
        interaction = ctx.interaction
        if not interaction.response.is_done():
            await ctx.defer()
    else:
        interaction = ctx
    await interaction.delete_original_response(*args, **kwargs)
