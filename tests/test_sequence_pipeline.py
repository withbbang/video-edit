import json

import pipeline


def _single_cache(work, out_dir, stem, meta=None):
    work.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (work / "clean.wav").write_bytes(b"clean")
    (work / "asr.wav").write_bytes(b"asr")
    (work / "audio.meta.json").write_text(json.dumps(meta or {"k": 1}), encoding="utf-8")
    (out_dir / f"{stem}.transcript.json").write_text('{"segments": []}', encoding="utf-8")


def test_file_work():
    assert pipeline.file_work(pipeline.Path("/w"), "abc") == pipeline.Path("/w/files/abc")


def test_adopt_copies_single_cache(tmp_path):
    work, out = tmp_path / "work", tmp_path / "out"
    _single_cache(work, out, "a")
    fw = work / "files" / "fa"
    pipeline.adopt_single_cache(work, out, "a", fw)
    assert (fw / "clean.wav").read_bytes() == b"clean"
    assert (fw / "asr.wav").read_bytes() == b"asr"
    assert json.loads((fw / "audio.meta.json").read_text()) == {"k": 1}
    assert (fw / "transcript.json").read_text() == '{"segments": []}'
    assert (work / "clean.wav").exists()  # 복사지 이동이 아니다


def test_adopt_leaves_existing_alone(tmp_path):
    work, out = tmp_path / "work", tmp_path / "out"
    _single_cache(work, out, "a")
    fw = work / "files" / "fa"
    fw.mkdir(parents=True)
    (fw / "audio.meta.json").write_text('{"mine": 1}', encoding="utf-8")
    pipeline.adopt_single_cache(work, out, "a", fw)
    assert json.loads((fw / "audio.meta.json").read_text()) == {"mine": 1}
    assert not (fw / "clean.wav").exists()


def test_adopt_skips_sequence_cache(tmp_path):
    work, out = tmp_path / "work", tmp_path / "out"
    _single_cache(work, out, "a", meta={"k": 1, "sequence": True})
    fw = work / "files" / "fa"
    pipeline.adopt_single_cache(work, out, "a", fw)
    assert not (fw / "audio.meta.json").exists()


def test_adopt_without_cache_or_missing_files(tmp_path):
    work, out = tmp_path / "work", tmp_path / "out"
    fw = work / "files" / "fa"
    pipeline.adopt_single_cache(work, out, "a", fw)  # 캐시 없음: 아무 일 없음
    _single_cache(work, out, "a")
    (work / "asr.wav").unlink()
    pipeline.adopt_single_cache(work, out, "a", fw)  # 없는 파일은 건너뜀
    assert (fw / "clean.wav").exists() and not (fw / "asr.wav").exists()
