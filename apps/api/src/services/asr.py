"""
ASR Service (Speech-to-Text)
============================
Audio transcription using Whisper (primary) and Deepgram (fallback).
Auto-detects audio format (WAV, WebM, etc.) for reliable transcription.
"""
from typing import Optional, Dict, Any
import structlog
from deepgram import DeepgramClient
from openai import AsyncOpenAI
import io

from src.config import settings

logger = structlog.get_logger()


def detect_audio_format(audio_data: bytes) -> tuple[str, str]:
    """
    Detect audio format from magic bytes.
    Returns (extension, mimetype).
    """
    if len(audio_data) < 4:
        return ("wav", "audio/wav")

    if audio_data[:4] == b'RIFF':
        return ("wav", "audio/wav")
    elif audio_data[:4] == b'\x1aE\xdf\xa3':
        return ("webm", "audio/webm")
    elif audio_data[:3] == b'ID3' or (len(audio_data) >= 2 and audio_data[:2] == b'\xff\xfb'):
        return ("mp3", "audio/mpeg")
    elif audio_data[:4] == b'fLaC':
        return ("flac", "audio/flac")
    elif audio_data[:4] == b'OggS':
        return ("ogg", "audio/ogg")
    else:
        # Default to wav for raw PCM data
        return ("wav", "audio/wav")


async def transcribe_audio(audio_data: bytes, language: str = "es", delayed: bool = False) -> str:
    """
    Transcribe audio using Whisper API (batch processing).

    Auto-detects audio format (WAV, WebM, etc.) from magic bytes.
    Falls back to Deepgram if Whisper fails.
    """
    try:
        if not settings.OPENAI_API_KEY:
            logger.error("OPENAI_API_KEY is missing - falling back to Deepgram")
            return await transcribe_audio_deepgram(audio_data, language)

        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

        # Auto-detect audio format
        ext, mimetype = detect_audio_format(audio_data)
        filename = f"audio.{ext}"

        logger.info(
            "Transcribing audio with Whisper",
            bytes=len(audio_data),
            format=ext,
            language=language,
        )

        # Create a file-like object from bytes
        audio_file = io.BytesIO(audio_data)
        audio_file.name = filename

        response = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language=language,
        )

        transcript = response.text.strip() if response.text else ""
        logger.info("Whisper transcription OK", transcript=transcript[:100])
        return transcript

    except Exception as e:
        logger.error("Whisper transcription failed", error=str(e))
        # Fallback to Deepgram
        return await transcribe_audio_deepgram(audio_data, language)


async def transcribe_audio_deepgram(audio_data: bytes, language: str = "es") -> str:
    """
    Transcribe audio using Deepgram API.
    Auto-detects mimetype from audio data.
    """
    logger.info("Attempting Deepgram transcription", audio_bytes=len(audio_data), language=language)
    try:
        client = DeepgramClient(api_key=settings.DEEPGRAM_API_KEY)

        # Auto-detect mimetype
        _, mimetype = detect_audio_format(audio_data)

        options = {
            "model": "nova-2",
            "language": language,
            "smart_format": True,
            "punctuate": True,
        }

        source = {"buffer": audio_data, "mimetype": mimetype}

        logger.info(f"Sending audio to Deepgram ({len(audio_data)} bytes, {mimetype})")

        response = await client.listen.prerecorded.v("1").transcribe_file(
            source,
            options,
            timeout=30,
        )

        transcript = response.results.channels[0].alternatives[0].transcript
        logger.info("Deepgram transcription OK", transcript=transcript[:100])
        return transcript

    except Exception as e:
        logger.error("Deepgram transcription failed", error=str(e))
        return ""


async def transcribe_audio_streaming(audio_chunk: bytes) -> Optional[Dict[str, Any]]:
    """
    Process an audio chunk for streaming transcription.
    """
    try:
        if len(audio_chunk) < 1000:
            return None

        transcript = await transcribe_audio_deepgram(audio_chunk)

        if transcript:
            return {
                "text": transcript,
                "is_final": True,
            }

        return None

    except Exception as e:
        logger.error("Streaming transcription error", error=str(e))
        return None
