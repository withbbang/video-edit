"""두 cuts.json 의 컷 판정 차이 (설정을 바꿨을 때 무엇이 달라졌는지)."""

UNTRANSCRIBED = "(전사 누락)"


def _parts(cut):
    return [p.strip() for p in str(cut.get("reason", "")).split(" + ")]


def _is_silence(cut):
    return "silence" in _parts(cut)


def _is_untranscribed(cut):
    return cut.get("reason") == "filler(전사 누락)" and not cut.get("words_idx")


def _overlaps(c, others):
    return any(min(c["end"], o["end"]) - max(c["start"], o["start"]) > 0 for o in others)


def _word_info(cut, key, transcript):
    seg, wi = key
    if transcript:
        try:
            w = transcript["segments"][seg]["words"][wi]
            return str(w["word"]).strip(), float(w["start"]), float(w["end"])
        except (KeyError, IndexError, TypeError, ValueError):
            pass
    single = len(cut.get("words_idx") or []) == 1 and " + " not in str(cut.get("word", ""))
    return (cut.get("word", "…") if single else "…"), cut["start"], cut["end"]


def _word_map(cuts_json, transcript):
    out = {}
    for c in cuts_json.get("cuts", []):
        for idx in c.get("words_idx") or []:
            key = (idx[0], idx[1])
            out.setdefault(key, _word_info(c, key, transcript))
    return out


def _region_changes(a, b):
    """a 에만 있는(b 와 겹치지 않는) 컷들."""
    return [c for c in a if not _overlaps(c, b)]


def compare(old: dict, new: dict, transcript: dict | None = None) -> dict:
    ow, nw = _word_map(old, transcript), _word_map(new, transcript)
    rows = []
    for k, (w, s, e) in nw.items():
        if k not in ow:
            rows.append({"word": w, "kind": "added", "start": s, "end": e})
    for k, (w, s, e) in ow.items():
        if k not in nw:
            rows.append({"word": w, "kind": "restored", "start": s, "end": e})

    oc, nc = old.get("cuts", []), new.get("cuts", [])
    osil, nsil = [c for c in oc if _is_silence(c)], [c for c in nc if _is_silence(c)]
    silence = {"added": len(_region_changes(nsil, osil)), "restored": len(_region_changes(osil, nsil))}

    ou, nu = [c for c in oc if _is_untranscribed(c)], [c for c in nc if _is_untranscribed(c)]
    for kind, cs in (("added", _region_changes(nu, ou)), ("restored", _region_changes(ou, nu))):
        for c in cs:
            rows.append({"word": UNTRANSCRIBED, "kind": kind, "start": c["start"], "end": c["end"]})

    rows.sort(key=lambda r: (r["start"], r["end"]))
    return {"rows": rows, "silence": silence}


def cut_ranges(cuts_json: dict) -> list:
    return sorted([c["start"], c["end"]] for c in cuts_json.get("cuts", []))


# ---- 잘린 곳 되살리기 --------------------------------------------------------
# 되살린 곳은 사용자가 고른 컷의 [시작, 끝] 으로 기억한다. 옵션을 바꿔 다시 판정하면 컷 경계가 조금 달라질 수 있어
# 새 컷 길이의 절반 이상이 되살린 구간과 겹치면 같은 자리로 본다.

def is_restored(cut: dict, restored: list) -> bool:
    length = float(cut["end"]) - float(cut["start"])
    if length <= 0:
        return False
    return any(min(float(cut["end"]), b) - max(float(cut["start"]), a) >= length / 2 for a, b in restored)


def _is_placeholder(word: str) -> bool:
    return word.startswith("(") and word.endswith(")")


def _kind(cut: dict) -> str:
    """화면에 보일 종류: 합쳐진 컷("(전사 누락) + 좀" / "filler(전사 누락) + habit")은 실제 단어의 이유를 앞세우고,
    단어가 없으면 말 쪽 이유, 그것도 없으면 무음."""
    kinds = [p.split("(")[0] for p in _parts(cut)]
    words = [w.strip() for w in str(cut.get("word", "")).split(" + ")]
    if len(words) == len(kinds):
        for w, k in zip(words, kinds):
            if w and not _is_placeholder(w) and k:
                return k
    return next((k for k in kinds if k and k != "silence"), "silence")


def _label(cut: dict) -> str:
    """합쳐진 컷의 단어에서 (무음)·(전사 누락) 같은 자리표시는 뺀다."""
    words = [w.strip() for w in str(cut.get("word", "")).split(" + ")]
    return " ".join(w for w in words if w and not _is_placeholder(w))


def cut_items(cuts_json: dict, restored: list) -> list:
    """결과 화면의 '잘린 곳' 목록: 시간순, 되살렸는지 포함."""
    return sorted(({"start": c["start"], "end": c["end"], "word": _label(c), "kind": _kind(c),
                    "restored": is_restored(c, restored)} for c in cuts_json.get("cuts", [])),
                  key=lambda i: (i["start"], i["end"]))


def without_restored(cuts_json: dict, restored: list) -> dict:
    """저장(렌더)용 cuts.json: 되살린 컷만 뺀다. 나머지 키(transcript_sha1 등)는 그대로."""
    return {**cuts_json, "cuts": [c for c in cuts_json.get("cuts", []) if not is_restored(c, restored)]}
