import pytest

from app import sequence as S


def E(**kw):
    base = {"restored": [], "range_ops": [], "word_edits": {}, "line_times": {}, "word_styles": {}, "overlays": []}
    base.update(kw)
    return base


def P(i, clip, a, b):
    return {"id": i, "clip": clip, "src": f"/{clip}.mp4", "name": clip, "in": a, "out": b}


def test_split_piece_middle_and_edge():
    ps = [P("a", "A", 0, 10)]
    new = P("n", "B", 0, 2)
    out = S.split_piece(ps, 4, new)
    assert [(p["clip"], p["in"], p["out"]) for p in out] == [("A", 0, 4), ("B", 0, 2), ("A", 4, 10)]
    assert out[0]["id"] == "a" and out[2]["id"] != "a" and out[2]["id"] != out[0]["id"]
    assert [p["clip"] for p in S.split_piece(ps, 0, new)] == ["B", "A"]
    assert [p["clip"] for p in S.split_piece(ps, 10, new)] == ["A", "B"]
    two = [P("a", "A", 0, 4), P("b", "B", 0, 3)]
    assert [p["clip"] for p in S.split_piece(two, 4, P("n", "C", 0, 1))] == ["A", "C", "B"]


def test_clip_spans():
    ps = [P("a", "A", 0, 2), P("b", "B", 5, 6), P("c", "C", 0, 1), P("d", "B", 6, 7), P("e", "A", 2, 4)]
    assert S.clip_spans(ps, "B") == [(2, 3, 5), (4, 5, 6)]


def test_insert_shifts_and_splits():
    e = E(range_ops=[{"op": "cut", "start": 1, "end": 5}], restored=[[0, 1], [3, 4]],
          overlays=[{"id": "o", "start": 2, "end": 4}], line_times={"3:0": [4, 5]})
    r = S.shift_for_insert(e, 3, 2)
    assert r["range_ops"] == [{"op": "cut", "start": 1, "end": 3}, {"op": "cut", "start": 5, "end": 7}]
    assert r["restored"] == [[0, 1], [5, 6]]
    assert r["overlays"][0]["start"] == 2 and r["overlays"][0]["end"] == 6
    assert r["line_times"] == {"3:0": [6, 7]}
    assert e["range_ops"][0]["end"] == 5  # 원본 불변


def test_insert_at_edges():
    e = E(range_ops=[{"op": "cut", "start": 1, "end": 5}], overlays=[{"id": "o", "start": 0, "end": 4}])
    r = S.shift_for_insert(e, 0, 2)
    assert r["range_ops"] == [{"op": "cut", "start": 3, "end": 7}]
    assert r["overlays"][0]["start"] == 2 and r["overlays"][0]["end"] == 6
    r = S.shift_for_insert(e, 5, 2)
    assert r["range_ops"] == e["range_ops"] and r["overlays"] == e["overlays"]


def test_remove_outer_keeps_inner():
    e = E(range_ops=[{"op": "cut", "start": 3, "end": 4}, {"op": "cut", "start": 2, "end": 3},
                     {"op": "restore", "start": 6, "end": 7}],
          overlays=[{"id": "o", "start": 2.5, "end": 4.5}, {"id": "p", "start": 2.2, "end": 2.8}],
          line_times={"1:0": [2.1, 2.9], "2:0": [3.1, 3.9], "3:0": [5.5, 6]})
    r = S.shift_for_remove(e, [(2, 3), (4, 5)])
    assert r["range_ops"] == [{"op": "cut", "start": 2, "end": 3}, {"op": "restore", "start": 4, "end": 5}]
    assert [(o["id"], o["start"], o["end"]) for o in r["overlays"]] == [("o", 2, 3)]
    assert r["line_times"] == {"2:0": [2.1, 2.9], "3:0": [3.5, 4]}


def test_remove_trims_partial():
    e = E(restored=[[1, 2.5], [2.5, 6]])
    r = S.shift_for_remove(e, [(2, 3)])
    assert r["restored"] == [[1, 2], [2, 5]]


TR = {"segments": [
    {"id": 0, "src": {"fkey": "A", "sid": 0, "w0": 0}, "words": [{}, {}]},
    {"id": 1, "src": {"fkey": "B", "sid": 3, "w0": 0}, "words": [{}, {}]},
    {"id": 2, "src": {"fkey": "B", "sid": 3, "w0": 2}, "words": [{}, {}, {}]},  # C 때문에 갈린 둘째 조각
    {"id": 3, "src": {"fkey": "A", "sid": 1, "w0": 2}, "words": [{}]},
], "pieces": [{"id": "a1", "clip": "main", "seg_from": 0, "seg_to": 1}, {"id": "b1", "clip": "cb", "seg_from": 1, "seg_to": 2},
              {"id": "c1", "clip": "cc", "seg_from": 2, "seg_to": 2}, {"id": "b2", "clip": "cb", "seg_from": 2, "seg_to": 3},
              {"id": "a2", "clip": "main", "seg_from": 3, "seg_to": 4}]}


