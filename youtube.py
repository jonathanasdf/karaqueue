"""Youtube utils."""
import os
import re
import shutil
from typing import Optional, Tuple
import discord
import pytube
import requests
import StringProgressBar

from . import common
from . import utils


YOUTUBE_PATTERN = re.compile(r'(vi/|v=|/v/|youtu.be/|/embed/)')


class YoutubeDownloader(common.Downloader):
    """Youtube downloader."""

    def match(self, url: str) -> bool:
        return 'youtu' in url or 'ytimg' in url

    async def load(
            self, interaction: discord.Interaction, url: str, path: str) -> Optional[common.Entry]:
        parts = re.split(YOUTUBE_PATTERN, url)
        if len(parts) < 3:
            await utils.respond(interaction, 'Unrecognized url!', ephemeral=True)
            return
        vid = re.split(r'[^0-9a-zA-Z_-]', parts[2])[0]
        if len(vid) != 11:
            await utils.respond(interaction, 'Unrecognized url!', ephemeral=True)
            return

        await utils.edit(interaction, content=f'Loading youtube id `{vid}`...')
        yt = pytube.YouTube(  # pylint: disable=invalid-name
            f'http://youtube.com/watch?v={vid}')
        if yt.length > common.VIDEO_LIMIT_MINS * 60:
            await utils.respond(
                interaction,
                f'Please only queue videos shorter than {common.VIDEO_LIMIT_MINS} minutes.',
                ephemeral=True)
            return
        await utils.edit(interaction, content=f'Loading youtube video `{yt.title}`...')

        def load_streams(entry: common.Entry) -> Optional[Tuple[str, str, str]]:
            audio_stream = yt.streams.filter(subtype='mp4').get_audio_only()
            video_streams = yt.streams.filter(subtype='mp4', only_video=True)
            video_stream = video_streams.filter(resolution='720p').first()
            if video_stream is None:
                video_stream = video_streams.order_by('resolution').last()
            if audio_stream is None or video_stream is None:
                entry.error_msg = 'Error: missing either audio or video stream.'
                return None
            total_size = audio_stream.filesize + video_stream.filesize
            total_size_mb = total_size / 1024 / 1024

            def progress_func(stream: pytube.Stream, _, remaining_bytes: int):
                downloaded = stream.filesize - remaining_bytes
                if stream.includes_video_track:
                    downloaded += audio_stream.filesize
                progress = StringProgressBar.progressBar.filledBar(
                    total_size, downloaded)  # type: ignore
                entry.load_msg = (
                    f'Loading youtube video `{yt.title}`...\n'
                    f'Downloading: {progress[0]} {progress[1]:0.0f}% of {total_size_mb:0.1f}Mb')

            yt.register_on_progress_callback(progress_func)
            video_path = 'video.mp4'
            video_stream.download(output_path=entry.path, filename=video_path)

            audio_path = 'audio.wav'
            audio_stream.download(output_path=entry.path, filename='audio.mp4')
            utils.call('ffmpeg',
                       f'-i {os.path.join(entry.path, "audio.mp4")} -ac 2 '
                       f'-f wav {os.path.join(entry.path, audio_path)}')

            req = requests.get(yt.thumbnail_url, stream=True, timeout=5)
            if req.status_code == 200:
                thumb_ext = os.path.splitext(yt.thumbnail_url)[1]
                thumb_path = f'thumb.{thumb_ext}'
                with open(os.path.join(entry.path, thumb_path), 'wb') as thumb_file:
                    shutil.copyfileobj(req.raw, thumb_file)
            else:
                thumb_path = 'thumb.jpg'
                utils.call('ffmpeg',
                           f'-i {os.path.join(entry.path, video_path)} '
                           f'-vf "select=eq(n,0)" -q:v 3 {os.path.join(entry.path, thumb_path)}')
            return video_path, audio_path, thumb_path

        return common.Entry(
            title=yt.title,
            original_url=yt.watch_url,
            path=path,
            load_fn=load_streams)