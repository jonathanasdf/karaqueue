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
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
import discord

from karaqueue import utils


_CONFIG_FILE = os.path.join(os.path.dirname(__file__), '..', 'config.ini')
CONFIG = configparser.ConfigParser()
CONFIG.read(_CONFIG_FILE)

# config.ini keys
_DEFAULT = 'DEFAULT'

HOST = CONFIG[_DEFAULT].get('host')
SERVING_DIR = CONFIG[_DEFAULT]['serving_dir']
ALLOWED_GUILDIDS = list(map(int, CONFIG[_DEFAULT]['allowed_guildids'].strip().split(',')))
DEV_CONTACT = CONFIG[_DEFAULT]['dev_contact']

VIDEO_LIMIT_MINS = 10
MAX_QUEUED = 20
MAX_QUEUED_PER_USER = 2

# Number of seconds after a new video started playing before the next button can be used.
ADVANCE_BUFFER_SECS = 0


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


LoadFn = Callable[['Entry', List[asyncio.Event]], Awaitable[LoadResult]]


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
    _process_task_cancel: Optional[asyncio.Event] = None
    load_msg: str = ''
    error_msg: str = ''
    _load_result: Optional[LoadResult] = None
    _processed_path: str = ''

    player_monitor_task: Optional[asyncio.Task] = None

    @property
    def name(self) -> str:
        """Get the formatted name of this entry."""
        name = self.title
        if self.pitch_shift != 0:
            name = f'{name} [{self.pitch_shift:+d}]'
        return name

    def onchange_locked(self) -> None:
        """A change that requires reprocessing the video was made."""
        self._reset()
        self.load_msg = ''
        self.error_msg = ''

    def create_process_task(
        self, loop: asyncio.AbstractEventLoop, global_cancel: asyncio.Event,
    ) -> asyncio.Task:
        """Return a task that processes the video."""
        self._reset()
        cancel = asyncio.Event()

        async def process():
            logging.info(f'Start processing {self.original_url}')
            await self._process([global_cancel, cancel])
            self.processed = True
            logging.info(f'Finished processing {self.original_url}')
        self._process_task_cancel = cancel
        return loop.create_task(process())

    def _reset(self) -> None:
        if self._process_task_cancel is not None:
            self._process_task_cancel.set()
            self._process_task_cancel = None
        if self.player_monitor_task is not None:
            self.player_monitor_task.cancel()
            self.player_monitor_task = None
        self.processed = False

    def delete(self) -> None:
        """Delete everything associated with this entry."""
        self._reset()
        self.error_msg = 'Cancelled'

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

    async def _process(self, cancel: List[asyncio.Event]) -> None:
        """Process the video."""
        if self._load_result is None:
            self._load_result = LoadResult()
            for load_fn in self.load_fns:
                try:
                    res = await load_fn(self, cancel)
                except asyncio.CancelledError:  # pylint: disable=try-except-raise
                    raise
                except Exception as err:  # pylint: disable=broad-except
                    logging.exception(err)
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

        for event in cancel:
            if event.is_set():
                raise asyncio.CancelledError()

        if self._load_result.width == 0 or self._load_result.height == 0:
            dimensions = utils.call(
                'ffprobe',
                '-loglevel quiet -select_streams v:0 -show_entries stream=width,height -of csv=p=0 '
                f'"{os.path.join(self.path, self._load_result.video_path)}"',
                return_stdout=True)
            self._load_result.width, self._load_result.height = map(int, dimensions.split(','))

        await asyncio.to_thread(self._process_load_result, cancel)

    def _process_load_result(self, cancel: List[asyncio.Event]) -> None:
        if self.queue is None:
            raise ValueError('Queue reference not set!')
        if self._load_result is None:
            raise ValueError('load_result is None! This should not happen!')
        audio_path = os.path.join(self.path, self._load_result.audio_path)
        if self.pitch_shift:
            self.load_msg = f'Loading video `{self.title}`...\nShifting pitch...'
            shift_path = os.path.join(self.path, 'shifted.mp3')
            pitch_cents = int(self.pitch_shift * 100)
            utils.call('sox', f'"{audio_path}" "{shift_path}" pitch {pitch_cents}')
            audio_path = shift_path

        for event in cancel:
            if event.is_set():
                raise asyncio.CancelledError()

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
        utils.call('ffmpeg', f'{input_flags} -movflags faststart {self._processed_path}')

        for event in cancel:
            if event.is_set():
                raise asyncio.CancelledError()

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
    lock = asyncio.Lock()

    global_offset_ms: int = 0
    local: bool = False
    next_advance_time: Optional[datetime.datetime] = None

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

    def format(self) -> str:
        """Format the queue as a string."""
        resp = []
        for i, entry in enumerate(self):
            row = f'{i+1}. [`{entry.name}`](<{entry.original_url}>)'
            resp.append(row)
        return '\n'.join(resp)


karaqueue: Dict[Tuple[int, int], Queue] = {}


async def get_queue_key(ctx: utils.DiscordContext) -> Tuple[int, int]:
    """Get the queue key corresponding to the given guild and channel."""
    guild_id = ctx.guild_id
    channel_id = ctx.channel_id
    if guild_id is None or channel_id is None:
        await utils.respond(ctx, content='Error: guild_id or channel_id is None', ephemeral=True)
        raise ValueError(f'guild_id or channel_id is None: {guild_id} {channel_id}')
    return guild_id, channel_id


async def get_queue(ctx: utils.DiscordContext) -> Queue:
    """Get the queue corresponding to the given guild and channel."""
    key = await get_queue_key(ctx)
    if key[0] not in ALLOWED_GUILDIDS:
        msg = f'This server is not allowed to use this bot. Please contact {DEV_CONTACT}.'
        await utils.respond(ctx, content=f'Error: {msg}', ephemeral=True)
        raise ValueError(f'{msg}: {key[0]}')
    if key not in karaqueue:
        karaqueue[key] = Queue(guild_id=key[0], channel_id=key[1])
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


@dataclasses.dataclass
class PlayerStatus:
    """The current status of a media player."""
    position: int  # Current position in ms
    duration: int  # Total duration in ms


class Player:
    """A local media player."""

    async def get_status(self) -> Optional[PlayerStatus]:
        """Returns the current status of the player."""
        raise NotImplementedError()
