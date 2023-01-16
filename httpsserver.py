import configparser
import http.server
import logging
import os
import ssl

SRC_DIR = os.path.dirname(__file__)
CERT_DIR = os.path.join(SRC_DIR, 'certs')

logging.basicConfig(level=logging.INFO)
cfg = configparser.ConfigParser()
cfg.read(os.path.join(SRC_DIR, 'config.ini'))

HOSTNAME = '192.168.0.185'
PORT = int(cfg['DEFAULT'].get('port'))

httpd = http.server.HTTPServer(
    (HOSTNAME, PORT), http.server.SimpleHTTPRequestHandler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_verify_locations(os.path.join(CERT_DIR, 'cert.ca-bundle'))
context.load_cert_chain(
    keyfile=os.path.join(CERT_DIR, 'cert.pem'),
    certfile=os.path.join(CERT_DIR, 'cert.crt'))
httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
logging.info(f'Serving on {httpd.server_address}')
httpd.serve_forever()
