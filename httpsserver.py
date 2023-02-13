"""Simple HTTPS webserver with byte range support."""
import configparser
import datetime
import email
import http
import http.server
import os
import ssl
import re
from typing import Optional, Tuple
import urllib.parse


cfg = configparser.ConfigParser()
cfg.read(os.path.join(os.path.dirname(__file__), 'config.ini'))


HOSTNAME = cfg['DEFAULT']['internal_host']
PORT = 443
CERT_DIR = os.path.join(os.path.dirname(__file__), 'certs')


def copy_byte_range(infile, outfile, start=None, stop=None, bufsize=16*1024):
    '''Like shutil.copyfileobj, but only copy a range of the streams.
    Both start and stop are inclusive.
    '''
    if start is not None:
        infile.seek(start)
    while True:
        to_read = min(bufsize, stop + 1 - infile.tell() if stop else bufsize)
        buf = infile.read(to_read)
        if not buf:
            break
        outfile.write(buf)


BYTE_RANGE_RE = re.compile(r'bytes=(\d+)-(\d+)?$')


def parse_byte_range(byte_range) -> Tuple[int, Optional[int]]:
    '''Returns the two numbers in 'bytes=123-456' or throws ValueError.
    The last number or both numbers may be None.
    '''
    if byte_range.strip() == '':
        return 0, None

    match = BYTE_RANGE_RE.match(byte_range)
    if not match:
        raise ValueError(f'Invalid byte range {byte_range}')
    if len(match.groups()) == 1:
        first = int(match.groups()[0])
        last = None
    elif len(match.groups()) == 2:
        first = int(match.groups()[0])
        last = int(match.groups()[1])
    else:
        raise ValueError(f'Invalid byte range {byte_range}')

    if last is not None and last < first:
        raise ValueError(f'Invalid byte range {byte_range}')
    return first, last


class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    '''Adds support for HTTP 'Range' requests to SimpleHTTPRequestHandler
    The approach is to:
    - Override send_head to look for 'Range' and respond appropriately.
    - Override copyfile to only transmit a range when requested.
    '''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.range = None

    def send_head(self):
        self.range = None
        if 'Range' in self.headers:
            try:
                self.range = parse_byte_range(self.headers['Range'])
            except ValueError:
                self.send_error(400, 'Invalid byte range')
                return None

        # Mirroring SimpleHTTPServer.py here
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            parts = urllib.parse.urlsplit(self.path)
            if not parts.path.endswith('/'):
                # redirect browser - doing basically what apache does
                self.send_response(http.HTTPStatus.MOVED_PERMANENTLY)
                new_parts = (parts[0], parts[1], parts[2] + '/',
                             parts[3], parts[4])
                new_url = urllib.parse.urlunsplit(new_parts)
                self.send_header('Location', new_url)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return None
            for index in 'index.html', 'index.htm':
                index = os.path.join(path, index)
                if os.path.exists(index):
                    path = index
                    break
            else:
                return self.list_directory(path)
        ctype = self.guess_type(path)
        # check for trailing '/' which should return 404. See Issue17324
        # The test for this was added in test_httpserver.py
        # However, some OS platforms accept a trailingSlash as a filename
        # See discussion on python-dev and Issue34711 regarding
        # parseing and rejection of filenames with a trailing slash
        if path.endswith('/'):
            self.send_error(http.HTTPStatus.NOT_FOUND, 'File not found')
            return None
        try:
            file = open(path, 'rb')  # pylint: disable=consider-using-with
        except OSError:
            self.send_error(http.HTTPStatus.NOT_FOUND, 'File not found')
            return None

        try:
            file_stat = os.fstat(file.fileno())
            if self.range:
                first, last = self.range
                file_len = file_stat[6]
                if first >= file_len:
                    self.send_error(416, 'Requested Range Not Satisfiable')
                    return None
                self.send_response(206)
                self.send_header('Content-type', ctype)
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Access-Control-Allow-Origin', '*')

                if last is None or last >= file_len:
                    last = file_len - 1
                response_length = last - first + 1

                self.send_header(
                    'Content-Range', f'bytes {first}-{last}/{file_len}')
                self.send_header('Content-Length', str(response_length))
                self.send_header(
                    'Last-Modified', self.date_time_string(file_stat.st_mtime))  # type: ignore
                self.send_header('Vary', 'Accept-Encoding')
                self.end_headers()
            else:
                # Use browser cache if possible
                if ('If-Modified-Since' in self.headers
                        and 'If-None-Match' not in self.headers):
                    # compare If-Modified-Since and time of last file modification
                    try:
                        ims = email.utils.parsedate_to_datetime(  # type: ignore
                            self.headers['If-Modified-Since'])
                    except (TypeError, IndexError, OverflowError, ValueError):
                        # ignore ill-formed values
                        pass
                    else:
                        if ims.tzinfo is None:
                            # obsolete format with no timezone, cf.
                            # https://tools.ietf.org/html/rfc7231#section-7.1.1.1
                            ims = ims.replace(tzinfo=datetime.timezone.utc)
                        if ims.tzinfo is datetime.timezone.utc:
                            # compare to UTC datetime of last modification
                            last_modif = datetime.datetime.fromtimestamp(
                                file_stat.st_mtime, datetime.timezone.utc)
                            # remove microseconds, like in If-Modified-Since
                            last_modif = last_modif.replace(microsecond=0)

                            if last_modif <= ims:
                                self.send_response(
                                    http.HTTPStatus.NOT_MODIFIED)
                                self.end_headers()
                                file.close()
                                return None

                self.send_response(http.HTTPStatus.OK)
                self.send_header('Content-type', ctype)
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(file_stat[6]))
                self.send_header(
                    'Last-Modified', self.date_time_string(file_stat.st_mtime))  # type: ignore
                self.send_header('Vary', 'Accept-Encoding')
                self.end_headers()
            return file
        except:
            file.close()
            raise

    def copyfile(self, source, outputfile):
        if not self.range:
            super().copyfile(source, outputfile)
            return

        # SimpleHTTPRequestHandler uses shutil.copyfileobj, which doesn't let
        # you stop the copying before the end of the file.
        start, stop = self.range  # set in send_head()
        copy_byte_range(source, outputfile, start, stop)


httpd = http.server.HTTPServer((HOSTNAME, PORT), RangeRequestHandler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_verify_locations(os.path.join(CERT_DIR, 'cert.ca-bundle'))
context.load_cert_chain(
    keyfile=os.path.join(CERT_DIR, 'cert.pem'),
    certfile=os.path.join(CERT_DIR, 'cert.crt'))
httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
print(f'Serving on {httpd.server_address}')
httpd.serve_forever()
