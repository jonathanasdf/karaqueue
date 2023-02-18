"""From https://github.com/AlexAplin/nndownload/blob/144de2de7a58f3baddd7c05c09a74045292cd6f2/nndownload/nndownload.py"""  # pylint: disable=line-too-long
import json
import math
import threading
import time
from typing import Any, Callable, Tuple
import xml.dom.minidom
from absl import flags
from bs4 import BeautifulSoup
import requests
import requests.adapters
import requests.utils
from urllib3.util import Retry

try:
    from tkinter import simpledialog
    _HAS_TKINTER = True
except ImportError:
    _HAS_TKINTER = False


FLAGS = flags.FLAGS


__license__ = 'MIT'


RETRY_ATTEMPTS = 5
# retry_timeout_s = BACKOFF_FACTOR * (2 ** ({RETRY_ATTEMPTS} - 1))
BACKOFF_FACTOR = 2
BLOCK_SIZE = 128 * 1024
DMC_HEARTBEAT_INTERVAL_S = 15


# pylint: disable=line-too-long
MY_URL = 'https://www.nicovideo.jp/my'
LOGIN_URL = 'https://account.nicovideo.jp/login/redirector?show_button_twitter=1&site=niconico&show_button_facebook=1&sec=header_pc&next_url=/'
# pylint: enable=line-too-long


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
                otp_code = (simpledialog.askstring(
                    title='NicoNico OTP', prompt='NicoNico OTP:') or '')
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
    data: Any = document.find(id='js-initial-watch-data')
    if not data:
        raise RuntimeError('Could not find js-initial-watch-data')

    params = json.loads(data['data-api-data'])
    if not params['media']['delivery']:
        if params['video']['isPrivate']:
            raise RuntimeError('Video is private')
        if params['video']['isDeleted']:
            raise RuntimeError('Video is deleted')
        if (params['payment']['video']['isPremium']
                or params['payment']['video']['isAdmission']
                or params['payment']['video']['isPpv']):
            raise RuntimeError(
                'Video requires payment or membership to download')
        raise RuntimeError('Video media not available for download')
    return params


def download_video(
    session: requests.Session, url: str, filename: str, on_progress: Callable[[int, int], None],
) -> None:
    """Request the video page and initiate download of the video URL."""
    download_url, stop_heartbeat = _get_video_download_url(session, url)

    dl_stream = session.head(download_url)
    dl_stream.raise_for_status()
    video_len = int(dl_stream.headers['content-length'])

    # Pad out file to full length
    with open(filename, 'wb') as file:
        file.truncate(video_len)

    threads = 20
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


def _get_video_download_url(session: requests.Session, url: str) -> Tuple[str, threading.Event]:
    """Collect parameters from video document and build API request for video URL."""
    params = get_video_params(session, url)
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
    session_id = api_request.getElementsByTagName('id')[0].firstChild.nodeValue
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
