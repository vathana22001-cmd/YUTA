# YUTA music bot

YUTA uses slash commands, a separate in-memory queue for each server, yt-dlp
for YouTube lookup, and FFmpeg for audio playback. Queues reset on restart.

## Setup

Use Python 3.10 or newer. YUTA has a separate `.yuta-runtime` environment;
your original `.venv` is unchanged. To recreate the runtime on another machine:

```bash
python3 -m venv .yuta-runtime
.yuta-runtime/bin/python -m pip install -U -r requirements.txt
```

Install the FFmpeg executable and make sure `ffmpeg -version` works.
For full YouTube support, use a supported JavaScript runtime such as Deno or Node;
see its [dependency instructions](https://github.com/yt-dlp/yt-dlp#dependencies).
`yt-dlp[default]` includes its companion JavaScript challenge solver.

Keep your existing `DISCORD_TOKEN` entry in `.env`. Check and start YUTA:

```bash
.yuta-runtime/bin/python bot.py --check
.yuta-runtime/bin/python bot.py
```

Keep that terminal running. Stop an older bot process before starting this one.
Run only one copy: duplicate bot processes can conflict over slash commands and
voice connections. On Linux/macOS, `.yuta.lock` prevents a second local copy from
starting. The lock releases when YUTA stops; do not delete it while YUTA runs.
Select `.yuta-runtime/bin/python` as your IDE's Python interpreter.
YUTA always loads `.env` beside `bot.py`, even if started from another directory.
Never share your token or commit `.env`.

Invite the bot with the `bot` and `applications.commands` scopes. Give it
View Channel, Send Messages, Connect, and Speak permissions in the channels
where you use it. The terminal prints an installation link when YUTA connects.
Slash commands sync once at login and global commands can take time to appear.

## If slash commands do not appear

Check the terminal for `Synced 10 command(s)` and `YUTA is online`.
The library's Message Content Intent warning concerns prefix commands; YUTA's
slash commands do not need that privileged intent.
If zero servers are reported, use the printed installation link to add YUTA
to your server. Install it as a server app, not just a personal app.

For direct server registration, enable Developer Mode in Discord's Advanced
settings, right-click your server, and choose Copy Server ID. Start the bot
with your numeric server ID in place of `YOUR_SERVER_ID`:

```bash
.yuta-runtime/bin/python bot.py --guild-id YOUR_SERVER_ID
```

Keep using this option for that server while testing. It copies all ten commands
to that server without waiting for global command propagation. YUTA must already
be installed there. Reload Discord, type `/hello`, then join voice and try
`/play never gonna give you up`. Check that your role can Use Application Commands
and that Server Settings → Integrations → YUTA does not restrict your commands.

Voice requires both PyNaCl and `davey`; the requirements include both. `--check`
reports missing dependencies without showing the token. If YouTube rejects a
video, try another public video. Node is explicitly enabled as a fallback to Deno.

## Offline tests

```bash
.yuta-runtime/bin/python -B -m unittest discover -s tests -v
```

Tests mock audio and network operations and never load your token.

## Commands

| Command | Action |
| --- | --- |
| `/hello` | Say hello to YUTA |
| `/join` | Join your current voice channel |
| `/play query` | Queue one YouTube video or the first song search result; join automatically |
| `/pause` | Pause playback |
| `/resume` | Resume playback |
| `/skip` | Skip the current or loading song |
| `/stop` | Stop playback and clear queued songs and pending searches |
| `/queue` | Show the current song and the first 10 waiting songs |
| `/nowplaying` | Show the current song and its link |
| `/leave` | Stop, clear the queue, and disconnect |

Join a regular voice channel before `/join` or `/play`. Playback controls require
you to be in YUTA's voice channel. Stage channels and playlist imports are not
supported. Failed tracks are skipped automatically. YouTube may restrict some
videos; try a public video and keep yt-dlp updated if extraction fails.

Playback uses discord.py's [voice API](https://discordpy.readthedocs.io/en/stable/api.html#voice).
Live playback needs a running bot connected to a Discord server.
