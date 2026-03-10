from __future__ import annotations

from dataclasses import dataclass

from openai import AsyncOpenAI


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    api_key: str
    model: str


class EmbeddingProvider:
    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self._client = AsyncOpenAI(base_url=config.base_url, api_key=config.api_key)

    @property
    def model(self) -> str:
        return self._config.model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = await self._client.embeddings.create(model=self._config.model, input=texts)
        return [list(item.embedding) for item in response.data]


def build_embedding_provider(
    *,
    base_url: str,
    api_key: str,
    model: str,
) -> EmbeddingProvider | None:
    if not base_url.strip() or not api_key.strip() or not model.strip():
        return None
    return EmbeddingProvider(
        EmbeddingConfig(
            base_url=base_url.strip(),
            api_key=api_key.strip(),
            model=model.strip(),
        )
    )

