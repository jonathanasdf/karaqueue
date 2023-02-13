"""Karaqueue discord bot."""
import asyncio
import configparser
import itertools
import logging
import os
import pathlib
import signal
import tempfile
import typing
from typing import Optional
import discord
from discord.ext import commands

from karaqueue import common
from karaqueue import nico
from karaqueue import youtube
from karaqueue import utils


logging.basicConfig(level=logging.INFO)
cfg = configparser.ConfigParser()
cfg.read(os.path.join(os.path.dirname(__file__), 'config.ini'))


BOT_TOKEN = cfg['DEFAULT']['token']
GUILD_IDS = cfg['DEFAULT']['guild_ids'].split(',')


os.makedirs(common.SERVING_DIR, exist_ok=True)


# A new video is queued for offline processing.
new_process_task = asyncio.Condition()


class AddSongModal(discord.ui.Modal):
    """Discord view for adding new song."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.add_item(discord.ui.InputText(label="URL"))  # type: ignore
        self.add_item(discord.ui.InputText(
            label="Pitch Shift (optional)", required=False))  # type: ignore

    async def callback(self, interaction: discord.Interaction):
        url = str(self.children[0].value)
        pitch_shift = 0
        if self.children[1].value:
            pitch_shift = int(self.children[1].value)
        await _load(interaction, url, pitch_shift)


class EmptyQueueView(discord.ui.View):
    """Discord view for showing an empty queue."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Add Song', style=discord.ButtonStyle.green, custom_id='add_song')
    async def add_callback(self, _, interaction):
        """Add song button clicked."""
        await interaction.response.send_modal(AddSongModal(title='Add Song'))


bot = commands.Bot()


@bot.event
async def on_ready():
    """Register persistent views."""
    bot.add_view(EmptyQueueView())


async def _help(ctx: discord.ApplicationContext):
    resp = [
        'Commands:',
        '`/q`: queue a video from youtube. Also `/add` or `/load`.',
        '`/list`: show the current playlist.',
        '`/next`: play the next entry on the playlist.',
        '`/delete index`: delete an entry from the playlist. Also `/remove`.',
        '`/move from to`: change the position of an entry in the playlist.',
        ('`/pitch pitch [index]`: change the pitch of a video on the playlist. '
         'Leave out index to change currently playing video.'),
    ]
    await utils.respond(ctx, '\n'.join(resp), ephemeral=True)


@bot.slash_command(name='help', guild_ids=GUILD_IDS)
async def command_help(ctx: discord.ApplicationContext):
    """Help."""
    await _help(ctx)


@bot.slash_command(name='commands', guild_ids=GUILD_IDS)
async def command_commands(ctx: discord.ApplicationContext):
    """Commands."""
    await _help(ctx)


@bot.slash_command(name='q', guild_ids=GUILD_IDS)
async def command_q(ctx: discord.ApplicationContext):
    """Add song."""
    await send_add_song_modal(ctx)


@bot.slash_command(name='add', guild_ids=GUILD_IDS)
async def command_add(ctx: discord.ApplicationContext):
    """Add song."""
    await send_add_song_modal(ctx)


@bot.slash_command(name='load', guild_ids=GUILD_IDS)
async def command_load(ctx: discord.ApplicationContext):
    """Add song."""
    await send_add_song_modal(ctx)


async def send_add_song_modal(ctx: discord.ApplicationContext):
    """Add song."""
    await ctx.send_modal(AddSongModal(title='Add Song'))


_downloaders = [
    youtube.YoutubeDownloader(),
    nico.NicoNicoDownloader(),
]


async def _load(interaction: discord.Interaction, url: str, pitch: int):
    """Load a song from a url."""
    user = interaction.user
    if user is None:
        return
    karaqueue = common.get_queue(interaction.guild_id, interaction.channel_id)
    async with karaqueue.lock:
        if len(karaqueue) >= common.MAX_QUEUED:
            await utils.respond(
                interaction, 'Queue is full! Delete some items with `/delete`', ephemeral=True)
            return
        if sum(entry.uid == user.id for entry in karaqueue) >= common.MAX_QUEUED_PER_USER:
            await utils.respond(
                interaction,
                f'Each user may only have {common.MAX_QUEUED_PER_USER} songs in the queue!',
                ephemeral=True)
            return
    await utils.respond(interaction, f'Loading `{url}`...', ephemeral=True)
    entry: Optional[common.Entry] = None
    has_match = False
    for downloader in _downloaders:
        if downloader.match(url):
            has_match = True
            path = tempfile.mkdtemp(dir=pathlib.PurePath(common.SERVING_DIR))
            entry = await downloader.load(interaction, url, path)
            break
    if not has_match:
        await utils.respond(interaction, 'Unrecognized url!', ephemeral=True)
    if entry is not None:
        entry.uid = user.id
        entry.pitch_shift = pitch
        async with karaqueue.lock:
            karaqueue.append(entry)
            await print_queue_locked(interaction, karaqueue)
            entry.onchange_locked()
            async with new_process_task:
                new_process_task.notify()
        await interaction.delete_original_response()


