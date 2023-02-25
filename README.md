This is intended as a spiritual successor to https://github.com/math4origami/karaqueue.

This project implements a Discord bot that manages a queue for a local karaoke session. Multiple users can interact with the queue concurrently through a Discord channel, and videos can be pitch shifted and served from the host machine.

## Setup

1. Register a Discord bot. There are plenty of tutorials on the internet. For the OAuth2 URL to invite the bot to your channel, the `bot` scope with `Send Messages`, `Embed Links`, and `Read Message History` permissions are required.

2. Setup config.ini

```dosini
[DEFAULT]
token = <discord bot token>
dev_user_id = <your user id>
internal_host = <internal ip to serve from, eg. 127.0.0.1>
host = <public url to host machine>
serving_dir = <absolute path to write videos to>

[NICONICO]
username = <email address>
password = <base64 encoded password>

[BILIBILI]
sessdata = <optional SESSDATA cookie>
```

3. Install prereqs

ffmpeg and sox on PATH.

## Usage

The bot writes out videos to `serving_dir`, and expects the host url in `config.ini` to be pointing to a webserver serving files from inside that directory.

Because Discord refuses to embed video files bigger than a certain size, we exploit a workaround where we link to an html page containing OpenGraph tags for a video and for some reason Discord is happy with that.

The webserver must be accessible from the public internet (so Discord's CDN can read it) and files must be served via the default HTTPS port 443. For some reason Discord does not embed videos served from HTTP links or links with an explicit port eg. `https://my-server:xxxx/blah`, even though they happily embed images from those links. The webserver must support HTTP byte range requests and must send the `accept-ranges: bytes` header for both the `index.html` file as well as for the videos. Feel free to use your existing webserver if it fits these constraints.

```bash
mkdir _generated_videos
(cd _generated_videos && python3 ../httpsserver.py &)
python3 main.py
```

In discord: `/help`.
