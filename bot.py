import argparse
import asyncio
import fcntl
import importlib.util
import os
import shutil
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

load_dotenv(Path(__file__).with_name(".env"))
TOKEN = os.getenv("DISCORD_TOKEN")


class YutaBot(commands.Bot):
    sync_guild_id = None

    async def setup_hook(self):
        # Sync once at login, rather than on every gateway reconnect.
        guild = discord.Object(id=self.sync_guild_id) if self.sync_guild_id else None
        if guild:
            self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
        except discord.HTTPException as error:
            print(f"Command sync failed (HTTP {error.status}, Discord code {error.code}).")
            print("Install YUTA in your server with bot and applications.commands scopes.")
            raise RuntimeError("Slash command sync failed; check the installation and server ID.") from None
        scope = f"server {guild.id}" if guild else "globally"
        print(f"Synced {len(synced)} command(s) {scope}.", flush=True)

    async def close(self):
        tasks = [player.task for player in players.values() if player.task]
        for player in list(players.values()):
            player.stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await super().close()


bot = YutaBot(command_prefix="!", intents=discord.Intents.default())


class MusicError(Exception):
    """An error that is safe to show in Discord."""


class QuietLogger:
    # Keep extractor diagnostics out of chat and console.
    def debug(self, message):
        pass

    info = warning = error = debug


@dataclass
class Song:
    title: str
    url: str
    channel: object


