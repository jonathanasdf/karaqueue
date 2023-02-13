"""Common classes."""
import asyncio
import configparser
import dataclasses
import os
import pathlib
import random
import shutil
import string
import tempfile
from typing import Callable, Dict, List, Optional, Tuple
import discord

from karaqueue import utils


cfg = configparser.ConfigParser()
cfg.read(os.path.join(os.path.dirname(__file__), '..', 'config.ini'))


HOST = cfg['DEFAULT'].get('host')
SERVING_DIR = cfg['DEFAULT'].get('serving_dir')
VIDEO_LIMIT_MINS = 10
MAX_QUEUED = 20
MAX_QUEUED_PER_USER = 2


@dataclasses.dataclass
class Entry:
    """An entry in the queue."""
    title: str
    original_url: str
    path: str
    always_process: bool
    load_fn: Callable[['Entry', asyncio.Event], Optional[Tuple[str, str, str]]]

    uid: int = 0
    pitch_shift: int = 0
    loaded: bool = False
    load_msg: str = ''
    error_msg: str = ''
    video_path: str = ''
    audio_path: str = ''
    thumb_path: str = ''
    processed: bool = False
    process_task: Optional[asyncio.Task] = None

    @property
    def name(self) -> str:
        name = self.title
        if self.pitch_shift != 0:
            name = f'{name} [{self.pitch_shift:+d}]'
        return name

    def set_pitch_shift(self, pitch_shift: int) -> None:
        """Set the pitch shift of the entry."""
        if self.pitch_shift == pitch_shift:
            return
        self.pitch_shift = pitch_shift

    def onchange_locked(self) -> None:
        """A change that requires reprocessing the video was made."""
        if self.process_task is not None:
            self.process_task.cancel()
            self.process_task = None
        self.processed = False
        self.load_msg = ''
        self.error_msg = ''

    def get_process_task(self, cancel: asyncio.Event) -> asyncio.Task:
        """Return a task that processes the video."""
        async def process(cancel: asyncio.Event):
            await asyncio.to_thread(self.process, cancel)
            self.process_task = None
            self.processed = True
        return asyncio.create_task(process(cancel))

    def delete(self) -> None:
        """Delete everything associated with this entry."""
        if self.process_task is not None:
            self.process_task.cancel()
            self.process_task = None
        self.processed = False
        shutil.rmtree(self.path)

    def _get_server_path(self, path: str) -> str:
        """Get external base path of this entry."""
        relpath = os.path.relpath(path, SERVING_DIR)
        relpath = pathlib.Path(relpath).as_posix()
        return f'https://{HOST}/{relpath}'

    def _need_processing(self) -> bool:
        if self.always_process:
            return True
        if self.pitch_shift:
            return True
        return False

    def url(self) -> str:
        """Get external video url for this entry."""
        if not self.processed:
            raise RuntimeError('task has not been processed!')
        if not self._need_processing():
            return self.original_url
        return (self._get_server_path(self.path) +
                '?' + ''.join(random.choice(string.ascii_letters) for _ in range(8)))

    def process(self, cancel: asyncio.Event) -> None:
        """Process the video."""
        if not self._need_processing():
            return

        if not self.loaded:
            res = self.load_fn(self, cancel)
            if res is None:
                return
            self.video_path, self.audio_path, self.thumb_path = res
            self.loaded = True

        audio_path = os.path.join(self.path, self.audio_path)
        if self.pitch_shift:
            self.load_msg = f'Loading youtube video `{self.title}`...\nShifting pitch...'
            shift_path = os.path.join(self.path, 'shifted.mp3')
            pitch_cents = int(self.pitch_shift * 100)
            utils.call('sox', f'{audio_path} {shift_path} pitch {pitch_cents}')
            audio_path = shift_path

        self.load_msg = f'Loading youtube video `{self.title}`...\nCreating video...'
        video_path = os.path.join(self.path, self.video_path)
        output = tempfile.mktemp(dir=self.path, suffix='.mp4')
        utils.call('ffmpeg', f'-i {audio_path} -i {video_path} '
                   f'-c:v copy -c:a copy -movflags faststart {output}')

        thumb_path = os.path.join(self.path, self.thumb_path)

        index_path = os.path.join(self.path, 'index.html')
        with open(index_path, 'w', encoding='utf-8') as index_file:
            index_file.write(f"""<!DOCTYPE html>
<html>
    <head>
        <meta property="og:title" content="{self.title}" />
        <meta property="og:type" content="video" />
        <meta property="og:image" content="{self._get_server_path(thumb_path)}" />
        <meta property="og:video" content="{self._get_server_path(output)}" />
        <meta property="og:video:type" content="video/mp4" />
    </head>
</html>
""")


@dataclasses.dataclass
class Queue:
    """A single instance of a queue for a channel."""
    guild_id: int
    channel_id: int
    msg_id: Optional[int] = None
    current: Optional[Entry] = None
    queue: List[Entry] = dataclasses.field(default_factory=list)
    lock = asyncio.Lock()

    def __len__(self):
        return len(self.queue)

    def __getitem__(self, index):
        return self.queue[index]

    def __setitem__(self, index, item):
        self.queue[index] = item

    def __delitem__(self, index):
        del self.queue[index]

    def __iter__(self):
        for elem in self.queue:
            yield elem

    def insert(self, index, item):
        """Insert."""
        self.queue.insert(index, item)

    def append(self, item):
        """Append."""
        self.queue.append(item)

    def pop(self, index):
        """Pop."""
        return self.queue.pop(index)


karaqueue: Dict[Tuple[int, int], Queue] = {}


def get_queue(guild_id: Optional[int], channel_id: Optional[int]) -> Queue:
    """Get the queue corresponding to the given guild and channel."""
    if guild_id is None or channel_id is None:
        raise ValueError('guild_id or channel_id is None')
    key = (int(guild_id), int(channel_id))
    if key not in karaqueue:
        karaqueue[key] = Queue(guild_id=guild_id, channel_id=channel_id)
    return karaqueue[key]


class Downloader:
    """A video downloader."""

    def match(self, url: str) -> bool:
        """Return true if the url can be loaded."""
        raise NotImplementedError()

    async def load(
        self, interaction: discord.Interaction, url: str, path: str,
    ) -> Optional[Entry]:
        """Create an entry object representing the video at the url under the base path."""
        raise NotImplementedError()
