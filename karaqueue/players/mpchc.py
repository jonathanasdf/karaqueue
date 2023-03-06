"""MPC-HC player."""
import asyncio
from typing import Optional
from bs4 import BeautifulSoup
import requests
from karaqueue import common

# config.ini keys
_SECTION = 'MPC-HC'
_WEBPORT = 'webport'

WEBPORT = common.CONFIG.get(_SECTION, _WEBPORT, fallback=None)
STATS_PAGE = f'http://localhost:{WEBPORT}/variables.html'


class MpcHcPlayer(common.Player):
    """MPC-HC Player."""
    async def get_status(self) -> Optional[common.PlayerStatus]:
        try:
            page = await asyncio.to_thread(requests.get, STATS_PAGE, timeout=1)
        except (requests.exceptions.ConnectTimeout, TimeoutError):
            return None
        soup = BeautifulSoup(page.text, 'html.parser')
        position = soup.find('p', attrs={'id': 'position'})
        if position is None:
            raise ValueError('Could not find position in stats page.')
        duration = soup.find('p', attrs={'id': 'duration'})
        if duration is None:
            raise ValueError('Could not find duration in stats page.')
        return common.PlayerStatus(
            position=int(position.text),
            duration=int(duration.text),
        )
