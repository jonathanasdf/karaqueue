import asyncio
import configparser
import dataclasses
import io
import itertools
import logging
import os
import re
import time
from typing import Coroutine, List, Optional
import discord
from discord.ext import commands
import pytube
import StringProgressBar


logging.basicConfig(level=logging.INFO)
cfg = configparser.ConfigParser()
cfg.read(os.path.join(os.path.dirname(__file__), 'config.ini'))


YOUTUBE_PATTERN = re.compile(r'(vi/|v=|/v/|youtu.be/|/embed/)')
BOT_TOKEN = cfg['DEFAULT']['token'] 
GUILD_IDS = [cfg['DEFAULT']['guild_id']]
MAX_QUEUED = 20


@dataclasses.dataclass
class LoadTask:
    ctx: discord.ApplicationContext
    msg: str
    load: Coroutine


@dataclasses.dataclass
class Entry:
    title: str
    url: str
    audio_buffer: io.BytesIO
    video_buffer: io.BytesIO
    pitch_shift: int

    processed: bool = False
    process_task: Optional[asyncio.Task] = None

    def set_pitch_shift_locked(self, pitch_shift: int) -> None:
        if self.pitch_shift == pitch_shift:
            return
        self.pitch_shift = pitch_shift

    def onchange_locked(self) -> None:
        self.processed = False
        if self.process_task is not None:
            self.process_task.cancel()
            self.process_task = None
        new_process_task.notify()

    def get_process_task(self) -> asyncio.Task:
        async def process():
            await asyncio.to_thread(self.process)
            self.processed = True
            self.process_task = None
        return asyncio.create_task(process())

    def process(self):
        time.sleep(10)


bot = commands.Bot()
queue_msg_id = None

current: Optional[Entry] = None
karaqueue: List[Entry] = []
lock = asyncio.Lock()
new_process_task = asyncio.Condition(lock)

loadqueue: List[LoadTask] = []
new_load_task = asyncio.Condition()


async def _help(ctx: discord.ApplicationContext):
    resp = ['hello world']
    await ctx.respond('\n'.join(resp), ephemeral=True)


@bot.slash_command(guild_ids=GUILD_IDS)
async def help(ctx: discord.ApplicationContext):
    await _help(ctx)


@bot.slash_command(guild_ids=GUILD_IDS)
async def commands(ctx: discord.ApplicationContext):
    await _help(ctx)


@bot.slash_command(guild_ids=GUILD_IDS)
async def q(ctx: discord.ApplicationContext, url: str, pitch: Optional[int] = 0):
    await _load(ctx, url, pitch)


@bot.slash_command(guild_ids=GUILD_IDS)
async def load(ctx: discord.ApplicationContext, url: str, pitch: Optional[int] = 0):
    await _load(ctx, url, pitch)


async def _load(ctx: discord.ApplicationContext, url: str, pitch: int):
    async with lock:
        if len(karaqueue) >= MAX_QUEUED:
            await ctx.respond('Queue is full! Delete some items with `/delete`', ephemeral=True)
            return
        if len(karaqueue) + len(loadqueue) >= MAX_QUEUED:
            await ctx.respond('Too many pending requests, please try again later.', ephemeral=True)
            return
    await ctx.respond(f'Loading `{url}`...', ephemeral=True)
    if 'youtu' in url or 'ytimg' in url:
        parts = re.split(YOUTUBE_PATTERN, url)
        if len(parts) < 3:
            await ctx.respond(f'Unrecognized url!', ephemeral=True)
            return
        id = re.split('[^0-9a-zA-Z_\-]', parts[2])[0]
        if len(id) != 11:
            await ctx.respond(f'Unrecognized url!', ephemeral=True)
            return

        await ctx.edit(content=f'Loading youtube id `{id}`...')
        yt = pytube.YouTube(f'http://youtube.com/watch?v={id}')
        msg = f'Loading youtube video `{yt.title}`...'
        await ctx.edit(content=msg)

        async def load_fn():
            await load_youtube(ctx, yt, pitch)
        async with new_load_task:
            await ctx.edit(content=msg + f'\nWaiting for {len(loadqueue)+1} tasks...')
            loadqueue.append(LoadTask(ctx=ctx, msg=msg, load=load_fn()))
            new_load_task.notify()
    else:
        await ctx.respond(f'Unrecognized url!', ephemeral=True)


@bot.slash_command(guild_ids=GUILD_IDS)
async def pitch(ctx: discord.ApplicationContext, pitch: int, index: Optional[int] = 0):
    async with lock:
        entry = None
        if index == 0:
            if current is not None:
                entry = current
        elif index <= len(karaqueue):
            entry = karaqueue[index-1]
        if entry:
            entry.set_pitch_shift_locked(pitch)
            await print_queue_locked(ctx)
            entry.onchange_locked()


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
        if len(karaqueue) == 0:
            await ctx.respond(content='No songs in queue! Add one with `/q`')
            return
        entry = karaqueue.pop(0)
        current = entry
    name = entry.title
    if entry.pitch_shift != 0:
        name = f'name [{entry.pitch_shift:+d}]'
    msg = await ctx.respond(content=f'Loading `{name}`...')
    async with lock:
        await print_queue_locked(ctx)
    spinner = itertools.cycle(['|', '/', '-', '\\'])
    while not entry.processed:
        await msg.edit(content=f'Loading `{name}`...\n`' + next(spinner)*4 + '`')
        await asyncio.sleep(0.1)
    await msg.edit(content=f'Now playing: `{name}`\n{entry.url}')


