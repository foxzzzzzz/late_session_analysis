"""长生命周期服务器池：临时失败不能永久排除已恢复节点。"""
import json
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


def write_server_config(tmp_path, monkeypatch, servers):
    path = tmp_path / "tdx_servers.json"
    path.write_text(json.dumps({"priority_servers": servers}), encoding="utf-8")
    monkeypatch.setattr(module, "_SERVER_CONFIG", path, raising=False)
    return path


def test_configured_servers_are_first_and_deduplicated(tmp_path, monkeypatch):
    configured = [["configured", 7709], ["119.147.212.81", 7709], ["configured", 7709]]
    write_server_config(tmp_path, monkeypatch, configured)

    servers = module._collect_servers()

    assert servers[:2] == [("configured", 7709), ("119.147.212.81", 7709)]
    assert len(servers) == len(set(servers))
    assert ("117.34.114.13", 7709) in servers


def test_server_config_is_reread_on_collection(tmp_path, monkeypatch):
    path = write_server_config(tmp_path, monkeypatch, [["first", 7709]])
    assert ("first", 7709) in module._collect_servers()

    path.write_text(json.dumps({"priority_servers": [["second", 7709]]}), encoding="utf-8")

    servers = module._collect_servers()
    assert servers[0] == ("second", 7709)
    assert ("first", 7709) not in servers


def test_configured_server_enters_pool_and_survives_restart(tmp_path, monkeypatch):
    write_server_config(tmp_path, monkeypatch, [["configured", 7709]])
    monkeypatch.setattr(module, "_probe_server", lambda addr, port: addr == "configured")
    cache = tmp_path / "known_good.json"

    assert module.TdxServerPool(cache_file=cache).get_servers() == [("configured", 7709)]

    probe = Mock(side_effect=AssertionError("healthy cache should avoid scanning"))
    monkeypatch.setattr(module, "_probe_server", probe)
    assert module.TdxServerPool(cache_file=cache).get_servers() == [("configured", 7709)]
    probe.assert_not_called()
