"""The three OpenAI endpoints the narrator uses, over plain HTTP.

Anki's Python has no OpenAI SDK and add-ons cannot install one, so the calls
are made with ``requests``, which Anki ships. Both are single requests with a
JSON body; nothing here streams.
"""

from __future__ import annotations

import json
from typing import Any

import requests

BASE_URL = "https://api.openai.com/v1"
TIMEOUT = 180


class OpenAIError(Exception):
    pass


class OpenAI:
    def __init__(self, api_key: str, base_url: str = BASE_URL) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()

    def _post(self, path: str, body: dict[str, Any]) -> requests.Response:
        if not self.api_key:
            raise OpenAIError("no OpenAI API key configured")
        try:
            response = self._session.post(
                f"{self.base_url}{path}",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                data=json.dumps(body),
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise OpenAIError(f"OpenAI not reachable: {exc}") from exc
        if response.status_code >= 400:
            raise OpenAIError(_describe(response))
        return response

    def complete(self, model: str, system: str, user: str) -> tuple[str, int, int]:
        """One answer from a chat model: its text, and the tokens in and out."""
        return self.chat(model, [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])

    def chat(self, model: str, messages: list[dict[str, Any]]) -> tuple[str, int, int]:
        """The next turn of a conversation: its text, and the tokens in and out."""
        body: dict[str, Any] = {"model": model, "messages": messages}
        # The reasoning models take no temperature and would spend their
        # effort thinking about a summary that needs none.
        if model.startswith(("gpt-5", "o1", "o3", "o4")):
            body["reasoning_effort"] = "low"
        else:
            body["temperature"] = 0.6
        data = self._post("/chat/completions", body).json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenAIError(f"unexpected answer from OpenAI: {data}") from exc
        usage = data.get("usage") or {}
        return text, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)

    def see(self, model: str, system: str, user: str, images: list[tuple[bytes, str, str]]) -> tuple[str, int, int]:
        """One answer from a model that is shown pictures: text, tokens in and out.

        ``images`` are (bytes, media type, detail) -- detail "low" costs a
        fixed few tokens and suits a whole figure, "high" a small crop.
        """
        import base64

        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for payload, mime, detail in images:
            data = base64.b64encode(payload).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}", "detail": detail}})
        return self.chat(model, [
            {"role": "system", "content": system},
            {"role": "user", "content": content},  # type: ignore[dict-item]
        ])

    def transcribe(self, model: str, audio: bytes, mime: str, language: str = "") -> str:
        """What was said in a recording, as text."""
        if not self.api_key:
            raise OpenAIError("no OpenAI API key configured")
        extension = "mp4" if "mp4" in mime else "ogg" if "ogg" in mime else "webm"
        data: dict[str, Any] = {"model": model, "response_format": "json"}
        if language and len(language) == 2:
            data["language"] = language
        try:
            response = self._session.post(
                f"{self.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                data=data, files={"file": (f"speech.{extension}", audio, mime)}, timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise OpenAIError(f"OpenAI not reachable: {exc}") from exc
        if response.status_code >= 400:
            raise OpenAIError(_describe(response))
        try:
            return (response.json().get("text") or "").strip()
        except ValueError as exc:
            raise OpenAIError("unexpected answer from OpenAI") from exc

    def speak(self, model: str, voice: str, text: str, instructions: str = "") -> bytes:
        """The text as MP3."""
        body: dict[str, Any] = {
            "model": model,
            "voice": voice,
            "input": text,
            "response_format": "mp3",
        }
        # Only the gpt-4o voices take directions; tts-1 refuses the field.
        if instructions and model.startswith("gpt-4o"):
            body["instructions"] = instructions
        return self._post("/audio/speech", body).content


def _describe(response: requests.Response) -> str:
    try:
        message = response.json()["error"]["message"]
    except Exception:
        message = response.text[:300]
    return f"OpenAI answered {response.status_code}: {message}"
