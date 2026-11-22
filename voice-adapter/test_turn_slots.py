"""turn_slots.TurnSlots: a call's bounded, FIFO, cancel-safe line of Hermes runs."""
import asyncio
import unittest

from turn_slots import QueueFull, TurnSlots


class TurnSlotsTests(unittest.IsolatedAsyncioTestCase):
    async def test_admits_up_to_run_max_then_queues_with_positions_then_refuses(self):
        slots, told = TurnSlots(2, 2), []
        self.assertEqual(await slots.acquire("a"), 0)
        self.assertEqual(await slots.acquire("b"), 0)
        c = asyncio.ensure_future(slots.acquire("c", lambda pos, running: told.append(("c", pos, running))))
        d = asyncio.ensure_future(slots.acquire("d", lambda pos, running: told.append(("d", pos, running))))
        await asyncio.sleep(0)
        self.assertEqual(told, [("c", 1, 2), ("d", 2, 2)])
        self.assertEqual((slots.state("c"), slots.position("d")), ("queued", 2))
        with self.assertRaises(QueueFull) as full:
            await slots.acquire("e")
        self.assertEqual((full.exception.running, full.exception.waiting), (2, 2))
        self.assertIsNone(slots.state("e"))
        slots.release("a")
        self.assertEqual(await c, 1)
        self.assertEqual(slots.running, {"b", "c"})
        slots.release("b")
        self.assertEqual(await d, 2)

    async def test_a_waiter_handed_a_slot_then_cancelled_passes_it_on(self):
        slots = TurnSlots(1, 2)
        await slots.acquire("a")
        b = asyncio.ensure_future(slots.acquire("b"))
        c = asyncio.ensure_future(slots.acquire("c"))
        await asyncio.sleep(0)
        slots.release("a")  # b is handed the slot...
        b.cancel()  # ...and cancelled before it runs
        await asyncio.gather(b, return_exceptions=True)
        self.assertEqual(await asyncio.wait_for(c, 1), 2)
        self.assertEqual(slots.running, {"c"})

    async def test_a_cancelled_waiter_leaves_the_line(self):
        slots = TurnSlots(1, 2)
        await slots.acquire("a")
        b = asyncio.ensure_future(slots.acquire("b"))
        c = asyncio.ensure_future(slots.acquire("c"))
        await asyncio.sleep(0)
        b.cancel()
        await asyncio.gather(b, return_exceptions=True)
        self.assertEqual(slots.position("c"), 1)
        slots.release("a")
        await asyncio.wait_for(c, 1)
        self.assertEqual(slots.running, {"c"})
        self.assertEqual(slots.waiting, [])

    async def test_slot_releases_on_error(self):
        slots = TurnSlots(1, 0)
        with self.assertRaises(RuntimeError):
            async with slots.slot("a"):
                raise RuntimeError("hermes failed")
        self.assertEqual(slots.running, set())
        async with slots.slot("b"):
            self.assertEqual(slots.state("b"), "running")


if __name__ == "__main__":
    unittest.main()
