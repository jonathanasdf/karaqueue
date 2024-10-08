"""From https://github.com/AlexAplin/nndownload/blob/ef8e9c1dffa3afcad5a7b4087a2c13563b8bd0c6/nndownload/nndownload.py"""  # pylint: disable=line-too-long
import json
import logging
import math
import os
import random
import re
import shutil
import string
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Tuple
import xml.dom.minidom

from absl import flags
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import requests
import requests.adapters
import requests.utils
from urllib3.util import Retry

from karaqueue import utils

try:
    from tkinter import simpledialog
    _HAS_TKINTER = True
except ImportError:
    _HAS_TKINTER = False


FLAGS = flags.FLAGS


__license__ = 'MIT'


RETRY_ATTEMPTS = 5
BACKOFF_FACTOR = 2
BLOCK_SIZE = 128 * 1024
DMC_HEARTBEAT_INTERVAL_S = 15


# pylint: disable=line-too-long
MY_URL = 'https://www.nicovideo.jp/my'
LOGIN_URL = 'https://account.nicovideo.jp/login/redirector?show_button_twitter=1&site=niconico&show_button_facebook=1&sec=header_pc&next_url=/'
VIDEO_DMS_WATCH_API = "https://nvapi.nicovideo.jp/v1/watch/{0}/access-rights/hls?actionTrackId={1}"
# pylint: enable=line-too-long

M3U8_STREAM_RE = re.compile(
    r"(?:(?:#EXT-X-STREAM-INF)|#EXT-X-I-FRAME-STREAM-INF):.*(?:BANDWIDTH=(\d+)).*\n(.*)")
M3U8_MEDIA_RE = re.compile(
    r"(?:#EXT-X-MEDIA:TYPE=)(?:(\w+))(?:.*),URI=\"(.*)\"")
M3U8_KEY_RE = re.compile(
    r"((?:#EXT-X-KEY)(?:.*),?URI=\")(?P<url>.*)\",IV=0x(?P<iv>.*)")
M3U8_MAP_RE = re.compile(r"((?:#EXT-X-MAP)(?:.*),?URI=\")(?P<url>.*)\"(.*)")
M3U8_SEGMENT_RE = re.compile(r"(?:#EXTINF):.*\n(.*)")
API_HEADERS = {
    "X-Frontend-Id": "6",
    "X-Frontend-Version": "0",
    "X-Niconico-Language": "ja-jp"  # Does not impact parameter extraction
}
REGION_LOCK_ERRORS = {
    "お住まいの地域・国からは視聴することができません。",
    "この動画は投稿( アップロード )された地域と同じ地域からのみ視聴できます。"
}


def login(username: str, password: str, session_cookie: str) -> Tuple[requests.Session, str]:
    """Login to Nico and create a session."""
    session = requests.session()

    retry = Retry(
        total=RETRY_ATTEMPTS,
        read=RETRY_ATTEMPTS,
        connect=RETRY_ATTEMPTS,
        backoff_factor=BACKOFF_FACTOR,
        status_forcelist=(500, 502, 503, 504),
    )
    adapter = requests.adapters.HTTPAdapter(pool_maxsize=50, max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)

    if session_cookie:
        session_dict = {
            'user_session': session_cookie
        }

        cookie_jar = session.cookies
        session.cookies = requests.utils.add_dict_to_cookiejar(
            cookie_jar, session_dict)

        my_request = session.get(MY_URL)
        my_request.raise_for_status()
        if my_request.history:
            session_cookie = ''

    if not session_cookie:
        login_post = {
            'mail_tel': username,
            'password': password
        }
        login_request = session.post(LOGIN_URL, data=login_post)
        login_request.raise_for_status()

        if 'message=cant_login' in login_request.url:
            raise ValueError(
                'Incorrect email/telephone or password. Please verify your login details')

        otp_requests_made = 0
        while otp_requests_made < 10 and not session.cookies.get_dict().get('user_session', ''):
            if FLAGS.gui and _HAS_TKINTER:
                otp_code = (
                    simpledialog.askstring(title='NicoNico OTP', prompt='NicoNico OTP:') or '')
            else:
                otp_code = input('NicoNico OTP: ')

            otp_post = {
                'otp': otp_code.strip(),
            }
            otp_post_request = session.post(login_request.url, data=otp_post)
            otp_requests_made += 1
            otp_post_request.raise_for_status()

    session_cookie = session.cookies.get_dict().get('user_session', '')
    if not session_cookie:
        raise ValueError('Failed to login to niconico.')
    return session, session_cookie


