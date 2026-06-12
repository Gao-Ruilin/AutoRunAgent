"""
SSH Client — Manage SSH connections via paramiko.

Features:
- Password and key-based authentication
- Connection pool (singleton per host:port)
- Keep-alive heartbeat
- Timeout-aware command execution
"""

from __future__ import annotations

import base64
import logging
import posixpath
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


# ── Lazy paramiko import ─────────────────────────────────────────────────
# 避免 bad DLL (cryptography/_rust) 导致整个 WebSocket 崩溃。
# paramiko 只在 SSH 工具实际执行时才被加载。

_paramiko_module = None
_paramiko_error: Optional[str] = None


def _get_paramiko():
    """惰性导入 paramiko，失败时返回 None 并缓存错误信息。"""
    global _paramiko_module, _paramiko_error
    if _paramiko_module is not None:
        return _paramiko_module
    if _paramiko_error is not None:
        return None
    try:
        import paramiko as _p
        _paramiko_module = _p
        return _p
    except ImportError as e:
        _paramiko_error = str(e)
        logger.error("Failed to import paramiko (SSH tools unavailable): %s", e)
        return None


class _ParamikoProxy:
    """惰性代理 — 只有实际访问时才导入 paramiko。"""

    def __getattr__(self, name: str):
        p = _get_paramiko()
        if p is None:
            raise ImportError(
                f"paramiko 不可用 ({_paramiko_error})，SSH 功能已禁用。"
                f"请检查 cryptography/bcrypt 依赖是否兼容当前 Python 版本。"
            )
        return getattr(p, name)


paramiko = _ParamikoProxy()

logger = logging.getLogger(__name__)

# ── Sensitive field filter for logging ──────────────────────────────────
SENSITIVE_KEYS = {"password", "key_path", "private_key", "passphrase"}