def extract_audio(query):
    """Runs in a background thread so searches never block Discord."""
    options = {
        "format": "bestaudio/best",
        "default_search": "ytsearch1",
        "noplaylist": True,
        "quiet": True,
        "logger": QuietLogger(),
        "socket_timeout": 20,
        "retries": 2,
        "cachedir": False,
        "js_runtimes": {"deno": {}, "node": {}},
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(query, download=False)
        if info and "entries" in info:
            info = next((entry for entry in info["entries"] if entry), None)
        if not info or not info.get("url"):
            raise MusicError("I couldn't find playable audio. Try another song or YouTube URL.")
        return info


def title_text(title):
    return discord.utils.escape_markdown(title.replace("\n", " "))[:120]


async def announce(channel, message):
    try:
        await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass


def create_audio_source(url, **overrides):
    options = {
        "before_options": "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        "options": "-vn",
    }
    options.update(overrides)
    # discord.py needs a file object here, not subprocess.DEVNULL's integer.
    # FFmpeg inherits the descriptor, so our copy can close after spawning it.
    with open(os.devnull, "wb") as error_output:
        return discord.FFmpegOpusAudio(url, stderr=error_output, **options)


class MusicPlayer:
    """One queue and one playback task for a Discord server."""

    def __init__(self, guild):
        self.guild = guild
        self.queue = deque()
        self.current = None
        self.task = None
        self.lock = asyncio.Lock()
        self.generation = 0

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    def cancel(self):
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = None
        self.current = None
        if self.guild.voice_client:
            self.guild.voice_client.stop()

    def stop(self):
        # Invalidate searches started before /stop or /leave.
        self.generation += 1
        self.queue.clear()
        self.cancel()

    async def run(self):
        try:
            while self.queue:
                voice = self.guild.voice_client
                if not voice or not voice.is_connected():
                    self.queue.clear()
                    break
                self.current = self.queue.popleft()
                song = self.current
                source = None
                try:
                    # Refresh the stream URL at playback time; queued URLs expire.
                    info = await asyncio.to_thread(extract_audio, song.url)
                    source = create_audio_source(info["url"])
                    loop = asyncio.get_running_loop()
                    finished = loop.create_future()

                    def finish(error, result=finished):
                        if not result.done():
                            result.set_result(error)

                    def after(error, callback=finish):
                        if not loop.is_closed():
                            loop.call_soon_threadsafe(callback, error)

                    voice.play(source, after=after)
                    source = None  # The voice player now owns cleanup.
                    await announce(song.channel, f"🎵 Now playing: **{title_text(song.title)}**")
                    error = await finished
                    if error:
                        print(f"Playback failed: {type(error).__name__}", flush=True)
                        await announce(song.channel, "Playback was interrupted. I'll try the next song.")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await announce(song.channel, "I couldn't play that song. Check FFmpeg and try another video. Moving to the next song.")
                finally:
                    if source is not None:
                        source.cleanup()
        finally:
            # A cancelled worker must not clear its replacement's current song.
            if self.task is asyncio.current_task():
                self.current = None
                self.task = None


players = {}


def get_player(guild):
    if guild.id not in players:
        players[guild.id] = MusicPlayer(guild)
    return players[guild.id]


def user_channel(interaction):
    voice = getattr(interaction.user, "voice", None)
    if not voice or not voice.channel:
        raise MusicError("Join a voice channel first, then try again.")
    if isinstance(voice.channel, discord.StageChannel):
        raise MusicError("Please use a regular voice channel for music.")
    return voice.channel


def control_voice(interaction):
    channel = user_channel(interaction)
    voice = interaction.guild.voice_client
    if not voice or not voice.is_connected():
        raise MusicError("I'm not in a voice channel. Use /join or /play first.")
    if voice.channel != channel:
        raise MusicError("Join my voice channel to control the music.")
    return voice


async def connect(interaction):
    channel = user_channel(interaction)
    voice = interaction.guild.voice_client
    if voice:
        if voice.channel != channel:
            raise MusicError("I'm already in another voice channel. Join me there first.")
        if not voice.is_connected():
            raise MusicError("My voice connection is recovering. Try again shortly.")
        return voice
    permissions = channel.permissions_for(interaction.guild.me)
    if not permissions.connect or not permissions.speak:
        raise MusicError("I need Connect and Speak permissions in your voice channel.")
    if any(importlib.util.find_spec(name) is None for name in ("nacl", "davey")):
        raise MusicError("Voice dependencies are missing. Start YUTA using .yuta-runtime/bin/python bot.py after setup.")
    try:
        return await channel.connect(timeout=20, self_deaf=True)
    except Exception:
        raise MusicError("I couldn't join voice. Check my permissions and that voice dependencies are installed.") from None


@bot.event
async def on_ready():
    print(f"YUTA is online as {bot.user}. Connected to {len(bot.guilds)} server(s).", flush=True)
    permissions = discord.Permissions(
        view_channel=True, send_messages=True, connect=True, speak=True,
    )
    print("Server installation link: " + discord.utils.oauth_url(
        bot.user.id, permissions=permissions, scopes=("bot", "applications.commands"),
    ), flush=True)


@bot.event
async def on_voice_state_update(member, before, after):
    if bot.user and member.id == bot.user.id and after.channel is None:
        player = players.pop(member.guild.id, None)
        if player:
            player.stop()


@bot.tree.error
async def command_error(interaction, error):
    original = getattr(error, "original", error)
    if isinstance(original, discord.HTTPException) and original.code in (40060, 10062):
        print("Interaction expired or already answered. Check for another YUTA process.", flush=True)
        return
    message = str(original) if isinstance(original, MusicError) else "Something went wrong. Please try again and check my voice permissions."
    if isinstance(original, app_commands.NoPrivateMessage):
        message = "Use music commands inside a server."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        print("Could not deliver the command error response.", flush=True)


@bot.tree.command(name="hello", description="Say hello to YUTA")
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message(f"Hello {interaction.user.mention}! I am YUTA 🎵")


@bot.tree.command(name="join", description="Join your voice channel")
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    user_channel(interaction)
    await interaction.response.defer()
    async with get_player(interaction.guild).lock:
        voice = await connect(interaction)
    await interaction.followup.send(f"Joined {voice.channel.mention}! 🎵")


@bot.tree.command(name="play", description="Play a YouTube URL or search for a song")
@app_commands.guild_only()
@app_commands.describe(query="A song name or YouTube video URL")
async def play(interaction: discord.Interaction, query: str):
    user_channel(interaction)
    query = query.strip()
    if not query:
        raise MusicError("Enter a song name or YouTube video URL.")
    if "://" in query:
        parsed = urlparse(query)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in (
            "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be",
        ):
            raise MusicError("Please provide a YouTube URL or a song name.")
    else:
        query = "ytsearch1:" + query
    if not shutil.which("ffmpeg"):
        raise MusicError("FFmpeg is missing. Please install it on the computer running YUTA.")
    await interaction.response.defer()
    player = get_player(interaction.guild)
    generation = player.generation
    async with player.lock:
        await connect(interaction)
    try:
        info = await asyncio.to_thread(extract_audio, query)
    except Exception:
        raise MusicError("I couldn't find or load that song. Try another search or a public YouTube video.") from None
    if player.generation != generation:
        raise MusicError("That request was cancelled because music was stopped or I left voice.")
    control_voice(interaction)
    song = Song(info.get("title", "Unknown song"), info["webpage_url"], interaction.channel)
    player.queue.append(song)
    player.start()
    await interaction.followup.send(f"Added to the queue: **{title_text(song.title)}**", allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="pause", description="Pause the current song")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    voice = control_voice(interaction)
    if not voice.is_playing():
        raise MusicError("No song is playing right now.")
    voice.pause()
    await interaction.response.send_message("⏸️ Music paused.")


@bot.tree.command(name="resume", description="Resume the paused song")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    voice = control_voice(interaction)
    if not voice.is_paused():
        raise MusicError("There isn't a paused song to resume.")
    voice.resume()
    await interaction.response.send_message("▶️ Music resumed.")


@bot.tree.command(name="skip", description="Skip the current song")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    control_voice(interaction)
    player = get_player(interaction.guild)
    if not player.current:
        raise MusicError("There isn't a current song to skip.")
    player.cancel()
    player.start()
    await interaction.response.send_message("⏭️ Skipped the song.")


@bot.tree.command(name="stop", description="Stop music and clear the queue")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    control_voice(interaction)
    get_player(interaction.guild).stop()
    await interaction.response.send_message("⏹️ Music stopped and queue cleared.")


@bot.tree.command(name="queue", description="Show this server's music queue")
@app_commands.guild_only()
async def queue(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    lines = [f"Current: **{title_text(player.current.title)}**" if player.current else "No current song."]
    lines.extend(f"{index}. {title_text(song.title)}" for index, song in enumerate(list(player.queue)[:10], 1))
    if len(player.queue) > 10:
        lines.append(f"…and {len(player.queue) - 10} more songs.")
    if not player.queue:
        lines.append("The queue is empty. Add a song with /play.")
    await interaction.response.send_message("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="nowplaying", description="Show the current song")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    song = get_player(interaction.guild).current
    voice = interaction.guild.voice_client
    status = "Paused" if voice and voice.is_paused() else "Now playing"
    if song and voice and not voice.is_playing() and not voice.is_paused():
        status = "Loading"
    message = f"🎵 {status}: **{title_text(song.title)}**\n<{song.url}>" if song else "No song is playing. Use /play to add one!"
    await interaction.response.send_message(message, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="leave", description="Clear the queue and leave voice")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    control_voice(interaction)
    await interaction.response.defer()
    player = get_player(interaction.guild)
    async with player.lock:
        voice = control_voice(interaction)
        player.stop()
        await voice.disconnect(force=True)
    await interaction.followup.send("👋 Left the voice channel and cleared the queue.")


def main():
    parser = argparse.ArgumentParser(description="YUTA Discord music bot")
    parser.add_argument("--guild-id", type=int, help="Sync commands directly to this server")
    parser.add_argument("--check", action="store_true", help="Check setup without connecting to Discord")
    args = parser.parse_args()
    if args.check:
        checks = {
            "Discord token configured (value hidden)": bool(TOKEN),
            "FFmpeg": shutil.which("ffmpeg") is not None,
            "PyNaCl": importlib.util.find_spec("nacl") is not None,
            "DAVE voice support": importlib.util.find_spec("davey") is not None,
            "YouTube challenge solver": importlib.util.find_spec("yt_dlp_ejs") is not None,
            "JavaScript runtime (Deno or Node)": bool(shutil.which("deno") or shutil.which("node")),
        }
        for label, passed in checks.items():
            print(f"{'OK' if passed else 'MISSING'}: {label}")
        raise SystemExit(0 if all(checks.values()) else 1)
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing from .env")
    # Keep this file open for the lifetime of the process (Linux/macOS).
    instance_lock = Path(__file__).with_name(".yuta.lock").open("a")
    try:
        fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        instance_lock.close()
        raise SystemExit("YUTA is already running. Stop the existing process before starting another.") from None
    bot.sync_guild_id = args.guild_id
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        raise SystemExit("Discord rejected the configured token. Check it privately in your .env file.") from None
    except (RuntimeError, OSError):
        raise SystemExit("YUTA could not start. Check the setup, sync messages above, and network connection.") from None
    finally:
        instance_lock.close()


if __name__ == "__main__":
    main()
