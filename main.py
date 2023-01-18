import asyncio
import configparser
import dataclasses
import itertools
import logging
import os
import random
import re
import shutil
import string
import subprocess
import tempfile
from typing import Callable, List, Optional, Union
import discord
from discord.ext import commands
from PIL import Image
import pytube
import StringProgressBar


logging.basicConfig(level=logging.INFO)
cfg = configparser.ConfigParser()
cfg.read(os.path.join(os.path.dirname(__file__), 'config.ini'))


YOUTUBE_PATTERN = re.compile(r'(vi/|v=|/v/|youtu.be/|/embed/)')
BOT_TOKEN = cfg['DEFAULT']['token']
GUILD_IDS = [cfg['DEFAULT']['guild_id']]
MAX_QUEUED = 20
MAX_QUEUED_PER_USER = 2
HOST = cfg['DEFAULT'].get('host')


SERVING_DIR = '_generated_videos'
os.makedirs(SERVING_DIR, exist_ok=True)
for folder in os.listdir(SERVING_DIR):
    shutil.rmtree(os.path.join(SERVING_DIR, folder))


def call(cmd: str) -> None:
    try:
        subprocess.run(cmd, shell=True, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        logging.error(e.stdout.decode('utf-8'))
        raise


@dataclasses.dataclass
class Entry:
    title: str
    original_url: str
    path: str
    pitch_shift: int
    load_fn: Callable[['Entry'], None]
    uid: int

    loaded: bool = False
    load_msg: str = ''
    error_msg: str = ''
    processed: bool = False
    process_task: Optional[asyncio.Task] = None

    def set_pitch_shift_locked(self, pitch_shift: int) -> None:
        if self.pitch_shift == pitch_shift:
            return
        self.pitch_shift = pitch_shift

    def onchange_locked(self) -> None:
        if self.process_task is not None:
            self.process_task.cancel()
            self.process_task = None
        self.processed = False
        self.load_msg = ''
        self.error_msg = ''
        new_process_task.notify()

    def get_process_task(self) -> asyncio.Task:
        async def process():
            await asyncio.to_thread(self.process)
            self.process_task = None
            self.processed = True
        return asyncio.create_task(process())

    def delete(self) -> None:
        if self.process_task is not None:
            self.process_task.cancel()
            self.process_task = None
        self.processed = False
        shutil.rmtree(self.path)

    def _get_server_path(self, path: str) -> str:
        relpath = os.path.relpath(path, os.path.join(os.getcwd(), SERVING_DIR))
        return f'https://{HOST}/{relpath}'

    def url(self) -> str:
        if not self.processed:
            raise RuntimeError('task has not been processed!')
        if not self.pitch_shift:
            return self.original_url
        return self._get_server_path(self.path) + '?' + ''.join(random.choice(string.ascii_letters) for _ in range(8))

    def process(self) -> None:
        if not self.pitch_shift:
            return

        if not self.loaded:
            self.load_fn(self)
            self.loaded = True

        audio_path = os.path.join(self.path, 'audio.wav')
        if self.pitch_shift:
            self.load_msg = f'Loading youtube video `{self.title}`...\nShifting pitch...'
            shift_path = os.path.join(self.path, 'shifted.wav')
            pitch_cents = int(self.pitch_shift * 100)
            call(f'sox {audio_path} {shift_path} pitch {pitch_cents}')
            audio_path = shift_path

        self.load_msg = f'Loading youtube video `{self.title}`...\nCreating video...'
        video_path = os.path.join(self.path, 'video.mp4')
        output = tempfile.mktemp(dir=self.path, suffix='.mp4')
        call(f'ffmpeg -i {audio_path} -i {video_path} '
             f'-c:v copy -c:a aac -b:a 160k -movflags faststart {output}')

        thumb_path = os.path.join(self.path, 'thumb.jpg')
        thumb = Image.open(thumb_path)

        with open(os.path.join(self.path, 'index.html'), 'w') as f:
            f.write(f"""<!DOCTYPE html>
<html>
    <head>
        <meta property="og:title" content="{self.title}" />
        <meta property="og:type" content="video" />
        <meta property="og:image" content="{self._get_server_path(thumb_path)}" />
        <meta property="og:video" content="{self._get_server_path(output)}" />
        <meta property="og:video:width" content="{thumb.width}" />
        <meta property="og:video:height" content="{thumb.height}" />
        <meta property="og:video:type" content="video/mp4" />
    </head>
</html>
""")


bot = commands.Bot()
queue_msg_id = None

current: Optional[Entry] = None
karaqueue: List[Entry] = []
lock = asyncio.Lock()
new_process_task = asyncio.Condition(lock)


DiscordContext = Union[discord.ApplicationContext, discord.Interaction]


async def respond(ctx: DiscordContext, *args, **kwargs) -> Union[discord.Interaction, discord.WebhookMessage]:
    interaction: discord.Interaction
    if isinstance(ctx, discord.ApplicationContext):
        interaction = ctx.interaction
    else:
        interaction = ctx
    try:
        if not interaction.response.is_done():
            return await interaction.response.send_message(*args, **kwargs)
        else:
            return await interaction.followup.send(*args, **kwargs)
    except discord.errors.InteractionResponded:
        return await interaction.followup.send(*args, **kwargs)


async def edit(ctx: DiscordContext, *args, **kwargs) -> Union[discord.Interaction, discord.WebhookMessage]:
    interaction: discord.Interaction
    if isinstance(ctx, discord.ApplicationContext):
        interaction = ctx.interaction
    else:
        interaction = ctx
    return await interaction.edit_original_response(*args, **kwargs)


async def delete(ctx: DiscordContext, *args, **kwargs) -> Union[discord.Interaction, discord.WebhookMessage]:
    interaction: discord.Interaction
    if isinstance(ctx, discord.ApplicationContext):
        interaction = ctx.interaction
        if not interaction.response.is_done():
            await ctx.defer()
    else:
        interaction = ctx
    return await interaction.delete_original_response(*args, **kwargs)


async def _help(ctx: discord.ApplicationContext):
    resp = [
        'Commands:',
        '`/q`: queue a video from youtube. Also `/add` or `/load`.',
        '`/list`: show the current playlist.',
        '`/next`: play the next entry on the playlist.',
        '`/delete index`: delete an entry from the playlist. Also `/remove`.',
        '`/move from to`: change the position of an entry in the playlist.',
        '`/pitch pitch [index]`: change the pitch of a video on the playlist. Leave out index to change currently playing video.',
    ]
    await respond(ctx, '\n'.join(resp), ephemeral=True)


@bot.slash_command(name='help', guild_ids=GUILD_IDS)
async def command_help(ctx: discord.ApplicationContext):
    await _help(ctx)


@bot.slash_command(name='commands', guild_ids=GUILD_IDS)
async def command_commands(ctx: discord.ApplicationContext):
    await _help(ctx)


class AddSongModal(discord.ui.Modal):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.add_item(discord.ui.InputText(label="URL"))
        self.add_item(discord.ui.InputText(
            label="Pitch Shift (optional)", required=False))

    async def callback(self, interaction: discord.Interaction):
        url = self.children[0].value
        pitch_shift = 0
        if self.children[1].value:
            pitch_shift = int(self.children[1].value)
        await _load(interaction, url, pitch_shift)


@bot.slash_command(name='q', guild_ids=GUILD_IDS)
async def command_q(ctx: discord.ApplicationContext):
    await send_add_song_modal(ctx)


@bot.slash_command(name='add', guild_ids=GUILD_IDS)
async def command_add(ctx: discord.ApplicationContext):
    await send_add_song_modal(ctx)


@bot.slash_command(name='load', guild_ids=GUILD_IDS)
async def command_load(ctx: discord.ApplicationContext):
    await send_add_song_modal(ctx)


async def send_add_song_modal(ctx: discord.ApplicationContext):
    await ctx.send_modal(AddSongModal(title='Add Song'))


async def _load(interaction: discord.Interaction, url: str, pitch: int):
    async with lock:
        if len(karaqueue) >= MAX_QUEUED:
            await respond(interaction, 'Queue is full! Delete some items with `/delete`', ephemeral=True)
            return
        if sum(entry.uid == interaction.user.id for entry in karaqueue) >= MAX_QUEUED_PER_USER:
            await respond(interaction, f'Each user may only have {MAX_QUEUED_PER_USER} songs in the queue!', ephemeral=True)
            return
    await respond(interaction, f'Loading `{url}`...', ephemeral=True)
    if 'youtu' in url or 'ytimg' in url:
        parts = re.split(YOUTUBE_PATTERN, url)
        if len(parts) < 3:
            await respond(interaction, f'Unrecognized url!', ephemeral=True)
            return
        id = re.split('[^0-9a-zA-Z_\-]', parts[2])[0]
        if len(id) != 11:
            await respond(interaction, f'Unrecognized url!', ephemeral=True)
            return

        await edit(interaction, content=f'Loading youtube id `{id}`...')
        yt = pytube.YouTube(f'http://youtube.com/watch?v={id}')
        await edit(interaction, content=f'Loading youtube video `{yt.title}`...')
        await load_youtube(interaction, yt, pitch)
    else:
        await respond(interaction, f'Unrecognized url!', ephemeral=True)


@bot.slash_command(name='pitch', guild_ids=GUILD_IDS)
async def command_pitch(ctx: discord.ApplicationContext, pitch: int, index: Optional[int] = 0):
    async with lock:
        if index < 0 or index > len(karaqueue):
            await respond(ctx, 'Invalid index!', ephemeral=True)
            return
        if index == 0:
            if current is None:
                await respond(ctx, 'No song currently playing!', ephemeral=True)
                return
            current.set_pitch_shift_locked(pitch)
            current.onchange_locked()
        elif index <= len(karaqueue):
            entry = karaqueue[index-1]
            entry.set_pitch_shift_locked(pitch)
            await print_queue_locked(ctx)
            entry.onchange_locked()
    if index == 0:
        await _update_with_current(ctx)


@bot.slash_command(name='list', guild_ids=GUILD_IDS)
async def command_list(ctx: discord.ApplicationContext):
    async with lock:
        await print_queue_locked(ctx)


@bot.slash_command(name='next', guild_ids=GUILD_IDS)
async def command_next(ctx: discord.ApplicationContext):
    await _next(ctx)


async def _next(ctx: discord.ApplicationContext):
    global current
    async with lock:
        if current != None:
            current.delete()
        if len(karaqueue) == 0:
            await respond(ctx, content='No songs in queue!')
            return
        current = karaqueue.pop(0)
    await _update_with_current(ctx)


async def _update_with_current(ctx: discord.ApplicationContext):
    entry: Entry = current
    name = entry.title
    if entry.pitch_shift != 0:
        name = f'{name} [{entry.pitch_shift:+d}]'
    resp = await respond(ctx, content=f'Loading `{name}`...')
    if isinstance(resp, discord.Interaction):
        resp = await resp.original_response()
    async with lock:
        await print_queue_locked(ctx)
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
            await resp.edit(content=f'Loading `{name}`...\n`' + next(spinner)*4 + '`')
            await asyncio.sleep(0.1)
    await resp.edit(content=f'Now playing: `{name}`\n{entry.url()}')


@bot.slash_command(name='delete', guild_ids=GUILD_IDS)
async def command_delete(ctx: discord.ApplicationContext, index: int):
    await _delete(ctx, index)


@bot.slash_command(name='remove', guild_ids=GUILD_IDS)
async def command_remove(ctx: discord.ApplicationContext, index: int):
    await _delete(ctx, index)


async def _delete(ctx: discord.ApplicationContext, index: int):
    async with lock:
        if index < 1 or index > len(karaqueue):
            await respond(ctx, 'Invalid index!', ephemeral=True)
            return
        entry = karaqueue[index-1]

    class DeleteConfirmView(discord.ui.View):

        @discord.ui.button(label='Delete', style=discord.ButtonStyle.red)
        async def delete_callback(self, _, __):
            async with lock:
                for i in range(len(karaqueue)):
                    if karaqueue[i] == entry:
                        karaqueue[i].delete()
                        del karaqueue[i]
                        break
                await respond(ctx, f'Successfully deleted `{entry.title}` from the queue.')
                await print_queue_locked(ctx)
            await delete(ctx)

        @discord.ui.button(label='Cancel', style=discord.ButtonStyle.gray)
        async def cancel_callback(self, _, __):
            await delete(ctx)

    await respond(ctx, f'Deleting `{entry.title}`, are you sure?', view=DeleteConfirmView(timeout=None))


@bot.slash_command(name='move', guild_ids=GUILD_IDS)
async def command_move(ctx: discord.ApplicationContext, index_from: int, index_to: int):
    async with lock:
        if (index_from < 1 or index_from > len(karaqueue)
                or index_to < 1 or index_to > len(karaqueue)):
            await respond(ctx, 'Invalid index!', ephemeral=True)
            return
        if index_from <= index_to:
            index_to -= 1
        entry = karaqueue[index_from-1]
        del karaqueue[index_from-1]
        karaqueue.insert(index_to-1, entry)
        await print_queue_locked(ctx)


async def load_youtube(interaction: discord.Interaction, yt: pytube.YouTube, pitch_shift: int):

    def load_streams(entry: Entry):
        audio_stream = yt.streams.filter(subtype='mp4').get_audio_only()
        video_streams = yt.streams.filter(subtype='mp4', only_video=True)
        video_stream = video_streams.filter(resolution='720p').first()
        if video_stream is None:
            video_stream = video_streams.order_by('resolution').last()
        if audio_stream is None or video_stream is None:
            entry.error_msg = 'Error: missing either audio or video stream.'
            return None, None
        total_size = audio_stream.filesize + video_stream.filesize
        total_size_mb = total_size / 1024 / 1024

        def progress_func(stream: pytube.Stream, _, remaining_bytes: int):
            downloaded = stream.filesize - remaining_bytes
            if stream.includes_video_track:
                downloaded += audio_stream.filesize
            progress = StringProgressBar.progressBar.filledBar(
                total_size, downloaded)
            entry.load_msg = (f'Loading youtube video `{yt.title}`...\n'
                              f'Downloading: {progress[0]} {progress[1]:0.0f}% of {total_size_mb:0.1f}Mb')

        yt.register_on_progress_callback(progress_func)
        audio_stream.download(output_path=entry.path, filename='audio.mp4')
        video_stream.download(output_path=entry.path, filename='video.mp4')
        call(f'ffmpeg -i {os.path.join(entry.path, "audio.mp4")} '
             f'-ac 2 -f wav {os.path.join(entry.path, "audio.wav")}')
        call(f'ffmpeg -i {os.path.join(entry.path, "video.mp4")} -vf "select=eq(n\,0)" '
             f'-q:v 3 {os.path.join(entry.path, "thumb.jpg")}')

    path = tempfile.mkdtemp(dir=SERVING_DIR)
    entry = Entry(title=yt.title, original_url=yt.watch_url,
                  path=path, pitch_shift=pitch_shift, uid=interaction.user.id, load_fn=load_streams)
    async with lock:
        karaqueue.append(entry)
        await print_queue_locked(interaction)
        entry.onchange_locked()
    await interaction.delete_original_response()


async def print_queue_locked(ctx: Union[discord.ApplicationContext, discord.Interaction]):
    global queue_msg_id
    if queue_msg_id is not None:
        try:
            channel = bot.get_channel(queue_msg_id[0])
            message = await channel.fetch_message(queue_msg_id[1])
            await message.delete()
        except:
            pass
        queue_msg_id = None

    class EmptyQueueView(discord.ui.View):

        @discord.ui.button(label='Add Song', style=discord.ButtonStyle.green)
        async def add_callback(self, _, interaction):
            await interaction.response.send_modal(AddSongModal(title='Add Song'))

    if len(karaqueue) == 0:
        msg = await respond(ctx, content='No songs in queue!', view=EmptyQueueView(timeout=None))
    else:
        resp = ['Up next:']
        for i, entry in enumerate(karaqueue):
            row = f'{i+1}. `{entry.title}`'
            if entry.pitch_shift != 0:
                row = f'{row} [{entry.pitch_shift:+d}]'
            resp.append(row)

        class QueueView(EmptyQueueView):

            @discord.ui.button(label='Next', style=discord.ButtonStyle.primary)
            async def next_callback(self, _, __):
                await _next(ctx)

        msg = await respond(ctx, content='\n'.join(resp), view=QueueView(timeout=None))

    if isinstance(msg, discord.Interaction):
        msg = await msg.original_response()
    queue_msg_id = (msg.channel.id, msg.id)


def main():
    async def background_process():
        while True:
            entry_to_process = None
            process_task = None
            async with new_process_task:
                while True:
                    if current is not None and not current.processed:
                        entry_to_process = current
                    else:
                        for entry in karaqueue:
                            if not entry.processed:
                                entry_to_process = entry
                                break
                    if entry_to_process is not None:
                        break
                    await new_process_task.wait()
                if entry_to_process.process_task is not None:
                    entry_to_process.process_task.cancel()
                process_task = entry_to_process.get_process_task()
                entry_to_process.process_task = process_task
            await process_task
    bot.loop.create_task(background_process())

    bot.run(BOT_TOKEN)


if __name__ == '__main__':
    main()
