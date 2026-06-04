"""LLM客户端 — LiteLLM统一接口封装，参考DSA + RD-Agent"""
import os
import time
import random
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class LLMClient:
    """LiteLLM 统一接口封装"""

    def __init__(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        timeout: int = 15,
    ):
        self.provider = provider or os.getenv("LLM_PROVIDER", "deepseek")
        self.model = model or os.getenv("LLM_MODEL", "deepseek-chat")
        self.api_key = api_key or os.getenv("LLM_API_KEY", "")
        self.api_base = api_base or os.getenv("LLM_API_BASE", "")
        self.max_tokens = max_tokens or int(os.getenv("LLM_MAX_TOKENS", "1024"))
        self.temperature = temperature or float(os.getenv("LLM_TEMPERATURE", "0.3"))
        self.timeout = timeout

        self._model_id = f"{self.provider}/{self.model}"

    def chat(self, system_prompt: str, user_prompt: str, max_retries: int = 2) -> Optional[str]:
        """发送聊天请求，空响应/失败时指数退避重试"""
        import litellm
        litellm.suppress_debug_info = True

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        for attempt in range(max_retries + 1):
            try:
                kwargs = {
                    "model": self._model_id,
                    "messages": messages,
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature,
                    "timeout": self.timeout,
                    "response_format": {"type": "json_object"},
                }
                if self.api_key:
                    kwargs["api_key"] = self.api_key
                if self.api_base:
                    kwargs["api_base"] = self.api_base

                response = litellm.completion(**kwargs)
                msg = response.choices[0].message
                content = msg.content

                # v4-pro 可能把内容放在 reasoning_content 字段
                if not content:
                    content = getattr(msg, "reasoning_content", None)

                if content and content.strip():
                    return content.strip()

                if attempt < max_retries:
                    delay = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(
                        f"LLM返回空内容 (finish_reason={response.choices[0].finish_reason}), "
                        f"退避 {delay:.1f}s 后重试 {attempt + 1}/{max_retries} "
                        f"({self._model_id})"
                    )
                    time.sleep(delay)

            except Exception as e:
                if attempt < max_retries:
                    delay = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(
                        f"LLM调用失败，退避 {delay:.1f}s 后重试 {attempt + 1}/{max_retries} "
                        f"({self._model_id}): {e}"
                    )
                    time.sleep(delay)
                else:
                    logger.error(f"LLM调用失败 ({self._model_id}): {e}")

        return None

    def available(self) -> bool:
        """检查LLM是否可用"""
        try:
            import litellm
            return True
        except ImportError:
            return False
