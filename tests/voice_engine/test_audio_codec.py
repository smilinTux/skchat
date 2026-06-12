import io
import struct
import wave

from skchat.voice_engine.audio_codec import pcm_to_wav, rms


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
