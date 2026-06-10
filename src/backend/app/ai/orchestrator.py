import json
import logging

from app.ai.ollama_service import OllamaService
from app.ai.schemas import AIOutputSchema
from app.ai.service import AIService
from app.ai.whisper_service import WhisperService

logger = logging.getLogger(__name__)


class AIOrchestrator:
    def __init__(self):
        self.text_service: AIService = OllamaService()
        self.audio_service: AIService = WhisperService()

    async def transcribe_audio(self, audio_url: str, trace_id: str | None = None) -> str:
        return await self.audio_service.transcribe_audio(audio_url, trace_id=trace_id)

    async def analyze_images(self, image_urls: list[str], trace_id: str | None = None) -> str:
        return await self.text_service.analyze_images(image_urls, trace_id=trace_id)

    async def generate_structured_output(
        self,
        transcript: str = "",
        vision_analysis: str = "",
        job_metadata: dict | None = None,
        trace_id: str | None = None,
    ) -> AIOutputSchema:
        return await self.text_service.generate_structured_output(
            transcript=transcript,
            vision_analysis=vision_analysis,
            job_metadata=job_metadata or {},
            trace_id=trace_id,
        )

    async def generate_chat_output(
        self,
        system_prompt: str = "",
        user_prompt: str = "",
        trace_id: str | None = None,
    ) -> dict | None:
        from app.config import get_settings

        _settings = get_settings()
        try:
            result = await self.text_service._generate(
                model=_settings.ollama_text_model,
                prompt=user_prompt,
                system=system_prompt,
                trace_id=trace_id,
            )
            if not result:
                return None
            cleaned = result.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1]
                cleaned = cleaned.rsplit("```", 1)[0]
            cleaned = cleaned.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[len("```json") :].strip()
                cleaned = cleaned.rsplit("```", 1)[0].strip()
            content = cleaned.strip()
            if not content:
                return None
            return json.loads(content)
        except Exception as e:
            logger.error("generate_chat_output failed: %s", e)
            return None

    async def health(self) -> dict:
        text_health = await self.text_service.health()
        audio_health = await self.audio_service.health()
        return {
            "text_service": text_health,
            "audio_service": audio_health,
            "ollama_available": text_health.get("available", False),
            "whisper_available": audio_health.get("available", False),
        }


orchestrator = AIOrchestrator()
