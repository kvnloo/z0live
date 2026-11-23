"""An answer handed to a voice is delivered when the voice says its words, not when a reply merely starts or a
timer runs out. Shapes from the 11-20 night calls (words made up: call transcripts stay off GitHub)."""
import unittest

from answer_delivery import SETTLE_SECONDS, Ledger


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()

    def test_merged_into_the_reply_already_playing_is_delivered(self):
        # 192702 at 698.7 s: the answer arrived mid-reply and the voice folded it into that reply.
        self.ledger.heard("primary", "Four of the five garden beds are planted.")
        self.ledger.sent("primary", "d1", "Nothing new has started. I need your budget cap and the shed key first.", 10)
        done = self.ledger.heard("primary", " I need your budget cap and the shed key.")
        self.assertEqual([entry.did for entry in done], ["d1"])
        self.assertEqual(self.ledger.due("primary", 99, quiet_since=20), [])

    def test_an_answer_handed_into_a_playing_reply_counts_only_once_it_says_one_of_its_sentences(self):
        # Take 15, line 3: the answer came 9 s into the reply to the request, which went on with "The update run is
        # still going, no new receipts yet". Three of the answer's words, each from another sentence, credited it.
        answer = ("That job is already running as card seven, so I won't start another. The last receipts are from "
                  "midnight. Nothing new is planted yet. The shed run keeps landing receipts on the card.")
        self.ledger.sent("primary", "d1", answer, 10, late=True)
        self.assertEqual(self.ledger.heard("primary", " The garden run is still going, no new receipts yet."), [])
        self.assertEqual(self.ledger.cut("primary"), [], "never begun: the whole answer stays pending")
        [(entry, text)] = self.ledger.due("primary", 30, quiet_since=None)
        self.assertEqual(text, answer)
        self.assertEqual([e.did for e in self.ledger.heard("primary", "It's already running as card seven, so "
                                                                         "I won't start another.")], ["d1"])

    def test_a_reply_that_never_says_it_does_not_deliver_it(self):
        # Any allowed reply starting used to mark every pending answer delivered ("Mm-hm." included).
        self.ledger.sent("primary", "d1", "The bike chain is fixed and the brakes need new pads.", 10)
        self.assertEqual(self.ledger.heard("primary", "Mm-hm. Sure, I'll pull it together."), [])
        self.assertEqual(len(self.ledger.pending("primary")), 1)

    def test_unspoken_answer_is_resent_once_the_voice_has_been_quiet_then_dropped(self):
        # 205504 at 240.1 s: the install answer was never spoken and never resent.
        self.ledger.sent("primary", "d1", "I couldn't repaint the fence because the paint store is closed.", 10)
        self.assertEqual(self.ledger.due("primary", 10 + SETTLE_SECONDS - 0.1, quiet_since=None), [])
        [(entry, text)] = self.ledger.due("primary", 10 + SETTLE_SECONDS, quiet_since=None)
        self.assertEqual((entry.did, entry.sends), ("d1", 1))
        self.assertIn("paint store", text)
        self.ledger.resent(entry, 20)
        self.assertEqual(self.ledger.due("primary", 20 + SETTLE_SECONDS - 0.1, quiet_since=None), [])
        [(entry, text)] = self.ledger.due("primary", 20 + SETTLE_SECONDS, quiet_since=None)
        self.assertIsNone(text, "a second miss is dropped, never sent a third time")
        self.assertEqual(self.ledger.pending("primary"), [])

    def test_quiet_clock_starts_when_the_last_reply_ends(self):
        self.ledger.sent("primary", "d1", "The bike chain is fixed and the brakes need new pads.", 10)
        self.assertEqual(self.ledger.due("primary", 30, quiet_since=29), [], "the voice just finished a reply")
        self.assertEqual(len(self.ledger.due("primary", 29 + SETTLE_SECONDS, quiet_since=29)), 1)

    def test_pieces_of_one_delegation_are_one_entry_and_only_unspoken_pieces_resend(self):
        # Each Hermes answer used to be two deliveries (first sentence + remainder), and a resend sent both.
        self.ledger.sent("primary", "d1", "The garden has two planted beds.", 10)
        self.ledger.sent("primary", "d1", "The tomato seedlings arrive Thursday from the nursery.", 10.1)
        self.assertEqual(len(self.ledger.pending("primary")), 1)
        self.ledger.heard("primary", "Two beds in the garden are planted.")
        [(entry, text)] = self.ledger.due("primary", 30, quiet_since=None)
        self.assertNotIn("planted beds", text)
        self.assertIn("tomato seedlings", text)

    def test_words_already_said_on_the_call_do_not_count_as_saying_the_answer(self):
        # Status answers repeat the topic's words ("lanes", "fast", "medium"); only the new words show delivery.
        self.ledger.heard("primary", "Three garden beds, two planters and one greenhouse.")
        self.ledger.sent("primary", "d1", "Three garden beds and two planters, and the greenhouse roof leaks.", 10)
        self.assertEqual(self.ledger.heard("primary", "Three garden beds, two planters."), [])
        self.assertEqual([e.did for e in self.ledger.heard("primary", "And the roof leaks.")], ["d1"])

    def test_speech_on_the_other_leg_does_not_deliver(self):
        self.ledger.sent("primary", "d1", "The bike chain is fixed and the brakes need new pads.", 10)
        self.assertEqual(self.ledger.heard("guest", "The bike chain is fixed, the brakes need pads."), [])
        self.assertEqual(len(self.ledger.pending("primary")), 1)

    def test_hermes_answer_supersedes_an_unspoken_lane_guess(self):
        self.ledger.sent("primary", "d1", "Probably the kettle is broken.", 10, lane=True)
        self.ledger.sent("primary", "d1", "The kettle fuse blew; a spare is in the drawer.", 11)
        [(entry, text)] = self.ledger.due("primary", 30, quiet_since=None)
        self.assertNotIn("Probably", text)
        self.assertIn("spare", text)

    def test_a_cut_returns_what_the_voice_had_begun_and_leaves_the_rest_pending(self):
        # Whether the rest comes back is the floor's call (GPTGuest.cut re-queues it), not the ledger's.
        self.ledger.sent("primary", "d1", "Here are two photos of the porch railing and the cracked steps.", 10)
        self.ledger.sent("primary", "d2", "The mailbox was replaced on Monday by the landlord.", 10)
        self.ledger.heard("primary", "Here are two photos")
        self.assertEqual([did for did, _ in self.ledger.cut("primary")], ["d1"])
        self.assertEqual([e.did for e in self.ledger.pending("primary")], ["d2"])
        self.assertEqual(self.ledger.cut("primary"), [], "once")

    def test_one_shared_word_before_a_cut_is_not_having_started_the_answer(self):
        # 205504 at 242.1 s: "Not yet—since" was cut off; only "yet" was in the install answer, which then was lost.
        self.ledger.sent("primary", "d1", "I couldn't install it yet because your phone isn't connected.", 10)
        self.ledger.heard("primary", "Not yet—since")
        self.assertEqual(self.ledger.cut("primary"), [])
        self.assertEqual([e.did for e in self.ledger.pending("primary")], ["d1"])

    def test_a_cut_parks_the_unheard_sentences_of_an_answer_already_counted_spoken(self):
        # Take 13, Tek: three words in ("It was a startup timing bug") the answer counted as spoken, and the cut two
        # sentences later lost the rest. The sentences the voice had said stay said.
        answer = ("The shed door stuck because the hinge pin rusted. The new pin went in on Tuesday. "
                  "Oil it twice a year.")
        self.ledger.sent("guest", "g1", answer, 10)
        self.assertEqual([e.did for e in self.ledger.heard("guest", "The shed door stuck, the hinge pin rusted.")],
                         ["g1"])
        self.ledger.heard("guest", " The new")
        [(_, rest)] = self.ledger.cut("guest")
        self.assertEqual(rest, "The new pin went in on Tuesday. Oil it twice a year.")

    def test_a_cut_resumes_from_the_piece_the_voice_was_saying_not_a_receipt_it_skipped(self):
        # Take 13 replay: the receipt ("Still working on that") was overtaken by the answer, and resuming from it
        # handed the voice a stale status line.
        self.ledger.sent("primary", "d1", "Still checking the shed in the workshop log.", 10)
        self.ledger.sent("primary", "d1", "The shed door stuck because the hinge pin rusted. Oil it twice a year.", 11)
        self.ledger.heard("primary", "The shed door stuck, the hinge pin rusted.")
        [(_, rest)] = self.ledger.cut("primary")
        self.assertEqual(rest, "Oil it twice a year.")

    def test_an_answer_said_to_the_end_leaves_nothing_to_park(self):
        self.ledger.sent("primary", "d1", "The bike chain is fixed. The brakes need new pads.", 10)
        self.ledger.heard("primary", "The bike chain is fixed, and the brakes need new pads.")
        self.assertEqual(self.ledger.cut("primary"), [])

    def test_a_reply_that_ran_its_course_leaves_nothing_for_a_later_cut(self):
        self.ledger.sent("primary", "d1", "The bike chain is fixed. The brakes need new pads.", 10)
        self.ledger.heard("primary", "The bike chain is fixed.")
        self.ledger.reply_done("primary")
        self.ledger.reply_started("primary")
        self.assertEqual(self.ledger.cut("primary"), [], "the voice chose to stop there")

    def test_a_reply_the_provider_ended_under_the_caller_still_parks_its_rest(self):
        # Take 14, line 5: the caller's turn arrives just after the provider's turn.done for the reply it cut.
        self.ledger.sent("primary", "d1", "The bike chain is fixed. The brakes need new pads.", 10)
        self.ledger.heard("primary", "The bike chain is fixed.")
        self.ledger.reply_done("primary")
        [(_, rest)] = self.ledger.cut("primary")
        self.assertEqual(rest, "The brakes need new pads.")

    def test_short_answer_said_in_other_words_is_delivered(self):
        # 113507 at 478 s: "No, I don't have access to your phone's location." was said as "No, I don't have your
        # phone's location." (2 of its 3 new words).
        self.ledger.sent("primary", "d1", "No, I don't have access to your kettle's timer.", 10)
        self.assertEqual([e.did for e in self.ledger.heard("primary", "No, I don't have your kettle's timer.")], ["d1"])

    def test_short_answer_with_no_new_words_is_delivered_by_any_finished_reply(self):
        self.ledger.heard("primary", "What's up?")
        self.ledger.sent("primary", "d1", "What's up?", 10)
        self.assertEqual([e.did for e in self.ledger.reply_done("primary")], ["d1"])

    def test_retire_hands_back_every_unspoken_piece(self):
        self.ledger.sent("primary", "d1", "The bike chain is fixed.", 10)
        self.ledger.sent("guest", "g1", "The release went out at noon.", 10)
        [(entry, text)] = self.ledger.retire("primary")
        self.assertEqual((entry.did, text), ("d1", "The bike chain is fixed."))
        self.assertEqual([e.did for e in self.ledger.pending("guest")], ["g1"])


if __name__ == "__main__":
    unittest.main()
