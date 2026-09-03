# accoreconsole_settings_dialog.py
#
# "accoreconsole 高级设置"弹窗：手动指定/清除 accoreconsole.exe 路径。
#
# 这是应急设置——程序默认会自动探测（先查注册表，再扫描常见安装
# 路径，见 accoreconsole_detect.py），只有探测失败、或者 AutoCAD 装在
# 非常规位置时才需要动一下，平时基本用不上。所以从主替换界面挪到这个
# 单独的小弹窗里，主界面上只留一个"高级设置"入口按钮，不占常驻空间。

import os

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog, QMessageBox
)
from PyQt5.QtCore import pyqtSignal

from config import get_accoreconsole_manual_path, save_accoreconsole_manual_path


class AccoreconsoleSettingsDialog(QDialog):
    # 路径变了（手动指定成功，或者点了清除）就发一次信号，带上最新路径
    # （清除时是空字符串）。要不要因此重启后台 accoreconsole 实例，
    # 交给主界面自己决定——这个弹窗只管路径本身的设置，不碰后台实例。
    path_changed_signal = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("accoreconsole 高级设置")
        self.resize(580, 180)

        layout = QVBoxLayout(self)

        intro = QLabel(
            "程序默认会自动探测本机的 AutoCAD 安装（先查注册表，查不到再扫描常见安装路径）。\n"
            "只有自动探测失败、或者 AutoCAD 装在了非常规目录时，才需要在这里手动指定一次。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.path_label = QLabel()
        self.path_label.setStyleSheet("color: #555555;")
        self._refresh_label()
        layout.addWidget(self.path_label)

        btn_layout = QHBoxLayout()
        pick_btn = QPushButton("手动指定 accoreconsole.exe…")
        pick_btn.clicked.connect(self._on_pick)
        btn_layout.addWidget(pick_btn)
        clear_btn = QPushButton("清除，改回自动探测")
        clear_btn.clicked.connect(self._on_clear)
        btn_layout.addWidget(clear_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addStretch()
        close_layout = QHBoxLayout()
        close_layout.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        close_layout.addWidget(close_btn)
        layout.addLayout(close_layout)

    def _refresh_label(self):
        manual_path = get_accoreconsole_manual_path()
        if manual_path:
            self.path_label.setText(f"当前：手动指定为 {manual_path}")
        else:
            self.path_label.setText("当前：自动探测（未手动指定）")

    def _on_pick(self):
        start_dir = "C:\\Program Files\\Autodesk" if os.path.isdir("C:\\Program Files\\Autodesk") else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 accoreconsole.exe", start_dir, "accoreconsole.exe (accoreconsole.exe);;所有文件 (*.*)"
        )
        if not path:
            return
        if os.path.basename(path).lower() != "accoreconsole.exe":
            QMessageBox.warning(self, "路径不对", "请选择 accoreconsole.exe 这个文件本身，不是它所在的文件夹。")
            return
        save_accoreconsole_manual_path(path)
        self._refresh_label()
        self.path_changed_signal.emit(path)

    def _on_clear(self):
        if not get_accoreconsole_manual_path():
            return  # 本来就没设置过，不用发信号触发一次没意义的重启
        save_accoreconsole_manual_path("")
        self._refresh_label()
        self.path_changed_signal.emit("")