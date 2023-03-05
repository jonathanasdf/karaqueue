"""MPC-HC player."""
from karaqueue import common

# config.ini keys
_SECTION = 'MPC-HC'
_WEBPORT = 'webport'

WEBPORT = common.CONFIG.get(_SECTION, _WEBPORT, fallback=None)


class MpcHcPlayer(common.Player):
    """MPC-HC Player."""
    def get_status(self) -> common.PlayerStatus:
        return common.PlayerStatus()