@bot.slash_command(guild_ids=GUILD_IDS)
async def delete(ctx: discord.ApplicationContext, index: int):
    async with lock:
        if index < 1 or index > len(karaqueue):
            await ctx.respond('Invalid index!', ephemeral=True)
            return
        entry = karaqueue[index-1]

    class DeleteConfirmView(discord.ui.View):

        @discord.ui.button(label='Delete', style=discord.ButtonStyle.red)
        async def delete_callback(self, _, __):
            async with lock:
                for i in range(len(karaqueue)):
                    if karaqueue[i] == entry:
                        del karaqueue[i]
                        break
                await ctx.respond(f'Successfully deleted `{entry.title}` from the queue.')
                await print_queue_locked(ctx)
            await ctx.delete()

        @discord.ui.button(label='Cancel', style=discord.ButtonStyle.gray)
        async def cancel_callback(self, _, __):
            await ctx.delete()

    await ctx.respond(f'Deleting `{entry.title}`, are you sure?', view=DeleteConfirmView())


@bot.slash_command(guild_ids=GUILD_IDS)
async def move(ctx: discord.ApplicationContext, index_from: int, index_to: int):
    async with lock:
        if index_from < 1 or index_from > len(karaqueue) or index_to < 1 or index_to > len(karaqueue):
            await ctx.respond('Invalid index!', ephemeral=True)
            return
        if index_from <= index_to:
            index_to -= 1
        entry = karaqueue[index_from-1]
        del karaqueue[index_from-1]
        karaqueue.insert(index_to-1, entry)
        await print_queue_locked(ctx)


async def load_youtube(ctx: discord.ApplicationContext, yt: pytube.YouTube, pitch_shift: int):
    loop = asyncio.get_running_loop()

    def load_streams():
        audio_buffer = io.BytesIO()
        video_buffer = io.BytesIO()
        audio_stream = yt.streams.get_audio_only()
        video_streams = yt.streams.filter(only_video=True)
        video_stream = video_streams.filter(resolution='720p').first()
        if video_stream is None:
            video_stream = video_streams.order_by('resolution').last()
        if audio_stream is None or video_stream is None:
            asyncio.run_coroutine_threadsafe(ctx.respond(
                content=f'Error: missing either audio or video stream.', ephemeral=True), loop)
            return None, None
        total_size = audio_stream.filesize + video_stream.filesize
        total_size_mb = total_size / 1024 / 1024

        def progress_func(stream: pytube.Stream, _, remaining_bytes: int):
            downloaded = stream.filesize - remaining_bytes
            if stream.includes_video_track:
                downloaded += audio_stream.filesize
            progress = StringProgressBar.progressBar.filledBar(
                total_size, downloaded)
            asyncio.run_coroutine_threadsafe(ctx.edit(
                content=f'Loading youtube video `{yt.title}`...\n{progress[0]} {progress[1]:0.0f}% of {total_size_mb:0.1f}Mb'), loop)

        yt.register_on_progress_callback(progress_func)
        audio_stream.stream_to_buffer(audio_buffer)
        video_stream.stream_to_buffer(video_buffer)
        return audio_buffer, video_buffer

    try:
        audio_buffer, video_buffer = await asyncio.to_thread(load_streams)
    except Exception as err:
        await ctx.respond(content=f'Error: `{err}`', ephemeral=True)
        return None

    entry = Entry(title=yt.title, url=yt.watch_url, audio_buffer=audio_buffer,
                  video_buffer=video_buffer, pitch_shift=pitch_shift)
    async with lock:
        karaqueue.append(entry)
        await print_queue_locked(ctx)
        entry.onchange_locked()
    await ctx.delete()


async def print_queue_locked(ctx: discord.ApplicationContext):
    global queue_msg_id
    if queue_msg_id is not None:
        try:
            channel = bot.get_channel(queue_msg_id[0])
            message = await channel.fetch_message(queue_msg_id[1])
            await message.delete()
        except:
            pass
        queue_msg_id = None

    if len(karaqueue) == 0:
        msg = await ctx.respond(content='No songs in queue! Add one with `/q`')
    else:
        resp = ['Up next:']
        for i, entry in enumerate(karaqueue):
            row = f'{i+1}. `{entry.title}`'
            if entry.pitch_shift != 0:
                row = f'{row} [{entry.pitch_shift:+d}]'
            resp.append(row)

        class QueueView(discord.ui.View):

            @discord.ui.button(label='Next', style=discord.ButtonStyle.primary)
            async def next_callback(self, _, __):
                await _next(ctx)

        msg = await ctx.respond(content='\n'.join(resp), view=QueueView())

    if isinstance(msg, discord.Interaction):
        msg = await msg.original_response()
    queue_msg_id = (msg.channel.id, msg.id)


def main():
    async def background_load():
        while True:
            async with new_load_task:
                while len(loadqueue) == 0:
                    await new_load_task.wait()
                loadtask = loadqueue.pop(0)
                for i, task in enumerate(loadqueue):
                    await task.ctx.edit(content=task.msg + f'\nWaiting for {i+1} tasks...')
            await loadtask.load
    bot.loop.create_task(background_load())

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
