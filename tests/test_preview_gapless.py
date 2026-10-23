"""fMP4 조각 목록 읽기: 합성 바이트로 만들어 ffmpeg 없이 확인한다."""
import json
import struct

import pipeline


def box(typ: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def sample_fmp4(audio: bool = False) -> bytes:
    mdhd = box(b"mdhd", struct.pack(">B3xIII", 0, 0, 0, 1000) + b"\0" * 8)  # v0: 오프셋 12 에 timescale
    if audio:
        entry = box(b"mp4a", b"\0" * 28 + box(b"esds", b"\0" * 8))
    else:
        entry = box(b"avc1", b"\0" * 78 + box(b"avcC", bytes([1, 0x64, 0x00, 0x20]) + b"\0" * 4))
    stsd = box(b"stsd", b"\0" * 8 + entry)
    moov = box(b"moov", box(b"trak", box(b"mdia", mdhd + box(b"minf", box(b"stbl", stsd))))
               + box(b"mvex", box(b"trex", b"\0" * 24)))
    tfdt1 = box(b"tfdt", struct.pack(">B3xQ", 1, 0))
    tfdt0 = box(b"tfdt", struct.pack(">B3xI", 0, 500))
    frag1 = box(b"moof", box(b"mfhd", b"\0" * 8) + box(b"traf", tfdt1)) + box(b"mdat", b"x" * 20)
    frag2 = box(b"moof", box(b"traf", tfdt0)) + box(b"mdat", b"y" * 30)
    return box(b"ftyp", b"isom\0\0\0\0isom") + moov + frag1 + frag2


def moov_end(data: bytes) -> int:
    return data.index(b"moof") - 4


def test_fmp4_index_reads_fragments(tmp_path):
    p = tmp_path / "v.mp4"
    p.write_bytes(sample_fmp4())
    idx = pipeline.fmp4_index(p)
    end = moov_end(p.read_bytes())
    assert idx["timescale"] == 1000 and idx["codec"] == "avc1.640020"
    assert idx["init"] == [0, end]
    assert [f[0] for f in idx["frags"]] == [0.0, 0.5]
    assert idx["frags"][0][1] == end and idx["frags"][-1][2] == p.stat().st_size
    assert idx["frags"][0][2] == idx["frags"][1][1]


def test_fmp4_index_audio_codec(tmp_path):
    p = tmp_path / "a.m4a"
    p.write_bytes(sample_fmp4(audio=True))
    assert pipeline.fmp4_index(p)["codec"] == "mp4a.40.2"


def test_write_index_merges_extra(tmp_path):
    p = tmp_path / "v.mp4"
    p.write_bytes(sample_fmp4())
    out = pipeline.write_index(p, {"keeps": [[0, 1]]})
    assert out == tmp_path / "v.index.json"
    d = json.loads(out.read_text())
    assert d["keeps"] == [[0, 1]] and d["timescale"] == 1000 and len(d["frags"]) == 2
