"""Youtube utils."""
import asyncio
import functools
import logging
import os
import re
import shutil
import tempfile
from typing import Any, List
import discord
from yt_dlp import YoutubeDL
import StringProgressBar

from karaqueue import common
from karaqueue import utils

logger = logging.getLogger(__name__)


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
            raise ValueError(f'Unrecognized url! {url}')
        vid = re.split(r'[^0-9a-zA-Z_-]', parts[2])[0]

        await utils.edit(interaction, content=f'Loading youtube id `{vid}`...')
        url = f'http://youtube.com/watch?v={vid}'

        with YoutubeDL() as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                raise ValueError('Could not get video info!')

            if info['duration'] > common.VIDEO_LIMIT_MINS * 60:
                raise ValueError(
                    f'Please only queue videos shorter than {common.VIDEO_LIMIT_MINS} minutes.')
            title = info['title']
            await utils.edit(interaction, content=f'Loading youtube video `{title}`...')

        def load_streams(entry: common.Entry, cancel: List[asyncio.Event]) -> common.LoadResult:

            def progress_func(args):
                for event in cancel:
                    if event.is_set():
                        raise asyncio.CancelledError()
                if args['status'] == 'error':
                    raise ValueError()
                total_bytes = args.get('total_bytes', None)
                if total_bytes is None and args.get('total_bytes_estimate', None) is not None:
                    total_bytes = args['total_bytes_estimate']
                downloaded = int(args.get('downloaded_bytes', 0))
                if total_bytes is None:
                    entry.load_msg = (
                        f'Loading youtube video `{title}`...\n'
                        f'Downloading... {downloaded} bytes downloaded')
                else:
                    progress = StringProgressBar.progressBar.filledBar(
                        int(total_bytes), downloaded)  # type: ignore
                    total_bytes_mb = max(downloaded, total_bytes) / 1024 / 1024
                    entry.load_msg = (
                        f'Loading youtube video `{title}`...\n'
                        f'Downloading: {progress[0]} {progress[1]:0.0f}% of {total_bytes_mb:0.1f}Mb')

            download_path = tempfile.mktemp(dir=entry.path, suffix='.mp4')
            ydl_opts: dict[str, Any] = {
                'format': 'bv+ba/b',
                'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
                'paths': {'home': entry.path},
                'outtmpl': {'default': download_path},
                'progress_hooks': [progress_func],
            }
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            result = common.LoadResult()
            if audio:
                result.audio_path = 'audio.mp3'
                utils.call('ffmpeg', f'-i "{download_path}" '
                        f'-ac 2 -f mp3 "{os.path.join(entry.path, result.audio_path)}"')
            if video:
                result.video_path = 'video.mp4'
                shutil.move(download_path, os.path.join(entry.path, 'video.mp4'))
            return result

        return common.DownloadResult(
            title=title,
            original_url=info['webpage_url'],
            load_fn=functools.partial(asyncio.to_thread, load_streams))
