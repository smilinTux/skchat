from skchat.voice_engine.audio_codec import detect_audio_format


def test_detect_audio_format_wav():
    assert detect_audio_format(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "wav"


def test_detect_audio_format_webm():
    assert detect_audio_format(b"\x1aE\xdf\xa3\x00\x00\x00\x00") == "webm"


def test_detect_audio_format_ogg():
    assert detect_audio_format(b"OggS\x00\x02\x00\x00") == "ogg"


def test_detect_audio_format_mp3_id3():
    assert detect_audio_format(b"ID3\x03\x00\x00\x00\x00") == "mp3"


def test_detect_audio_format_mp3_frame_sync():
    assert detect_audio_format(b"\xff\xfb\x90\x00\x00\x00\x00\x00") == "mp3"


def test_detect_audio_format_m4a():
    assert detect_audio_format(b"\x00\x00\x00\x18ftypM4A ") == "m4a"


def test_detect_audio_format_unknown():
    assert detect_audio_format(b"not audio data here") == ""


def test_detect_audio_format_empty():
    assert detect_audio_format(b"") == ""


def test_detect_audio_format_none():
    assert detect_audio_format(None) == ""


def test_detect_audio_format_too_short():
    assert detect_audio_format(b"RI") == ""
