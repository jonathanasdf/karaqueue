import asyncio
import dataclasses
import io
import logging
import re
from typing import List, Optional
import discord
from discord.ext import commands
import pytube


logging.basicConfig(level=logging.INFO)


YOUTUBE_PATTERN = re.compile(r'(vi/|v=|/v/|youtu.be/|/embed/)')
GUILD_IDS = ['1063321924291280927']
MAX_QUEUED = 10


@dataclasses.dataclass
class Entry:
    title: str
    url: str
    audio_buffer: io.BytesIO
    video_buffer: io.BytesIO
    pitch_shift: int


bot = commands.Bot()
lock = asyncio.Lock()
karaqueue: List[Entry] = []
queue_msg_id = None
workqueue = asyncio.Queue()


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

        async def task():
            await load_youtube(ctx, id, pitch)
        await workqueue.put(task)
        return
    else:
        await ctx.respond(f'Unrecognized url!', ephemeral=True)
        return


@bot.slash_command(guild_ids=GUILD_IDS)
async def list(ctx: discord.ApplicationContext):
    async with lock:
        await print_queue_locked(ctx)


@bot.slash_command(guild_ids=GUILD_IDS)
async def next(ctx: discord.ApplicationContext):
    async with lock:
        if len(karaqueue) == 0:
            await ctx.respond(content='No songs in queue! Add one with `/q`')
            return
        entry = karaqueue.pop(0)
    name = entry.title
    if entry.pitch_shift != 0:
        name = f'name [{entry.pitch_shift:+d}]'
    await ctx.respond(content=f'Now playing: `{name}`\n{entry.url}')
    async with lock:
        await print_queue_locked(ctx)


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


async def load_youtube(ctx: discord.ApplicationContext, id: str, pitch_shift: int):
    loop = asyncio.get_running_loop()
    await ctx.edit(content=f'Loading youtube id `{id}`...')
    yt = pytube.YouTube(f'http://youtube.com/watch?v={id}')
    title = yt.title
    await ctx.edit(content=f'Loading youtube video `{title}`...')

    # def load_streams():
    #     audio_buffer = io.BytesIO()
    #     video_buffer = io.BytesIO()
    #     audio_stream = yt.streams.get_audio_only()
    #     video_streams = yt.streams.filter(only_video=True)
    #     video_stream = video_streams.filter(resolution='720p').first()
    #     if video_stream is None:
    #         video_stream = video_streams.order_by('resolution').last()
    #     if audio_stream is None or video_stream is None:
    #         logging.info('Audio: %s\nVideo (%d): %s', audio_stream,
    #                      len(video_streams), video_streams)
    #         asyncio.run_coroutine_threadsafe(ctx.respond(
    #             content=f'Error: missing either audio or video stream.', ephemeral=True), loop)
    #         return None, None
    #     total_size = audio_stream.filesize + video_stream.filesize
    #     total_size_mb = total_size / 1024 / 1024

    #     def progress_func(stream: pytube.Stream, _, remaining_bytes: int):
    #         downloaded = stream.filesize - remaining_bytes
    #         if stream.includes_video_track:
    #             downloaded += audio_stream.filesize
    #         progress = StringProgressBar.progressBar.filledBar(
    #             total_size, downloaded)
    #         asyncio.run_coroutine_threadsafe(ctx.edit(
    #             content=f'Loading youtube video `{title}`...\n{progress[0]} {progress[1]:0.0f}% of {total_size_mb:0.1f}Mb'), loop)

    #     yt.register_on_progress_callback(progress_func)
    #     audio_stream.stream_to_buffer(audio_buffer)
    #     video_stream.stream_to_buffer(video_buffer)
    #     return audio_buffer, video_buffer

    # try:
    #     audio_buffer, video_buffer = await asyncio.to_thread(load_streams)
    # except Exception as err:
    #     await ctx.respond(content=f'Error: `{err}`', ephemeral=True)
    #     return None

    # entry = Entry(title=title, url=yt.watch_url, audio_buffer=audio_buffer,
    #               video_buffer=video_buffer, pitch_shift=pitch_shift)
    entry = Entry(title=title, url=yt.watch_url, audio_buffer=None,
                  video_buffer=None, pitch_shift=pitch_shift)
    await ctx.delete()
    async with lock:
        karaqueue.append(entry)
        await print_queue_locked(ctx)


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
        msg = await ctx.respond(content='\n'.join(resp))
    if isinstance(msg, discord.Interaction):
        msg = await msg.original_response()
    queue_msg_id = (msg.channel.id, msg.id)


def main():
    async def background():
        while True:
            work = await workqueue.get()
            await work()
    bot.loop.create_task(background())

    token = open('token', 'r')
    bot.run(token.read())


if __name__ == '__main__':
    main()
