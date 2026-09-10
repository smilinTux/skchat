import io
import struct
import wave

from skchat.voice_engine.audio_codec import detect_audio_format, pcm_to_wav, rms


def _silence(n_samples: int) -> bytes:
    return struct.pack("<%dh" % n_samples, *([0] * n_samples))


def _tone(n_samples: int, amp: int = 8000) -> bytes:
    return struct.pack("<%dh" % n_samples, *([amp, -amp] * (n_samples // 2)))


def test_pcm_to_wav_roundtrips_header_and_frames():
    pcm = _tone(1600)  # 0.1s @ 16k
    wav = pcm_to_wav(pcm, sample_rate=16000, channels=1)
    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 16000
        assert wf.getsampwidth() == 2
        assert wf.readframes(wf.getnframes()) == pcm


def test_rms_zero_for_silence():
    assert rms(_silence(1600)) == 0


def test_rms_high_for_tone():
    assert rms(_tone(1600, amp=8000)) > 5000


def test_detect_audio_format_wav():
    assert detect_audio_format(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "wav"


def test_detect_audio_format_webm():
    assert detect_audio_format(b"\x1aE\xdf\xa3" + b"\x00" * 8) == "webm"


def test_detect_audio_format_ogg():
    assert detect_audio_format(b"OggS" + b"\x00" * 8) == "ogg"


def test_detect_audio_format_mp3_id3():
    assert detect_audio_format(b"ID3" + b"\x00" * 8) == "mp3"


def test_detect_audio_format_mp3_frame_sync():
    assert detect_audio_format(b"\xff\xfb" + b"\x00" * 8) == "mp3"


def test_detect_audio_format_m4a():
    assert detect_audio_format(b"\x00\x00\x00\x18ftypM4A ") == "m4a"


def test_detect_audio_format_unknown_blob():
    assert detect_audio_format(b"garbage!" * 4) == ""


def test_detect_audio_format_empty():
    assert detect_audio_format(b"") == ""


def test_detect_audio_format_none():
    assert detect_audio_format(None) == ""


def test_detect_audio_format_too_short():
    assert detect_audio_format(b"RI") == ""
