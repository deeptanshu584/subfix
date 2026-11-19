"""Edge cases for subtitle reading/writing: encodings, odd layouts, format sniffing, writing."""

from __future__ import annotations

from pathlib import Path

import pytest

from subfix.srt_io import (
    Subtitle,
    decode_bytes,
    format_timestamp,
    load_subtitles,
    parse_srt,
    parse_subtitles,
    parse_vtt,
    sanitize,
    shift,
    to_srt,
    transform,
    write_srt,
)

TWO_CUES = "1\n00:00:01,000 --> 00:00:02,000\n{a}\n\n2\n00:00:03,000 --> 00:00:04,000\n{b}\n"


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _triples(subs):
    return [(s.start, s.end, s.text) for s in subs]


# --------------------------------------------------------------------------- encodings


@pytest.mark.parametrize(
    "encoding,a,b",
    [
        (
            "cp1252",
            "“Don’t go,” she said — it’s late… Café naïve",
            "Oh – really? «Ça va» crème brûlée",
        ),
        ("latin-1", "Ärger über Öl, Straße", "¿Qué pasó? ¡Mañana!"),
        (
            "cp1251",
            "Привет, как дела? Всё хорошо, спасибо.",
            "Мы должны идти прямо сейчас, пожалуйста.",
        ),
        ("gb18030", "你好，你今天怎么样？我很好，谢谢。", "我们现在必须走了，请快点。"),
        ("gbk", "你好，你今天怎么样？我很好，谢谢。", "我们现在必须走了，请快点。"),
    ],
)
def test_load_legacy_encodings(tmp_path, encoding, a, b):
    p = _write(tmp_path, "x.srt", TWO_CUES.format(a=a, b=b).encode(encoding))
    doc = load_subtitles(p)
    assert [s.text for s in doc.subs] == [a, b], doc.encoding


def test_load_utf8_bom(tmp_path):
    p = _write(tmp_path, "bom.srt", TWO_CUES.format(a="Ünïcødé", b="ok").encode("utf-8-sig"))
    doc = load_subtitles(p)
    assert doc.encoding == "utf-8-sig"
    assert doc.subs[0].text == "Ünïcødé"
    assert all("﻿" not in s.text for s in doc.subs)


@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
def test_load_utf16_without_bom(tmp_path, codec):
    text = TWO_CUES.format(a="Hello there, Café", b="General Kenobi")
    doc = load_subtitles(_write(tmp_path, "x.srt", text.encode(codec)))
    assert doc.encoding == codec
    assert [s.text for s in doc.subs] == ["Hello there, Café", "General Kenobi"]


def test_decode_short_utf16_le_without_bom():
    data = "1\n00:00:01,000 --> 00:00:02,000\nHi\n".encode("utf-16-le")
    text, enc = decode_bytes(data)
    assert "\x00" not in text and "Hi" in text, enc


# --------------------------------------------------------------------------- SRT layout quirks


def test_cr_only_line_endings(tmp_path):
    text = (
        "1\r00:00:01,000 --> 00:00:02,000\rA\rsecond line\r\r2\r00:00:03,000 --> 00:00:04,000\rB\r"
    )
    doc = load_subtitles(_write(tmp_path, "mac.srt", text.encode("ascii")))
    assert _triples(doc.subs) == [(1.0, 2.0, "A\nsecond line"), (3.0, 4.0, "B")]


def test_trailing_whitespace_on_timestamp_and_text_lines():
    text = (
        "1  \n00:00:01,000 --> 00:00:02,000   \t\nA   \n\n2\t\n00:00:03,000 --> 00:00:04,000 \nB\n"
    )
    assert _triples(parse_srt(text)) == [(1.0, 2.0, "A"), (3.0, 4.0, "B")]


def test_position_coordinates_after_timestamps():
    text = (
        "1\n00:00:01,000 --> 00:00:02,000 X1:100 X2:200 Y1:300 Y2:400\nA\n\n"
        "2\n00:00:03,000 --> 00:00:04,000  X1:1 X2:2 Y1:3 Y2:4\nB\n"
    )
    assert _triples(parse_srt(text)) == [(1.0, 2.0, "A"), (3.0, 4.0, "B")]


def test_hours_above_99_parse_and_roundtrip(tmp_path):
    subs = parse_srt("1\n100:00:01,000 --> 123:04:05,678\nLong film\n")
    assert subs[0].start == 360001.0
    assert subs[0].end == pytest.approx(123 * 3600 + 4 * 60 + 5.678)
    assert format_timestamp(subs[0].end) == "123:04:05,678"
    again = load_subtitles(write_srt(subs, tmp_path / "long.srt")).subs
    assert again[0].start == 360001.0 and again[0].end == pytest.approx(subs[0].end)