def _sanitize(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of data with sensitive fields redacted."""
    return {
        k: ("***" if k in SENSITIVE_KEYS else v)
        for k, v in data.items()
    }


# ── Connection pool ─────────────────────────────────────────────────────
# Keyed by (host, port), stores SSHSession instances

_pool: Dict[Tuple[str, int], "_SSHSession"] = {}
_pool_lock = threading.Lock()


@dataclass
class _SSHSession:
    """Internal session wrapper with metadata."""
    client: paramiko.SSHClient
    transport: paramiko.Transport
    host: str
    port: int
    user: str
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)


class SSHClient:
    """SSH connection manager with singleton pool.

    Usage:
        client = SSHClient()
        client.connect("myserver", host="192.168.1.100", port=22,
                       user="root", password="secret")
        stdout, stderr, exit_code = client.exec_command("myserver", "ls -la")
        client.disconnect("myserver")
    """

    DEFAULT_PORT = 22
    DEFAULT_TIMEOUT = 10  # seconds for connect
    COMMAND_TIMEOUT = 30  # seconds for exec_command
    KEEPALIVE_INTERVAL = 60  # seconds

    # ── Connection management ───────────────────────────────────────────

    def connect(
        self,
        name: str,
        *,
        host: str,
        port: int = DEFAULT_PORT,
        user: str,
        password: Optional[str] = None,
        key_path: Optional[str] = None,
        passphrase: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Dict[str, Any]:
        """Establish an SSH connection and register it by name.

        Returns:
            Dict with 'ok', optionally 'error' or 'fingerprint'.
        """
        conn_key = (host, port)

        with _pool_lock:
            if conn_key in _pool:
                existing = _pool[conn_key]
                if existing.client.get_transport() and existing.client.get_transport().is_active():
                    existing.last_used = time.time()
                    return {"ok": True, "fingerprint": existing.transport.get_remote_server_key().get_fingerprint().hex()}
                # Stale connection, remove
                try:
                    existing.client.close()
                except Exception:
                    pass
                del _pool[conn_key]

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: Dict[str, Any] = {
            "hostname": host,
            "port": port,
            "username": user,
            "timeout": timeout,
            "banner_timeout": timeout,
            "compress": True,
        }

        # Authentication
        auth_method = "password"
        if key_path:
            auth_method = "key"
            try:
                if passphrase:
                    pkey = paramiko.RSAKey.from_private_key_file(key_path, password=passphrase)
                else:
                    pkey = paramiko.RSAKey.from_private_key_file(key_path)
                connect_kwargs["pkey"] = pkey
            except paramiko.SSHException:
                # Try other key types
                try:
                    if passphrase:
                        pkey = paramiko.Ed25519Key.from_private_key_file(key_path, password=passphrase)
                    else:
                        pkey = paramiko.Ed25519Key.from_private_key_file(key_path)
                    connect_kwargs["pkey"] = pkey
                except paramiko.SSHException:
                    try:
                        if passphrase:
                            pkey = paramiko.ECDSAKey.from_private_key_file(key_path, password=passphrase)
                        else:
                            pkey = paramiko.ECDSAKey.from_private_key_file(key_path)
                        connect_kwargs["pkey"] = pkey
                    except paramiko.SSHException as e:
                        return {"ok": False, "error": f"Failed to load private key: {e}"}
        elif password:
            connect_kwargs["password"] = password
        else:
            return {"ok": False, "error": "No authentication method provided (password or key_path required)"}

        try:
            client.connect(**connect_kwargs)
            transport = client.get_transport()
            if transport is None:
                return {"ok": False, "error": "Failed to establish transport"}

            transport.set_keepalive(self.KEEPALIVE_INTERVAL)

            session = _SSHSession(
                client=client,
                transport=transport,
                host=host,
                port=port,
                user=user,
            )

            with _pool_lock:
                _pool[conn_key] = session

            fingerprint = transport.get_remote_server_key().get_fingerprint().hex()
            logger.info("SSH connected to %s:%d as %s (auth: %s, fingerprint: %s)",
                        host, port, user, auth_method, fingerprint)

            return {"ok": True, "fingerprint": fingerprint}

        except paramiko.AuthenticationException as e:
            logger.warning("SSH auth failed for %s@%s:%d - %s", user, host, port, e)
            return {"ok": False, "error": f"Authentication failed: {e}"}
        except paramiko.SSHException as e:
            logger.error("SSH error connecting to %s:%d - %s", host, port, e)
            return {"ok": False, "error": f"SSH error: {e}"}
        except Exception as e:
            logger.error("Failed to connect to %s:%d - %s", host, port, e)
            return {"ok": False, "error": f"Connection failed: {e}"}

    def disconnect(self, name: str = "", host: str = "", port: int = DEFAULT_PORT) -> Dict[str, Any]:
        """Disconnect an SSH session."""
        target_keys = []
        with _pool_lock:
            if name:
                # Search by configured name — need to iterate
                # Name-based disconnect is handled at config level,
                # here we use host:port
                pass

            if host:
                conn_key = (host, port)
                if conn_key in _pool:
                    target_keys.append(conn_key)
            else:
                # Disconnect all
                target_keys = list(_pool.keys())

            for key in target_keys:
                session = _pool.pop(key, None)
                if session:
                    try:
                        session.client.close()
                    except Exception:
                        pass
                    logger.info("SSH disconnected from %s:%d", key[0], key[1])

        if not target_keys:
            return {"ok": False, "error": "No matching connection found"}

        return {"ok": True, "disconnected": len(target_keys)}

    def disconnect_by_host_port(self, host: str, port: int = DEFAULT_PORT) -> Dict[str, Any]:
        """Disconnect by host:port."""
        return self.disconnect(host=host, port=port)

    def is_connected(self, host: str, port: int = DEFAULT_PORT) -> bool:
        """Check if a connection is active."""
        conn_key = (host, port)
        with _pool_lock:
            session = _pool.get(conn_key)
            if session is None:
                return False
            transport = session.client.get_transport()
            if transport is None or not transport.is_active():
                try:
                    session.client.close()
                except Exception:
                    pass
                del _pool[conn_key]
                return False
            return True

    # ── Command execution ───────────────────────────────────────────────

    def exec_command(
        self,
        host: str,
        command: str,
        port: int = DEFAULT_PORT,
        timeout: float = COMMAND_TIMEOUT,
    ) -> Dict[str, Any]:
        """Execute a command on the remote host.

        Returns:
            Dict with 'stdout', 'stderr', 'exit_code', and 'ok'.
        """
        conn_key = (host, port)

        with _pool_lock:
            session = _pool.get(conn_key)
            if session is None:
                return {
                    "ok": False,
                    "error": f"No active connection to {host}:{port}. Connect first.",
                    "stdout": "",
                    "stderr": "",
                    "exit_code": -1,
                }
            session.last_used = time.time()

        try:
            transport = session.client.get_transport()
            if transport is None or not transport.is_active():
                with _pool_lock:
                    _pool.pop(conn_key, None)
                return {
                    "ok": False,
                    "error": f"Connection to {host}:{port} is no longer active.",
                    "stdout": "",
                    "stderr": "",
                    "exit_code": -1,
                }

            # Open a new channel for this command
            channel = transport.open_session()
            channel.settimeout(timeout)
            channel.exec_command(command)

            stdout_b = b""
            stderr_b = b""

            # Read stdout
            while True:
                try:
                    chunk = channel.recv(65536)
                    if not chunk:
                        break
                    stdout_b += chunk
                except socket.timeout:
                    break
                except Exception:
                    break

            # Read stderr
            try:
                channel.recv_stderr_ready()
                while True:
                    try:
                        chunk = channel.recv_stderr(65536)
                        if not chunk:
                            break
                        stderr_b += chunk
                    except socket.timeout:
                        break
                    except Exception:
                        break
            except Exception:
                pass

            exit_code = channel.recv_exit_status() if not channel.closed else -1
            channel.close()

            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")

            return {
                "ok": True,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
            }

        except paramiko.SSHException as e:
            logger.error("SSH command error on %s:%d - %s", host, port, e)
            return {
                "ok": False,
                "error": f"SSH error: {e}",
                "stdout": "",
                "stderr": str(e),
                "exit_code": -1,
            }
        except Exception as e:
            logger.error("Command execution failed on %s:%d - %s", host, port, e)
            return {
                "ok": False,
                "error": f"Command failed: {e}",
                "stdout": "",
                "stderr": str(e),
                "exit_code": -1,
            }

    def read_file(self, host: str, path: str, port: int = DEFAULT_PORT) -> Dict[str, Any]:
        """Read a remote file via SFTP.

        Returns:
            Dict with 'content', 'ok', optional 'error'.
        """
        conn_key = (host, port)

        with _pool_lock:
            session = _pool.get(conn_key)
            if session is None:
                return {"ok": False, "error": f"No active connection to {host}:{port}."}
            session.last_used = time.time()

        try:
            sftp = session.client.open_sftp()
            try:
                with sftp.file(path, "r") as f:
                    content = f.read().decode("utf-8", errors="replace")
                return {"ok": True, "content": content}
            finally:
                sftp.close()
        except FileNotFoundError:
            return {"ok": False, "error": f"Remote file not found: {path}"}
        except PermissionError:
            return {"ok": False, "error": f"Permission denied: {path}"}
        except Exception as e:
            return {"ok": False, "error": f"SFTP read failed: {e}"}

    def write_file(self, host: str, path: str, content: str, port: int = DEFAULT_PORT) -> Dict[str, Any]:
        """Write content to a remote file via SFTP.

        Returns:
            Dict with 'ok', optional 'error'.
        """
        conn_key = (host, port)

        with _pool_lock:
            session = _pool.get(conn_key)
            if session is None:
                return {"ok": False, "error": f"No active connection to {host}:{port}."}
            session.last_used = time.time()

        try:
            sftp = session.client.open_sftp()
            try:
                # Ensure parent directory exists
                import posixpath
                parent = posixpath.dirname(path)
                if parent and parent != "/":
                    self._mkdir_p_sftp(sftp, parent)

                with sftp.file(path, "w") as f:
                    f.write(content.encode("utf-8"))
                return {"ok": True, "path": path}
            finally:
                sftp.close()
        except PermissionError:
            return {"ok": False, "error": f"Permission denied: {path}"}
        except Exception as e:
            return {"ok": False, "error": f"SFTP write failed: {e}"}

    @staticmethod
    def _mkdir_p_sftp(sftp: paramiko.SFTPClient, remote_dir: str):
        """Recursively create remote directory via SFTP."""
        import posixpath
        if remote_dir in ("", "/"):
            return
        try:
            sftp.stat(remote_dir)
        except IOError:
            parent = posixpath.dirname(remote_dir)
            if parent and parent != remote_dir:
                SSHClient._mkdir_p_sftp(sftp, parent)
            try:
                sftp.mkdir(remote_dir)
            except IOError:
                pass  # Might already exist due to race

    def list_connections(self) -> list:
        """List all active connections."""
        result = []
        with _pool_lock:
            for (host, port), session in list(_pool.items()):
                transport = session.client.get_transport()
                is_active = transport is not None and transport.is_active()
                if not is_active:
                    try:
                        session.client.close()
                    except Exception:
                        pass
                    del _pool[(host, port)]
                    continue
                result.append({
                    "host": host,
                    "port": port,
                    "user": session.user,
                    "connected_since": session.created_at,
                    "last_used": session.last_used,
                })
        return result


# ── Singleton ───────────────────────────────────────────────────────────
_ssh_client: Optional[SSHClient] = None


def get_ssh_client() -> SSHClient:
    """Get or create the global SSH client singleton."""
    global _ssh_client
    if _ssh_client is None:
        _ssh_client = SSHClient()
    return _ssh_client
