"""Offline tests: no Discord login, token access, or YouTube requests."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import dotenv

with patch.object(dotenv, 'load_dotenv'), patch.dict('os.environ', {'DISCORD_TOKEN': ''}):
    import bot as music


class Voice:
    def __init__(self):
        self.after = None
        self.played = 0

    def is_connected(self):
        return True

    def play(self, source, after):
        self.after = after
        self.played += 1

    def stop(self):
        if self.after:
            callback, self.after = self.after, None
            callback(None)


class MusicTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        music.players.clear()
        self.voice = Voice()
        self.player = music.get_player(SimpleNamespace(id=1, voice_client=self.voice))
        self.channel = SimpleNamespace(send=AsyncMock())
        self.thread = patch.object(music.asyncio, 'to_thread', AsyncMock(return_value={'url': 'audio'}))
        self.source = patch.object(music.discord, 'FFmpegOpusAudio', Mock())
        self.thread.start()
        self.source.start()

    async def asyncTearDown(self):
        tasks = [p.task for p in music.players.values() if p.task]
        for player in music.players.values():
            player.stop()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.thread.stop()
        self.source.stop()

    async def tick(self):
        for _ in range(5):
            await asyncio.sleep(0)

    def add(self, title):
        self.player.queue.append(music.Song(title, 'url', self.channel))

    async def test_commands_and_isolation(self):
        self.assertEqual({c.name for c in music.bot.tree.get_commands()}, {
            'hello', 'join', 'play', 'pause', 'resume', 'skip', 'stop',
            'queue', 'nowplaying', 'leave',
        })
        self.add('one')
        other = music.get_player(SimpleNamespace(id=2, voice_client=Voice()))
        self.assertFalse(other.queue)

    async def test_advance_skip_and_stale_callback(self):
        for title in ('one', 'two', 'three'):
            self.add(title)
        self.player.start()
        await self.tick()
        old_callback = self.voice.after
        old_callback(None)
        await self.tick()
        self.assertEqual(self.player.current.title, 'two')
        # A late callback from an earlier track must not finish the next track.
        old_callback(None)
        await self.tick()
        self.assertEqual(self.player.current.title, 'two')
        self.player.cancel()
        self.player.start()
        await self.tick()
        self.assertEqual(self.player.current.title, 'three')

    async def test_stop_during_loading(self):
        async def slow(*args):
            await asyncio.Event().wait()
        music.asyncio.to_thread.side_effect = slow
        self.add('loading')
        self.player.start()
        await self.tick()
        generation = self.player.generation
        self.player.stop()
        await self.tick()
        self.assertIsNone(self.player.current)
        self.assertFalse(self.player.queue)
        self.assertEqual(self.voice.played, 0)
        self.assertEqual(self.player.generation, generation + 1)

    async def test_failed_track_advances(self):
        music.asyncio.to_thread.side_effect = [RuntimeError(), {'url': 'audio'}]
        self.add('bad')
        self.add('good')
        self.player.start()
        await self.tick()
        self.assertEqual(self.player.current.title, 'good')
        self.assertEqual(self.voice.played, 1)

    async def test_voice_required(self):
        with self.assertRaises(music.MusicError):
            music.user_channel(SimpleNamespace(user=SimpleNamespace(voice=None)))

    async def test_sync_modes(self):
        client = music.YutaBot(command_prefix='!', intents=music.discord.Intents.default())
        with patch.object(client.tree, 'sync', AsyncMock(return_value=[])) as sync:
            await client.setup_hook()
            sync.assert_awaited_with(guild=None)
            client.sync_guild_id = 123
            with patch.object(client.tree, 'copy_global_to') as copy:
                await client.setup_hook()
                self.assertEqual(copy.call_args.kwargs['guild'].id, 123)
                self.assertEqual(sync.call_args.kwargs['guild'].id, 123)
        await client.close()


class FFmpegTests(unittest.TestCase):
    def test_real_audio_source_has_packets_and_no_pipe_error(self):
        source = music.create_audio_source(
            'sine=frequency=440:duration=0.2', before_options='-f lavfi',
        )
        try:
            packets = []
            while packet := source.read():
                packets.append(packet)
            self.assertGreater(len(packets), 0)
            self.assertIsNone(source._current_error)
            self.assertIsNone(source._pipe_reader_thread)
        finally:
            source.cleanup()


if __name__ == '__main__':
    unittest.main()
