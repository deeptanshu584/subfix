from pathlib import Path

import pytest

from subfix.srt_io import (
    Subtitle,
    SubtitleError,
    decode_bytes,
    find_sidecar,
    format_timestamp,
    load_subtitles,
    parse_srt,
    parse_subtitles,
    parse_vtt,
    shift,
    to_srt,
    write_srt,
)

SRT = """1
00:00:01,000 --> 00:00:02,500
Hello <i>world</i>

2
00:00:03,000 --> 00:00:04,000
Two
lines

3
00:01:05,250 --> 00:01:06,000
Third
"""


def test_parse_basic():
    subs = parse_srt(SRT)
    assert [s.start for s in subs] == [1.0, 3.0, 65.25]
    assert subs[0].text == "Hello <i>world</i>"
    assert subs[1].text == "Two\nlines"
    assert subs[2].end == 66.0


def test_parse_tolerates_missing_indices_dots_and_short_ms():
    text = "00:00:01.5 --> 00:00:02.25\nA\n\n00:00:03,000-->00:00:04,000\nB\n"
    subs = parse_srt(text)
    assert [(s.start, s.end, s.text) for s in subs] == [(1.5, 2.25, "A"), (3.0, 4.0, "B")]


def test_parse_missing_blank_line_between_cues():
    text = "1\n00:00:01,000 --> 00:00:02,000\nA\n2\n00:00:03,000 --> 00:00:04,000\nB\n"
    subs = parse_srt(text)
    assert [s.text for s in subs] == ["A", "B"]


def test_parse_crlf_and_blank_line_inside_cue():
    text = "1\r\n00:00:01,000 --> 00:00:02,000\r\nA\r\n\r\nstill A\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\nB"
    subs = parse_srt(text)
    assert subs[0].text == "A\n\nstill A"
    assert subs[1].text == "B"


def test_parse_vtt():
    text = (
        "WEBVTT\n\nNOTE a comment\n\nintro\n00:01.000 --> 00:02.000 align:start\n"
        "<v Bob>Hi</v> <c.yellow>there</c>\n\n00:00:03.000 --> 00:00:04.000\n<i>Bye</i>\n"
    )
    subs = parse_vtt(text)
    assert [(s.start, s.text) for s in subs] == [(1.0, "Hi there"), (3.0, "<i>Bye</i>")]


def test_parse_ass():
    text = (
        "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize\nStyle: Default,Arial,20\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,{\\i1}Hello{\\i0}\\Nworld\n"
        "Comment: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,ignored\n"
    )
    subs, fmt = parse_subtitles(text, ".ass")
    assert fmt == "ass"
    assert len(subs) == 1 and subs[0].text == "Hello\nworld" and subs[0].end == 2.5


@pytest.mark.parametrize(
    "encoding,expected",
    [("utf-8", "utf-8"), ("utf-8-sig", "utf-8-sig"), ("cp1252", "cp1252"), ("utf-16", "utf-16")],
)
def test_decode_encodings(encoding, expected):
    text = "1\n00:00:01,000 --> 00:00:02,000\nCafé — naïve façade “quotes”\n"
    enc = encoding if encoding != "cp1252" else "cp1252"
    got, name = decode_bytes(text.encode(enc))
    assert "Café" in got and "façade" in got
    assert name == expected


def test_decode_latin1_fallback():
    data = "1\n00:00:01,000 --> 00:00:02,000\nÄrger über Öl\n".encode("latin-1")
    got, _ = decode_bytes(data)
    assert "Ärger über Öl" in got


def test_decode_cyrillic_cp1251():
    text = "1\n00:00:01,000 --> 00:00:02,000\n" + "Привет, как дела? Всё хорошо, спасибо. " * 5
    got, enc = decode_bytes(text.encode("cp1251"))
    assert "Привет" in got, enc


def test_format_timestamp():
    assert format_timestamp(0) == "00:00:00,000"
    assert format_timestamp(3661.2345) == "01:01:01,234" or format_timestamp(3661.2345) == (
        "01:01:01,235"
    )
    assert format_timestamp(-5) == "00:00:00,000"


def test_roundtrip(tmp_path: Path):
    subs = parse_srt(SRT)
    p = write_srt(subs, tmp_path / "out.srt")
    raw = p.read_bytes()
    assert b"\r\n" in raw and raw.decode("utf-8").startswith("1\r\n00:00:01,000")
    again = load_subtitles(p).subs
    assert [(s.start, s.end, s.text) for s in again] == [(s.start, s.end, s.text) for s in subs]


def test_sanitize_drops_negative_and_fixes_order():
    subs = [Subtitle(5, 6, "b"), Subtitle(-3, -1, "gone"), Subtitle(-0.5, 1, "clamped")]
    out = to_srt(subs)
    assert "gone" not in out
    assert out.index("clamped") < out.index("b")
    assert "00:00:00,000 --> 00:00:01,000" in out


def test_shift_scale():
    out = shift([Subtitle(10, 12, "x")], offset=1.0, scale=2.0)
    assert (out[0].start, out[0].end) == (21.0, 25.0)


def test_load_empty_raises(tmp_path: Path):
    p = tmp_path / "empty.srt"
    p.write_text("nothing here", encoding="utf-8")
    with pytest.raises(SubtitleError):
        load_subtitles(p)


def test_find_sidecar(tmp_path: Path):
    video = tmp_path / "Movie.2020.mkv"
    video.write_bytes(b"")
    assert find_sidecar(video) is None
    (tmp_path / "Movie.2020.synced.srt").write_text("x")
    (tmp_path / "Movie.2020.generated.srt").write_text("x")
    assert find_sidecar(video) is None  # our own outputs are ignored
    (tmp_path / "Movie.2020.fr.srt").write_text("x")
    (tmp_path / "Movie.2020.en.srt").write_text("x")
    assert find_sidecar(video).name == "Movie.2020.en.srt"
    (tmp_path / "Movie.2020.srt").write_text("x")
    assert find_sidecar(video).name == "Movie.2020.srt"
