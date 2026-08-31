"""Audio transcription client for the remote app server."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import httpx


class TranscriptionError(Exception):
    """Error during transcription."""

    pass


class TranscriptionClient(Protocol):
    """Protocol for audio transcription clients."""

    async def transcribe(self, audio_file: Path) -> str:
        """Transcribe an audio file and return the text."""
        ...


class OpenAICompatibleTranscriptionClient:
    """Transcription client for OpenAI-compatible APIs."""

    def __init__(self, server_url: str, auth_token: str | None = None):
        """Initialize the transcription client.

        Args:
            server_url: Base URL of the transcription server
            auth_token: Optional bearer token for authentication
        """
        self.server_url = server_url.rstrip("/")
        self.auth_token = auth_token

    async def transcribe(self, audio_file: Path) -> str:
        """Transcribe an audio file using the configured server.

        Args:
            audio_file: Path to the audio file to transcribe

        Returns:
            Transcribed text

        Raises:
            TranscriptionError: If transcription fails
        """
        if not audio_file.exists():
            raise TranscriptionError(f"Audio file not found: {audio_file}")

        headers = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(audio_file, "rb") as f:
                    files = {"file": (audio_file.name, f, "audio/webm")}
                    data = {"model": "whisper-1"}

                    response = await client.post(
                        f"{self.server_url}/v1/audio/transcriptions",
                        headers=headers,
                        files=files,
                        data=data,
                    )

                if response.status_code != 200:
                    error_detail = response.text
                    try:
                        error_json = response.json()
                        error_detail = error_json.get("error", {}).get("message", error_detail)
                    except Exception:
                        pass
                    raise TranscriptionError(
                        f"Transcription failed with status {response.status_code}: {error_detail}"
                    )

                result = response.json()
                return result.get("text", "")

        except httpx.TimeoutException as e:
            raise TranscriptionError(f"Transcription request timed out: {e}")
        except httpx.RequestError as e:
            raise TranscriptionError(f"Transcription request failed: {e}")
        except Exception as e:
            if isinstance(e, TranscriptionError):
                raise
            raise TranscriptionError(f"Unexpected transcription error: {e}")


def create_transcription_client(
    server_url: str | None, auth_token: str | None = None
) -> TranscriptionClient | None:
    """Create a transcription client if configuration is available.

    Args:
        server_url: Base URL of the transcription server
        auth_token: Optional bearer token for authentication

    Returns:
        TranscriptionClient instance or None if not configured
    """
    if not server_url:
        return None

    return OpenAICompatibleTranscriptionClient(server_url, auth_token)
