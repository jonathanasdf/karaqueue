"""Common classes."""
import asyncio
import configparser
import dataclasses
import datetime
import logging
import os
import pathlib
import random
import string
import tempfile
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
import discord

from karaqueue import utils


_CONFIG_FILE = os.path.join(os.path.dirname(__file__), '..', 'config.ini')
CONFIG = configparser.ConfigParser()
CONFIG.read(_CONFIG_FILE)


HOST = CONFIG['DEFAULT'].get('host')
SERVING_DIR = CONFIG['DEFAULT']['serving_dir']
VIDEO_LIMIT_MINS = 10
MAX_QUEUED = 20
MAX_QUEUED_PER_USER = 2


def update_config_file() -> None:
    """Update config.ini."""
    with open(_CONFIG_FILE, 'w', encoding='utf-8') as file:
        CONFIG.write(file)


@dataclasses.dataclass
class LoadResult:
    """Results from loading a video."""
    video_path: str = ""
    audio_path: str = ""
    width: int = 0
    height: int = 0


LoadFn = Callable[['Entry', asyncio.Event], Awaitable[LoadResult]]


@dataclasses.dataclass
class Entry:
    """An entry in the queue."""
    path: str
    title: str
    original_url: str
    load_fns: List[LoadFn]

    queue: 'Queue'
    user_id: int
    pitch_shift: int
    offset_ms: int

    processed: bool = False
    process_task: Optional[asyncio.Task] = None
    load_msg: str = ''
    error_msg: str = ''
    _load_result: Optional[LoadResult] = None
    _processed_path: str = ''

    @property
    def name(self) -> str:
        """Get the formatted name of this entry."""
        name = self.title
        if self.pitch_shift != 0:
            name = f'{name} [{self.pitch_shift:+d}]'
        return name

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
            await self.process(cancel)
            self.process_task = None
            self.processed = True
            logging.info(f'Finished processing {self.original_url}')
        return asyncio.create_task(process(cancel))

    def delete(self) -> None:
        """Delete everything associated with this entry."""
        if self.process_task is not None:
            self.process_task.cancel()
            self.process_task = None
        self.processed = False

    def _get_server_path(self, path: str) -> str:
        """Get external base path of this entry."""
        relpath = os.path.relpath(path, SERVING_DIR)
        relpath = pathlib.Path(relpath).as_posix()
        return f'https://{HOST}/{relpath}'

    def url(self) -> str:
        """Get external video url for this entry."""
        if not self.processed:
            raise RuntimeError('task has not been processed!')
        return (self._get_server_path(self.path) +
                '?' + ''.join(random.choice(string.ascii_letters) for _ in range(8)))

    def video_path(self) -> str:
        """Get internal path to processed video."""
        if not self.processed:
            raise RuntimeError('task has not been processed!')
        return self._processed_path

    async def process(self, cancel: asyncio.Event) -> None:
        """Process the video."""
        if self._load_result is None:
            self._load_result = LoadResult()
            for load_fn in self.load_fns:
                try:
                    res = await load_fn(self, cancel)
                except Exception as err:  # pylint: disable=broad-except
                    self.error_msg = f'Error: {err}'
                    return
                if res.video_path:
                    self._load_result.video_path = res.video_path
                if res.audio_path:
                    self._load_result.audio_path = res.audio_path
                if res.width:
                    self._load_result.width = res.width
                if res.height:
                    self._load_result.height = res.height

        if self._load_result.width == 0 or self._load_result.height == 0:
            dimensions = utils.call(
                'ffprobe',
                '-loglevel quiet -select_streams v:0 -show_entries stream=width,height -of csv=p=0 '
                f'"{os.path.join(self.path, self._load_result.video_path)}"',
                return_stdout=True)
            self._load_result.width, self._load_result.height = map(
                int, dimensions.split(','))

        await asyncio.to_thread(self._process_load_result)

    def _process_load_result(self) -> None:
        if self.queue is None:
            raise ValueError('Queue reference not set!')
        if self._load_result is None:
            return
        audio_path = os.path.join(self.path, self._load_result.audio_path)
        if self.pitch_shift:
            self.load_msg = f'Loading video `{self.title}`...\nShifting pitch...'
            shift_path = os.path.join(self.path, 'shifted.mp3')
            pitch_cents = int(self.pitch_shift * 100)
            utils.call(
                'sox', f'"{audio_path}" "{shift_path}" pitch {pitch_cents}')
            audio_path = shift_path

        video_path = os.path.join(self.path, self._load_result.video_path)

        offset_ms = self.offset_ms + self.queue.global_offset_ms
        if offset_ms == 0:
            input_flags = (f'-i "{video_path}" -i "{audio_path}" -c:v copy -c:a copy '
                           f'-map 0:v:0 -map 1:a:0')
        elif offset_ms > 0:
            input_flags = (f'-i "{video_path}" -i "{audio_path}" -c:v copy -c:a mp3 '
                           f'-af "adelay={offset_ms}|{offset_ms}" -map 0:v:0 -map 1:a:0')
        else:
            delay_str = datetime.timedelta(milliseconds=-offset_ms)
            input_flags = (f'-i "{audio_path}" -itsoffset {delay_str} -i "{video_path}" '
                           f'-c:a copy -c:v copy -map 1:v:0 -map 0:a:0')

        self.load_msg = f'Loading video `{self.title}`...\nCreating video...'
        self._processed_path = tempfile.mktemp(dir=self.path, suffix='.mp4')
        utils.call(
            'ffmpeg', f'{input_flags} -movflags faststart {self._processed_path}')

        thumb_path = os.path.join(self.path, 'thumb.jpg')
        if not os.path.exists(thumb_path):
            utils.call('ffmpeg',
                       rf'-i "{video_path}" -vf "select=eq(n\,0)" -q:v 3 "{thumb_path}"')

        index_path = os.path.join(self.path, 'index.html')
        with open(index_path, 'w', encoding='utf-8') as index_file:
            index_file.write(f"""<!DOCTYPE html>
<html>
    <head>
        <meta property="og:title" content="{self.name}" />
        <meta property="og:type" content="video" />
        <meta property="og:image" content="{self._get_server_path(thumb_path)}" />
        <meta property="og:video" content="{self._get_server_path(self._processed_path)}" />
        <meta property="og:video:width" content="{self._load_result.width}" />
        <meta property="og:video:height" content="{self._load_result.height}" />
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
    flags: Dict[str, Any] = dataclasses.field(default_factory=dict)
    lock = asyncio.Lock()

    global_offset_ms: int = 0

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


@dataclasses.dataclass
class DownloadResult:
    """Result of a Downloader.load() call."""
    title: str
    original_url: str
    load_fn: LoadFn


class Downloader:
    """A video downloader."""

    def match(self, url: str) -> bool:
        """Return true if the url can be loaded."""
        raise NotImplementedError()

    async def load(
        self, interaction: discord.Interaction, url: str, *, video: bool, audio: bool,
    ) -> DownloadResult:
        """Return a loader function for the url."""
        raise NotImplementedError()
