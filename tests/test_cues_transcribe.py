import numpy as np

from subfix.config import MAX_LINE_CHARS
from subfix.cues import transcript_to_subtitles, words_to_cues, wrap_lines
from subfix.transcribe import Segment, Transcript, Word, clean_segments, plan_chunks
from subfix.vad import HOP_SECONDS, SpeechSignal


def _words(text: str, start: float = 0.0, step: float = 0.3) -> list[Word]:
    return [
        Word(start + i * step, start + (i + 1) * step - 0.02, " " + w)
        for i, w in enumerate(text.split())
    ]


def test_wrap_lines_balanced():
    s = wrap_lines("This is a fairly long subtitle line that needs to be wrapped nicely")
    a, b = s.split("\n")
    assert len(a) <= MAX_LINE_CHARS and len(b) <= MAX_LINE_CHARS
    assert abs(len(a) - len(b)) < 15


def test_short_line_not_wrapped():
    assert wrap_lines("Short one.") == "Short one."


def test_words_split_on_sentence_and_pause():
    words = _words("Hello there, it is good to see you again. How are you doing today") + _words(
        "Later words", start=10.0
    )
    groups = words_to_cues(words)
    texts = ["".join(w.text for w in g).strip() for g in groups]
    assert texts[0] == "Hello there, it is good to see you again."
    assert texts[1] == "How are you doing today"
    assert texts[-1] == "Later words"


def test_cues_respect_max_duration():
    words = _words(" ".join(["word"] * 60), step=0.4)
    for g in words_to_cues(words):
        assert g[-1].end - g[0].start <= 6.5


def test_transcript_to_subtitles_no_overlap_and_min_duration():
    segs = [
        Segment(0, 1, "Hi.", _words("Hi.", 0.0, 0.2)),
        Segment(1.0, 3, "Second line here.", _words("Second line here.", 0.3)),
    ]
    subs = transcript_to_subtitles(Transcript("en", "transcribe", "x", segs))
    for a, b in zip(subs, subs[1:], strict=False):
        assert a.end <= b.start
    assert all(s.end - s.start >= 0.2 for s in subs)


def test_clean_segments_drops_hallucinations_and_loops():
    segs = [
        Segment(0, 1, "Real speech", avg_logprob=-0.2),
        Segment(1, 2, "Thanks for watching!", avg_logprob=-0.9),
        Segment(2, 3, "again", avg_logprob=-0.2),
        Segment(3, 4, "again", avg_logprob=-0.2),
        Segment(4, 5, "again", avg_logprob=-0.2),
        Segment(5, 6, "again", avg_logprob=-0.2),
    ]
    out = [s.text for s in clean_segments(segs)]
    assert out == ["Real speech", "again", "again"]


def test_plan_chunks_cut_in_silence():
    dur = 1500.0
    n = int(dur / HOP_SECONDS)
    p = np.full(n, 0.9, dtype=np.float32)
    quiet = int(612 / HOP_SECONDS)
    p[quiet : quiet + int(2 / HOP_SECONDS)] = 0.0  # a 2 s pause near the 600 s target
    chunks = plan_chunks(dur, SpeechSignal(p))
    assert chunks[0][0] == 0 and chunks[-1][1] == dur
    assert 612 <= chunks[0][1] <= 614
    assert all(b > a for a, b in chunks)


def test_plan_chunks_short_file_single_chunk():
    assert plan_chunks(300.0, None) == [(0.0, 300.0)]


def test_transcript_json_roundtrip():
    tr = Transcript("en", "transcribe", "m", [Segment(0, 1, "a b", _words("a b"))], 0.9)
    again = Transcript.from_json(tr.to_json())
    assert again == tr
