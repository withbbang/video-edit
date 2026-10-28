"""작업별 옵션과 파이프라인 인자 변환."""
import math
from dataclasses import asdict, dataclass, fields

# 바꾸면 컷을 다시 판정한다 (잡음은 미리보기 소리를 다시 만든다). 나머지는 저장할 때만 쓴다.
FAST_KEYS = {"filler", "habit", "silence", "denoise"}
LEVEL_KEYS = ("silence", "denoise")   # 0~100 슬라이더. 50 = 예전 "remove", 0 = 끔
LEVEL_DEFAULT = 50
_OLD_LEVEL = {"keep": 0, "remove": 50}

_HABIT = {"keep": "off", "some": "on", "all": "aggressive"}
_ONOFF = {"remove": "on", "keep": "off"}
_QUALITY = {"low": "0.6", "auto": "1.0", "high": "1.5"}


def parse_level(value) -> int:
    """슬라이더 값 → 0~100 정수. int·정수 float·숫자 문자열을 받고 범위 밖은 자른다. 숫자가 아니면 ValueError."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"숫자가 아님: {value!r}")
    try:
        f = float(value)
    except ValueError:
        raise ValueError(f"숫자가 아님: {value!r}") from None
    if not math.isfinite(f):
        raise ValueError(f"숫자가 아님: {value!r}")
    return max(0, min(100, int(round(f))))


@dataclass
class JobOptions:
    filler: str = "remove"
    habit: str = "some"      # keep|some|all
    silence: int = LEVEL_DEFAULT   # 0~100 (0 = 안 자름)
    denoise: int = LEVEL_DEFAULT   # 0~100 (0 = 끔)
    loudnorm: str = "match"  # keep|match
    subs: str = "embed"      # omit|embed
    burn: str = "off"        # off|on — 유튜브용: 꾸민 자막을 영상 화면에 새김 (켜면 자막 트랙은 넣지 않음)
    quality: str = "auto"    # low|auto|high

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "JobOptions":
        names = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in names}
        for k in LEVEL_KEYS:   # 예전 작업: keep → 0, remove → 50. 알 수 없는 값은 기본값
            if k in kw:
                v = kw[k]
                try:
                    kw[k] = _OLD_LEVEL[v] if isinstance(v, str) and v in _OLD_LEVEL else parse_level(v)
                except ValueError:
                    kw[k] = LEVEL_DEFAULT
        return cls(**kw)


def analysis_args(o: JobOptions) -> list[str]:
    args = ["--filler", _ONOFF[o.filler], "--habit", _HABIT[o.habit]]
    args += ["--silence", "off"] if o.silence <= 0 else ["--silence", "on", "--silence-level", str(o.silence)]
    # 미리보기 소리도 슬라이더와 같게 (50 이면 파이프라인이 clean.wav 를 그대로 쓴다)
    return args + ["--denoise-level", str(o.denoise)]


def render_args(o: JobOptions, fonts_dir=None) -> list[str]:
    """fonts_dir: 자막 새기기에 쓸 글꼴 폴더 (app/ui/fonts)."""
    burn = o.burn == "on"
    args = ["--denoise-level", str(o.denoise),
            "--render-loudnorm", "on" if o.loudnorm == "match" else "off",
            # 새긴 자막 위에 켜고 끄는 트랙까지 넣으면 두 번 보인다
            "--embed-subs", "on" if o.subs == "embed" and not burn else "off",
            "--vbitrate-scale", _QUALITY[o.quality]]
    if burn:
        args += ["--burn-subs", "on"] + (["--burn-fonts", str(fonts_dir)] if fonts_dir else [])
    return args
