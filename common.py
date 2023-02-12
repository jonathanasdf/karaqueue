import asyncio
import configparser
import dataclasses
import os
import random
import shutil
import string
import tempfile
from typing import Callable, Dict, List, Optional, Tuple
import discord
from PIL import Image

import utils


cfg = configparser.ConfigParser()
cfg.read(os.path.join(os.path.dirname(__file__), 'config.ini'))


HOST = cfg['DEFAULT'].get('host')
SERVING_DIR = '_generated_videos'
VIDEO_LENGTH_LIMIT_MINS = 10
MAX_QUEUED = 20
MAX_QUEUED_PER_USER = 2


@dataclasses.dataclass
class Entry:
    title: str
    original_url: str
    path: str
    pitch_shift: int
    load_fn: Callable[['Entry'], None]
    uid: int

    loaded: bool = False
    load_msg: str = ''
    error_msg: str = ''
    processed: bool = False
    process_task: Optional[asyncio.Task] = None

    def set_pitch_shift_locked(self, pitch_shift: int) -> None:
        if self.pitch_shift == pitch_shift:
            return
        self.pitch_shift = pitch_shift

    def onchange_locked(self) -> None:
        if self.process_task is not None:
            self.process_task.cancel()
            self.process_task = None
        self.processed = False
        self.load_msg = ''
        self.error_msg = ''

    def get_process_task(self) -> asyncio.Task:
        async def process():
            await asyncio.to_thread(self.process)
            self.process_task = None
            self.processed = True
        return asyncio.create_task(process())

    def delete(self) -> None:
        if self.process_task is not None:
            self.process_task.cancel()
            self.process_task = None
        self.processed = False
        shutil.rmtree(self.path)

    def _get_server_path(self, path: str) -> str:
        relpath = os.path.relpath(path, os.path.join(os.getcwd(), SERVING_DIR))
        return f'https://{HOST}/{relpath}'

    def url(self) -> str:
        if not self.processed:
            raise RuntimeError('task has not been processed!')
        if not self.pitch_shift:
            return self.original_url
        return self._get_server_path(self.path) + '?' + ''.join(random.choice(string.ascii_letters) for _ in range(8))

    def process(self) -> None:
        if not self.pitch_shift:
            return

        if not self.loaded:
            self.load_fn(self)
            self.loaded = True

        audio_path = os.path.join(self.path, 'audio.wav')
        if self.pitch_shift:
            self.load_msg = f'Loading youtube video `{self.title}`...\nShifting pitch...'
            shift_path = os.path.join(self.path, 'shifted.wav')
            pitch_cents = int(self.pitch_shift * 100)
            utils.call('sox', f'{audio_path} {shift_path} pitch {pitch_cents}')
            audio_path = shift_path

        self.load_msg = f'Loading youtube video `{self.title}`...\nCreating video...'
        video_path = os.path.join(self.path, 'video.mp4')
        output = tempfile.mktemp(dir=self.path, suffix='.mp4')
        utils.call('ffmpeg', f'-i {audio_path} -i {video_path} '
                   f'-c:v copy -c:a aac -b:a 160k -movflags faststart {output}')

        thumb_path = os.path.join(self.path, 'thumb.jpg')
        thumb = Image.open(thumb_path)

        with open(os.path.join(self.path, 'index.html'), 'w') as f:
            f.write(f"""<!DOCTYPE html>
<html>
    <head>
        <meta property="og:title" content="{self.title}" />
        <meta property="og:type" content="video" />
        <meta property="og:image" content="{self._get_server_path(thumb_path)}" />
        <meta property="og:video" content="{self._get_server_path(output)}" />
        <meta property="og:video:width" content="{thumb.width}" />
        <meta property="og:video:height" content="{thumb.height}" />
        <meta property="og:video:type" content="video/mp4" />
    </head>
</html>
""")


@dataclasses.dataclass
class Queue:
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
        self.queue.insert(index, item)

    def append(self, item):
        self.queue.append(item)

    def pop(self, index):
        return self.queue.pop(index)


karaqueue: Dict[Tuple[int, int], Queue] = {}


def get_queue(guild_id: int, channel_id: int) -> Queue:
    key = (guild_id, channel_id)
    if key not in karaqueue:
        karaqueue[key] = Queue(guild_id=guild_id, channel_id=channel_id)
    return karaqueue[key]
