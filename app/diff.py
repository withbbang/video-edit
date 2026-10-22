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
