from app import options


def test_analysis_args_defaults():
    assert options.analysis_args(options.JobOptions()) == ["--filler", "on", "--habit", "on", "--silence", "on"]


def test_analysis_args_all_habit_keep_filler():
    o = options.JobOptions(filler="keep", habit="all", silence="keep")
    assert options.analysis_args(o) == ["--filler", "off", "--habit", "aggressive", "--silence", "off"]


def test_habit_keep():
    assert options.analysis_args(options.JobOptions(habit="keep"))[3] == "off"


def test_render_args():
    o = options.JobOptions(denoise="keep", loudnorm="keep", subs="omit", quality="low")
    assert options.render_args(o) == ["--render-denoise", "none", "--render-loudnorm", "off",
                                      "--embed-subs", "off", "--vbitrate-scale", "0.6"]


def test_render_args_defaults():
    assert options.render_args(options.JobOptions()) == ["--render-denoise", "rnnoise", "--render-loudnorm", "on",
                                                         "--embed-subs", "on", "--vbitrate-scale", "1.0"]


def test_dict_roundtrip():
    o = options.JobOptions(habit="all", quality="high")
    assert options.JobOptions.from_dict(o.to_dict()) == o
    assert options.JobOptions.from_dict({"bogus": 1, "filler": "keep"}).filler == "keep"


def test_fast_keys():
    assert options.FAST_KEYS == {"filler", "habit", "silence"}
