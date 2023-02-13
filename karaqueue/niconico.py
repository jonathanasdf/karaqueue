"""NicoNico utils."""
import asyncio
import base64
import os
from typing import Optional
import discord
import StringProgressBar

from karaqueue import common
from karaqueue import nicoutils
from karaqueue import utils


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
        self, interaction: discord.Interaction, url: str, path: str,
    ) -> Optional[common.Entry]:
        sess, session_cookie = nicoutils.login(
            USERNAME, PASSWORD, SESSION_COOKIE)
        update_session_cookie(session_cookie)
        params = nicoutils.get_video_params(sess, url)
        title = params['video']['title']
        if params['video']['duration'] > common.VIDEO_LIMIT_MINS * 60:
            await utils.respond(
                interaction,
                f'Please only queue videos shorter than {common.VIDEO_LIMIT_MINS} minutes.',
                ephemeral=True)
            return
        await utils.edit(
            interaction, content=f'Loading niconico video `{title}`...')

        def load_streams(entry: common.Entry, cancel: asyncio.Event) -> Optional[common.LoadResult]:
            sess, session_cookie = nicoutils.login(
                USERNAME, PASSWORD, SESSION_COOKIE)
            update_session_cookie(session_cookie)

            def progress_func(current: int, total_size: int):
                if cancel.is_set():
                    raise asyncio.CancelledError()
                total_size_mb = total_size / 1024 / 1024
                progress = StringProgressBar.progressBar.filledBar(
                    total_size, current)  # type: ignore
                entry.load_msg = (
                    f'Loading niconico video `{title}`...\n'
                    f'Downloading: {progress[0]} {progress[1]:0.0f}% of {total_size_mb:0.1f}Mb')

            video_path = 'video.mp4'
            nicoutils.download_video(sess, url, os.path.join(
                entry.path, video_path), progress_func)

            audio_path = 'audio.mp3'
            utils.call('ffmpeg', f'-i {os.path.join(entry.path, video_path)} '
                       f'-ac 2 -f mp3 {os.path.join(entry.path, audio_path)}')

            return common.LoadResult(
                video_path=video_path,
                audio_path=audio_path)

        return common.Entry(
            title=title,
            original_url=url,
            path=path,
            always_process=True,
            load_fn=load_streams)
