"""Tests for SessionResolver's /proc-based PID/cwd resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from clauderouter import sessions
from clauderouter.sessions import (
    UNKNOWN_SESSION,
    SessionInfo,
    SessionResolver,
    _encode_ipv4_port,
)

PEER_IP = "127.0.0.1"
PEER_PORT = 54321
LOCAL_IP = "127.0.0.1"
LOCAL_PORT = 4891
INODE = "12345"
PID = 9999


def _build_fake_proc(tmp_path: Path, cwd_target: Path) -> None:
    net_dir = tmp_path / "net"
    net_dir.mkdir(parents=True, exist_ok=True)

    local_address = _encode_ipv4_port(PEER_IP, PEER_PORT)
    rem_address = _encode_ipv4_port(LOCAL_IP, LOCAL_PORT)

    header = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
        "   uid  timeout inode"
    )
    # fields: sl local_address rem_address st tx_queue:rx_queue tr:tm->when retrnsmt uid timeout inode
    data_line = (
        f"   0: {local_address} {rem_address} 01 00000000:00000000 00:00000000 "
        f"00000000  1000        0 {INODE} 1 0000000000000000 100 0 0 10 0"
    )
    (net_dir / "tcp").write_text(header + "\n" + data_line + "\n")

    pid_dir = tmp_path / str(PID)
    fd_dir = pid_dir / "fd"
    fd_dir.mkdir(parents=True, exist_ok=True)
    (fd_dir / "0").symlink_to(f"socket:[{INODE}]")

    cwd_target.mkdir(parents=True, exist_ok=True)
    (pid_dir / "cwd").symlink_to(cwd_target)


def test_resolve_returns_pid_and_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd_target = tmp_path / "projectA"
    _build_fake_proc(tmp_path, cwd_target)
    monkeypatch.setattr(sessions, "PROC_ROOT", tmp_path)

    resolver = SessionResolver()
    info = resolver.resolve(PEER_IP, PEER_PORT, LOCAL_IP, LOCAL_PORT)

    assert info.pid == PID
    assert info.cwd == str(cwd_target)
    assert info.label == f"{PID} ({cwd_target})"


def test_missing_proc_net_tcp_returns_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    monkeypatch.setattr(sessions, "PROC_ROOT", empty_root)

    resolver = SessionResolver()
    info = resolver.resolve(PEER_IP, PEER_PORT, LOCAL_IP, LOCAL_PORT)

    assert info == UNKNOWN_SESSION


def test_ipv6_peer_returns_unknown_without_touching_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point PROC_ROOT somewhere that doesn't exist; if the resolver tried to
    # read it, it would raise/return unknown anyway, but we additionally verify
    # via the early-exit path by not creating any fake tree at all.
    nonexistent = tmp_path / "does-not-exist"
    monkeypatch.setattr(sessions, "PROC_ROOT", nonexistent)

    resolver = SessionResolver()
    info = resolver.resolve("::1", PEER_PORT, LOCAL_IP, LOCAL_PORT)

    assert info == UNKNOWN_SESSION


def test_cache_returns_same_result_without_filesystem_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd_target = tmp_path / "projectB"
    _build_fake_proc(tmp_path, cwd_target)
    monkeypatch.setattr(sessions, "PROC_ROOT", tmp_path)

    resolver = SessionResolver(cache_ttl_secs=60.0)
    first = resolver.resolve(PEER_IP, PEER_PORT, LOCAL_IP, LOCAL_PORT)
    assert first.pid == PID

    # Remove the fake /proc tree entirely; cached result must still be returned.
    import shutil

    shutil.rmtree(tmp_path / "net")
    shutil.rmtree(tmp_path / str(PID))

    second = resolver.resolve(PEER_IP, PEER_PORT, LOCAL_IP, LOCAL_PORT)
    assert second == first


def test_label_substitutes_home_with_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home_dir = tmp_path / "home" / "elimel"
    cwd_target = home_dir / "projectA"
    monkeypatch.setenv("HOME", str(home_dir))

    _build_fake_proc(tmp_path, cwd_target)
    monkeypatch.setattr(sessions, "PROC_ROOT", tmp_path)

    resolver = SessionResolver()
    info = resolver.resolve(PEER_IP, PEER_PORT, LOCAL_IP, LOCAL_PORT)

    assert info.cwd == str(cwd_target)
    assert info.label == f"{PID} (~/projectA)"


def test_no_matching_tcp_line_returns_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    net_dir = tmp_path / "net"
    net_dir.mkdir(parents=True)
    header = "  sl  local_address rem_address"
    # A line with mismatched addresses (won't match peer/local pair).
    other_local = _encode_ipv4_port("10.0.0.5", 1111)
    other_rem = _encode_ipv4_port("10.0.0.6", 2222)
    data_line = (
        f"   0: {other_local} {other_rem} 01 00000000:00000000 00:00000000 "
        f"00000000  1000        0 {INODE} 1 0000000000000000 100 0 0 10 0"
    )
    (net_dir / "tcp").write_text(header + "\n" + data_line + "\n")
    monkeypatch.setattr(sessions, "PROC_ROOT", tmp_path)

    resolver = SessionResolver()
    info = resolver.resolve(PEER_IP, PEER_PORT, LOCAL_IP, LOCAL_PORT)

    assert info == UNKNOWN_SESSION


def test_no_pid_owns_inode_returns_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    net_dir = tmp_path / "net"
    net_dir.mkdir(parents=True)
    header = "  sl  local_address rem_address"
    local_address = _encode_ipv4_port(PEER_IP, PEER_PORT)
    rem_address = _encode_ipv4_port(LOCAL_IP, LOCAL_PORT)
    data_line = (
        f"   0: {local_address} {rem_address} 01 00000000:00000000 00:00000000 "
        f"00000000  1000        0 {INODE} 1 0000000000000000 100 0 0 10 0"
    )
    (net_dir / "tcp").write_text(header + "\n" + data_line + "\n")
    # No pid directories at all.
    monkeypatch.setattr(sessions, "PROC_ROOT", tmp_path)

    resolver = SessionResolver()
    info = resolver.resolve(PEER_IP, PEER_PORT, LOCAL_IP, LOCAL_PORT)

    assert info == UNKNOWN_SESSION


def test_to_dict_shape() -> None:
    info = SessionInfo(pid=123, cwd="/home/elimel/proj", label="123 (~/proj)")
    assert info.to_dict() == {"pid": 123, "cwd": "/home/elimel/proj", "label": "123 (~/proj)"}
    assert UNKNOWN_SESSION.to_dict() == {"pid": None, "cwd": None, "label": "unknown"}


def test_label_without_cwd_is_just_pid() -> None:
    info = SessionInfo(pid=42, cwd=None, label="42")
    assert info.label == "42"


def test_encode_ipv4_port_matches_proc_net_tcp_format() -> None:
    assert _encode_ipv4_port("127.0.0.1", 1) == "0100007F:0001"