def test_cue_whose_text_is_a_number():
    text = (
        "1\n00:00:01,000 --> 00:00:02,000\n1984\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n42\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\n7\n"
    )
    assert [s.text for s in parse_srt(text)] == ["1984", "42", "7"]


def test_number_text_without_indices_is_kept():
    # No indices at all, but cues separated by blank lines: "2001" is text, not an index.
    text = "00:00:01,000 --> 00:00:02,000\n2001\n\n00:00:03,000 --> 00:00:04,000\nB\n"
    assert [s.text for s in parse_srt(text)] == ["2001", "B"]


def test_multiline_text_ending_in_number():
    text = "1\n00:00:01,000 --> 00:00:02,000\nCall\n911\n\n2\n00:00:03,000 --> 00:00:04,000\nB\n"
    assert [s.text for s in parse_srt(text)] == ["Call\n911", "B"]


def test_out_of_order_cues_sorted_with_warning(tmp_path):
    text = (
        "1\n00:00:05,000 --> 00:00:06,000\nlate\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nearly\n\n"
        "3\n00:00:03,000 --> 00:00:04,000\nmiddle\n"
    )
    doc = load_subtitles(_write(tmp_path, "x.srt", text.encode()))
    assert [s.text for s in doc.subs] == ["early", "middle", "late"]
    assert any("sorted" in w for w in doc.warnings)


def test_end_before_start_warning(tmp_path):
    text = (
        "1\n00:00:05,000 --> 00:00:04,000\nbackwards\n\n"
        "2\n00:00:06,000 --> 00:00:06,000\nzero\n\n"
        "3\n00:00:07,000 --> 00:00:08,000\nfine\n"
    )
    doc = load_subtitles(_write(tmp_path, "x.srt", text.encode()))
    assert any("2 cue(s) had end <= start" in w for w in doc.warnings)
    assert not any("sorted" in w for w in doc.warnings)


def test_clean_file_has_no_warnings(tmp_path):
    doc = load_subtitles(_write(tmp_path, "x.srt", TWO_CUES.format(a="A", b="B").encode()))
    assert doc.warnings == [] and doc.fmt == "srt" and doc.encoding == "utf-8"


# --------------------------------------------------------------------------- ASS / VTT

