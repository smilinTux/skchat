"""wav_to_pcm must never hand an error body to the audio track.

It falls back to returning its input unchanged for backends that emit raw PCM.
That is correct for PCM and catastrophic for an error body: publishing JSON or
HTML as int16 samples is a burst of loud distortion, which is what Chef heard
while the narration backend was failing.
"""

import struct

from skchat.transports.livekit import wav_to_pcm


def _wav(samples: bytes, rate: int = 16000) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples)
    return buf.getvalue()


def test_decodes_a_real_wav():
    pcm = struct.pack("<4h", 1000, -1000, 2000, -2000)
    assert wav_to_pcm(_wav(pcm), 16000) == pcm


def test_refuses_a_json_error_body():
    assert wav_to_pcm(b'{"detail":"model returned empty"}', 16000) == b""


def test_refuses_an_html_error_page():
    assert wav_to_pcm(b"<html><body>502 Bad Gateway</body></html>", 16000) == b""


def test_still_passes_through_raw_pcm():
    """Real PCM has high bytes and is not printable text, so the guard must not
    eat it: a backend that returns headerless PCM keeps working."""
    pcm = struct.pack("<8h", 30000, -30000, 12000, -12000, 500, -500, 9000, -9000)
    assert wav_to_pcm(pcm, 16000) == pcm


def test_empty_input_is_unchanged():
    assert wav_to_pcm(b"", 16000) == b""
