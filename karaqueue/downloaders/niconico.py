"""NicoNico downloader."""
import asyncio
import base64
import functools
import os
import tempfile
from typing import List
import discord
import StringProgressBar

from karaqueue import common
from karaqueue import utils
from karaqueue.downloaders import nicoutils


USERNAME = common.CONFIG['NICONICO']['username']
PASSWORD = base64.b64decode(common.CONFIG['NICONICO']['password']).decode('utf-8')
SESSION_COOKIE = common.CONFIG['NICONICO'].get('session', '')


def update_session_cookie(session_cookie: str) -> None:
    """Update the session cookie."""
    global SESSION_COOKIE  # pylint: disable=global-statement
    if SESSION_COOKIE != session_cookie:
        SESSION_COOKIE = session_cookie
        common.CONFIG['NICONICO']['session'] = SESSION_COOKIE
        common.update_config_file()


class NicoNicoDownloader(common.Downloader):
    """NicoNico Downloader."""

    def match(self, url: str) -> bool:
        return 'nicovideo.jp/watch/sm' in url

    async def load(
        self, interaction: discord.Interaction, url: str, *, video: bool, audio: bool,
    ) -> common.DownloadResult:
        sess, session_cookie = nicoutils.login(USERNAME, PASSWORD, SESSION_COOKIE)
        update_session_cookie(session_cookie)
        params = nicoutils.get_video_params(sess, url)
        title = params['video']['title']
        duration = params['video']['duration']
        if duration == 0:
            raise ValueError('Error getting video info, please try again.')
        if duration > common.VIDEO_LIMIT_MINS * 60:
            raise ValueError(
                f'Please only queue videos shorter than {common.VIDEO_LIMIT_MINS} minutes.')
        await utils.edit(interaction, content=f'Loading niconico video `{title}`...')

        def load_streams(entry: common.Entry, cancel: List[asyncio.Event]) -> common.LoadResult:
            sess, session_cookie = nicoutils.login(USERNAME, PASSWORD, SESSION_COOKIE)
            update_session_cookie(session_cookie)

            def progress_func(current: int, total_size: int):
                for event in cancel:
                    if event.is_set():
                        raise asyncio.CancelledError()
                total_size_mb = total_size / 1024 / 1024
                progress = StringProgressBar.progressBar.filledBar(
                    total_size, current)  # type: ignore
                entry.load_msg = (
                    f'Loading niconico video `{title}`...\n'
                    f'Downloading: {progress[0]} {progress[1]:0.0f}% of {total_size_mb:0.1f}Mb')

            result = common.LoadResult()
            if video:
                result.video_path = 'video.mp4'
                nicoutils.download_video(sess, url, os.path.join(
                    entry.path, result.video_path), progress_func)

            if audio:
                if video:
                    video_path = os.path.join(entry.path, result.video_path)
                else:
                    video_path = tempfile.mktemp(dir=entry.path, suffix='.mp4')
                    nicoutils.download_video(sess, url, video_path, progress_func)
                result.audio_path = 'audio.mp3'
                utils.call('ffmpeg', f'-i "{video_path}" '
                           f'-ac 2 -f mp3 "{os.path.join(entry.path, result.audio_path)}"')
            return result

        return common.DownloadResult(
            title=title,
            original_url=url,
            load_fn=functools.partial(asyncio.to_thread, load_streams))
