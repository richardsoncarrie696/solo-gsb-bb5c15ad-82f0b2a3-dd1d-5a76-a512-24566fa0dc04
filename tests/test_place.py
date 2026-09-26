"""Unit + API tests for occlusion-aware placement (/segment/place).

Run with either:
    python3 -m unittest discover -s tests
    pytest
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app
from app.place import segment_with_placement
from app.segmenter import SegmentationError, segment_words
from app.vtt import format_vtt_timestamp


def W(text: str, start_ms: int, end_ms: int) -> SimpleNamespace:
    return SimpleNamespace(text=text, start_ms=start_ms, end_ms=end_ms)


def OCC(start_ms: int, end_ms: int, region: str) -> SimpleNamespace:
    return SimpleNamespace(start_ms=start_ms, end_ms=end_ms, region=region)


OPTS = dict(
    max_chars=42,
    min_duration_ms=1000,
    max_duration_ms=7000,
    target_duration_ms=3000,
    pause_threshold_ms=500,
)

WORDS = [
    W("Hello", 0, 500),
    W("world.", 500, 1400),
    W("How", 2000, 2400),
    W("are", 2400, 2700),
    W("you", 2700, 3200),
    W("doing", 3200, 3800),
    W("today?", 3800, 4600),
    W("Fine", 5200, 5600),
    W("thanks.", 5600, 6400),
]
# The unconstrained optimum: [0..1] / [2..6] / [7..8], total cost 5.96.
BASE_COST = 5.96


class TestPlacement(unittest.TestCase):
    def test_no_occlusions_matches_plain_segmentation_all_bottom(self):
        result = segment_with_placement(WORDS, [], **OPTS)
        plain = segment_words(WORDS, **OPTS)
        self.assertEqual(
            [(s.text, s.start_ms, s.end_ms) for s in result.segments],
            [(s.text, s.start_ms, s.end_ms) for s in plain.segments],
        )
        self.assertEqual(
            [s.position for s in result.segments], ["bottom"] * len(plain.segments)
        )
        self.assertAlmostEqual(result.total_cost, plain.total_cost)

    def test_bottom_occlusion_prefers_all_top_over_two_switches(self):
        # The middle subtitle (2000..4600) loses the bottom region; moving
        # only it to the top would cost 2 switches, so everything goes top.
        result = segment_with_placement(WORDS, [OCC(1800, 4800, "bottom")], **OPTS)
        self.assertEqual([s.position for s in result.segments], ["top"] * 3)
        self.assertAlmostEqual(result.total_cost, BASE_COST)

    def test_forced_mix_pays_exactly_one_switch(self):
        # First subtitle must be top, last must be bottom -> >= 1 switch.
        result = segment_with_placement(
            WORDS,
            [OCC(0, 1500, "bottom"), OCC(5000, 6600, "top")],
            **OPTS,
        )
        self.assertEqual(
            [s.position for s in result.segments], ["top", "bottom", "bottom"]
        )
        self.assertAlmostEqual(result.total_cost, BASE_COST + 1.0)

    def test_touching_occlusion_edges_do_not_block(self):
        # [1400, 2000] only touches the first two subtitles' edges: the
        # zero-length intersections leave both regions available.
        result = segment_with_placement(WORDS, [OCC(1400, 2000, "bottom")], **OPTS)
        self.assertEqual([s.position for s in result.segments], ["bottom"] * 3)
        self.assertAlmostEqual(result.total_cost, BASE_COST)

    def test_top_occlusion_everywhere_keeps_bottom(self):
        result = segment_with_placement(WORDS, [OCC(0, 10000, "top")], **OPTS)
        self.assertEqual([s.position for s in result.segments], ["bottom"] * 3)
        self.assertAlmostEqual(result.total_cost, BASE_COST)

    def test_bottom_occlusion_everywhere_forces_top(self):
        result = segment_with_placement(WORDS, [OCC(0, 10000, "bottom")], **OPTS)
        self.assertEqual([s.position for s in result.segments], ["top"] * 3)
        self.assertAlmostEqual(result.total_cost, BASE_COST)

    def test_tie_break_prefers_bottom_over_top(self):
        # One subtitle, both regions free: identical cost and count, and the
        # position-sequence tie-break prefers bottom.
        words = [W("a", 0, 500), W("b", 500, 1400)]
        result = segment_with_placement(words, [], **OPTS)
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(result.segments[0].position, "bottom")

    def test_unsorted_and_overlapping_occlusions_are_accepted(self):
        result = segment_with_placement(
            WORDS,
            [OCC(5000, 6600, "top"), OCC(0, 1500, "bottom"), OCC(1000, 1600, "bottom")],
            **OPTS,
        )
        self.assertEqual(
            [s.position for s in result.segments], ["top", "bottom", "bottom"]
        )

    def test_max_line_chars_still_applies(self):
        words = [
            W("aaa", 0, 450),
            W("bbb", 450, 900),
            W("ccc", 900, 1350),
            W("ddd", 1350, 1800),
        ]
        result = segment_with_placement(
            words,
            [],
            **{**OPTS, "min_duration_ms": 0, "target_duration_ms": 2000,
               "max_line_chars": 7},
        )
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(result.segments[0].text, "aaa bbb\nccc ddd")
        self.assertEqual(result.segments[0].position, "bottom")

    def test_word_indices_and_coverage_are_preserved(self):
        result = segment_with_placement(WORDS, [OCC(1800, 4800, "bottom")], **OPTS)
        self.assertEqual(result.segments[0].word_start, 0)
        self.assertEqual(result.segments[-1].word_end, len(WORDS) - 1)
        for a, b in zip(result.segments, result.segments[1:]):
            self.assertEqual(b.word_start, a.word_end + 1)


class TestPlacementValidation(unittest.TestCase):
    def test_both_regions_blocked_is_infeasible(self):
        with self.assertRaisesRegex(SegmentationError, "available region"):
            segment_with_placement(
                WORDS,
                [OCC(1800, 4800, "bottom"), OCC(1800, 4800, "top")],
                **OPTS,
            )

    def test_unknown_region_rejected(self):
        with self.assertRaisesRegex(SegmentationError, "region"):
            segment_with_placement(WORDS, [OCC(0, 100, "middle")], **OPTS)

    def test_empty_interval_rejected(self):
        with self.assertRaisesRegex(SegmentationError, "start_ms < end_ms"):
            segment_with_placement(WORDS, [OCC(100, 100, "top")], **OPTS)

    def test_inverted_interval_rejected(self):
        with self.assertRaisesRegex(SegmentationError, "start_ms < end_ms"):
            segment_with_placement(WORDS, [OCC(200, 100, "top")], **OPTS)

    def test_no_words_rejected(self):
        with self.assertRaisesRegex(SegmentationError, "at least one word"):
            segment_with_placement([], [], **OPTS)

    def test_overlapping_words_rejected(self):
        words = [W("a", 0, 500), W("b", 400, 900)]
        with self.assertRaisesRegex(SegmentationError, "non-overlapping"):
            segment_with_placement(words, [], **OPTS)

    def test_impossible_min_duration_still_422(self):
        with self.assertRaisesRegex(SegmentationError, "no legal segmentation"):
            segment_with_placement([W("a", 0, 300)], [], **OPTS)


def _word(text, start_ms, end_ms):
    return {"text": text, "start_ms": start_ms, "end_ms": end_ms}


PLACE_BODY = {
    "words": [
        _word("Hello", 0, 500),
        _word("world.", 500, 1400),
        _word("How", 2000, 2400),
        _word("are", 2400, 2700),
        _word("you", 2700, 3200),
        _word("doing", 3200, 3800),
        _word("today?", 3800, 4600),
        _word("Fine", 5200, 5600),
        _word("thanks.", 5600, 6400),
    ],
    "occlusions": [{"start_ms": 1800, "end_ms": 4800, "region": "bottom"}],
    "max_chars": 42,
    "min_duration_ms": 1000,
    "max_duration_ms": 7000,
    "target_duration_ms": 3000,
    "pause_threshold_ms": 500,
}


class TestPlaceAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_place_json(self):
        r = self.client.post("/segment/place", json=PLACE_BODY)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["segment_count"], 3)
        self.assertAlmostEqual(data["total_cost"], 5.96)
        positions = [s["position"] for s in data["segments"]]
        self.assertEqual(positions, ["top", "top", "top"])
        first, _, last = data["segments"]
        self.assertEqual(first["text"], "Hello world.")
        self.assertEqual((first["word_start"], first["word_end"]), (0, 1))
        self.assertEqual((last["word_start"], last["word_end"]), (7, 8))
        for s in data["segments"]:
            self.assertLessEqual(len(s["text"]), 42)
            self.assertGreaterEqual(s["duration_ms"], 1000)
            self.assertLessEqual(s["duration_ms"], 7000)

    def test_place_without_occlusions_matches_plain_segment(self):
        body = {k: v for k, v in PLACE_BODY.items() if k != "occlusions"}
        r = self.client.post("/segment/place", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        placed = r.json()
        plain = self.client.post("/segment", json=body).json()
        self.assertEqual(placed["total_cost"], plain["total_cost"])
        for got, want in zip(placed["segments"], plain["segments"]):
            self.assertEqual(got["text"], want["text"])
            self.assertEqual(got["position"], "bottom")

    def test_place_vtt_uses_cue_line_positions(self):
        r = self.client.post("/segment/place/vtt", json=PLACE_BODY)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.headers["content-type"].startswith("text/vtt"))
        vtt = r.text
        self.assertTrue(vtt.startswith("WEBVTT\n"))
        self.assertIn("00:00:00.000 --> 00:00:01.400 line:0%", vtt)
        self.assertIn("00:00:05.200 --> 00:00:06.400 line:0%", vtt)
        self.assertIn("Hello world.", vtt)
        self.assertNotIn("line:100%", vtt)

    def test_place_vtt_positions_match_json(self):
        body = {
            **PLACE_BODY,
            "occlusions": [
                {"start_ms": 0, "end_ms": 1500, "region": "bottom"},
                {"start_ms": 5000, "end_ms": 6600, "region": "top"},
            ],
        }
        rj = self.client.post("/segment/place", json=body)
        rv = self.client.post("/segment/place/vtt", json=body)
        self.assertEqual(rj.status_code, 200, rj.text)
        self.assertEqual(rv.status_code, 200, rv.text)
        segs = rj.json()["segments"]
        self.assertEqual(
            [s["position"] for s in segs], ["top", "bottom", "bottom"]
        )
        blocks = rv.text.strip().split("\n\n")
        self.assertEqual(blocks[0], "WEBVTT")
        cues = blocks[1:]
        self.assertEqual(len(cues), len(segs))
        line_for = {"top": "line:0%", "bottom": "line:100%"}
        for cue, seg in zip(cues, segs):
            timing, *text_lines = cue.split("\n")
            self.assertIn(
                f"{format_vtt_timestamp(seg['start_ms'])} --> "
                f"{format_vtt_timestamp(seg['end_ms'])} "
                f"{line_for[seg['position']]}",
                timing,
            )
            self.assertEqual("\n".join(text_lines), seg["text"])

    def test_place_vtt_two_line_text(self):
        body = {
            "words": [
                _word("aaa", 0, 450),
                _word("bbb", 450, 900),
                _word("ccc", 900, 1350),
                _word("ddd", 1350, 1800),
            ],
            "min_duration_ms": 0,
            "target_duration_ms": 2000,
            "max_line_chars": 7,
        }
        r = self.client.post("/segment/place/vtt", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("line:100%\naaa bbb\nccc ddd\n", r.text)

    def test_422_when_no_region_available(self):
        body = {
            **PLACE_BODY,
            "occlusions": [
                {"start_ms": 1800, "end_ms": 4800, "region": "bottom"},
                {"start_ms": 1800, "end_ms": 4800, "region": "top"},
            ],
        }
        r = self.client.post("/segment/place", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("available region", r.json()["detail"])

    def test_422_on_unknown_region(self):
        body = {
            **PLACE_BODY,
            "occlusions": [{"start_ms": 0, "end_ms": 100, "region": "middle"}],
        }
        r = self.client.post("/segment/place", json=body)
        self.assertEqual(r.status_code, 422)

    def test_422_on_inverted_occlusion(self):
        body = {
            **PLACE_BODY,
            "occlusions": [{"start_ms": 500, "end_ms": 100, "region": "top"}],
        }
        r = self.client.post("/segment/place", json=body)
        self.assertEqual(r.status_code, 422)

    def test_422_on_negative_occlusion_ms(self):
        body = {
            **PLACE_BODY,
            "occlusions": [{"start_ms": -100, "end_ms": 500, "region": "top"}],
        }
        r = self.client.post("/segment/place", json=body)
        self.assertEqual(r.status_code, 422)

    def test_422_on_occlusion_missing_region(self):
        body = {**PLACE_BODY, "occlusions": [{"start_ms": 0, "end_ms": 100}]}
        r = self.client.post("/segment/place", json=body)
        self.assertEqual(r.status_code, 422)

    def test_place_vtt_422_matches_json_422(self):
        body = {
            **PLACE_BODY,
            "occlusions": [
                {"start_ms": 0, "end_ms": 10000, "region": "bottom"},
                {"start_ms": 0, "end_ms": 10000, "region": "top"},
            ],
        }
        r = self.client.post("/segment/place/vtt", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("available region", r.json()["detail"])

    def test_root_lists_place_endpoints(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        endpoints = r.json()["endpoints"]
        self.assertEqual(endpoints["segment_place_json"], "POST /segment/place")
        self.assertEqual(endpoints["segment_place_vtt"], "POST /segment/place/vtt")


if __name__ == "__main__":
    unittest.main()