def get_video_params(session: requests.Session, url: str) -> Any:
    """Get params for the video."""
    video_request = session.get(url)
    video_request.raise_for_status()
    document = BeautifulSoup(video_request.text, 'html.parser')
    data: Any = document.find("meta", {"name": "server-response"})
    if not data:
        raise RuntimeError('Could not find server-response')

    params = json.loads(data["content"])["data"]["response"]
    if params['video']['isDeleted']:
        raise RuntimeError('Video is deleted')
    if params['media']['domand']:
        pass
    elif params['media']['delivery']:
        if (params['payment']['video']['isPremium']
                or params['payment']['video']['isAdmission']
                or params['payment']['video']['isPpv']):
            raise RuntimeError(
                'Video requires payment or membership to download')
    else:
        potential_region_error = document.select_one(
            "p.fail-message") or document.select_one("p.font12")
        if potential_region_error and potential_region_error.text in REGION_LOCK_ERRORS:
            raise RuntimeError("This video is not available in your region")
        else:
            logging.info(params)
            raise RuntimeError("Failed to collect video paramters")
    return params


def _download_hls(m3u8_url, filename, session, on_progress, threads):
    """Perform a native HLS download of a provided M3U8 manifest."""
    with session.get(m3u8_url) as m3u8_request:
        m3u8_request.raise_for_status()
        m3u8 = m3u8_request.text
    key_match = M3U8_KEY_RE.search(m3u8)
    init_match = M3U8_MAP_RE.search(m3u8)
    segments = M3U8_SEGMENT_RE.findall(m3u8)
    if not key_match:
        raise RuntimeError("Could not retrieve key file from manifest")
    if not init_match:
        raise RuntimeError("Could not retrieve init file from manifest")
    if not segments:
        raise RuntimeError("Could not retrieve segments from manifest")

    key_url = key_match["url"]
    with session.get(key_url) as key_request:
        key_request.raise_for_status()
        key = key_request.content
    iv = key_match["iv"]
    iv = bytes.fromhex(iv)
    init_url = init_match["url"]
    with open(filename, "wb") as f:
        f.write(session.get(init_url).content)

    def download_segment(segment):
        with session.get(segment) as r:
            r.raise_for_status()
            cipher = AES.new(key, AES.MODE_CBC, iv=iv)
            return unpad(cipher.decrypt(r.content), AES.block_size)

    with ThreadPoolExecutor(max_workers=threads) as executor:
        results = executor.map(download_segment, segments)
        for i, decrypted in enumerate(results):
            with open(filename, "ab") as f:
                f.write(decrypted)
            on_progress(i, len(segments), parts=True)


def download_video(
    session: requests.Session, url: str, filename: str, on_progress: Callable[[int, int], None],
) -> None:
    """Request the video page and initiate download of the video URL."""
    threads = 20

    params = get_video_params(session, url)
    if params['media']['domand']:
        # Perform request to Dwango Media Service (DMS)
        # Began rollout starting 2023-11-01 for select videos and users (https://blog.nicovideo.jp/niconews/205042.html)
        stop_heartbeat = threading.Event()
        video_url, audio_url = _get_video_download_url_dms(params, session)
        with tempfile.TemporaryDirectory() as temp_dir:
            tasks = []
            for stream in (video_url, audio_url):
                random_path_string = ''.join(random.choices(
                    string.ascii_letters + string.digits, k=16))
                stream_filename = os.path.join(
                    temp_dir, random_path_string) + ".ts"
                thread = threading.Thread(target=_download_hls, args=(
                    stream, stream_filename, session, on_progress, threads))
                thread.start()
                tasks.append({
                    "thread": thread,
                    "filename": stream_filename,
                })
            for task in tasks:
                task["thread"].join()
            if not tasks:
                raise RuntimeError("No HLS download tasks were received")

            # Video and audio
            if len(tasks) > 1:
                input_flags = (f'-i "{tasks[0]["filename"]}" -i "{tasks[1]["filename"]}" '
                               '-c:v copy -c:a copy -map 0:v:0 -map 1:a:0')
                utils.call(
                    'ffmpeg', f'{input_flags} -movflags faststart {filename}')
            # Only audio or video
            else:
                shutil.move(tasks[0]["filename"], filename)
        return

    # Perform request to Dwango Media Cluster (DMC)
    download_url, stop_heartbeat = _get_video_download_url_dmc(
        params, session)

    dl_stream = session.head(download_url)
    dl_stream.raise_for_status()
    video_len = int(dl_stream.headers['content-length'])

    # Pad out file to full length
    with open(filename, 'wb') as file:
        file.truncate(video_len)

    progress = [0] * threads

    def download_video_part(i: int, start: int, end: int):
        dl_stream = session.get(
            download_url, headers={'Range': f'bytes={start}-{end-1}'}, stream=True)
        dl_stream.raise_for_status()
        stream_iterator = dl_stream.iter_content(BLOCK_SIZE)

        # part_length = end - start
        with open(filename, 'r+b') as file:
            file.seek(start)
            for block in stream_iterator:
                file.write(block)
                progress[i] += len(block)

    part_size = math.ceil(video_len / threads)
    for i in range(threads):
        start = part_size * i
        end = min(video_len, start + part_size)

        part_thread = threading.Thread(
            target=download_video_part,
            kwargs={'i': i, 'start': start, 'end': end},
            daemon=True)
        part_thread.start()

    while True:
        total_progress = sum(progress)
        on_progress(total_progress, video_len)
        if total_progress >= video_len:
            break
        time.sleep(1)

    stop_heartbeat.set()


