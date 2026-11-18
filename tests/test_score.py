"""Unit tests for scoring math: z-score, NMS, weighted composite."""

from __future__ import annotations

import json

from clipsys.score import _keyword_hits, _nms, _zscore


class TestZscore:
    def test_basic(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        z = _zscore(vals)
        assert len(z) == 5
        # Mean of z-scores should be ~0
        assert abs(sum(z) / len(z)) < 1e-9
        # Highest value should have highest z
        assert z[-1] > z[0]

    def test_constant_signal(self):
        # All same value: std=0, should return zeros without crashing
        z = _zscore([5.0, 5.0, 5.0])
        assert z == [0.0, 0.0, 0.0]

    def test_single_value(self):
        z = _zscore([42.0])
        assert z == [0.0]

    def test_empty(self):
        assert _zscore([]) == []

    def test_two_values(self):
        z = _zscore([0.0, 1.0])
        assert len(z) == 2
        assert z[0] < 0 and z[1] > 0


class TestNMS:
    def test_basic_suppression(self):
        # window=45: 20 beats 25 (within 5s); 80 is 60s from 20 → survives
        moments = [(10.0, 2.0), (20.0, 3.0), (25.0, 1.5), (80.0, 1.0)]
        kept = _nms(moments, window_s=45.0)
        kept_times = {t for t, _ in kept}
        assert 20.0 in kept_times
        assert 25.0 not in kept_times
        assert 80.0 in kept_times

    def test_empty(self):
        assert _nms([], 45.0) == []

    def test_no_suppression(self):
        moments = [(0.0, 1.0), (60.0, 2.0), (120.0, 3.0)]
        kept = _nms(moments, window_s=45.0)
        assert len(kept) == 3

    def test_all_suppressed_to_one(self):
        moments = [(10.0, 1.0), (11.0, 2.0), (12.0, 1.5)]
        kept = _nms(moments, window_s=45.0)
        # Only the highest (11.0, 2.0) should survive
        assert len(kept) == 1
        assert kept[0][0] == 11.0

    def test_sorted_descending(self):
        moments = [(5.0, 0.5), (50.0, 2.0), (100.0, 1.0)]
        kept = _nms(moments, window_s=30.0)
        scores = [s for _, s in kept]
        assert scores == sorted(scores, reverse=True)


class TestKeywordHits:
    def _segs(self, entries: list[tuple[float, float, str]]) -> str:
        return json.dumps([{"start": s, "end": e, "text": t} for s, e, t in entries])

    def test_basic(self):
        segs = self._segs([(0.0, 5.0, "big explosion nearby"), (10.0, 15.0, "nothing here")])
        result = _keyword_hits(segs, ["explosion", "race"], window_s=10.0)
        # Bucket center for t=2.5 is 5.0 (bin 0-10, center 5)
        assert any(v > 0 for v in result.values())

    def test_no_hits(self):
        segs = self._segs([(0.0, 5.0, "calm driving")])
        result = _keyword_hits(segs, ["explosion", "heist"], window_s=10.0)
        assert all(v == 0 for v in result.values()) or result == {}

    def test_multiple_hits_accumulate(self):
        segs = self._segs([
            (0.0, 5.0, "explosion and crash and chase"),
        ])
        result = _keyword_hits(segs, ["explosion", "crash", "chase"], window_s=10.0)
        total = sum(result.values())
        assert total == 3

    def test_case_insensitive(self):
        segs = self._segs([(0.0, 5.0, "EXPLOSION happened")])
        result = _keyword_hits(segs, ["explosion"], window_s=10.0)
        assert sum(result.values()) == 1