def test_stable_roundtrip_across_split():
    keys = {"1:1": "x", "2:0": "y", "0:1": "z", "9:0": "none"}
    st = S.to_stable(keys, TR)
    assert st == {"cb|3|1": "x", "cb|3|2": "y", "main|0|1": "z"}
    new = {"segments": [
        {"id": 0, "src": {"fkey": "B", "sid": 3, "w0": 0}, "words": [{}, {}, {}]},
        {"id": 1, "src": {"fkey": "C", "sid": 0, "w0": 0}, "words": [{}]},
        {"id": 2, "src": {"fkey": "B", "sid": 3, "w0": 3}, "words": [{}, {}]},
    ], "pieces": [{"id": "b1", "clip": "cb", "seg_from": 0, "seg_to": 1}, {"id": "c1", "clip": "cc", "seg_from": 1, "seg_to": 2},
                  {"id": "b2", "clip": "cb", "seg_from": 2, "seg_to": 3}]}
    # 새 전사: cb|3|2 는 0번 세그먼트 어절 2, main 키는 사라짐
    assert S.from_stable(st, new) == {"0:1": "x", "0:2": "y"}
    assert S.from_stable(st, TR) == {"1:1": "x", "2:0": "y", "0:1": "z"}


def test_stable_same_file_two_clips():
    """같은 파일을 두 번 넣어도(같은 fkey) 영상 id 가 달라 키가 겹치지 않는다."""
    tr = {"segments": [{"id": 0, "src": {"fkey": "B", "sid": 0, "w0": 0}, "words": [{}]},
                       {"id": 1, "src": {"fkey": "B", "sid": 0, "w0": 0}, "words": [{}]}],
          "pieces": [{"id": "p", "clip": "b1", "seg_from": 0, "seg_to": 1}, {"id": "q", "clip": "b2", "seg_from": 1, "seg_to": 2}]}
    st = S.to_stable({"0:0": "x", "1:0": "y"}, tr)
    assert st == {"b1|0|0": "x", "b2|0|0": "y"}
    assert S.from_stable(st, tr) == {"0:0": "x", "1:0": "y"}


def test_extract_place_roundtrip():
    spans = [(2, 3, 5), (4, 5, 6)]  # 조각 B1 (파일 5~6), B2 (파일 6~7)
    e = E(range_ops=[{"op": "cut", "start": 1, "end": 2.5}, {"op": "cut", "start": 2.2, "end": 2.4},
                     {"op": "restore", "start": 4.5, "end": 8}],
          restored=[[2.5, 6]], overlays=[{"id": "o", "start": 2.5, "end": 4.5, "name": "n"}],
          line_times={"1:0": [2.1, 2.9], "0:0": [0.5, 1]})
    stable = {"word_edits": {"cb|3|1": "x", "main|0|1": "z"}, "line_times": {"cb|3|0": [2.1, 2.9], "main|0|0": [0.5, 1]},
              "word_styles": {"cb|3|1": [None]}}
    s = S.extract_clip(e, stable, spans, "cb")
    assert s["range_ops"] == [{"op": "cut", "start": 5, "end": 5.5}, {"op": "cut", "start": 5.2, "end": 5.4},
                              {"op": "restore", "start": 6.5, "end": 7}]
    assert s["restored"] == [[5.5, 6], [6, 7]]
    assert [(o["start"], o["end"]) for o in s["overlays"]] == [(5.5, 6.5)]
    assert s["word_edits"] == {"3|1": "x"} and s["word_styles"] == {"3|1": [None]}
    assert s["line_times"] == {"3|0": [5.1, 5.9]}
    p = S.place_clip(s, [(10, 11, 5), (12, 13, 6)], "nb")
    assert p["range_ops"][0] == {"op": "cut", "start": 10, "end": 10.5}
    assert p["range_ops"][2] == {"op": "restore", "start": 12.5, "end": 13}
    assert p["line_times"] == {"nb|3|0": [10.1, 10.9]}
    assert p["word_edits"] == {"nb|3|1": "x"} and p["word_styles"] == {"nb|3|1": [None]}
    # 겹치는 두 조각에 걸친 이미지는 둘로 나뉨
    assert [(o["start"], o["end"]) for o in p["overlays"]] == [(10.5, 11), (12, 12.5)]
    assert p["overlays"][0]["id"] == "o" and p["overlays"][1]["id"] not in ("o", None)
    # 같은 id 로 이어지는 조각은 extract 에서 하나로 합쳐진다
    again = S.extract_clip(E(overlays=[{**p["overlays"][0]}, {**p["overlays"][1], "id": "o"}]),
                           {}, [(10, 11, 5), (12, 13, 6)], "nb")
    assert [(o["start"], o["end"]) for o in again["overlays"]] == [(5.5, 6.5)]


def test_insert_line_crossing_is_clamped():
    r = S.shift_for_insert(E(line_times={"3:0": [2, 5]}), 3, 2)
    assert r["line_times"] == {"3:0": [2, 3]}


