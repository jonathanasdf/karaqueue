"""NicoNico utils."""
import asyncio
import base64
import configparser
import logging
import os
import re
import shutil
from typing import Optional, Tuple
import discord
from niconico.niconico import NicoNico
import requests
import StringProgressBar

from karaqueue import common
from karaqueue import utils


cfg = configparser.ConfigParser()
cfg.read(os.path.join(os.path.dirname(__file__), '..', 'config.ini'))


USERNAME = cfg['NICONICO']['username']
PASSWORD = base64.b64decode(cfg['NICONICO']['password']).decode('utf-8')


class NicoNicoDownloader(common.Downloader):
    """NicoNico Downloader."""

    def __init__(self):
        super().__init__()

        # sess = requests.session()
        # data = {
        #     'mail_tel': USERNAME,
        #     'password': PASSWORD,
        # }
        # resp = sess.post(
        #     'https://account.nicovideo.jp/api/v1/login', data=data, timeout=5)
        # if resp.status_code != 200:
        #     raise RuntimeError(f'Could not log in to nicovideo: {resp}')

    def match(self, url: str) -> bool:
        return 'nicovideo.jp/watch/sm' in url

    async def load(
        self, interaction: discord.Interaction, url: str, path: str,
    ) -> Optional[common.Entry]:
        video = NicoNico().video.get_video(url)
        if video.video.duration > common.VIDEO_LIMIT_MINS * 60:
            await utils.respond(
                interaction,
                f'Please only queue videos shorter than {common.VIDEO_LIMIT_MINS} minutes.',
                ephemeral=True)
            return
        await utils.edit(
            interaction, content=f'Loading niconico video `{video.video.title}`...')

        def load_streams(entry: common.Entry, cancel: asyncio.Event) -> Optional[Tuple[str, str, str]]:

            def progress_func(log: str):
                if cancel.is_set():
                    raise asyncio.CancelledError()
                match = re.search(r'.*\((\d+)/(\d+)\)', log)
                if match is None:
                    entry.load_msg = f'Loading niconico video `{video.video.title}`...\n{log}'
                else:
                    total_size = int(match.groups()[1])
                    total_size_mb = total_size / 1024 / 1024
                    progress = StringProgressBar.progressBar.filledBar(
                        total_size, int(match.groups()[0]))  # type: ignore
                    entry.load_msg = (
                        f'Loading niconico video `{video.video.title}`...\n'
                        f'Downloading: {progress[0]} {progress[1]:0.0f}% of {total_size_mb:0.1f}Mb')

            video_path = 'video.mp4'
            with video:
                video._download_log = progress_func  # pylint: disable=protected-access
                video.download(os.path.join(entry.path, video_path),
                               load_chunk_size=1024*1024)

            audio_path = 'audio.mp3'
            utils.call('ffmpeg', f'-i {os.path.join(entry.path, video_path)} '
                       f'-ac 2 -f mp3 {os.path.join(entry.path, audio_path)}')

            thumb_path = 'thumb.jpg'
            req = requests.get(video.video.thumbnail.largeUrl,
                               stream=True, timeout=5)
            if req.status_code == 200:
                with open(os.path.join(entry.path, thumb_path), 'wb') as thumb_file:
                    shutil.copyfileobj(req.raw, thumb_file)
            else:
                utils.call('ffmpeg',
                           f'-i {os.path.join(entry.path, video_path)} '
                           f'-vf "select=eq(n,0)" -q:v 3 '
                           f'{os.path.join(entry.path, thumb_path)}')
            return video_path, audio_path, thumb_path

        return common.Entry(
            title=video.video.title,
            original_url=video.url,
            path=path,
            always_process=True,
            load_fn=load_streams)
