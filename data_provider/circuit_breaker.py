"""三态熔断器 — API连续失败后自动跳过数据源，避免浪费窗口时间

借鉴 daily_stock_analysis 的三态熔断器模式。
CLOSED → (连续失败 ≥ threshold) → OPEN → (冷却期满) → HALF_OPEN → (成功) → CLOSED
"""

import logging
import time
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """熔断器打开 — 调用方应跳过该数据源"""


class CircuitBreaker:
    """三态熔断器

    Args:
        name: 熔断器名称 (用于日志)
        failure_threshold: 连续失败次数阈值
        cooldown_sec: 熔断冷却时间 (秒)
    """

    def __init__(self, name: str, failure_threshold: int = 3, cooldown_sec: float = 300):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_sec = cooldown_sec
        self.state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._opened_at = 0.0

    @property
    def is_open(self) -> bool:
        """快速检查熔断器是否打开 (无副作用)"""
        if self.state == CircuitState.OPEN:
            if time.time() - self._opened_at >= self.cooldown_sec:
                self._transition_to(CircuitState.HALF_OPEN)
                return False
            return True
        return False

    def call(self, fn, *args, **kwargs):
        """执行调用，熔断打开时抛出 CircuitOpenError

        Usage:
            try:
                result = breaker.call(api.get_data, param1, param2)
            except CircuitOpenError:
                logger.warning("数据源已熔断，跳过")
                return fallback_value
        """
        if self.is_open:
            remaining = self.cooldown_sec - (time.time() - self._opened_at)
            raise CircuitOpenError(
                f"[{self.name}] 熔断中 (剩余 {remaining:.0f}s)"
            )

        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            self._on_failure()
            raise

        self._on_success()
        return result

    def _on_success(self):
        if self.state == CircuitState.HALF_OPEN:
            logger.info(f"[{self.name}] 半开探测成功 → 关闭熔断器")
        self._failure_count = 0
        self.state = CircuitState.CLOSED

    def _on_failure(self):
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._failure_count >= self.failure_threshold:
            self._transition_to(CircuitState.OPEN)

    def record_failure(self):
        """手动记录一次失败 (用于非异常的失败检测，如返回空数据)"""
        self._on_failure()

    def record_success(self):
        """手动记录一次成功 (用于非异常的成功检测)"""
        self._on_success()

    def _transition_to(self, state: CircuitState):
        old = self.state
        self.state = state
        if state == CircuitState.OPEN:
            self._opened_at = time.time()
            logger.warning(
                f"[{self.name}] 连续失败 {self._failure_count} 次 → 熔断打开 "
                f"(冷却 {self.cooldown_sec}s)"
            )
        elif state == CircuitState.HALF_OPEN:
            logger.info(
                f"[{self.name}] 冷却期满 → 半开探测 "
                f"(熔断了 {time.time() - self._opened_at:.0f}s)"
            )