def _perform_heartbeat(
    session: requests.Session,
    heartbeat_url: str,
    api_request_el: xml.dom.minidom.Node,
    stop_heartbeat: threading.Event,
) -> None:
    """Perform a response heartbeat to keep the video download connection alive."""
    while True:
        if stop_heartbeat.is_set():
            break
        heartbeat_response = session.post(
            heartbeat_url, data=api_request_el.toxml())
        heartbeat_response.raise_for_status()
        time.sleep(DMC_HEARTBEAT_INTERVAL_S)


def _get_highest_quality(sources: list) -> str:
    """Get the highest quality available."""
    # Assumes qualities are in descending order
    for source in sources:
        if not source['isAvailable']:
            continue
        return source['id']
    raise RuntimeError('No source are available!')


def _get_media_from_manifest(manifest_text, media_type):
    """Return the first seen media match for a given type from a .m3u8 manifest."""

    media_type = media_type.capitalize()
    match = M3U8_MEDIA_RE.search(manifest_text)

    if not match:
        raise RuntimeError(
            "Could not retrieve media playlist from manifest")

    media_url = match[2]
    return media_url


def _get_stream_from_manifest(manifest_text):
    """Return the highest quality stream from a .m3u8 manifest."""

    best_bandwidth, best_stream = -1, None
    matches = M3U8_STREAM_RE.findall(manifest_text)

    if not matches:
        raise RuntimeError(
            "Could not retrieve stream playlist from manifest")

    else:
        for match in matches:
            stream_bandwidth = int(match[0])
            if stream_bandwidth > best_bandwidth:
                best_bandwidth = stream_bandwidth
                best_stream = match[1]

    return best_stream


def _get_video_download_url_dms(params, session: requests.Session) -> Tuple[str, str]:
    video_id = params['video']['id']
    access_right_key = params['media']['domand']['accessRightKey']
    watch_track_id = params['client']['watchTrackId']

    video_source = _get_highest_quality(
        params['media']['domand']['videos'])
    audio_source = _get_highest_quality(
        params['media']['domand']['audios'])
    payload = json.dumps({"outputs": [[video_source, audio_source]]})
    headers = {
        "X-Access-Right-Key": access_right_key,
        "X-Request-With": "nicovideo",  # Only provided on this endpoint
    }
    session.options(VIDEO_DMS_WATCH_API.format(
        video_id, watch_track_id))  # OPTIONS
    get_manifest_request = session.post(VIDEO_DMS_WATCH_API.format(
        video_id, watch_track_id), headers={**API_HEADERS, **headers}, data=payload)
    get_manifest_request.raise_for_status()
    manifest_url = get_manifest_request.json()["data"]["contentUrl"]
    manifest_request = session.get(manifest_url)
    manifest_request.raise_for_status()
    manifest_text = manifest_request.text
    video_url = _get_stream_from_manifest(manifest_text)
    audio_url = _get_media_from_manifest(manifest_text, "audio")
    return video_url, audio_url


