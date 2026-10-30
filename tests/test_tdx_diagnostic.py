import struct
from unittest.mock import Mock
from contextlib import nullcontext
from types import SimpleNamespace

from tools.diagnose_tdx_access import inspect_query, probe_backend
from tools import diagnose_tdx_access as diagnostic


class Parser:
    def __init__(self, client):
        self.client = client

    def setParams(self, *args):
        pass

    def call_api(self):
        return self.parseResponse(self.client)

    def parseResponse(self, body):
        struct.unpack('<I', body[2:6])
        return [{'datetime': '2026-10-29 14:00'}]


def test_short_response_keeps_raw_evidence():
    result = inspect_query(Mock(client=b'\x20\x03'), Parser, [9, 0, '000001', 0, 5], 32)
    assert result['status'] == 'query_error'
    assert result['body_bytes'] == 2
    assert result['body_hex_prefix'] == '2003'
    assert result['declared_count'] == 800
    assert 'unpack' in result['error']


def test_valid_response_keeps_row_count_and_latest():
    result = inspect_query(Mock(client=b'\x01\x00' + b'\x00' * 4), Parser, [], 32)
    assert result['status'] == 'ok'
    assert result['rows'] == 1
    assert result['latest'] == '2026-10-29 14:00'


def test_backend_connection_error_is_reported():
    factory = Mock()
    factory.return_value.connect.side_effect = ConnectionRefusedError('refused')
    queries = [{'name': 'daily', 'parser': 'security', 'args': []}]
    results = probe_backend(('host', 7709), factory, {'security': Parser}, queries, 3, 32)
    assert results[0]['status'] == 'connect_error'
    assert 'refused' in results[0]['error']
    factory.return_value.disconnect.assert_called_once()


def test_each_query_has_independent_connection():
    first, second = Mock(client=b'\x20\x03'), Mock(client=b'\x01\x00' + b'\x00' * 4)
    factory = Mock(side_effect=[first, second])
    queries = [{'name': name, 'parser': 'security', 'args': []} for name in ('daily', 'minute')]
    results = probe_backend(('host', 7709), factory, {'security': Parser}, queries, 3, 32)
    assert [r['status'] for r in results] == ['query_error', 'ok']
    assert factory.call_count == 2
    first.disconnect.assert_called_once()
    second.disconnect.assert_called_once()


def test_probe_loads_both_backend_parsers(monkeypatch):
    imports = []
    hq = SimpleNamespace(TdxHq_API=Mock(), GetSecurityBarsCmd=Parser, GetIndexBarsCmd=Parser)
    def load(name):
        imports.append(name)
        return hq
    monkeypatch.setattr(diagnostic.importlib, 'import_module', load)
    monkeypatch.setattr(diagnostic.socket, 'create_connection', lambda *a, **k: nullcontext())
    probe = Mock(return_value=[{'status': 'ok'}])
    monkeypatch.setattr(diagnostic, 'probe_backend', probe)
    result = diagnostic.probe_server(('host', 7709), {
        'timeout_seconds': 3, 'queries': [], 'hex_prefix_bytes': 32})
    assert imports == ['pytdx.hq', 'tdxpy.hq']
    assert set(result['backends']) == {'pytdx', 'tdxpy'}
    assert probe.call_count == 2
