"""Downloaders"""
from karaqueue.downloaders import bilibili
from karaqueue.downloaders import niconico
from karaqueue.downloaders import soundcloud
from karaqueue.downloaders import youtube

all = [
    bilibili.BilibiliDownloader(),
    niconico.NicoNicoDownloader(),
    soundcloud.SoundcloudDownloader(),
    youtube.YoutubeDownloader(),
]
