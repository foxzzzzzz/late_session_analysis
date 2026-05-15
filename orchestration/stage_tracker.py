"""阶段状态追踪 — 记录每阶段耗时、筛选数量、异常"""
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class StageInfo:
    name: str
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    input_count: int = 0
    output_count: int = 0
    error: Optional[str] = None

    @property
    def elapsed(self) -> float:
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return 0.0

    @property
    def pass_rate(self) -> float:
        if self.input_count > 0:
            return self.output_count / self.input_count * 100
        return 0.0


class StageTracker:
    """7阶段时间线和状态追踪"""

    STAGES = ["S0_板块预筛选", "S1_K线扫描", "S2_尾盘异常", "S3_均线验证", "S4_评分冲刺"]

    def __init__(self):
        self.stages: dict[str, StageInfo] = {s: StageInfo(s) for s in self.STAGES}
        self.pipeline_start: float = 0.0
        self.pipeline_end: float = 0.0
        self.llm_success: int = 0
        self.llm_total: int = 0

    def _ensure_stage(self, stage: str):
        """动态创建未预定义的阶段"""
        if stage not in self.stages:
            self.stages[stage] = StageInfo(stage)

    def start(self):
        """开始整个流水线"""
        self.pipeline_start = time.time()
        logger.info("=" * 50)
        logger.info("尾盘分析系统启动")

    def stage_start(self, stage: str, input_count: int):
        """某阶段开始"""
        self._ensure_stage(stage)
        s = self.stages[stage]
        s.start_time = time.time()
        s.input_count = input_count
        logger.info(f"[{stage}] 开始, 输入: {input_count} 只")

    def stage_end(self, stage: str, output_count: int, error: str = ""):
        """某阶段结束"""
        self._ensure_stage(stage)
        s = self.stages[stage]
        s.end_time = time.time()
        s.output_count = output_count
        if error:
            s.error = error
        logger.info(f"[{stage}] 完成: {s.input_count}→{output_count} ({s.elapsed:.1f}s)")

    def finish(self) -> dict:
        """完成流水线，返回汇总统计"""
        self.pipeline_end = time.time()
        total = self.pipeline_end - self.pipeline_start

        logger.info("=" * 50)
        logger.info(f"流水线完成, 总耗时: {total:.1f}s")

        return {
            'total_elapsed': total,
            'llm_success': self.llm_success,
            'llm_total': self.llm_total,
        }