@bot.slash_command(name='pitch', guild_ids=GUILD_IDS)
async def command_pitch(ctx: discord.ApplicationContext, pitch: int, index: int = 0):
    """Change the pitch of a song."""
    karaqueue = common.get_queue(ctx.guild_id, ctx.channel_id)
    async with karaqueue.lock:
        if index < 0 or index > len(karaqueue):
            await utils.respond(ctx, 'Invalid index!', ephemeral=True)
            return
        if index == 0:
            if karaqueue.current is None:
                await utils.respond(ctx, 'No song currently playing!', ephemeral=True)
                return
            karaqueue.current.set_pitch_shift(pitch)
            karaqueue.current.onchange_locked()
            async with new_process_task:
                new_process_task.notify()
        elif index <= len(karaqueue):
            entry = karaqueue[index-1]
            entry.set_pitch_shift(pitch)
            await print_queue_locked(ctx, karaqueue)
            entry.onchange_locked()
            async with new_process_task:
                new_process_task.notify()
    if index == 0:
        await _update_with_current(ctx)


@bot.slash_command(name='list', guild_ids=GUILD_IDS)
async def command_list(ctx: discord.ApplicationContext):
    """Show the queue."""
    karaqueue = common.get_queue(ctx.guild_id, ctx.channel_id)
    async with karaqueue.lock:
        await print_queue_locked(ctx, karaqueue)


@bot.slash_command(name='next', guild_ids=GUILD_IDS)
async def command_next(ctx: discord.ApplicationContext):
    """Play the next song."""
    await _next(ctx)


async def _next(ctx: utils.DiscordContext):
    """Play the next song."""
    karaqueue = common.get_queue(ctx.guild_id, ctx.channel_id)
    async with karaqueue.lock:
        if karaqueue.current is not None:
            karaqueue.current.delete()
        if len(karaqueue) == 0:
            await utils.respond(ctx, content='No songs in queue!')
            return
        karaqueue.current = karaqueue.pop(0)
    await _update_with_current(ctx)


async def _update_with_current(ctx: utils.DiscordContext):
    """Update the currently playing song in the queue."""
    karaqueue = common.get_queue(ctx.guild_id, ctx.channel_id)
    entry = karaqueue.current
    if entry is None:
        return
    resp = await utils.respond(ctx, content=f'Loading `{entry.name}`...')
    if isinstance(resp, discord.Interaction):
        resp = await resp.original_response()
    async with karaqueue.lock:
        await print_queue_locked(ctx, karaqueue)
    spinner = itertools.cycle(['|', '/', '-', '\\'])
    cur_msg = ''
    while not entry.processed:
        if entry.error_msg:
            await resp.edit(content=entry.error_msg)
            return
        if entry.load_msg:
            if entry.load_msg != cur_msg:
                await resp.edit(content=entry.load_msg)
            await asyncio.sleep(0.1)
        else:
            await resp.edit(content=f'Loading `{entry.name}`...\n`' + next(spinner)*4 + '`')
            await asyncio.sleep(0.1)
    embed = discord.Embed(
        title='Now playing', description=f'[`{entry.name}`]({entry.original_url})`\n{entry.url()}')
    await resp.edit(embed=embed)


@bot.slash_command(name='delete', guild_ids=GUILD_IDS)
async def command_delete(ctx: discord.ApplicationContext, index: int):
    """Delete a song from the queue."""
    await _delete(ctx, index)


@bot.slash_command(name='remove', guild_ids=GUILD_IDS)
async def command_remove(ctx: discord.ApplicationContext, index: int):
    """Delete a song from the queue."""
    await _delete(ctx, index)