def _get_video_download_url_dmc(params, session: requests.Session) -> Tuple[str, threading.Event]:
    # Perform request to Dwango Media Cluster (DMC)
    recipe_id = params['media']['delivery']['movie']['session']['recipeId']
    content_id = params['media']['delivery']['movie']['session']['contentId']
    protocol = params['media']['delivery']['movie']['session']['protocols'][0]
    file_extension = 'mp4'
    priority = params['media']['delivery']['movie']['session']['priority']
    heartbeat_lifetime = params['media']['delivery']['movie']['session']['heartbeatLifetime']
    token = params['media']['delivery']['movie']['session']['token']
    signature = params['media']['delivery']['movie']['session']['signature']
    auth_type = params['media']['delivery']['movie']['session']['authTypes']['http']
    service_user_id = params['media']['delivery']['movie']['session']['serviceUserId']
    player_id = params['media']['delivery']['movie']['session']['playerId']

    # Build initial heartbeat request
    post = f"""
            <session>
                <recipe_id>{recipe_id}</recipe_id>
                <content_id>{content_id}</content_id>
                <content_type>movie</content_type>
                <protocol>
                <name>{protocol}</name>
                <parameters>
                    <http_parameters>
                    <method>GET</method>
                    <parameters>
                        <http_output_download_parameters>
                        <file_extension>{file_extension}</file_extension>
                        </http_output_download_parameters>
                    </parameters>
                    </http_parameters>
                </parameters>
                </protocol>
                <priority>{priority}</priority>
                <content_src_id_sets>
                <content_src_id_set>
                    <content_src_ids>
                    <src_id_to_mux>
                        <video_src_ids>
                        </video_src_ids>
                        <audio_src_ids>
                        </audio_src_ids>
                    </src_id_to_mux>
                    </content_src_ids>
                </content_src_id_set>
                </content_src_id_sets>
                <keep_method>
                <heartbeat>
                    <lifetime>{heartbeat_lifetime}</lifetime>
                </heartbeat>
                </keep_method>
                <timing_constraint>unlimited</timing_constraint>
                <session_operation_auth>
                <session_operation_auth_by_signature>
                    <token>{token}</token>
                    <signature>{signature}</signature>
                </session_operation_auth_by_signature>
                </session_operation_auth>
                <content_auth>
                <auth_type>{auth_type}</auth_type>
                <service_id>nicovideo</service_id>
                <service_user_id>{service_user_id}</service_user_id>
                <max_content_count>10</max_content_count>
                <content_key_timeout>600000</content_key_timeout>
                </content_auth>
                <client_info>
                <player_id>{player_id}</player_id>
                </client_info>
            </session>
        """
    root = xml.dom.minidom.parseString(post)

    video_source = _get_highest_quality(
        params['media']['delivery']['movie']['videos'])
    sources = root.getElementsByTagName('video_src_ids')[0]
    element = root.createElement('string')
    quality = root.createTextNode(video_source)
    element.appendChild(quality)
    sources.appendChild(element)

    audio_source = _get_highest_quality(
        params['media']['delivery']['movie']['audios'])
    sources = root.getElementsByTagName('audio_src_ids')[0]
    element = root.createElement('string')
    quality = root.createTextNode(audio_source)
    element.appendChild(quality)
    sources.appendChild(element)

    api_url = (params['media']['delivery']['movie']['session']['urls'][0]['url']
               + '?suppress_response_codes=true&_format=xml')
    headers = {'Content-Type': 'application/xml'}
    api_response = session.post(
        api_url, headers=headers, data=root.toxml())
    api_response.raise_for_status()
    api_request = xml.dom.minidom.parseString(api_response.text)

    # Collect response for heartbeat
    session_id = api_request.getElementsByTagName(
        'id')[0].firstChild.nodeValue
    session_url = params['media']['delivery']['movie']['session']['urls'][0]['url']
    heartbeat_url = f'{session_url}/{session_id}?_format=xml&_method=PUT'
    api_request_el = api_request.getElementsByTagName('session')[0]

    stop_heartbeat = threading.Event()
    threading.Thread(
        target=_perform_heartbeat,
        kwargs={
            'session': session,
            'heartbeat_url': heartbeat_url,
            'api_request_el': api_request_el,
            'stop_heartbeat': stop_heartbeat,
        },
        daemon=True).start()

    return api_request.getElementsByTagName('content_uri')[0].firstChild.nodeValue, stop_heartbeat
