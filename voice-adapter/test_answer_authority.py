import unittest
from unittest.mock import AsyncMock, patch

import mobile_live as live


class AnswerAuthority(unittest.IsolatedAsyncioTestCase):
    async def test_tier_diagnosis_never_reaches_the_speaker(self):
        call = live.Call(live.Manager(None))
        try:
            call.latest_did = 'd1'
            with patch.object(live.tiers, 'load', return_value={'fast': ['fixture']}), \
                    patch.object(live.tiers, 'race', AsyncMock(return_value=('A symlink blocked both.', 'fixture', 1, []))):
                await call.tier_lanes('d1', 'Status on the hill climbs?')
            self.assertFalse(call.spoken_for.get('d1'))
        finally:
            await call.close('test')

    async def test_first_sentence_and_final_are_not_repeated(self):
        call = live.Call(live.Manager(None))
        try:
            call.latest_did = 'd1'
            await call.offer_finding('d1', 'FINDING: Both climbs logged new output. The', action=False)
            self.assertEqual(call.finish_finding('d1', 'FINDING: Both climbs logged new output. The run logs agree.'),
                             'The run logs agree.')
            self.assertEqual(call.spoken_for['d1'], ['Both climbs logged new output.'])
        finally:
            await call.close('test')

    async def test_changed_finding_is_one_correction(self):
        call = live.Call(live.Manager(None))
        try:
            call.latest_did = 'd1'
            await call.offer_finding('d1', 'FINDING: The symlink is broken. More', action=False)
            self.assertEqual(call.finish_finding('d1', 'Sorry. Correction: The symlink is intact.'),
                             'correction: The symlink is intact.')
        finally:
            await call.close('test')
