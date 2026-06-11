"""
守护模式悬浮球 (DaemonBall)。

半透明圆形悬浮球，可拖拽移动，点击展开详情面板，
显示核心统计信息、日志和对话入口。
"""

import sys
import time
from datetime import datetime, timedelta
from typing import Optional

try:
    from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QLabel,
                                  QLineEdit, QSystemTrayIcon, QMenu, QAction,
                                  QTextEdit, QPushButton, QHBoxLayout)
    from PyQt5.QtCore import Qt, QTimer, QPoint, pyqtSignal
    from PyQt5.QtGui import QPainter, QBrush, QColor, QFont, QIcon, QPalette, QPixmap
    PYQT_VERSION = 5
except ImportError:
    from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QLabel,
                                  QLineEdit, QSystemTrayIcon, QMenu,
                                  QTextEdit, QPushButton, QHBoxLayout)
    from PyQt6.QtCore import Qt, QTimer, QPoint, pyqtSignal
    from PyQt6.QtGui import QPainter, QBrush, QColor, QFont, QIcon, QPalette, QPixmap, QAction
    PYQT_VERSION = 6


class DaemonBall(QWidget):
    """守护模式悬浮球。

    功能:
    - 半透明圆形悬浮球(40x40)，显示 API 调用次数
    - 可拖拽移动，hover 时透明度变为 1.0
    - 点击展开详情面板(200x300)：显示触发/任务数、运行时长、最近日志
    - 输入框可对话
    - 系统托盘图标：右键菜单隐藏/显示/退出
    """

    message_submitted = pyqtSignal(str)  # 用户提交消息信号

    def __init__(self, daemon_core=None):
        super().__init__()
        self._core = daemon_core
        self._expanded = False
        self._logs = []  # 最近日志 [(timestamp, message)]
        self._init_ui()
        self._init_tray()
        self._init_timer()

    def _init_ui(self):
        """初始化悬浮球 UI。"""
        # 球体本身 40x40
        self.setFixedSize(40, 40)
        self.setWindowOpacity(0.7)

        # 窗口标志: 置顶 + 无边框
        flags = Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint
        if sys.platform == "win32":
            flags |= Qt.Tool  # Windows 上加上 Tool 避免任务栏显示
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        # 初始位置：右下角
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.right() - 60, geo.bottom() - 60)

    def paintEvent(self, event):
        """绘制半透明圆形球体 + API 调用次数。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 背景圆形
        color = QColor(60, 60, 80, 200)
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(2, 2, 36, 36)

        # API 调用次数文本
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Arial", 9, QFont.Bold))
        count = self._core._api_call_count if self._core else 0
        text = str(count) if count < 1000 else f"{count/1000:.0f}k"
        painter.drawText(self.rect(), Qt.AlignCenter, text)

    def mousePressEvent(self, event):
        """点击切换展开/收起。"""
        if event.button() == Qt.LeftButton:
            self._toggle_expand()
        self._drag_pos = event.globalPos()

    def mouseMoveEvent(self, event):
        """拖拽移动。"""
        if hasattr(self, '_drag_pos') and event.buttons() == Qt.LeftButton:
            delta = event.globalPos() - self._drag_pos
            self.move(self.pos() + delta)
            self._drag_pos = event.globalPos()

    def enterEvent(self, event):
        self.setWindowOpacity(1.0)

    def leaveEvent(self, event):
        if not self._expanded:
            self.setWindowOpacity(0.7)

    def _toggle_expand(self):
        """展开/收起详情面板。"""
        if self._expanded:
            self._panel.hide()
            self.setFixedSize(40, 40)
            self._expanded = False
            self.setWindowOpacity(0.7)
        else:
            if not hasattr(self, '_panel'):
                self._create_panel()
            self._refresh_panel()
            self._panel.show()
            self.setFixedSize(40, 40)  # 球体保持原样
            self._expanded = True
            self.setWindowOpacity(1.0)

    def _create_panel(self):
        """创建展开面板（独立窗口，放在球体旁边）。"""
        self._panel = QWidget()
        self._panel.setWindowFlags(
            Qt.WindowStaysOnTopHint | Qt.Tool | Qt.FramelessWindowHint
        )
        self._panel.setFixedSize(200, 300)
        self._panel.setStyleSheet("""
            background: #2a2a3a; color: #e0e0e0; border: 1px solid #444;
            border-radius: 8px; font-family: "Microsoft YaHei", Arial;
        """)

        layout = QVBoxLayout(self._panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # 统计信息
        self._stats_label = QLabel()
        self._stats_label.setStyleSheet("font-size: 11px; color: #aaa;")
        layout.addWidget(self._stats_label)

        # 日志区域
        self._log_text = QTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setStyleSheet("""
            QTextEdit { background: #1a1a2a; border: 1px solid #333; border-radius: 4px;
                        font-size: 10px; color: #ccc; }
        """)
        self._log_text.setMaximumHeight(120)
        layout.addWidget(self._log_text)

        # 输入区
        input_layout = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText("输入对话...")
        self._input.setStyleSheet("""
            QLineEdit { background: #1a1a2a; border: 1px solid #333; border-radius: 4px;
                        padding: 4px; font-size: 11px; color: #e0e0e0; }
        """)
        self._input.returnPressed.connect(self._on_submit)
        input_layout.addWidget(self._input)

        send_btn = QPushButton("发")
        send_btn.setFixedSize(28, 24)
        send_btn.setStyleSheet("""
            QPushButton { background: #4a4aff; border: none; border-radius: 3px;
                          color: white; font-size: 10px; }
            QPushButton:hover { background: #5a5aff; }
        """)
        send_btn.clicked.connect(self._on_submit)
        input_layout.addWidget(send_btn)
        layout.addLayout(input_layout)

    def _refresh_panel(self):
        """刷新面板数据。"""
        if not hasattr(self, '_panel'):
            return

        core = self._core
        api_calls = core._api_call_count if core else 0
        triggers = core._trigger_count if core else 0
        tasks = core._task_count if core else 0

        uptime = ""
        if core and core._started_at > 0:
            secs = int(time.time() - core._started_at)
            h, m = divmod(secs, 3600)
            m, s = divmod(m, 60)
            uptime = f"{h}h {m}m {s}s"

        self._stats_label.setText(
            f"API: {api_calls} | 触发: {triggers} | 任务: {tasks}\n运行: {uptime}"
        )

        # 最近日志
        logs_text = ""
        for ts, msg in self._logs[-5:]:
            logs_text += f"[{ts.strftime('%H:%M:%S')}] {msg[:60]}\n"
        self._log_text.setPlainText(logs_text or "(暂无日志)")

        # 面板位置：球体左侧
        ball_pos = self.pos()
        self._panel.move(ball_pos.x() - 210, ball_pos.y() - 130)

    def _on_submit(self):
        """用户提交消息。"""
        text = self._input.text().strip()
        if text:
            self.log(f"用户: {text}")
            self.message_submitted.emit(text)
            self._input.clear()

    def log(self, message: str):
        """添加日志。"""
        self._logs.append((datetime.now(), message))
        if len(self._logs) > 100:
            self._logs = self._logs[-100:]

    def _init_tray(self):
        """初始化系统托盘。"""
        self._tray = QSystemTrayIcon(self)

        # 创建简单图标
        try:
            pixmap = QPixmap(16, 16)
            pixmap.fill(QColor(60, 60, 80))
            self._tray.setIcon(QIcon(pixmap))
        except Exception:
            self._tray.setIcon(QIcon())

        menu = QMenu()
        show_action = menu.addAction("显示/隐藏悬浮球")
        show_action.triggered.connect(self._toggle_visibility)
        menu.addSeparator()
        quit_action = menu.addAction("退出守护模式")
        quit_action.triggered.connect(self._quit)
        self._tray.setContextMenu(menu)
        self._tray.show()

    def _toggle_visibility(self):
        self.setVisible(not self.isVisible())
        if hasattr(self, '_panel'):
            self._panel.setVisible(self.isVisible() and self._expanded)

    def _quit(self):
        self._tray.hide()
        QApplication.quit()

    def _init_timer(self):
        """定时刷新 UI。"""
        self._timer = QTimer()
        self._timer.timeout.connect(self._refresh)
        self._timer.start(2000)  # 每 2 秒刷新

    def _refresh(self):
        """定时刷新。"""
        self.update()  # 重绘球体
        if self._expanded:
            self._refresh_panel()

    @staticmethod
    def run(daemon_core=None):
        """启动悬浮球（阻塞）。

        Args:
            daemon_core: DaemonCore 实例，用于获取统计数据。

        Note:
            此方法会阻塞当前线程直到 QApplication 退出。
            如需与 asyncio 事件循环共存，应在单独线程中调用。
        """
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        ball = DaemonBall(daemon_core)
        ball.show()
        sys.exit(app.exec())
