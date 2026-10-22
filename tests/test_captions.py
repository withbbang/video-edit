import pipeline


def _tr():
    def seg(sid, spec):
        return {"id": sid, "words": [{"word": w, "start": s, "end": e} for w, s, e in spec]}
    return {"segments": [
        seg(0, [(" 안녕하세요", 0.0, 0.5), (" 어", 0.6, 0.8), (" 여러분", 0.9, 1.4),
                (" 오늘은.", 1.5, 2.0), (" 영상을", 2.1, 2.6)]),
        seg(1, [(" 만들어", 4.0, 4.4), (" 보겠습니다", 4.5, 5.2), (" 정말정말정말", 5.3, 5.9),
                (" 긴문장을", 6.0, 6.4), (" 이어서", 6.5, 6.9)]),
        seg(2, [(" 말씀", 9.0, 9.4), (" 드리면", 9.5, 9.9), (" 그니까", 10.0, 10.4),
                (" 끝입니다", 10.5, 11.0), (" 감사", 11.1, 11.5)]),
        # 컷으로 안 사라지는 0.7초 넘는 멈춤(11.5 -> 12.5) 뒤, 5초 넘게 이어지는 줄
        seg(3, [(" 가나", 12.5, 13.5), (" 다라", 13.6, 14.6), (" 마바", 14.7, 15.7),
                (" 사아", 15.8, 16.8), (" 자차", 16.9, 17.9), (" 카타", 18.0, 19.0)]),
    ]}


def _cuts():
    return [
        pipeline.Cut(0.6, 0.85, "어", "filler", "", 0, words_idx=[[0, 1]]),
        pipeline.Cut(2.7, 3.9, "", "silence", "", 0),
        pipeline.Cut(7.0, 8.9, "", "silence", "", 1),
        pipeline.Cut(10.0, 10.45, "그니까", "habit", "", 2, words_idx=[[2, 2]]),
    ]


FILLERS = {"어"}
FINAL = 30.0
# 리팩터링 전 build_srt 가 낸 출력을 그대로 박아 둔 값
EXPECTED = '1\n00:00:00,000 --> 00:00:01,750\n안녕하세요 여러분 오늘은.\n\n2\n00:00:01,850 --> 00:00:04,950\n영상을 만들어 보겠습니다 정말정말정말 긴문장을\n\n3\n00:00:05,050 --> 00:00:07,700\n이어서 말씀 드리면 끝입니다 감사\n\n4\n00:00:08,700 --> 00:00:13,000\n가나 다라 마바 사아\n\n5\n00:00:13,100 --> 00:00:15,200\n자차 카타\n\n'


def _caps(**kw):
    return pipeline.build_captions(_tr(), set(pipeline.TimeRemap(_cuts()).cut_words),
                                   pipeline.TimeRemap(_cuts()), FILLERS, set(), **kw)


def _srt(**kw):
    return pipeline.build_srt(_tr(), pipeline.TimeRemap(_cuts()), FINAL, FILLERS, set(), **kw)


def test_build_srt_unchanged():
    assert _srt() == EXPECTED


def test_captions_keys_and_original_times():
    caps = _caps()
    assert caps[0]["key"] == "0:0"
    assert caps[0]["start"] == 0.0
    assert [w["key"] for w in caps[0]["words"]][:2] == ["0:0", "0:2"]
    assert all(c["key"] == c["words"][0]["key"] for c in caps)
    assert caps[0]["words"][0]["orig"] == "안녕하세요"
    last = caps[-1]
    assert last["end"] == last["words"][-1]["end"]


def test_word_edit_changes_text_not_grouping():
    before = [c["key"] for c in _caps()]
    caps = _caps(word_edits={"0:2": "아주아주아주긴글자로바뀐어절"})
    assert [c["key"] for c in caps] == before
    assert any(w["text"] == "아주아주아주긴글자로바뀐어절" for c in caps for w in c["words"])


def test_empty_edit_drops_word():
    caps = _caps(word_edits={"0:2": ""})
    assert "0:2" not in [w["key"] for c in caps for w in c["words"]]
    srt = pipeline.captions_to_srt(caps, pipeline.TimeRemap(_cuts()), FINAL)
    assert "여러분" not in srt


def test_line_time_fixed():
    remap = pipeline.TimeRemap(_cuts())
    caps = _caps()
    key = caps[1]["key"]
    srt = pipeline.captions_to_srt(caps, remap, FINAL, {key: [2.0, 4.0]})
    blocks = [b.split("\n") for b in srt.strip().split("\n\n")]
    assert blocks[1][1] == f"{pipeline.fmt_srt_time(remap(2.0))} --> {pipeline.fmt_srt_time(remap(4.0))}"
    prev_end = blocks[0][1].split(" --> ")[1]
    assert prev_end <= pipeline.fmt_srt_time(remap(2.0) - 0.05)


def test_line_key_stable_when_first_word_emptied():
    remap = pipeline.TimeRemap(_cuts())
    before = _caps()
    first = before[0]["key"]
    caps = _caps(word_edits={first: ""})
    assert [c["key"] for c in caps] == [c["key"] for c in before]
    assert first not in [w["key"] for w in caps[0]["words"]]
    srt = pipeline.captions_to_srt(caps, remap, FINAL, {first: [0.0, 1.0]})
    assert srt.split("\n")[1] == f"{pipeline.fmt_srt_time(remap(0.0))} --> {pipeline.fmt_srt_time(remap(1.0))}"


def _edits_file(tmp_path, sha):
    import json
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"transcript_sha1": sha, "words": {"0:0": "안녕"},
                             "lines": {"0:0": [0.0, 1.0]}}), encoding="utf-8")
    return p


def test_load_subs_edits_matches_sha(tmp_path):
    tr = _tr()
    p = _edits_file(tmp_path, pipeline.transcript_sha1(tr))
    assert pipeline.load_subs_edits(p, tr) == ({"0:0": "안녕"}, {"0:0": [0.0, 1.0]})


def test_load_subs_edits_stale_ignored(tmp_path):
    assert pipeline.load_subs_edits(_edits_file(tmp_path, "nope"), _tr()) == ({}, {})


def test_load_subs_edits_none():
    assert pipeline.load_subs_edits(None, _tr()) == ({}, {})
