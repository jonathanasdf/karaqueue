"""NicoNico utils."""
import os
import shutil
from typing import Optional, Tuple
import discord
from niconico.niconico import NicoNico
import requests

from . import common
from . import utils


class NicoNicoDownloader(common.Downloader):
    """NicoNico Downloader."""

    def __init__(self):
        self.client = NicoNico()

    def match(self, url: str) -> bool:
        return 'nicovideo.jp/watch/sm' in url

    async def load(
            self, interaction: discord.Interaction, url: str, path: str) -> Optional[common.Entry]:
        with self.client.video.get_video(url) as video:
            if video.video.duration > common.VIDEO_LIMIT_MINS * 60:
                await utils.respond(
                    interaction,
                    f'Please only queue videos shorter than {common.VIDEO_LIMIT_MINS} minutes.',
                    ephemeral=True)
                return
            await utils.edit(
                interaction, content=f'Loading niconico video `{video.video.title}`...')

            def load_streams(entry: common.Entry) -> Optional[Tuple[str, str, str]]:
                video_path = 'video.mp4'
                video.download(os.path.join(entry.path, video_path))

                audio_path = 'audio.wav'
                utils.call('ffmpeg', f'-i {os.path.join(entry.path, video_path)} '
                           f'-ac 2 -f wav {os.path.join(entry.path, audio_path)}')

                req = requests.get(video.video.thumbnail.url,
                                 stream=True, timeout=5)
                if req.status_code == 200:
                    thumb_ext = os.path.splitext(video.video.thumbnail.url)[1]
                    thumb_path = f'thumb.{thumb_ext}'
                    with open(os.path.join(entry.path, thumb_path), 'wb') as thumb_file:
                        shutil.copyfileobj(req.raw, thumb_file)
                else:
                    thumb_path = 'thumb.jpg'
                    utils.call('ffmpeg',
                               f'-i {os.path.join(entry.path, video_path)} '
                               f'-vf "select=eq(n,0)" -q:v 3 '
                               f'{os.path.join(entry.path, thumb_path)}')
                return video_path, audio_path, thumb_path

            return common.Entry(
                title=video.video.title,
                original_url=video.url,
                path=path,
                load_fn=load_streams)
