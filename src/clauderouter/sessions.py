"""Resolve a connecting client's PID + cwd via /proc, for dashboard "session" labels."""

from __future__ import annotations

import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

# Seam for tests: monkeypatch this to point at a fake /proc tree.
PROC_ROOT = Path("/proc")


@dataclass(frozen=True)
class SessionInfo:
    pid: int | None
    cwd: str | None
    label: str  # "12345 (~/projectA)" or "unknown"

    def to_dict(self) -> dict:
        return {"pid": self.pid, "cwd": self.cwd, "label": self.label}


UNKNOWN_SESSION = SessionInfo(pid=None, cwd=None, label="unknown")


def _encode_ipv4_port(ip: str, port: int) -> str:
    """Encode ip:port as /proc/net/tcp's HEXIP:HEXPORT (little-endian IP, big-endian port)."""
    octets = [int(part) for part in ip.split(".")]
    hex_ip = "".join(f"{octet:02X}" for octet in reversed(octets))
    hex_port = f"{port:04X}"
    return f"{hex_ip}:{hex_port}"


def _build_label(pid: int, cwd: str | None) -> str:
    if cwd is None:
        return str(pid)
    home = os.environ.get("HOME")
    display_cwd = cwd
    if home and cwd.startswith(home):
        display_cwd = "~" + cwd[len(home):]
    return f"{pid} ({display_cwd})"


class SessionResolver:
    def __init__(self, cache_size: int = 256, cache_ttl_secs: float = 60.0) -> None:
        self._cache_size = cache_size
        self._cache_ttl_secs = cache_ttl_secs
        self._cache: OrderedDict[tuple[str, int], tuple[float, SessionInfo]] = OrderedDict()

    def resolve(self, peer_ip: str, peer_port: int, local_ip: str, local_port: int) -> SessionInfo:
        try:
            return self._resolve(peer_ip, peer_port, local_ip, local_port)
        except Exception:
            return UNKNOWN_SESSION

    def _resolve(self, peer_ip: str, peer_port: int, local_ip: str, local_port: int) -> SessionInfo:
        cache_key = (peer_ip, peer_port)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        if ":" in peer_ip or ":" in local_ip:
            return self._cache_put(cache_key, UNKNOWN_SESSION)

        target_local = _encode_ipv4_port(peer_ip, peer_port).upper()
        target_rem = _encode_ipv4_port(local_ip, local_port).upper()

        inode = self._find_inode(target_local, target_rem)
        if inode is None:
            return self._cache_put(cache_key, UNKNOWN_SESSION)

        pid = self._find_pid_for_inode(inode)
        if pid is None:
            return self._cache_put(cache_key, UNKNOWN_SESSION)

        cwd = self._read_cwd(pid)
        info = SessionInfo(pid=pid, cwd=cwd, label=_build_label(pid, cwd))
        return self._cache_put(cache_key, info)

    def _find_inode(self, target_local: str, target_rem: str) -> str | None:
        tcp_path = PROC_ROOT / "net" / "tcp"
        try:
            lines = tcp_path.read_text().splitlines()
        except (FileNotFoundError, PermissionError, OSError):
            return None

        for line in lines[1:]:
            fields = line.split()
            if len(fields) < 10:
                continue
            local_address = fields[1].upper()
            rem_address = fields[2].upper()
            if local_address == target_local and rem_address == target_rem:
                return fields[9]
        return None

    def _find_pid_for_inode(self, inode: str) -> int | None:
        target = f"socket:[{inode}]"
        for pid_dir in PROC_ROOT.glob("[0-9]*"):
            fd_dir = pid_dir / "fd"
            try:
                fds = list(fd_dir.iterdir())
            except (PermissionError, FileNotFoundError, OSError):
                continue
            for fd_path in fds:
                try:
                    link = os.readlink(fd_path)
                except (PermissionError, FileNotFoundError, OSError):
                    continue
                if link == target:
                    try:
                        return int(pid_dir.name)
                    except ValueError:
                        return None
        return None

    def _read_cwd(self, pid: int) -> str | None:
        try:
            return os.readlink(PROC_ROOT / str(pid) / "cwd")
        except (PermissionError, FileNotFoundError, OSError):
            return None

    def _cache_get(self, key: tuple[str, int]) -> SessionInfo | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        timestamp, info = entry
        if time.monotonic() - timestamp >= self._cache_ttl_secs:
            del self._cache[key]
            return None
        return info

    def _cache_put(self, key: tuple[str, int], info: SessionInfo) -> SessionInfo:
        self._cache[key] = (time.monotonic(), info)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return info
