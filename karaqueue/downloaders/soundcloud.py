"""Soundcloud downloader."""
import asyncio
import os
from typing import List
import sclib.asyncio
import discord

from karaqueue import common
from karaqueue import utils


class SoundcloudDownloader(common.Downloader):
    """Soundcloud Downloader."""

    def match(self, url: str) -> bool:
        return 'soundcloud.com/' in url

    async def load(
        self, interaction: discord.Interaction, url: str, *, video: bool, audio: bool,
    ) -> common.DownloadResult:
        if video:
            raise ValueError('Soundtrack does not support videos.')
        track = await sclib.asyncio.SoundcloudAPI().resolve(url)
        if not isinstance(track, sclib.asyncio.Track):
            raise ValueError('Invalid url.')
        if track.duration == 0:
            raise ValueError('Error getting audio info, please try again.')
        if track.duration / 1000 > common.VIDEO_LIMIT_MINS * 60:  # pylint: disable=no-member
            raise ValueError(
                f'Please only queue videos shorter than {common.VIDEO_LIMIT_MINS} minutes.')
        await utils.edit(interaction, content=f'Loading soundcloud audio `{track.title}`...')

        async def load_streams(entry: common.Entry, cancel: List[asyncio.Event]):
            del cancel  # Unused.
            entry.load_msg = f'Loading soundcloud audio `{entry.title}`...'
            result = common.LoadResult()
            if audio:
                result.audio_path = 'audio.mp3'
                with open(os.path.join(entry.path, result.audio_path), 'wb+') as file:
                    await track.write_mp3_to(file)
            return result

        return common.DownloadResult(
            title=track.title,
            original_url=track.permalink_url,  # pylint: disable=no-member
            load_fn=load_streams)
