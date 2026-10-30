"""引用只允许空白差异；非空白内容变化仍拒绝。"""
import json

import pytest

from analysis.parallel_runner import ParallelLLMRunner


SOURCE = '截至 2026 年\n10 月 29 日，累计回购 2,176,900 股，金额为 56,523,860.00 元；尚未完成。'


def parse_quote(quote, evidence_id='E1', text=SOURCE):
    response = {'summary': '公告研究', 'events': [{
        'classification': 'fact', 'subject': '公司', 'description': '回购进展',
        'evidence_id': evidence_id, 'quote': quote}], 'conflicts': [], 'unknowns': []}
    return ParallelLLMRunner._parse_response(json.dumps(response, ensure_ascii=False), '002891', [{
        'id': 'E1', 'text': text, 'url': 'https://example.test/notice',
        'published_at': '2026-10-29T17:43:08'}])


@pytest.mark.parametrize('quote,mode', [
    (SOURCE, 'exact'),
    (SOURCE.replace('\n', ' '), 'whitespace_only'),
    (SOURCE.replace(' ', '').replace('\n', ''), 'whitespace_only'),
    (SOURCE.replace(' ', '\t\r\n'), 'whitespace_only'),
    (SOURCE.replace(' ', '\u3000\u00a0'), 'whitespace_only'),
])
def test_whitespace_variants_preserve_original_quote(quote, mode):
    parsed = parse_quote(quote)
    assert len(parsed['events']) == 1
    event = parsed['events'][0]
    assert event['quote_matched'] is True
    assert event['quote_match_mode'] == mode
    assert event['quote'] == quote


@pytest.mark.parametrize('quote', [
    SOURCE.replace('2,176,900', '2,176,901'),
    SOURCE.replace('56,523,860.00', '56,523,860.01'),
    SOURCE.replace('元', '万元'),
    SOURCE.replace('尚未完成', '已经完成'),
    SOURCE.replace('尚未', ''),
    SOURCE.replace('；', '，'),
    SOURCE.replace('2,176,900', '2176900'),
    SOURCE.replace('累计回购', '回购累计'),
    SOURCE.replace('2026', '２０２６'),
    SOURCE.replace('公司', '企业').replace('股', '股份'),
    SOURCE.replace('回购', '回\u200b购'),
    ' \t\r\n\u3000',
])
def test_non_whitespace_changes_and_blank_quotes_are_rejected(quote):
    parsed = parse_quote(quote)
    assert parsed['events'] == []
    assert parsed['unknowns']


def test_unknown_evidence_id_still_rejected():
    parsed = parse_quote(SOURCE.replace('\n', ''), evidence_id='E999')
    assert not parsed['events']


def test_whitespace_in_source_can_be_absent_in_quote_but_case_changes_cannot():
    assert parse_quote('ABC', text='A B\nC')['events'][0]['quote_match_mode'] == 'whitespace_only'
    assert not parse_quote('abc', text='A B\nC')['events']


def test_cannot_join_noncontiguous_source_fragments():
    parsed = parse_quote('回购100股。已完成。', text='回购100股。尚有50股待回购。已完成。')
    assert not parsed['events']
    assert parsed['unknowns']
