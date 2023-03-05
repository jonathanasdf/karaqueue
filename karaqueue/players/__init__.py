"""Local media players."""
from karaqueue.players import mpchc

player_lookup = {
    'mpc-hc': mpchc.MpcHcPlayer(),
}
