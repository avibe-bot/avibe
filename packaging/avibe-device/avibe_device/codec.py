"""Fixed CoreS3 Opus clock and bounded WAV conversion."""
from __future__ import annotations

import audioop
import io
import struct
import wave

HEADER = struct.Struct("!HHIII")
FRAME_BYTES = 1920


def pack_audio(opus: bytes, timestamp: int = 0) -> bytes:
    if not 0 < len(opus) <= 8192:
        raise ValueError("invalid_packet")
    return HEADER.pack(2, 0, 0, timestamp, len(opus)) + opus


def unpack_audio(data: bytes) -> bytes:
    if len(data) < HEADER.size:
        raise ValueError("invalid_packet")
    version, kind, reserved, _, size = HEADER.unpack_from(data)
    if (version, kind, reserved) != (2, 0, 0) or not 0 < size <= 8192 or len(data) != HEADER.size + size:
        raise ValueError("invalid_packet")
    return data[HEADER.size:]


def wav_bytes(pcm: bytes, rate: int = 16000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(pcm)
    return out.getvalue()


class Codec:
    def __init__(self):
        try:
            import opuslib_next
        except Exception as exc:
            # The binding raises a plain Exception when native libopus is absent.
            # Keep that optional capability failure on the diagnostic boundary.
            raise ImportError("opus_unavailable") from exc

        self.decoder = opuslib_next.Decoder(16000, 1)
        self.encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_VOIP)
        self.encoder.bitrate = 24000

    def decode(self, packet: bytes) -> bytes:
        import opuslib_next

        try:
            pcm = self.decoder.decode(unpack_audio(packet), 960, decode_fec=False)
        except opuslib_next.OpusError:
            raise ValueError("invalid_opus") from None
        if len(pcm) != FRAME_BYTES:
            raise ValueError("invalid_frame_duration")
        return pcm

    def encode(self, pcm: bytes) -> bytes:
        if len(pcm) != FRAME_BYTES:
            raise ValueError("invalid_frame_duration")
        return pack_audio(self.encoder.encode(pcm, 960))

    def output_frames(self, data: bytes) -> list[bytes]:
        if len(data) > 2_000_000:
            raise ValueError("audio_too_large")
        with wave.open(io.BytesIO(data), "rb") as audio:
            if (
                audio.getnchannels() != 1 or audio.getsampwidth() != 2
                or audio.getframerate() != 24000 or audio.getcomptype() != "NONE"
                or not 0 < audio.getnframes() <= 24000 * 40
            ):
                raise ValueError("invalid_cloud_wav")
            pcm = audio.readframes(audio.getnframes())
            if len(pcm) != audio.getnframes() * 2:
                raise ValueError("truncated_cloud_wav")
        pcm, _ = audioop.ratecv(pcm, 2, 1, 24000, 16000, None)
        self.encoder.reset_state()
        return [
            self.encode(pcm[offset:offset + FRAME_BYTES].ljust(FRAME_BYTES, b"\0"))
            for offset in range(0, len(pcm), FRAME_BYTES)
        ]


class Utterance:
    """Bounded energy endpoint detector; acoustics require separate device tuning."""
    def __init__(self, threshold: int = 600):
        self.threshold = threshold
        self.frames: list[bytes] = []
        self.preroll: list[bytes] = []
        self.silence = 0
        self.onset = 0
        self.started = False

    def feed(self, pcm: bytes) -> bool:
        voiced = audioop.rms(pcm, 2) >= self.threshold
        if not self.started:
            self.preroll = (self.preroll + [pcm])[-5:]
            self.onset = self.onset + 1 if voiced else 0
            if self.onset < 2:
                return False
            self.started = True
            self.frames = self.preroll
        else:
            self.frames.append(pcm)
        self.silence = 0 if voiced else self.silence + 1
        return self.silence >= 10 or len(self.frames) >= 334

    def wav(self) -> bytes:
        return wav_bytes(b"".join(self.frames))
