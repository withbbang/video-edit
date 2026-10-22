"""파이프라인의 `@progress {...}` 줄을 전체 진행률로 바꾼다."""
import json

ANALYZE_WEIGHTS = {"audio": .10, "transcribe": .60, "align": .05, "judge": .05, "preview": .20}
RENDER_WEIGHTS = {"audio": .10, "render": .90}

_PREFIX = "@progress "


class Tracker:
    def __init__(self, weights: dict):
        self.weights = weights
        self.total = 0.0

    def feed(self, line: str) -> float | None:
        if not line.startswith(_PREFIX):
            return None
        try:
            msg = json.loads(line[len(_PREFIX):])
            stage, frac = msg["stage"], min(max(float(msg["frac"]), 0.0), 1.0)
        except (ValueError, KeyError, TypeError):
            return None
        if stage not in self.weights:
            return self.total
        done = 0.0
        for name, w in self.weights.items():  # 앞 단계는 끝난 것으로 본다
            if name == stage:
                done += w * frac
                break
            done += w
        self.total = max(self.total, done)
        return self.total
