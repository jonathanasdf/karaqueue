"""Karaqueue discord bot."""
import asyncio
import datetime
import itertools
import logging
import math
import os
import pathlib
import signal
import tempfile
import typing
from typing import Optional
from absl import app
from absl import flags
import discord
from discord.ext import commands

from karaqueue import common
from karaqueue import utils
from karaqueue import downloaders
from karaqueue import players


FLAGS = flags.FLAGS

flags.DEFINE_bool('gui', False, 'If windows gui is available.')


def setup_logging():
    """Set up logging."""
    # Clear handlers from imports.
    logging.getLogger().handlers = []
    fmt = '%(asctime)s %(levelname)-8s %(message)s'
    datefmt = '%Y-%m-%d %H:%M:%S'
    formatter = logging.Formatter(fmt, datefmt)
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt=datefmt)
    file_handler = logging.FileHandler(
        os.path.join(os.path.dirname(__file__), 'main.log'))
    file_handler.formatter = formatter
    logging.getLogger().addHandler(file_handler)


setup_logging()


# config.ini keys
_DEFAULT = 'DEFAULT'
_TOKEN = 'token'

BOT_TOKEN = common.CONFIG[_DEFAULT][_TOKEN]
DEV_USER_ID = common.CONFIG[_DEFAULT].get('dev_user_id')
PLAYER = common.CONFIG[_DEFAULT].get('player')
LAUNCH_BINARY = common.CONFIG[_DEFAULT].get('launch_binary')
LAUNCH_OPTS = common.CONFIG[_DEFAULT].get('launch_opts')


os.makedirs(common.SERVING_DIR, exist_ok=True)


# A new video is queued for offline processing.
global_cancel = asyncio.Event()
new_process_task = asyncio.Condition()


