"""Youtube utils."""
import asyncio
import functools
import os
import re
import discord
import pytube
import pytube.exceptions
import StringProgressBar

from karaqueue import common
from karaqueue import utils


YOUTUBE_PATTERN = re.compile(r'(vi/|v=|/v/|youtu.be/|/embed/)')


class YoutubeDownloader(common.Downloader):
    """Youtube downloader."""

    def match(self, url: str) -> bool:
        return 'youtu' in url or 'ytimg' in url

    async def load(
        self, interaction: discord.Interaction, url: str, *, video: bool, audio: bool,
    ) -> common.DownloadResult:
        parts = re.split(YOUTUBE_PATTERN, url)
        if len(parts) < 3:
            raise ValueError('Unrecognized url!')
        vid = re.split(r'[^0-9a-zA-Z_-]', parts[2])[0]
        if len(vid) != 11:
            raise ValueError('Unrecognized url!')

        await utils.edit(interaction, content=f'Loading youtube id `{vid}`...')
        yt = pytube.YouTube(  # pylint: disable=invalid-name
            f'http://youtube.com/watch?v={vid}')
        if yt.age_restricted:
            yt.bypass_age_gate()
        yt.check_availability()
        if yt.length > common.VIDEO_LIMIT_MINS * 60:
            raise ValueError(
                f'Please only queue videos shorter than {common.VIDEO_LIMIT_MINS} minutes.')
        await utils.edit(interaction, content=f'Loading youtube video `{yt.title}`...')

        def load_streams(entry: common.Entry, cancel: asyncio.Event) -> common.LoadResult:
            total_size = 0
            audio_stream = None
            if audio:
                audio_stream = yt.streams.filter(subtype='mp4').get_audio_only()
                if audio_stream is None:
                    raise ValueError('missing audio stream.')
                total_size += audio_stream.filesize
            video_stream = None
            if video:
                video_streams = yt.streams.filter(subtype='mp4', only_video=True)
                video_stream = video_streams.filter(resolution='720p').first()
                if video_stream is None:
                    video_stream = video_streams.order_by('resolution').last()
                if video_stream is None:
                    raise ValueError('missing video stream.')
                total_size += video_stream.filesize
            total_size_mb = total_size / 1024 / 1024

            def progress_func(stream: pytube.Stream, _, remaining_bytes: int):
                if cancel.is_set():
                    raise asyncio.CancelledError()
                downloaded = stream.filesize - remaining_bytes
                if audio_stream is not None and stream.includes_video_track:
                    downloaded += audio_stream.filesize
                progress = StringProgressBar.progressBar.filledBar(
                    total_size, downloaded)  # type: ignore
                entry.load_msg = (
                    f'Loading youtube video `{yt.title}`...\n'
                    f'Downloading: {progress[0]} {progress[1]:0.0f}% of {total_size_mb:0.1f}Mb')

            yt.register_on_progress_callback(progress_func)

            result = common.LoadResult()
            if video_stream is not None:
                result.video_path = 'video.mp4'
                video_stream.download(output_path=entry.path, filename=result.video_path)

            if audio_stream is not None:
                result.audio_path = 'audio.mp3'
                audio_stream.download(output_path=entry.path, filename='audio.mp4')
                utils.call('ffmpeg',
                        f'-i "{os.path.join(entry.path, "audio.mp4")}" -ac 2 '
                        f'-f mp3 "{os.path.join(entry.path, result.audio_path)}"')
            return result

        return common.DownloadResult(
            title=yt.title,
            original_url=yt.watch_url,
            load_fn=functools.partial(asyncio.to_thread, load_streams))
