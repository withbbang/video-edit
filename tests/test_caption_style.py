"""자막 글자 꾸밈과 유튜브용 자막 새기기: 옵션 인자, 꾸밈 API·되돌리기·저장 파일, ASS 만들기, 화면 계산(capstyle.js)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import options, paths, subtitles
from app.api import save_subs_edits
from tests.test_subtitles import setup

ROOT = Path(__file__).resolve().parent.parent
CAPSTYLE = ROOT / "app" / "ui" / "capstyle.js"
pl = subtitles.pipeline_module()


# ---- 옵션 → 파이프라인 인자 ------------------------------------------------------
def test_render_args_burn_turns_off_track():
    o = options.JobOptions(burn="on", subs="embed")
    args = options.render_args(o, Path("/f"))
    assert args[args.index("--embed-subs") + 1] == "off"          # 새긴 자막 + 트랙 = 두 번 보임
    assert args[args.index("--burn-subs") + 1] == "on"
    assert args[args.index("--burn-fonts") + 1] == str(Path("/f"))
    assert "--burn-subs" not in options.render_args(options.JobOptions(), Path("/f"))


def test_old_job_options_default_no_burn():
    assert options.JobOptions.from_dict({"subs": "omit"}).burn == "off"


def test_fonts_dir_has_every_font():
    css = (ROOT / "app" / "ui" / "style.css").read_text(encoding="utf-8")
    js = CAPSTYLE.read_text(encoding="utf-8")
    families = re.findall(r"family: '([^']+)'", js)
    assert pl.BURN_FONT in families
    for fam in families:
        assert f'font-family: "{fam}"' in css
    for src in re.findall(r'url\("fonts/([^"]+)"\)', css):
        assert (paths.fonts_dir() / src).is_file()


# ---- 꾸밈 API -----------------------------------------------------------------
def _word(r, key):
    return next(w for c in r["captions"] for w in c["words"] if w["key"] == key)


def test_set_style_per_char_and_undo(tmp_path):
    a, store, job, _ = setup(tmp_path)
    r = a.set_style(job.id, [["0:0", 1, 3]], {"color": "#FF0000", "bold": True})
    w = _word(r, "0:0")                                          # "안녕하세요"
    assert w["style"] == [None, {"color": "#ff0000", "bold": True}, {"color": "#ff0000", "bold": True}, None, None]
    assert store.get(job.id).edits_sha1                          # 받아쓰기 지문을 같이 남김
    r = a.set_style(job.id, [["0:0", 2, 3]], {"bold": False, "size": 1.5})
    assert _word(r, "0:0")["style"][2] == {"color": "#ff0000", "size": 1.5}
    r = a.set_style(job.id, [["0:0", 0, 5]], None)               # 꾸밈 지우기
    assert _word(r, "0:0")["style"] is None
    assert store.get(job.id).word_styles == {} and store.get(job.id).edits_sha1 is None
    a.undo(job.id)
    assert store.get(job.id).word_styles["0:0"][2] == {"color": "#ff0000", "size": 1.5}


def test_set_style_rejects_bad_input(tmp_path):
    a, _, job, _ = setup(tmp_path)
    assert a.set_style(job.id, [["9:9", 0, 1]], {"bold": True}) is None   # 없는 어절
    assert a.set_style(job.id, [["0:0", 2, 1]], {"bold": True}) is None
    assert a.set_style(job.id, [["0:0", 0, 1]], {"weird": 1}) is None
    assert a.set_style(job.id, [], {"bold": True}) is None
    r = a.set_style(job.id, [["0:0", 0, 1]], {"color": "red", "size": 99})   # 잘못된 색은 버리고 크기는 자름
    assert _word(r, "0:0")["style"][0] == {"size": pl.STYLE_SIZE_RANGE[1]}


def test_styles_survive_word_edit_by_index(tmp_path):
    a, _, job, _ = setup(tmp_path)
    a.set_style(job.id, [["0:1", 0, 1]], {"color": "#00ff00"})
    r = a.set_word(job.id, "0:1", "여러분들")
    assert _word(r, "0:1")["style"] == [{"color": "#00ff00"}, None, None, None]


def _line(r, key):
    return next(c for c in r["captions"] if c["key"] == key)


def test_plan_line_maps_tokens_to_words():
    W = lambda *t: [{"key": f"k{i}", "text": x} for i, x in enumerate(t)]   # noqa: E731
    assert subtitles.plan_line(W("오늘은", "콜럼버스데이고요."), "오늘은 콜럼버스 데이고요.", {}) == (
        {"k1": "콜럼버스 데이고요."}, {})
    assert subtitles.plan_line(W("안녕", "하세요"), "안녕하세요", {})[0] == {"k0": "안녕하세요", "k1": ""}
    assert subtitles.plan_line(W("a", "b", "c"), "x a b c", {})[0] == {"k0": "x a"}
    assert subtitles.plan_line(W("a", "", "c"), "a new c", {})[0] == {"k0": "a new"}   # 뺀 어절은 그대로
    assert subtitles.plan_line(W("", ""), "되살림", {})[0] == {"k0": "되살림"}
    # 꾸밈은 글자를 따라간다: 끼운 글자는 앞 글자 꾸밈
    red, b = {"color": "#f00"}, {"bold": True}
    assert subtitles.plan_line(W("가나다"), "가나X다", {"k0": [None, red, b]})[1] == {"k0": [None, red, red, b]}


def test_set_line_edits_whole_line_and_restores(tmp_path):
    a, store, job, _ = setup(tmp_path)
    a.set_style(job.id, [["0:0", 0, 2]], {"color": "#ff0000"})
    r = a.set_line(job.id, "0:0", "안녕 하세요  여러분들")
    words = {w["key"]: w for w in _line(r, "0:0")["words"]}
    assert words["0:0"]["text"] == "안녕" and words["0:1"]["text"] == "하세요 여러분들"   # 바뀐 묶음: 앞 어절부터
    assert words["0:0"]["style"][:2] == [{"color": "#ff0000"}] * 2       # 꾸밈은 남은 글자를 따라감
    assert store.get(job.id).word_edits == {"0:0": "안녕", "0:1": "하세요 여러분들"}
    r = a.set_line(job.id, "0:0", None)                                   # 줄 글자 원래대로
    assert [w["text"] for w in _line(r, "0:0")["words"]] == ["안녕하세요", "여러분"]
    assert store.get(job.id).word_edits == {}
    a.undo(job.id)
    assert store.get(job.id).word_edits == {"0:0": "안녕", "0:1": "하세요 여러분들"}
    assert a.set_line(job.id, "9:9", "x") is None and a.set_line(job.id, "0:0", 3) is None


def test_save_subs_edits_writes_styles(tmp_path):
    a, store, job, _ = setup(tmp_path)
    a.set_style(job.id, [["0:0", 0, 2]], {"font": "Jua"})
    data = json.loads(save_subs_edits(store.get(job.id)).read_text(encoding="utf-8"))
    assert data["styles"] == {"0:0": [{"font": "Jua"}, {"font": "Jua"}]} and data["words"] == {}
    tr = json.loads((paths.job_dir(Path(job.src)) / "out" / "a.transcript.json").read_text(encoding="utf-8"))
    p = save_subs_edits(store.get(job.id))
    assert pl.load_subs_styles(p, tr) == {"0:0": [{"font": "Jua"}, {"font": "Jua"}]}
    assert pl.load_subs_edits(p, tr) == ({}, {})


def test_stale_transcript_drops_styles(tmp_path):
    a, store, job, _ = setup(tmp_path)
    a.set_style(job.id, [["0:0", 0, 1]], {"bold": True})
    store.get(job.id).edits_sha1 = "old"
    a.get_captions(job.id)
    assert store.get(job.id).word_styles == {}


# ---- 새기는 자막 (ASS) ------------------------------------------------------------
def test_clean_style():
    assert pl.clean_style({"font": "Jua", "size": 1.0, "color": "#ABCDEF", "bold": False}) == {"font": "Jua", "color": "#abcdef"}
    assert pl.clean_style({"font": "a{b", "size": True, "color": "#123"}) is None
    assert pl.clean_styles({"k": [None, {"bold": True}, None, {"x": 1}], "e": [None], 3: [{}]}) == {"k": [None, {"bold": True}]}


def _caps(*words):
    return [{"key": words[0][0], "words": [{"key": k, "text": t} for k, t in words], "start": 0.0, "end": 1.0}]


def test_ass_line_runs_and_escape():
    c = _caps(("a", "오늘은"), ("b", "{x}"))[0]
    styles = {"a": [{"color": "#ff0000", "size": 2}, {"color": "#ff0000", "size": 2}, {"bold": True}]}
    line = pl.ass_line(c, styles, 54)
    assert line == "{\\fs108\\c&H0000FF&}오늘{\\r}{\\b1}은{\\r} ｛x｝"


def test_build_ass_scales_with_video():
    ass = pl.build_ass([(1.5, 3.25, _caps(("a", "안녕"))[0])], {}, 1080, 1920)   # 세로 영상: 짧은 변 기준
    assert "PlayResX: 1080\nPlayResY: 1920" in ass
    assert f"Style: Default,{pl.BURN_FONT},54," in ass
    assert "Dialogue: 0,0:00:01.50,0:00:03.25,Default,,0,0,0,,안녕" in ass


def test_caption_cues_match_srt():
    tr = {"segments": [{"id": 0, "words": [{"word": " 가", "start": 0.0, "end": 0.5}, {"word": " 나.", "start": 0.6, "end": 1.0},
                                            {"word": " 다", "start": 3.0, "end": 3.5}]}]}
    remap = pl.TimeRemap([])
    caps = pl.build_captions(tr, set(), remap)
    cues = pl.caption_cues(caps, remap, 10.0)
    srt = pl.captions_to_srt(caps, remap, 10.0)
    assert [pl.caption_text(c) for _, _, c in cues] == ["가 나.", "다"]
    assert pl.fmt_srt_time(cues[1][0]) in srt


def test_ffmpeg_filter_path_escapes():
    assert pl.ffmpeg_filter_path(Path("/a/b c.ass")) == "/a/b c.ass"
    assert pl.ffmpeg_filter_path("C:\\x\\it's,[1].ass") == "C\\\\:/x/it\\\\\\'s\\,\\[1\\].ass"


def _ffmpeg_with_ass():
    for exe in (ROOT / "packaging" / "bin" / "ffmpeg", shutil.which("ffmpeg")):
        if exe and Path(exe).is_file():
            out = subprocess.run([str(exe), "-hide_banner", "-filters"], capture_output=True, text=True).stdout
            if re.search(r"\sass\s", out):
                return str(exe)
    return None


@pytest.mark.skipif(_ffmpeg_with_ass() is None, reason="libass 가 든 ffmpeg 필요")
def test_burn_draws_styled_text(tmp_path):
    """글꼴 폴더의 글꼴로 영상 아래쪽에 그려지는지 (경로에 : , 가 있어도)."""
    import numpy as np
    d = tmp_path / "a:b,c"
    d.mkdir()
    ass = d / "s.ass"
    c = _caps(("a", "자막"), ("b", "새기기"))[0]
    ass.write_text(pl.build_ass([(0.0, 2.0, c)], {"b": [{"color": "#ffd60a", "font": "Jua", "size": 1.5}] * 3}, 640, 360),
                   encoding="utf-8")
    vf = f"ass=filename={pl.ffmpeg_filter_path(ass)}:fontsdir={pl.ffmpeg_filter_path(paths.fonts_dir())}"
    raw = subprocess.run([_ffmpeg_with_ass(), "-v", "error", "-f", "lavfi", "-i", "color=c=0x202020:s=640x360:d=1",
                          "-vf", vf, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    img = np.frombuffer(raw, np.uint8).reshape(360, 640, 3)
    top, bottom = img[:180], img[240:]
    assert (top == top[0, 0]).all()                              # 위쪽은 그대로
    yellow = (bottom[..., 0] > 200) & (bottom[..., 1] > 180) & (bottom[..., 2] < 80)
    assert yellow.sum() > 30 and ((bottom > 200).all(axis=2)).sum() > 30   # 노란 글자 + 흰 글자


# ---- 화면 계산 (capstyle.js, node) ------------------------------------------------
def run_js(body: str):
    if not shutil.which("node"):
        pytest.skip("node 필요")
    code = f"const C = require({json.dumps(str(CAPSTYLE))});\nlet out;\n{body}\nprocess.stdout.write(JSON.stringify(out));"
    res = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_js_base_matches_pipeline():
    b = run_js("out = C.BASE;")
    assert b == {"font": pl.BURN_FONT, "size": pl.BURN_SIZE, "outline": pytest.approx(pl.BURN_OUTLINE),
                 "marginV": pl.BURN_MARGIN_V, "marginH": pl.BURN_MARGIN_H}
    sizes = run_js("out = C.SIZES;")
    assert pl.STYLE_SIZE_RANGE[0] <= min(sizes) and max(sizes) <= pl.STYLE_SIZE_RANGE[1]


def test_js_content_box_and_layout():
    c = run_js("out = C.contentBox(1000, 1000, 1920, 1080);")      # 위아래 검은 띠
    assert c["w"] == pytest.approx(1000) and c["h"] == pytest.approx(562.5) and c["y"] == pytest.approx(218.75)
    L = run_js("out = C.overlayLayout({x: 0, y: 0, w: 1920, h: 1080});")
    assert L["fs"] == pytest.approx(54) and L["bottom"] == pytest.approx(1080 * 0.94) and L["left"] == pytest.approx(96)
    assert run_js("out = C.contentBox(0, 10, 1, 1);") is None


def test_js_runs_common_ranges_steps():
    r = run_js("out = C.runs(['가','나','다','라'], [null, {color:'#f00'}, {color:'#f00'}, null]);")
    assert [(x["text"], x["i"]) for x in r] == [("가", 0), ("나다", 1), ("라", 3)]
    cm = run_js("out = C.common([null, {bold: true}]);")
    assert cm == {"font": "NanumGothic", "size": 1, "color": "#ffffff"}            # bold 는 서로 달라 빠짐
    assert run_js("out = C.toRanges([['a',0],['a',1],['a',3],['b',0]]);") == [["a", 0, 2], ["a", 3, 4], ["b", 0, 1]]
    assert run_js("out = [C.stepSize(1, 1), C.stepSize(1, -1), C.stepSize(1.1, 1), C.stepSize(2.5, 1), C.stepSize(0.6, -1)];") \
        == [1.25, 0.8, 1.25, 2.5, 0.6]