class AddSongModal(discord.ui.Modal):
    """Discord view for adding new song."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.add_item(discord.ui.InputText(label="Video URL"))  # type: ignore
        self.add_item(discord.ui.InputText(
            label="Audio URL (optional)", required=False))  # type: ignore
        self.add_item(discord.ui.InputText(
            label="Pitch Shift (optional)", required=False))  # type: ignore
        self.add_item(discord.ui.InputText(
            label="Audio Delay Milliseconds (optional)", required=False))  # type: ignore

    async def callback(self, interaction: discord.Interaction):
        video_url = str(self.children[0].value)
        audio_url = str(self.children[1].value)
        pitch_shift = 0
        if self.children[2].value:
            pitch_shift = int(self.children[2].value)
        offset_ms = 0
        if self.children[3].value:
            offset_ms = int(self.children[3].value)
        await _load(interaction, video_url, audio_url, pitch_shift, offset_ms)


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


def _is_dev_id(author_id) -> bool:
    """Is this id the dev user."""
    if not DEV_USER_ID:
        return False
    return str(author_id) == DEV_USER_ID


async def _help(ctx: discord.ApplicationContext):
    resp = [
        'Commands:',
        '`/q`: queue a video from youtube. Also `/add` or `/load`.',
        '`/list`: show the current playlist.',
        '`/next`: play the next entry on the playlist.',
        '`/reload`: reload the current song if for some reason it got bugged.',
        '`/delete index`: delete an entry from the playlist. Also `/remove`.',
        '`/move from to`: change the position of an entry in the playlist.',
        ('`/pitch pitch [index]`: change the pitch of a video on the playlist. '
         'Leave out index to change currently playing video.'),
        ('`/offset offset [index]`: change the audio delay of a video on the playlist. '
         'Leave out index to change currently playing video. '
         'Delay is in milliseconds. Positive numbers make the audio later.'),
    ]
    await utils.respond(ctx, '\n'.join(resp), ephemeral=True)


@bot.slash_command(name='help', aliases=['commands'])
async def command_help(ctx: discord.ApplicationContext):
    """Help."""
    await _help(ctx)


@bot.slash_command(name='commands')
async def command_commands(ctx: discord.ApplicationContext):
    """Commands."""
    await _help(ctx)


@bot.slash_command(name='q')
async def command_q(ctx: discord.ApplicationContext):
    """Add song."""
    await send_add_song_modal(ctx)


@bot.slash_command(name='add')
async def command_add(ctx: discord.ApplicationContext):
    """Add song."""
    await send_add_song_modal(ctx)


@bot.slash_command(name='load')
async def command_load(ctx: discord.ApplicationContext):
    """Add song."""
    await send_add_song_modal(ctx)


async def send_add_song_modal(ctx: discord.ApplicationContext):
    """Add song."""
    await ctx.send_modal(AddSongModal(title='Add Song'))


async def _load(
    interaction: discord.Interaction, video_url: str, audio_url: str, pitch: int, offset_ms: int,
):
    """Load a song from a url."""
    user = interaction.user
    if user is None:
        return
    video_url = video_url.strip()
    audio_url = audio_url.strip()
    q = await common.get_queue(interaction)
    if len(q) >= common.MAX_QUEUED:
        await utils.respond(
            interaction, 'Queue is full! Delete some items with `/delete`', ephemeral=True)
        return
    if (not _is_dev_id(user.id) and
            sum(entry.user_id == user.id for entry in q) >= q.per_user_limit):
        await utils.respond(
            interaction,
            f'Each user may only have {q.per_user_limit} songs in the queue!',
            ephemeral=True)
        return

    await utils.respond(interaction, f'Loading `{video_url}`...', ephemeral=True)
    path = tempfile.mkdtemp(dir=pathlib.PurePath(common.SERVING_DIR))

    async def download(url: str, *, video: bool, audio: bool) -> common.DownloadResult:
        for downloader in downloaders.all_downloaders:
            if downloader.match(url):
                logging.info(f'Loading {url}...')
                return await downloader.load(interaction, url, video=video, audio=audio)
        raise ValueError(f'Unrecognized url `{url}`')

    try:
        video_result = await download(video_url, video=True, audio=not audio_url)
    except Exception as err:  # pylint: disable=broad-except
        logging.exception(err)
        await utils.respond(interaction, f'Error: {err}', ephemeral=True)
        return
    load_fns = [video_result.load_fn]
    if audio_url:
        try:
            audio_result = await download(audio_url, video=False, audio=True)
        except Exception as err:  # pylint: disable=broad-except
            logging.exception(err)
            await utils.respond(interaction, f'Error: {err}', ephemeral=True)
            return
        load_fns.append(audio_result.load_fn)
    entry = common.Entry(
        path=path,
        title=video_result.title,
        original_url=video_result.original_url,
        load_fns=load_fns,
        queue=q,
        user_id=user.id,
        pitch_shift=pitch,
        offset_ms=offset_ms)
    # Delete the loading message.
    await interaction.delete_original_response()
    async with q.lock:
        q.append(entry)
        if q.current is not None:
            await print_queue_locked(interaction, q)
            entry.onchange_locked()
            async with new_process_task:
                new_process_task.notify()
        else:
            logging.info('Next called from load because nothing is playing.')
            await _next_locked(interaction, q, is_user_action=False)


@bot.slash_command(name='pitch')
async def command_pitch(ctx: discord.ApplicationContext, pitch: int, index: int = 0):
    """Change the pitch of a song."""
    q = await common.get_queue(ctx)
    current_updated = False
    async with q.lock:
        if index < 0 or index > len(q):
            await utils.respond(ctx, 'Invalid index!', ephemeral=True)
            return
        if index == 0:
            if q.current is None:
                await utils.respond(ctx, 'No song currently playing!', ephemeral=True)
                return
            if q.current.pitch_shift != pitch:
                q.current.pitch_shift = pitch
                q.current.onchange_locked()
                async with new_process_task:
                    new_process_task.notify()
                current_updated = True
        elif index <= len(q):
            entry = q[index-1]
            if entry.pitch_shift != pitch:
                entry.pitch_shift = pitch
                await print_queue_locked(ctx, q)
                entry.onchange_locked()
                async with new_process_task:
                    new_process_task.notify()
    if current_updated:
        await _update_with_current(ctx)


@bot.slash_command(name='offset')
async def command_offset(
    ctx: discord.ApplicationContext, offset_ms: int, index: Optional[int] = None,
):
    """Change the offset of a song."""
    q = await common.get_queue(ctx)
    current_updated = False
    async with q.lock:
        if index is None:
            if q.global_offset_ms != offset_ms:
                q.global_offset_ms = offset_ms
                if q.current is not None:
                    q.current.onchange_locked()
                    current_updated = True
                for entry in q:
                    entry.onchange_locked()
                async with new_process_task:
                    new_process_task.notify()
            await utils.respond(ctx, f'Updated global offset to {offset_ms}', ephemeral=True)
        else:
            if index < 0 or index > len(q):
                await utils.respond(ctx, 'Invalid index!', ephemeral=True)
                return
            if index == 0:
                if q.current is None:
                    await utils.respond(ctx, 'No song currently playing!', ephemeral=True)
                    return
                if q.current.offset_ms != offset_ms:
                    q.current.offset_ms = offset_ms
                    q.current.onchange_locked()
                    async with new_process_task:
                        new_process_task.notify()
                    current_updated = True
            elif index <= len(q):
                entry = q[index-1]
                if entry.offset_ms != offset_ms:
                    entry.offset_ms = offset_ms
                    entry.onchange_locked()
                    async with new_process_task:
                        new_process_task.notify()
                await utils.respond(
                    ctx, f'Updated offset for {entry.title} to {offset_ms}', ephemeral=True)
    if current_updated:
        await _update_with_current(ctx)


@bot.slash_command(name='list')
async def command_list(ctx: discord.ApplicationContext):
    """Show the queue."""
    q = await common.get_queue(ctx)
    async with q.lock:
        await print_queue_locked(ctx, q)


@bot.slash_command(name='next')
async def command_next(ctx: discord.ApplicationContext):
    """Play the next song."""
    logging.info('Next called from command.')
    await _next(ctx, is_user_action=True)


async def _next(ctx: utils.DiscordContext, is_user_action: bool):
    """Play the next song."""
    q = await common.get_queue(ctx)
    async with q.lock:
        await _next_locked(ctx, q, is_user_action=is_user_action)


async def _next_locked(ctx: utils.DiscordContext, q: common.Queue, is_user_action: bool):
    """Play the next song."""
    now = datetime.datetime.now()
    if q.next_advance_time is not None and now < q.next_advance_time:
        if is_user_action:
            diff = math.ceil(
                (q.next_advance_time - now).microseconds / 1e6)
            msg = f'A new song just started! The next button will be enabled in {diff}s.'
            await utils.respond(ctx, content=msg, ephemeral=True)
        return
    q.next_advance_time = now + \
        datetime.timedelta(seconds=common.ADVANCE_BUFFER_SECS)
    if q.current is not None:
        q.current.delete()
        q.current = None
    if len(q) == 0:
        await utils.respond(ctx, content='No songs in queue!')
        return
    q.current = q.pop(0)
    async with new_process_task:
        new_process_task.notify()
    bot.loop.create_task(_update_with_current(ctx, delete_old_queue_msg=False))


async def _update_with_current(ctx: utils.DiscordContext, delete_old_queue_msg: bool = True):
    """Update the currently playing song in the queue."""
    q = await common.get_queue(ctx)
    if q.local and not LAUNCH_BINARY:
        await utils.respond(
            ctx, content='launch_binary must be configured for local mode.', ephemeral=True)
        return
    async with q.lock:
        entry = q.current
        if entry is None:
            return
        await print_queue_locked(ctx, q, delete_old_queue_msg)
    resp = await utils.respond(ctx, content=f'Loading `{entry.name}`...')
    if isinstance(resp, discord.Interaction):
        resp = await resp.original_response()
    spinner = itertools.cycle(['|', '/', '-', '\\'])
    cur_msg = ''
    while not entry.processed:
        if entry.error_msg:
            await resp.edit(content=f'Loading `{entry.name}`...\n{entry.error_msg}')
            return
        if entry.load_msg:
            if entry.load_msg != cur_msg:
                await resp.edit(content=entry.load_msg)
            await asyncio.sleep(0.1)
        else:
            await resp.edit(content=f'Loading `{entry.name}`...\n`' + next(spinner)*4 + '`')
            await asyncio.sleep(0.1)
    logging.info(f'Now playing {entry.name} {entry.url()}')
    if q.local:
        await resp.edit(content=f'**Now playing**\n[`{entry.name}`](<{entry.original_url}>)')
        args = f'"{entry.video_path()}"'
        if LAUNCH_OPTS:
            args += f' {LAUNCH_OPTS}'
        utils.call(LAUNCH_BINARY, args, background=True)

        if PLAYER:
            async def monitor_player():
                player = players.player_lookup[PLAYER]
                playback_started = False
                sleep_time = 1
                while True:
                    if global_cancel.is_set():
                        raise asyncio.CancelledError()
                    await asyncio.sleep(sleep_time)
                    sleep_time = 5
                    try:
                        status = await player.get_status()
                    except Exception:  # pylint: disable=broad-exception-caught
                        continue
                    if status is not None:
                        if status.filename == os.path.basename(entry.video_path()):
                            if (playback_started and
                                    (status.position == status.duration or status.position == 0)):
                                bot.loop.create_task(
                                    _next(ctx, is_user_action=False))
                                return
                            if status.position > 0 and status.position < status.duration:
                                playback_started = True
                            if status.duration - status.position < 5000:
                                sleep_time = (status.duration -
                                              status.position) / 1000.0 + 0.2
                        elif playback_started:
                            # Somehow we're on the next song already, stop waiting.
                            return
            entry.player_monitor_task = bot.loop.create_task(monitor_player())
    else:
        await resp.edit(
            content=(f'**Now playing**\n[`{entry.name}`](<{entry.original_url}>)'
                     f'[.]({entry.url()})'))


@bot.slash_command(name='delete')
async def command_delete(ctx: discord.ApplicationContext, index: int):
    """Delete a song from the queue."""
    await _delete(ctx, index)


@bot.slash_command(name='remove')
async def command_remove(ctx: discord.ApplicationContext, index: int):
    """Delete a song from the queue."""
    await _delete(ctx, index)


async def _delete(ctx: discord.ApplicationContext, index: int):
    """Delete a song from the queue."""
    q = await common.get_queue(ctx)
    async with q.lock:
        if index < 1 or index > len(q):
            await utils.respond(ctx, 'Invalid index!', ephemeral=True)
            return
        entry = q[index-1]

    class DeleteConfirmView(discord.ui.View):
        """Confirmation dialog for deleting a song."""

        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label='Delete', style=discord.ButtonStyle.red)
        async def delete_callback(self, _, __):
            """Delete a song from the queue."""
            async with q.lock:
                for i, _ in enumerate(q):
                    if q[i] == entry:
                        q[i].delete()
                        del q[i]
                        break
                await utils.respond(ctx, f'Successfully deleted `{entry.title}` from the queue.')
                await print_queue_locked(ctx, q, delete_old_queue_msg=False)
            await utils.delete(ctx)

        @discord.ui.button(label='Cancel', style=discord.ButtonStyle.gray)
        async def cancel_callback(self, _, __):
            """Cancel deleting song."""
            await utils.delete(ctx)

    await utils.respond(ctx, f'Deleting `{entry.title}`, are you sure?', view=DeleteConfirmView())


@bot.slash_command(name='move')
async def command_move(ctx: discord.ApplicationContext, index_from: int, index_to: int):
    """Change the position of a song in the queue."""
    q = await common.get_queue(ctx)
    async with q.lock:
        if (index_from < 1 or index_from > len(q)
                or index_to < 1 or index_to > len(q)):
            await utils.respond(ctx, 'Invalid index!', ephemeral=True)
            return
        entry = q[index_from-1]
        del q[index_from-1]
        q.insert(index_to-1, entry)
        await print_queue_locked(ctx, q)


@bot.slash_command(name='reload')
async def command_reload(ctx: discord.ApplicationContext):
    """Reload the current song."""
    q= await common.get_queue(ctx)
    async with q.lock:
        if not q.current:
            await utils.respond(ctx, 'No current song to reload!', ephemeral=True)
            return
        q.current.onchange_locked()
        async with new_process_task:
            new_process_task.notify()
    await utils.respond(ctx, content='Success', ephemeral=True)
    await _update_with_current(ctx)


async def is_dev(ctx: commands.Context) -> bool:
    """Is the user the dev user."""
    return _is_dev_id(ctx.author.id)


@bot.slash_command(name='dev')
@commands.check(is_dev)
async def command_dev(ctx: discord.ApplicationContext, command: str):
    """Dev commands."""
    q = await common.get_queue(ctx)
    if command == 'info':
        key = await common.get_queue_key(ctx)
        msg = f'Current queue: {key} [{id(q)}]'
        msg = msg + '\nAll queues:'
        for queue_key, queue in common.queues.items():
            msg = msg + f'\n{queue_key} [{id(queue)}]'
            if queue.current:
                msg = msg + \
                    f'\nCurrent: [`{queue.current.name}`](<{queue.current.original_url}>)'
            contents = queue.format()
            if contents:
                msg = msg + f'\n{contents}'
        msg += f'\nLocal mode: {q.local}'
        if q.local and PLAYER:
            player = players.player_lookup[PLAYER]
            status = await player.get_status()
            if status is None:
                msg = msg + '\nLocal player not active.'
            else:
                msg = msg + f'\n{status}'
        await utils.respond(ctx, content=msg, ephemeral=True)
    elif command == 'local':
        q.local = not q.local
        await utils.respond(ctx, content=f'Success! Local mode is now {q.local}', ephemeral=True)
    elif command.startswith('limit '):
        try:
            value = float(command[len('limit '):])
        except ValueError:
            await utils.respond(ctx, content=f'Unrecognized command {command}', ephemeral=True)
        q.per_user_limit = value
        await utils.respond(ctx, content='Success', ephemeral=True)
    else:
        await utils.respond(ctx, content=f'Unrecognized command {command}', ephemeral=True)


async def print_queue_locked(
    ctx: utils.DiscordContext, q: common.Queue, delete_old_queue_msg: bool = True,
):
    """Print the current queue."""
    if delete_old_queue_msg and q.msg_id is not None:
        try:
            channel = typing.cast(discord.TextChannel,
                                  bot.get_channel(q.channel_id))
            message = await channel.fetch_message(q.msg_id)
            await message.delete()
        except Exception:  # pylint: disable=broad-exception-caught
            # Message already deleted, etc.
            pass
    q.msg_id = None

    msg = ''
    if q.current:
        msg = f'**Now playing**\n`{q.current.name}`'
    if len(q) == 0:
        msg = f'{msg}\nNo songs in queue!'.strip()
        msg = await utils.respond(ctx, content=msg, view=EmptyQueueView())
    else:
        msg = f'{msg}\n**Up Next**\n{q.format()}'.strip()

        class QueueView(EmptyQueueView):
            """Discord view for when queue is not empty. Has a Next Song button."""

            @discord.ui.button(label='Next', style=discord.ButtonStyle.primary)
            async def next_callback(self, _, __):
                """Play the next song."""
                logging.info('Next called from button click.')
                await _next(ctx, is_user_action=True)

        msg = await utils.respond(ctx, msg, view=QueueView())

    if isinstance(msg, discord.Interaction):
        msg = await msg.original_response()
    q.msg_id = msg.id


def main(_):
    """Main."""
    def interrupt(*_):
        global_cancel.set()
        bot.loop.stop()
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)

    async def background_process():
        while True:
            entry_to_process = None
            async with new_process_task:
                # Find next entry to process.
                for q in common.queues.values():
                    if q.current is not None and not q.current.processed:
                        entry_to_process = q.current
                    else:
                        for entry in q:
                            if not entry.processed:
                                entry_to_process = entry
                                break
                    if entry_to_process is not None:
                        break
                if entry_to_process is None:
                    await new_process_task.wait()
                    continue
            try:
                await entry_to_process.create_process_task(bot.loop, global_cancel)
            except asyncio.CancelledError:
                pass
    bot.loop.create_task(background_process())
    bot.run(BOT_TOKEN)


if __name__ == '__main__':
    app.run(main)
