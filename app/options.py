"""작업별 옵션과 파이프라인 인자 변환."""
from dataclasses import asdict, dataclass, fields

FAST_KEYS = {"filler", "habit", "silence"}  # 바꾸면 컷을 다시 판정한다. 나머지는 저장할 때만 쓴다.

_HABIT = {"keep": "off", "some": "on", "all": "aggressive"}
_ONOFF = {"remove": "on", "keep": "off"}
_QUALITY = {"low": "0.6", "auto": "1.0", "high": "1.5"}


@dataclass
class JobOptions:
    filler: str = "remove"
    habit: str = "some"      # keep|some|all
    silence: str = "remove"
    denoise: str = "remove"
    loudnorm: str = "match"  # keep|match
    subs: str = "embed"      # omit|embed
    quality: str = "auto"    # low|auto|high

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "JobOptions":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def analysis_args(o: JobOptions) -> list[str]:
    return ["--filler", _ONOFF[o.filler], "--habit", _HABIT[o.habit], "--silence", _ONOFF[o.silence]]


def render_args(o: JobOptions) -> list[str]:
    return ["--render-denoise", "rnnoise" if o.denoise == "remove" else "none",
            "--render-loudnorm", "on" if o.loudnorm == "match" else "off",
            "--embed-subs", "on" if o.subs == "embed" else "off",
            "--vbitrate-scale", _QUALITY[o.quality]]