async def _delete(ctx: discord.ApplicationContext, index: int):
    """Delete a song from the queue."""
    karaqueue = common.get_queue(ctx.guild_id, ctx.channel_id)
    async with karaqueue.lock:
        if index < 1 or index > len(karaqueue):
            await utils.respond(ctx, 'Invalid index!', ephemeral=True)
            return
        entry = karaqueue[index-1]

    class DeleteConfirmView(discord.ui.View):
        """Confirmation dialog for deleting a song."""

        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label='Delete', style=discord.ButtonStyle.red)
        async def delete_callback(self, _, __):
            """Delete a song from the queue."""
            async with karaqueue.lock:
                for i in enumerate(karaqueue):
                    if karaqueue[i] == entry:
                        karaqueue[i].delete()
                        del karaqueue[i]
                        break
                await utils.respond(ctx, f'Successfully deleted `{entry.title}` from the queue.')
                await print_queue_locked(ctx, karaqueue)
            await utils.delete(ctx)

        @discord.ui.button(label='Cancel', style=discord.ButtonStyle.gray)
        async def cancel_callback(self, _, __):
            """Cancel deleting song."""
            await utils.delete(ctx)

    await utils.respond(ctx, f'Deleting `{entry.title}`, are you sure?', view=DeleteConfirmView())


@bot.slash_command(name='move', guild_ids=GUILD_IDS)
async def command_move(ctx: discord.ApplicationContext, index_from: int, index_to: int):
    """Change the position of a song in the queue."""
    karaqueue = common.get_queue(ctx.guild_id, ctx.channel_id)
    async with karaqueue.lock:
        if (index_from < 1 or index_from > len(karaqueue)
                or index_to < 1 or index_to > len(karaqueue)):
            await utils.respond(ctx, 'Invalid index!', ephemeral=True)
            return
        if index_from <= index_to:
            index_to -= 1
        entry = karaqueue[index_from-1]
        del karaqueue[index_from-1]
        karaqueue.insert(index_to-1, entry)
        await print_queue_locked(ctx, karaqueue)


async def print_queue_locked(ctx: utils.DiscordContext, karaqueue: common.Queue):
    """Print the current queue."""
    if karaqueue.msg_id is not None:
        try:
            channel = typing.cast(discord.TextChannel,
                                  bot.get_channel(karaqueue.channel_id))
            message = await channel.fetch_message(karaqueue.msg_id)
            await message.delete()
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        karaqueue.msg_id = None

    if len(karaqueue) == 0:
        msg = await utils.respond(ctx, content='No songs in queue!', view=EmptyQueueView())
    else:
        resp = []
        for i, entry in enumerate(karaqueue):
            row = f'{i+1}. [`{entry.name}`]({entry.original_url})'
            resp.append(row)
        embed = discord.Embed(title='Up Next', description='\n'.join(resp))

        class QueueView(EmptyQueueView):
            """Discord view for when queue is not empty. Has a Next Song button."""

            @discord.ui.button(label='Next', style=discord.ButtonStyle.primary)
            async def next_callback(self, _, __):
                """Play the next song."""
                await _next(ctx)

        msg = await utils.respond(ctx, embed=embed, view=QueueView())

    if isinstance(msg, discord.Interaction):
        msg = await msg.original_response()
    karaqueue.msg_id = msg.id


def main():
    """Main."""
    cancel = asyncio.Event()

    def set_cancel(*_):
        cancel.set()
        bot.loop.stop()
    signal.signal(signal.SIGINT, set_cancel)
    signal.signal(signal.SIGTERM, set_cancel)

    async def background_process():
        while True:
            entry_to_process = None
            async with new_process_task:
                # Find next entry to process.
                for karaqueue in common.karaqueue.values():
                    if karaqueue.current is not None and not karaqueue.current.processed:
                        entry_to_process = karaqueue.current
                    else:
                        for entry in karaqueue:
                            if not entry.processed:
                                entry_to_process = entry
                                break
                    if entry_to_process is not None:
                        break
                if entry_to_process is None:
                    await new_process_task.wait()
                    continue
            if entry_to_process.process_task is not None:
                entry_to_process.process_task.cancel()
            process_task = entry_to_process.get_process_task(cancel)
            entry_to_process.process_task = process_task
            await process_task
    bot.loop.create_task(background_process())

    bot.run(BOT_TOKEN)


if __name__ == '__main__':
    main()
