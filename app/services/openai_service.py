import json
import logging
from openai import AzureOpenAI, OpenAI
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential
from app.config import settings

logger = logging.getLogger(__name__)


class LLMNotConfigured(RuntimeError):
    """
    Raised immediately (no retries) when no LLM provider is configured.
    Every agent catches LLM failures and falls back to its deterministic,
    rule-based output, so the full pipeline still runs offline.
    """


class OpenAIService:
    """
    Single LLM gateway. Provider is chosen from configuration:
      • Azure OpenAI  — AZURE_OPENAI_KEY + AZURE_OPENAI_ENDPOINT
      • OpenAI        — OPENAI_API_KEY (optional OPENAI_BASE_URL for
                        any OpenAI-compatible server, e.g. a local model)
      • offline       — neither set; agents use their rule-based fallbacks
    """

    def __init__(self):
        self.provider = settings.llm_provider
        self.client = None
        if self.provider == "azure_openai":
            self.client = AzureOpenAI(
                api_key=settings.AZURE_OPENAI_KEY,
                azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
                api_version="2024-08-01-preview",
            )
            self.large_model = settings.AZURE_OPENAI_DEPLOYMENT_LARGE
            self.small_model = settings.AZURE_OPENAI_DEPLOYMENT_SMALL
        elif self.provider == "openai":
            self.client = OpenAI(
                api_key=settings.OPENAI_API_KEY,
                base_url=settings.OPENAI_BASE_URL or None,
            )
            self.large_model = settings.OPENAI_MODEL_LARGE
            self.small_model = settings.OPENAI_MODEL_SMALL
        else:
            self.large_model = self.small_model = "offline"
            logger.warning(
                "No LLM configured — agents will use rule-based fallbacks. "
                "Set AZURE_OPENAI_KEY/AZURE_OPENAI_ENDPOINT or OPENAI_API_KEY for AI analysis."
            )

    @property
    def is_configured(self) -> bool:
        return self.client is not None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_not_exception_type(LLMNotConfigured),
        reraise=True,
    )
    def call(
        self,
        system_prompt: str,
        user_message: str,
        use_large_model: bool = False,
        json_mode: bool = True,
        temperature: float = 0.3,
        max_tokens: int = 2000,
    ) -> str:
        """
        Single method for all LLM calls.
        use_large_model=True  → large model (Legal Agent, Negotiation Agent)
        use_large_model=False → small model (Intake, Analytics, Document)
        """
        if self.client is None:
            raise LLMNotConfigured("offline mode (no LLM configured) — using rule-based fallback")

        model = self.large_model if use_large_model else self.small_model

        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = self.client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content
            usage = getattr(response, "usage", None)
            logger.info(
                f"LLM call successful | provider={self.provider} | model={model} | "
                f"tokens={getattr(usage, 'total_tokens', '?')}"
            )
            return content

        except Exception as e:
            logger.error(f"LLM call failed | provider={self.provider} | model={model} | error={e}")
            raise

    def call_json(
        self,
        system_prompt: str,
        user_message: str,
        use_large_model: bool = False,
        temperature: float = 0.3,
        max_tokens: int = 2000,
    ) -> dict:
        """
        Calls LLM and parses JSON response.
        Use this when you need a dict back directly.
        """
        raw = self.call(
            system_prompt=system_prompt,
            user_message=user_message,
            use_large_model=use_large_model,
            json_mode=True,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse failed | raw={raw[:200]} | error={e}")
            raise ValueError(f"LLM returned invalid JSON: {e}")


# Singleton — import this everywhere
openai_service = OpenAIService()
