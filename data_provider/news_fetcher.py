"""个股新闻/公告数据获取 — 东方财富公告API

数据源: np-anotice-stock.eastmoney.com
用途: L3阶段检测个股近3日是否有重大利空公告 (has_bad_news)

东财请求统一走 rate_limiter.em_get() 门控 (QPS≤1)。
"""
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

from data_provider.rate_limiter import em_get

logger = logging.getLogger(__name__)

# 利空关键词 — 匹配到任何一个即标记为利空
NEGATIVE_KEYWORDS = [
    '减持', '违规', '亏损', '立案', '监管', '警示', '处罚',
    '退市', '破产', '清算', '诉讼', '冻结', '质押',
    'ST', '*ST', '终止上市', '暂停上市', '问询函', '关注函',
    '终止重组', '重组失败', '下修业绩', '业绩变脸', '预亏',
    '商誉减值', '资产减值', '计提减值', '大额坏账',
    '无法表示意见', '保留意见', '否定意见',
    '更正公告', '修正公告',  # 可能暗示之前披露有误
    '立案调查', '行政处罚', '市场禁入',
    '控股股东', '被动减持',  # 组合判断
    '退市风险', '风险警示',
]


class NewsFetcher:
    """个股公告/新闻数据获取器

    直接调用东方财富公告API (非akshare)，返回结构化JSON数据。
    限流由 rate_limiter.em_get() 统一门控 (QPS≤1)。
    """

    BASE_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"

    def __init__(self, lookback_days: int = 3):
        self.lookback_days = lookback_days

    def fetch_announcements(self, code: str, max_items: int = 30, timeout: float = 15,
                            deadline: float = None) -> list[dict]:
        """获取个股公告列表

        Args:
            code: 股票代码 (6位)
            max_items: 最大返回条数

        Returns:
            公告列表，每项含 title, notice_date, announcement_type 等字段
        """
        params = {
            'page_size': max_items,
            'page_index': 1,
            'ann_type': 'A',  # A=全部公告
            'client_source': 'web',
            'stock_list': code,
        }
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0'}

        try:
            r = em_get(self.BASE_URL, params=params, headers=headers, timeout=timeout, deadline=deadline)
            r.raise_for_status()
            r.encoding = 'utf-8'
            data = r.json()
            if data.get('success') and data.get('data'):
                return data['data'].get('list', [])
        except requests.RequestException as e:
            logger.warning(f"公告API请求失败 [{code}]: {e}")
        except (ValueError, KeyError) as e:
            logger.warning(f"公告API解析失败 [{code}]: {e}")

        return []

    def collect_evidence(self, code: str, as_of: datetime, *, max_items: int,
                         max_chars: int, timeout: float, lookback_days: int = None,
                         deadline: float = None) -> dict:
        """旁路公告原文摘录。仅接纳明确披露时间，不能将日期当成盘中可见时间。"""
        items, unknowns = [], []
        if deadline is not None and time.monotonic() >= deadline:
            return {'status': 'unavailable', 'items': [], 'unknowns': ['旁路整批截止，未获取公告']}
        notices = self.fetch_announcements(code, timeout=timeout, deadline=deadline)
        if not notices:
            return {'status': 'unavailable', 'items': [],
                    'unknowns': ['公告列表为空或获取失败，不代表无公告/无风险']}
        cutoff = as_of - timedelta(days=lookback_days if lookback_days is not None else self.lookback_days)
        seen = set()
        attempted = 0
        for notice in notices:
            if deadline is not None and time.monotonic() >= deadline:
                unknowns.append('旁路整批截止，剩余公告未获取')
                break
            if attempted >= max_items:
                break
            stamp = notice.get('display_time', '')
            try:
                # 数据源格式: YYYY-MM-DD HH:MM:SS:毫秒。
                stamp = re.sub(r'(?<=\d{2}:\d{2}:\d{2}):(\d+)$', r'.\1', stamp)
                published = datetime.fromisoformat(stamp)
            except (ValueError, TypeError):
                unknowns.append(f"发布时间不明确，未采用: {notice.get('title', '')}")
                continue
            if published > as_of or published < cutoff:
                continue
            art_code = notice.get('art_code')
            if not art_code or art_code in seen:
                continue
            seen.add(art_code)
            attempted += 1
            endpoint = 'https://np-cnotice-stock.eastmoney.com/api/content/ann'
            try:
                r = em_get(endpoint, params={'art_code': art_code, 'client_source': 'web',
                                             'page_index': 1}, timeout=timeout, deadline=deadline)
                r.raise_for_status()
                r.encoding = 'utf-8'
                content = r.json().get('data') or {}
                if content.get('is_ai_summary') in (True, 1, '1', 'true'):
                    unknowns.append(f'返回的是AI摘要而非原文，未采用: {art_code}')
                    continue
                text = content.get('notice_content') or ''
                if not text.strip():
                    unknowns.append(f'公告原文未获取: {art_code}')
                    continue
                items.append({
                    'id': f'E{len(items)+1}', 'title': notice.get('title', ''),
                    'published_at': published.isoformat(),
                    'source': 'eastmoney_announcement',
                    'url': f'{endpoint}?art_code={art_code}&client_source=web&page_index=1',
                    'attachment_url': content.get('attach_url', ''),
                    'text': text[:max_chars], 'truncated': len(text) > max_chars,
                })
            except (requests.RequestException, ValueError, TypeError) as e:
                logger.warning(f'旁路公告正文获取失败 [{code}/{art_code}]: {type(e).__name__}')
                unknowns.append(f'公告原文获取失败: {art_code}')
        # 数量、正文长度和列表覆盖均有限，不能据此断言不存在其他事件。
        unknowns.append('公告列表及原文摘录覆盖有限，不代表完整新闻检索或无其他风险')
        return {'status': 'partial' if items else 'unavailable', 'items': items, 'unknowns': unknowns}

    def check_bad_news(self, code: str) -> bool:
        """检查个股近N日是否有重大利空公告

        Args:
            code: 股票代码

        Returns:
            True = 存在利空公告, 应排除该股票
        """
        cutoff_date = datetime.now() - timedelta(days=self.lookback_days)
        announcements = self.fetch_announcements(code)

        if not announcements:
            return False

        for item in announcements:
            notice_date_str = item.get('notice_date', '')
            if not notice_date_str:
                continue

            # 解析公告日期
            try:
                notice_date = datetime.strptime(notice_date_str[:10], '%Y-%m-%d')
            except ValueError:
                continue

            if notice_date < cutoff_date:
                continue

            # 检查标题关键词
            title = item.get('title', '')
            if self._has_negative_keywords(title):
                logger.info(
                    f"[{code}] 检测到利空公告: {title[:60]}... "
                    f"({notice_date.strftime('%m-%d')})"
                )
                return True

        return False

    def _has_negative_keywords(self, text: str) -> bool:
        """检查文本是否包含利空关键词

        使用组合规则避免误判:
          - '控股股东' 单独出现不算利空，需配合其他关键词
          - '质押' 需区分'解除质押'(利好) 和'新增质押'(利空)
        """
        if not text:
            return False

        # 先排除常见利好表述
        if '解除质押' in text or '回购' in text:
            # 但不能仅凭这个放过 — 同一条公告可能同时有利好和利空
            pass

        for kw in NEGATIVE_KEYWORDS:
            if kw in text:
                # 特殊处理: '质押' 仅在明确为'新增质押'或'股权质押'且非'解除'时标记
                if kw == '质押':
                    if '解除质押' in text:
                        continue
                return True

        return False

    def health_check(self) -> dict:
        """健康检查 — 测试API是否可达"""
        announcements = self.fetch_announcements('600519', max_items=5)
        return {
            'ok': len(announcements) > 0,
            'sample_count': len(announcements),
            'source': 'eastmoney_announcement',
        }