def test_to_stable_plain_is_main_roundtrip():
    plain = {"segments": [{"id": 0, "words": [{}, {}]}, {"id": 1, "words": [{}]}]}
    keys = {"0:1": "x", "1:0": "y"}
    st = S.to_stable(keys, plain)
    assert st == {"main|0|1": "x", "main|1|0": "y"}
    comb = {"segments": [
        {"id": 0, "src": {"fkey": "A", "sid": 0, "w0": 0}, "words": [{}, {}]},
        {"id": 1, "src": {"fkey": "B", "sid": 0, "w0": 0}, "words": [{}]},
        {"id": 2, "src": {"fkey": "A", "sid": 1, "w0": 0}, "words": [{}]},
    ], "pieces": [{"id": "a1", "clip": "main", "seg_from": 0, "seg_to": 1}, {"id": "b", "clip": "cb", "seg_from": 1, "seg_to": 2},
                  {"id": "a2", "clip": "main", "seg_from": 2, "seg_to": 3}]}
    assert S.from_stable(st, comb) == {"0:1": "x", "2:0": "y"}


def test_piece_bounds_and_block_span():
    ps = [P("a", "main", 0, 4), P("b", "B", 0, 1), P("c", "C", 0, 2), P("d", "B", 1, 3), P("e", "main", 4, 10)]
    assert S.piece_bounds(ps) == [4, 5, 7, 9]
    assert S.block_span(ps, "B") == (4, 9)
    assert S.block_span(ps, "C") == (5, 7)
    assert S.block_span(ps, "X") is None


def test_snap_prefers_piece_boundary_in_cut():
    fps = 30.0
    # 경계 5.0 이 잘린 구간 (4.6, 5.5) 안 → 가장 가까운 경계
    assert S.snap_at(5.3, [(4.6, 5.5)], [5.0], fps, 10) == 5.0
    # 경계 없는 잘린 구간 → 구간 시작 (지금 넣기 규칙)
    assert S.snap_at(3.0, [(2.6, 3.4)], [5.0], fps, 10) == pytest.approx(2.6)
    # 잘린 구간 밖 → 격자만
    assert S.snap_at(4.01, [], [], fps, 10) == pytest.approx(4.0)
    # 범위 밖은 [0, total]
    assert S.snap_at(-1, [], [], fps, 10) == 0 and S.snap_at(99, [], [], fps, 10) == 10
    # 한 잘린 구간 안 경계 둘 → t 에 가까운 쪽
    assert S.snap_at(6.9, [(4.5, 7.5)], [5.0, 7.0], fps, 10) == 7.0


def test_move_block_after_and_before():
    ps = [P("a", "main", 0, 4), P("b", "B", 0, 2), P("e", "main", 4, 10)]
    out, t2 = S.move_block(ps, "B", 8)           # 뒤로: 8 - 2
    assert t2 == 6 and [(p["clip"], p["in"], p["out"]) for p in out] == \
        [("main", 0, 4), ("main", 4, 6), ("B", 0, 2), ("main", 6, 10)]
    out, t2 = S.move_block(ps, "B", 1)           # 앞으로
    assert t2 == 1 and [p["clip"] for p in out] == ["main", "B", "main", "main"]


def test_move_block_carries_nested():
    ps = [P("a", "main", 0, 4), P("b", "B", 0, 1), P("c", "C", 0, 2), P("d", "B", 1, 3), P("e", "main", 4, 10)]
    out, t2 = S.move_block(ps, "B", 10)
    assert t2 == 5 and [p["clip"] for p in out] == ["main", "main", "B", "C", "B", "main"]


def test_shift_for_move_inside_follows():
    e = E(range_ops=[{"op": "cut", "start": 4.5, "end": 5.0}], line_times={"9:0": [4.25, 4.75]},
          overlays=[{"id": "in", "start": 4.125, "end": 4.875}], word_edits={"x": 1})
    r = S.shift_for_move(e, 4, 6, 7)             # 덩어리 4~6 → 옮긴 뒤 7 에서 시작
    assert r["range_ops"] == [{"op": "cut", "start": 7.5, "end": 8.0}]
    assert r["line_times"] == {"9:0": [7.25, 7.75]}
    assert [(o["start"], o["end"]) for o in r["overlays"]] == [(7.125, 7.875)]
    assert r["word_edits"] == {"x": 1}


def test_shift_for_move_straddlers():
    e = E(range_ops=[{"op": "cut", "start": 3, "end": 5}],          # 경계 4 를 넘음
          overlays=[{"id": "o", "start": 2, "end": 5}],              # 덩어리에 걸침
          restored=[[8, 9]])                                          # 바깥, 옮긴 자리 뒤
    r = S.shift_for_move(e, 4, 6, 7)
    assert sorted((o["start"], o["end"]) for o in r["range_ops"]) == [(3, 4), (7, 8)]
    assert [(o["start"], o["end"]) for o in r["overlays"]] == [(2, 4)]
    assert r["restored"] == [[6, 7]]
