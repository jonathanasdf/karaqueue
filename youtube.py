import os
import re
import tempfile
from typing import Optional
import discord
import pytube
import StringProgressBar
import common
import utils


YOUTUBE_PATTERN = re.compile(r'(vi/|v=|/v/|youtu.be/|/embed/)')


def match(url: str) -> bool:
    return 'youtu' in url or 'ytimg' in url


async def load_youtube(interaction: discord.Interaction, url: str, pitch_shift: int) -> Optional[common.Entry]:
    parts = re.split(YOUTUBE_PATTERN, url)
    if len(parts) < 3:
        await utils.respond(interaction, f'Unrecognized url!', ephemeral=True)
        return
    id = re.split('[^0-9a-zA-Z_\-]', parts[2])[0]
    if len(id) != 11:
        await utils.respond(interaction, f'Unrecognized url!', ephemeral=True)
        return

    await utils.edit(interaction, content=f'Loading youtube id `{id}`...')
    yt = pytube.YouTube(f'http://youtube.com/watch?v={id}')
    await utils.edit(interaction, content=f'Loading youtube video `{yt.title}`...')
    if yt.length > common.VIDEO_LENGTH_LIMIT_MINS * 60:
        await utils.respond(
            interaction,
            f'Please only queue videos shorter than {common.VIDEO_LENGTH_LIMIT_MINS} minutes.',
            ephemeral=True)
        return

    def load_streams(entry: common.Entry):
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
        utils.call('ffmpeg', f'-i {os.path.join(entry.path, "audio.mp4")} '
                   f'-ac 2 -f wav {os.path.join(entry.path, "audio.wav")}')
        utils.call('ffmpeg', f'-i {os.path.join(entry.path, "video.mp4")} -vf "select=eq(n\,0)" '
                   f'-q:v 3 {os.path.join(entry.path, "thumb.jpg")}')

    path = tempfile.mkdtemp(dir=common.SERVING_DIR)
    entry = common.Entry(title=yt.title, original_url=yt.watch_url,
                         path=path, pitch_shift=pitch_shift, uid=interaction.user.id, load_fn=load_streams)
    return entry
