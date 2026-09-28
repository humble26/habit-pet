"""气泡排队器测试。"""
from __future__ import annotations

import unittest

from habitpet.bubble_queue import BubbleQueue


class TestBubbleQueue(unittest.TestCase):
    def test_first_push_displays_immediately(self):
        q = BubbleQueue()
        self.assertTrue(q.push("a", 6))
        self.assertEqual(q.current, ("a", 6))

    def test_queued_while_showing(self):
        q = BubbleQueue()
        q.push("a")
        self.assertFalse(q.push("b"))
        self.assertEqual(q.current, ("a", 6.0))
        self.assertEqual(q.on_hidden(), ("b", 6.0))
        self.assertIsNone(q.on_hidden())
        self.assertFalse(q.showing)

    def test_fifo_order(self):
        q = BubbleQueue()
        q.push("a")
        q.push("b")
        q.push("c")
        self.assertEqual(q.on_hidden(), ("b", 6.0))
        self.assertEqual(q.on_hidden(), ("c", 6.0))
        self.assertIsNone(q.on_hidden())

    def test_capacity_drops_oldest(self):
        q = BubbleQueue(capacity=2)
        q.push("live")
        q.push("old1")
        q.push("old2")
        q.push("new")            # 挤掉 old1
        self.assertEqual(q.on_hidden(), ("old2", 6.0))
        self.assertEqual(q.on_hidden(), ("new", 6.0))

    def test_free_again_after_drain(self):
        q = BubbleQueue()
        q.push("a")
        q.on_hidden()
        self.assertTrue(q.push("b", 3))
        self.assertEqual(q.current, ("b", 3))


if __name__ == "__main__":
    unittest.main()
