#!/usr/bin/env python3
"""Unit tests for the change detector using synthetic frames (no ffmpeg)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from detect_changes import (  # noqa: E402
    block_score,
    build_parser,
    detect_frames,
    validate_args,
)


def solid(value: int, h: int = 64, w: int = 64) -> np.ndarray:
    return np.full((h, w), value, dtype=np.uint8)


def timed(frames: list[np.ndarray], fps: float = 4.0):
    return [(i / fps, f) for i, f in enumerate(frames)]


class BlockScoreTests(unittest.TestCase):
    def test_identical_frames_score_zero(self):
        a = solid(10)
        self.assertEqual(
            block_score(a, a, block=8, pix_t=25, block_t=0.03, mask=None),
            0.0,
        )

    def test_full_change_scores_high(self):
        a = solid(0)
        b = solid(255)
        score = block_score(a, b, block=8, pix_t=25, block_t=0.03, mask=None)
        self.assertGreater(score, 0.9)

    def test_block_zero_raises(self):
        a = solid(0)
        with self.assertRaises(ValueError):
            block_score(a, a, block=0, pix_t=25, block_t=0.03, mask=None)


class DetectFramesTests(unittest.TestCase):
    def test_settled_change_emits_one_event(self):
        frames = timed(
            [solid(0), solid(0), solid(255), solid(255), solid(255), solid(255), solid(255)]
        )
        events = detect_frames(
            frames,
            block=8,
            pix_t=25,
            block_t=0.03,
            high=0.2,
            low=0.01,
            settle=3,
            min_gap=0.0,
        )
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].get("unsettled", False))
        self.assertGreater(events[0]["score"], 0.2)

    def test_unsettled_eof_emits_event(self):
        # Jump to a new frame and end before calm settle frames accumulate.
        frames = timed([solid(0), solid(0), solid(255), solid(200)])
        events = detect_frames(
            frames,
            block=8,
            pix_t=25,
            block_t=0.03,
            high=0.2,
            low=0.01,
            settle=5,
            min_gap=0.0,
        )
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["unsettled"])

    def test_min_gap_keeps_ref_until_gap_allows_report(self):
        # black -> white (event 1), then gray soon after; keep gray until min_gap opens.
        seq = [solid(0), solid(0)]
        seq += [solid(255)] * 5
        seq += [solid(128)] * 16
        frames = timed(seq, fps=4.0)
        events = detect_frames(
            frames,
            block=8,
            pix_t=25,
            block_t=0.03,
            high=0.2,
            low=0.01,
            settle=3,
            min_gap=2.0,
        )
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[0]["start"], 0.5)
        # Preserve true onset of the second change (first gray frame).
        self.assertEqual(events[1]["start"], 1.75)
        # Reporting waits until settled time clears min_gap vs prior start.
        self.assertGreaterEqual(events[1]["settled"] - events[0]["start"], 2.0)

    def test_min_gap_pending_emitted_at_eof(self):
        # Second lasting change settles inside min_gap and video ends while STABLE.
        seq = [solid(0), solid(0)]
        seq += [solid(255)] * 5
        seq += [solid(128)] * 8
        frames = timed(seq, fps=4.0)
        events = detect_frames(
            frames,
            block=8,
            pix_t=25,
            block_t=0.03,
            high=0.2,
            low=0.01,
            settle=3,
            min_gap=5.0,
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["start"], 0.5)
        self.assertEqual(events[1]["start"], 1.75)
        self.assertFalse(events[1].get("unsettled", False))

    def test_no_false_positive_on_static(self):
        frames = timed([solid(40)] * 10)
        events = detect_frames(
            frames,
            block=8,
            pix_t=25,
            block_t=0.03,
            high=0.2,
            low=0.01,
            settle=3,
            min_gap=0.0,
        )
        self.assertEqual(events, [])


class ValidateArgsTests(unittest.TestCase):
    def test_rejects_non_positive_fps(self):
        args = build_parser().parse_args(["clip.mp4", "--fps", "0"])
        self.assertIsNotNone(validate_args(args))

    def test_rejects_zero_block(self):
        args = build_parser().parse_args(["clip.mp4", "--block", "0"])
        self.assertIsNotNone(validate_args(args))

    def test_defaults_ok(self):
        args = build_parser().parse_args(["clip.mp4"])
        self.assertIsNone(validate_args(args))


if __name__ == "__main__":
    unittest.main()