ASS_HEADER = (
    "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize\nStyle: Default,Arial,20\n\n[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)


def test_ass_override_tags_hard_breaks_and_commas():
    text = ASS_HEADER + (
        "Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,Hello, world, again\n"
        "Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,"
        "{\\pos(10,20)\\b1}Bold{\\b0}\\hspace\\Nline2\n"
        "Dialogue: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,{\\an8}\n"
    )
    subs, fmt = parse_subtitles(text, ".ass")
    assert fmt == "ass"
    by_start = {s.start: s.text for s in subs}
    assert by_start == {1.0: "Bold space\nline2", 3.0: "Hello, world, again"}


def test_ass_loaded_from_file_sorted(tmp_path):
    text = ASS_HEADER + (
        "Dialogue: 0,0:00:09.00,0:00:10.00,Default,,0,0,0,,second\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,first\n"
    )
    doc = load_subtitles(_write(tmp_path, "x.ass", text.encode("utf-8-sig")))
    assert doc.fmt == "ass"
    assert [s.text for s in doc.subs] == ["first", "second"]


def test_ass_without_styles_section():
    text = (
        "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hi\n"
    )
    subs, fmt = parse_subtitles(text, ".ass")
    assert fmt == "ass" and [s.text for s in subs] == ["Hi"]


VTT = """WEBVTT - Some title

STYLE
::cue {
  color: yellow;
}

NOTE This is a comment
spanning two lines

intro-1
00:01.000 --> 00:02.500 line:0 position:10%
<v Alice>Hello there</v>

NOTE between cues

00:03.250 --> 00:04.000
Second <b>cue</b>

speaker-2
01:02:03.004 --> 01:02:04.000
Third <00:01:02.500>karaoke
"""


def test_vtt_hourless_notes_styles_and_ids():
    assert _triples(parse_vtt(VTT)) == [
        (1.0, 2.5, "Hello there"),
        (3.25, 4.0, "Second <b>cue</b>"),
        (3723.004, 3724.0, "Third karaoke"),
    ]


def test_vtt_html_entities_decoded():
    subs = parse_vtt("WEBVTT\n\n00:01.000 --> 00:02.000\nTom &amp; Jerry &lt;3\n")
    assert subs[0].text == "Tom & Jerry <3"


def test_sniff_webvtt_saved_as_srt(tmp_path):
    doc = load_subtitles(_write(tmp_path, "lying.srt", VTT.encode("utf-8")))
    assert doc.fmt == "vtt"
    assert doc.subs[0].text == "Hello there"
    assert len(doc.subs) == 3


def test_sniff_ass_saved_as_srt(tmp_path):
    text = ASS_HEADER + "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\i1}Hi{\\i0}\n"
    doc = load_subtitles(_write(tmp_path, "lying.srt", text.encode()))
    assert doc.fmt == "ass" and doc.subs[0].text == "Hi"


def test_srt_body_saved_as_vtt(tmp_path):
    text = TWO_CUES.format(a="A", b="B") + "\n3\n00:00:05,000 --> 00:00:06,000\nC\n"
    doc = load_subtitles(_write(tmp_path, "x.vtt", text.encode()))
    assert doc.fmt == "vtt"
    assert _triples(doc.subs) == [(1.0, 2.0, "A"), (3.0, 4.0, "B"), (5.0, 6.0, "C")]


# --------------------------------------------------------------------------- writing


def test_write_read_roundtrip_keeps_tags_and_unicode(tmp_path):
    subs = [
        Subtitle(1.0, 2.0, "<i>Ça va?</i>\n日本語のテキスト"),
        Subtitle(2.5, 3.75, "<b>Привет</b> — “quoted”"),
        Subtitle(4.0, 5.0, "♪ la la ♪"),
    ]
    p = write_srt(subs, tmp_path / "out.srt")
    raw = p.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\n" not in raw.replace(b"\r\n", b"")  # only CRLF line endings
    doc = load_subtitles(p)
    assert doc.encoding == "utf-8"
    assert _triples(doc.subs) == _triples(subs)
    assert not (tmp_path / "out.srt.tmp").exists()


def test_write_overwrites_existing(tmp_path):
    p = tmp_path / "out.srt"
    p.write_text("old junk")
    write_srt([Subtitle(1, 2, "new")], p)
    assert [s.text for s in load_subtitles(p).subs] == ["new"]


def test_to_srt_renumbers_and_strips_text():
    out = to_srt([Subtitle(3, 4, "  b  \n"), Subtitle(1, 2, "\na")])
    assert out == "1\n00:00:01,000 --> 00:00:02,000\na\n\n2\n00:00:03,000 --> 00:00:04,000\nb\n"


def test_sanitize_rules():
    inp = [
        Subtitle(5.0, 5.0, "zero length"),
        Subtitle(-2.0, 0.0, "ends at zero"),
        Subtitle(-1.0, 0.5, "straddles"),
        Subtitle(7.0, 6.0, "backwards"),
        Subtitle(1.0, 3.0, "overlap a"),
        Subtitle(2.0, 4.0, "overlap b"),
    ]
    out = sanitize(inp)
    assert _triples(out) == [
        (0.0, 0.5, "straddles"),
        (1.0, 3.0, "overlap a"),
        (2.0, 4.0, "overlap b"),
        (5.0, pytest.approx(5.3), "zero length"),
        (7.0, pytest.approx(7.3), "backwards"),
    ]
    assert inp[0].end == 5.0  # input untouched


def test_sanitize_custom_min_duration_and_stable_order():
    out = sanitize([Subtitle(1, 1, "x"), Subtitle(1, 2, "y")], min_duration=1.0)
    assert _triples(out) == [(1, 2.0, "x"), (1, 2, "y")]


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (59.9996, "00:01:00,000"),
        (59.9994, "00:00:59,999"),
        (3599.9996, "01:00:00,000"),
        (0.0004, "00:00:00,000"),
        (1.0006, "00:00:01,001"),
        (86399.999, "23:59:59,999"),
        (-0.0001, "00:00:00,000"),
    ],
)
def test_format_timestamp_rounding(seconds, expected):
    assert format_timestamp(seconds) == expected


def test_format_timestamp_vtt_separator():
    assert format_timestamp(61.5, sep=".") == "00:01:01.500"


def test_shift_composes():
    subs = [Subtitle(10.0, 12.0, "a"), Subtitle(100.0, 101.5, "b")]
    a, s1, b, s2 = 3.0, 1.04, -7.5, 0.96
    twice = shift(shift(subs, a, s1), b, s2)
    once = shift(subs, s2 * a + b, s1 * s2)
    for x, y in zip(twice, once, strict=True):
        assert x.start == pytest.approx(y.start) and x.end == pytest.approx(y.end)
        assert x.text == y.text


def test_shift_inverse_restores_original():
    subs = [Subtitle(10.0, 12.0, "a"), Subtitle(1000.0, 1003.0, "b")]
    s, o = 25 / 23.976, 4.2
    back = shift(shift(subs, o, s), -o / s, 1 / s)
    for x, y in zip(back, subs, strict=True):
        assert x.start == pytest.approx(y.start) and x.end == pytest.approx(y.end)


def test_transform_returns_new_objects():
    subs = [Subtitle(1.0, 2.0, "a")]
    out = transform(subs, lambda t: t * 2)
    assert (out[0].start, out[0].end) == (2.0, 4.0)
    assert (subs[0].start, subs[0].end) == (1.0, 2.0)
    assert out[0] is not subs[0]
