"""
进程安全文件锁 — 基于 lockfile 的简单跨平台实现。

使用方式:
    with FileLock("/path/to/file.json"):
        # 写入操作

锁超时 5 秒，超时后记录警告但继续执行（降级模式）。
"""

import logging
import os
import time

logger = logging.getLogger(__name__)

# 锁超时（秒）
LOCK_TIMEOUT = 5.0
# 锁轮询间隔（秒）
LOCK_POLL_INTERVAL = 0.05


class FileLock:
    """基于 lockfile 的文件锁上下文管理器。

    在目标文件旁创建 `<target>.lock` 文件作为锁标记。
    - 写入前创建 `.lock` 文件（原子操作）
    - 如果 `.lock` 已存在，等待（最多 LOCK_TIMEOUT 秒）
    - 写入完成后删除 `.lock` 文件

    线程安全说明：
    - 跨进程安全（所有进程通过文件系统协调）
    - 同一进程内重复加锁同一文件安全（支持重入）
    """

    def __init__(self, file_path: str, timeout: float = LOCK_TIMEOUT):
        self._file_path = file_path
        self._lock_path = file_path + ".lock"
        self._timeout = timeout
        self._locked = False
        self._reentrant_count = 0

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False

    def acquire(self) -> bool:
        """获取锁。超时后返回 False 并记录警告。"""
        # 重入计数：同一进程内重复加锁
        if self._locked:
            self._reentrant_count += 1
            return True

        deadline = time.time() + self._timeout
        first_attempt = True

        while True:
            try:
                # 使用 O_CREAT | O_EXCL 原子创建 lock 文件
                fd = os.open(
                    self._lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                # 写入 PID 信息便于调试
                os.write(fd, str(os.getpid()).encode("utf-8"))
                os.close(fd)
                self._locked = True
                self._reentrant_count = 0
                return True
            except FileExistsError:
                # 锁文件已存在，等待重试
                pass
            except OSError as e:
                logger.debug("FileLock acquire error for %s: %s", self._lock_path, e)
                # 意外错误，等待重试

            if first_attempt:
                first_attempt = False
                logger.debug(
                    "FileLock waiting for %s (timeout=%.1fs)",
                    self._lock_path,
                    self._timeout,
                )

            if time.time() >= deadline:
                # 超时：降级处理，检查锁文件是否已过期
                self._check_stale_lock()
                # 再次尝试一次
                try:
                    fd = os.open(
                        self._lock_path,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    )
                    os.write(fd, str(os.getpid()).encode("utf-8"))
                    os.close(fd)
                    self._locked = True
                    self._reentrant_count = 0
                    logger.warning(
                        "FileLock timeout for %s — 已覆盖过期锁并继续",
                        self._lock_path,
                    )
                    return True
                except FileExistsError:
                    logger.warning(
                        "FileLock timeout for %s — 无法获取锁，继续执行（降级）",
                        self._lock_path,
                    )
                    return False
                except OSError:
                    logger.warning(
                        "FileLock timeout for %s — 无法获取锁，继续执行（降级）",
                        self._lock_path,
                    )
                    return False

            time.sleep(LOCK_POLL_INTERVAL)

    def release(self) -> None:
        """释放锁。"""
        if self._reentrant_count > 0:
            self._reentrant_count -= 1
            return

        if self._locked:
            try:
                if os.path.exists(self._lock_path):
                    os.remove(self._lock_path)
            except OSError:
                pass
            self._locked = False

    def _check_stale_lock(self) -> None:
        """检查并清理过期锁文件。"""
        try:
            if not os.path.exists(self._lock_path):
                return
            # 读取 lock 文件中的 PID
            with open(self._lock_path, "r") as f:
                content = f.read().strip()
            if content:
                pid = int(content)
                if not self._is_process_alive(pid):
                    logger.warning(
                        "FileLock removing stale lock from PID %d: %s",
                        pid,
                        self._lock_path,
                    )
                    os.remove(self._lock_path)
        except (ValueError, OSError, IOError):
            pass

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        """检查 PID 对应的进程是否存活。"""
        try:
            # Unix: 检查进程是否存在
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except (OSError, ValueError):
            # Windows 或权限不足: 使用 tasklist 检查
            pass

        try:
            import subprocess
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=5,
            )
            return str(pid) in result.stdout
        except Exception:
            return True  # 无法判断时保守处理，不清理
