import pytest

from app import options


def test_analysis_args_defaults():
    assert options.analysis_args(options.JobOptions()) == [
        "--filler", "on", "--habit", "on", "--silence", "on", "--silence-level", "50", "--denoise-level", "50"]


def test_analysis_args_all_habit_keep_filler():
    o = options.JobOptions(filler="keep", habit="all", silence=0)
    assert options.analysis_args(o) == ["--filler", "off", "--habit", "aggressive", "--silence", "off",
                                        "--denoise-level", "50"]


def test_habit_keep():
    assert options.analysis_args(options.JobOptions(habit="keep"))[3] == "off"


def test_analysis_args_levels():
    a = options.analysis_args(options.JobOptions(silence=0, denoise=0))
    assert a[a.index("--silence") + 1] == "off" and "--silence-level" not in a
    assert a[a.index("--denoise-level") + 1] == "0"
    a = options.analysis_args(options.JobOptions(silence=70, denoise=80))
    assert a[a.index("--silence") + 1] == "on" and a[a.index("--silence-level") + 1] == "70"
    assert a[a.index("--denoise-level") + 1] == "80"


def test_render_args():
    o = options.JobOptions(denoise=0, loudnorm="keep", subs="omit", quality="low")
    assert options.render_args(o) == ["--denoise-level", "0", "--render-loudnorm", "off",
                                      "--embed-subs", "off", "--vbitrate-scale", "0.6"]


def test_render_args_defaults():
    assert options.render_args(options.JobOptions()) == ["--denoise-level", "50", "--render-loudnorm", "on",
                                                         "--embed-subs", "on", "--vbitrate-scale", "1.0"]


def test_dict_roundtrip():
    o = options.JobOptions(habit="all", quality="high", silence=70, denoise=20)
    assert options.JobOptions.from_dict(o.to_dict()) == o
    assert options.JobOptions.from_dict({"bogus": 1, "filler": "keep"}).filler == "keep"


def test_old_options_migrate():
    o = options.JobOptions.from_dict({"silence": "keep", "denoise": "remove"})
    assert (o.silence, o.denoise) == (0, 50)
    o = options.JobOptions.from_dict({"silence": "remove", "denoise": "keep"})
    assert (o.silence, o.denoise) == (50, 0)
    o = options.JobOptions.from_dict({"silence": "70", "denoise": 150})
    assert (o.silence, o.denoise) == (70, 100)
    o = options.JobOptions.from_dict({"silence": -5, "denoise": "junk"})
    assert (o.silence, o.denoise) == (0, 50)   # 알 수 없는 값은 기본값
    assert options.JobOptions().silence == 50 and options.JobOptions().denoise == 50


@pytest.mark.parametrize("value,expected", [(0, 0), (80, 80), (80.0, 80), ("80", 80), (" 42 ", 42),
                                            (150, 100), (-3, 0), ("150", 100), (49.6, 50)])
def test_parse_level(value, expected):
    assert options.parse_level(value) == expected


@pytest.mark.parametrize("value", ["abc", "", None, True, float("nan"), float("inf"), [50]])
def test_parse_level_rejects(value):
    with pytest.raises(ValueError):
        options.parse_level(value)


def test_fast_keys():
    assert options.FAST_KEYS == {"filler", "habit", "silence", "denoise"}
