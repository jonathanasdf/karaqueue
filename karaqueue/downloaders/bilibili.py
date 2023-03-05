"""bilibili downloader."""
import asyncio
import logging
import os
from typing import List
import bilix
import bilix.api.bilibili
import bilix.progress
import bilix.utils
import discord
import StringProgressBar

from karaqueue import common
from karaqueue import utils


SESSDATA = common.CONFIG['BILIBILI'].get('SESSDATA', '')


class Progress(bilix.progress.CLIProgress):
    """Progress bar."""

    def __init__(self, entry: common.Entry, cancel: List[asyncio.Event]) -> None:
        self._entry = entry
        self._cancel = cancel
        super().__init__()

    async def add_task(self, *args, **kwargs):
        return await super().add_task(*args, **kwargs)

    async def update(self, task_id, **kwargs):
        for event in self._cancel:
            if event.is_set():
                raise asyncio.CancelledError()
        task = self.tasks[task_id]
        await super().update(task_id, **kwargs)
        total_size_mb = task.total / 1024 / 1024
        progress = StringProgressBar.progressBar.filledBar(
            task.total, task.completed)  # type: ignore
        self._entry.load_msg = (
            f'Loading bilibili video `{self._entry.title}`...\n'
            f'Downloading: {progress[0]} {progress[1]:0.0f}% of {total_size_mb:0.1f}Mb')


class BilibiliDownloader(common.Downloader):
    """bilibili Downloader."""

    def match(self, url: str) -> bool:
        return 'bilibili.com/video/' in url

    async def load(
        self, interaction: discord.Interaction, url: str, *, video: bool, audio: bool,
    ) -> common.DownloadResult:
        informer = bilix.info.InformerBilibili(sess_data=SESSDATA)
        info = await bilix.api.bilibili.get_video_info(informer.client, url)
        await informer.aclose()
        if not info.dash:
            raise RuntimeError('Unknown error getting video.')
        if info.dash.duration == 0:
            raise ValueError('Error getting video info, please try again.')
        if info.dash.duration > common.VIDEO_LIMIT_MINS * 60:
            raise ValueError(
                f'Please only queue videos shorter than {common.VIDEO_LIMIT_MINS} minutes.')

        for quality in info.dash.video_formats:
            if len(info.dash.video_formats[quality]) == 0:
                if '720P' in quality:
                    logging.warning(f'Could not get video for {quality}, try updating SESSDATA.')

        title = bilix.utils.legal_title(info.h1_title, info.pages[info.p].p_name)
        await utils.edit(interaction, content=f'Loading bilibili video `{title}`...')

        async def load_streams(entry: common.Entry, cancel: List[asyncio.Event]):
            downloader = bilix.DownloaderBilibili(
                videos_dir=entry.path, progress=Progress(entry, cancel),
                sess_data=SESSDATA)
            await downloader.get_video(url)
            await downloader.aclose()
            video_path = f'{title}.mp4'

            result = common.LoadResult()
            if video:
                result.video_path = video_path
            if audio:
                result.audio_path = 'audio.mp3'
                utils.call('ffmpeg', f'-i "{os.path.join(entry.path, video_path)}" '
                           f'-ac 2 -f mp3 "{os.path.join(entry.path, result.audio_path)}"')
            return result

        return common.DownloadResult(
            title=title,
            original_url=url,
            load_fn=load_streams)
