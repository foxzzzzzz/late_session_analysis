"""长生命周期服务器池：临时失败不能永久排除已恢复节点。"""
from unittest.mock import Mock

from data_provider import tdx_server_pool as module


def make_pool(tmp_path, monkeypatch):
    servers = [("recovered", 7709), ("offline", 7709)]
    monkeypatch.setattr(module, "_collect_servers", lambda: servers)
    pool = module.TdxServerPool(cache_file=tmp_path / "servers.json")
    pool._loaded = True
    pool._good = [servers[0]]
    return pool, servers


def test_full_rescan_reprobes_failed_server(tmp_path, monkeypatch):
    pool, servers = make_pool(tmp_path, monkeypatch)
    pool.mark_failed(*servers[0])
    probe = Mock(side_effect=lambda addr, port: addr == "recovered")
    monkeypatch.setattr(module, "_probe_server", probe)

    assert pool.get_servers() == [servers[0]]
    assert servers[0] in [call.args for call in probe.call_args_list]
    assert pool.snapshot()["failed"] == []
    # 恢复结果落盘，服务重启也能复用。
    restarted = module.TdxServerPool(cache_file=tmp_path / "servers.json")
    assert restarted.get_servers() == [servers[0]]


def test_recovery_after_repeated_empty_scans(tmp_path, monkeypatch):
    pool, servers = make_pool(tmp_path, monkeypatch)
    pool.mark_failed(*servers[0])
    probe = Mock(return_value=False)
    monkeypatch.setattr(module, "_probe_server", probe)

    assert pool.get_servers() == []
    assert pool.get_servers() == []
    probe.side_effect = lambda addr, port: addr == "recovered"
    assert pool.get_servers() == [servers[0]]


def test_healthy_server_still_used_without_full_scan(tmp_path, monkeypatch):
    pool, servers = make_pool(tmp_path, monkeypatch)
    pool._good = list(servers)
    pool.mark_failed(*servers[0])
    probe = Mock()
    monkeypatch.setattr(module, "_probe_server", probe)

    assert pool.get_servers() == [servers[1]]
    probe.assert_not_called()
