import asyncio
from pathlib import Path
import sys
import unittest

import numpy as np

original_path = sys.path[:]
sys.path.insert(0, str(Path(__file__).resolve().parent / 'bench'))
try:
    from local_voice_replay import conversation, grade
finally:
    sys.path[:] = original_path


def input_pcm(*segments):
    return np.concatenate([np.full(round(48000 * duration), amplitude, '<i2')
                           for duration, amplitude in segments]).tobytes()


class VoiceReplayTest(unittest.IsolatedAsyncioTestCase):
    async def test_silence_then_speech_plays_only_after_endpoint(self):
        class Models:
            async def asr(self, pcm, marks):
                return 'What is two plus two?'

            async def reply(self, messages, marks):
                return 'Four.' if messages[-1]['content'] == 'What is two plus two?' else 'Wrong.'

            async def tts(self, text):
                if text == 'Four.':
                    yield np.full(4800, 1234, '<i2')

        audio, rows = await conversation(Models(), input_pcm((0.2, 0), (0.16, 2000), (0.1, 0)),
                                         -43, 80, drain=True)
        samples = np.frombuffer(audio, '<i2')
        self.assertEqual([r['status'] for r in rows], ['played'])
        self.assertGreaterEqual(np.flatnonzero(samples)[0], round(0.44 * 48000))
        self.assertEqual(np.count_nonzero(samples == 1234), 4800)

    async def test_barge_in_drops_unheard_pcm(self):
        class Models:
            async def asr(self, pcm, marks):
                return 'Hello.'

            async def reply(self, messages, marks):
                return 'Hello back.'

            async def tts(self, text):
                yield np.full(48000, 1234, '<i2')
                await asyncio.sleep(10)

        audio, rows = await conversation(Models(), input_pcm((0.16, 2000), (0.24, 0), (0.14, 2000)),
                                         -43, 80)
        self.assertEqual([r['status'] for r in rows], ['barge_in'])
        samples = np.frombuffer(audio, '<i2')
        self.assertTrue(np.any(samples[:round(0.4 * 48000)]))
        self.assertFalse(np.any(samples[round(0.52 * 48000):]))

    def test_skip_grades_the_audible_deferral_not_the_control_token(self):
        self.assertTrue(grade({'skip': True}, "I can't check that from here.", audible=True))
        self.assertFalse(grade({'skip': True}, 'SKIP', audible=True))
        self.assertFalse(grade({'skip': True}, 'Nothing new in your email.', audible=True))
