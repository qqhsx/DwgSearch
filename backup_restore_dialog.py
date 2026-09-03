# backup_restore_dialog.py
#
# "从备份恢复"弹窗：挑一个之前的备份批次（按时间戳分的那些文件夹），
# 列出这一批里备份过的每个文件，用户勾选后一键恢复回各自的原始路径。
#
# 这是为了解决"备份目录里的文件可能来自好几个不同的源文件夹，光看
# 一堆同名/不同名的 dwg 文件，人工根本不知道该恢复回哪里"这个问题：
# 具体文件在磁盘上是按盘符+目录结构镜像存放的（见 backup_manager.py），
# 但普通用户不一定会去手动对照目录结构，这个弹窗直接把
# "备份文件 -> 原始路径"这层关系读出来展示，恢复只需要勾选+点按钮。

import os
import sys
import subprocess

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QPlainTextEdit, QMessageBox, QCheckBox, QSpinBox, QFileDialog
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal

from config import (
    get_backup_root, save_backup_root, get_backup_max_runs, save_backup_max_runs,
)
from backup_manager import list_backup_runs, read_manifest, restore_entry, delete_backup_run


class RestoreThread(QThread):
    """恢复操作本身就是逐个文件复制，量大的时候（成百上千个文件）在
    UI 线程里做会卡住界面，所以单独起一个线程跑，跟 replace_worker.py
    里 ReplaceThread 的信号命名习惯保持一致，方便以后维护的人不用
    重新熟悉一套新规则。"""

    progress_signal = pyqtSignal(str)                 # 一行一行的日志文本
    entry_done_signal = pyqtSignal(int, bool, str)     # (在列表里的行号, 是否成功, 消息)
    finished_signal = pyqtSignal(int, int)             # (成功数, 失败数)

    def __init__(self, entries_with_rows, overwrite):
        super().__init__()
        self._entries_with_rows = entries_with_rows  # [(row, entry), ...]
        self._overwrite = overwrite

    def run(self):
        ok_count = 0
        fail_count = 0
        for row, entry in self._entries_with_rows:
            original = entry.get("original_path", "")
            self.progress_signal.emit(f"恢复中：{original}")
            ok, msg = restore_entry(entry, overwrite=self._overwrite)
            if ok:
                ok_count += 1
                self.progress_signal.emit(f"  ✅ {msg}")
            else:
                fail_count += 1
                self.progress_signal.emit(f"  ❌ {msg}")
            self.entry_done_signal.emit(row, ok, msg)
        self.finished_signal.emit(ok_count, fail_count)


class BackupRestoreDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("从备份恢复")
        self.resize(820, 560)

        self._entries = []  # 当前选中批次的 manifest 记录，跟表格行一一对应
        self._restore_thread = None

        layout = QVBoxLayout(self)

        # ---- 备份设置：保存位置 + 自动保留数量，放一起统一管理 ----
        settings_layout = QHBoxLayout()
        self.backup_root_label = QLabel()
        self.backup_root_label.setStyleSheet("color: #555555;")
        self._refresh_backup_root_label()
        settings_layout.addWidget(self.backup_root_label)
        change_root_btn = QPushButton("更改…")
        change_root_btn.setToolTip("改变以后新备份的保存位置，比如改到团队共享盘。已有的备份批次不会跟着移动。")
        change_root_btn.clicked.connect(self._on_pick_backup_root)
        settings_layout.addWidget(change_root_btn)
        settings_layout.addSpacing(20)
        settings_layout.addWidget(QLabel("自动保留最近"))
        self.max_runs_spin = QSpinBox()
        self.max_runs_spin.setRange(0, 9999)
        self.max_runs_spin.setValue(get_backup_max_runs())
        self.max_runs_spin.setSpecialValueText("不限制")
        self.max_runs_spin.setToolTip("超出保留数量的旧批次，会在每次执行完成后自动清理。改动后立即生效，不用额外保存。")
        self.max_runs_spin.valueChanged.connect(self._on_retention_changed)
        settings_layout.addWidget(self.max_runs_spin)
        settings_layout.addWidget(QLabel("个批次"))
        settings_layout.addStretch()
        layout.addLayout(settings_layout)

        # ---- 批次选择 ----
        run_layout = QHBoxLayout()
        run_layout.addWidget(QLabel("备份批次："))
        self.run_combo = QComboBox()
        self.run_combo.setMinimumWidth(320)
        self.run_combo.currentIndexChanged.connect(self._on_run_changed)
        run_layout.addWidget(self.run_combo)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._reload_runs)
        run_layout.addWidget(refresh_btn)
        self.delete_run_btn = QPushButton("删除此批次")
        self.delete_run_btn.setToolTip("整批删除，包括这一批次里备份的所有原文件、manifest 记录，不可恢复。")
        self.delete_run_btn.clicked.connect(self._on_delete_run_clicked)
        run_layout.addWidget(self.delete_run_btn)
        run_layout.addStretch()
        layout.addLayout(run_layout)

        # ---- 文件列表 ----
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["恢复", "文件名", "原始路径", "备份时间", "当前状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Interactive)
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(1, 160)
        self.table.setColumnWidth(3, 150)
        self.table.setColumnWidth(4, 200)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setToolTip("双击某一行：打开该文件原本所在的文件夹（原文件不在了则打开备份文件所在的文件夹）")
        self.table.cellDoubleClicked.connect(self._on_row_double_clicked)
        layout.addWidget(self.table)

        select_layout = QHBoxLayout()
        select_all_btn = QPushButton("全选")
        select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        select_layout.addWidget(select_all_btn)
        select_none_btn = QPushButton("全不选")
        select_none_btn.clicked.connect(lambda: self._set_all_checked(False))
        select_layout.addWidget(select_none_btn)
        select_layout.addStretch()
        layout.addLayout(select_layout)

        # ---- 选项 + 执行 ----
        action_layout = QHBoxLayout()
        self.overwrite_cb = QCheckBox("覆盖原路径已存在的文件")
        self.overwrite_cb.setChecked(True)
        self.overwrite_cb.setToolTip(
            "勾选：恢复时直接覆盖原路径现有文件（常见场景：替换出问题了，想整批退回备份前的状态）。\n"
            "不勾选：原路径已经有文件的就跳过，只恢复原路径已经不存在文件的那些条目。"
        )
        action_layout.addWidget(self.overwrite_cb)
        action_layout.addStretch()
        self.restore_btn = QPushButton("恢复所选")
        self.restore_btn.clicked.connect(self._on_restore_clicked)
        action_layout.addWidget(self.restore_btn)
        layout.addLayout(action_layout)

        self.log_area = QPlainTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMaximumHeight(140)
        layout.addWidget(self.log_area)

        close_layout = QHBoxLayout()
        close_layout.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        close_layout.addWidget(close_btn)
        layout.addLayout(close_layout)

        self._reload_runs()

    # ------------------------------------------------------------------
    def _reload_runs(self):
        self.run_combo.blockSignals(True)
        self.run_combo.clear()
        runs = list_backup_runs(get_backup_root())
        if not runs:
            self.run_combo.addItem("（还没有任何备份批次）", None)
            self.table.setRowCount(0)
            self.run_combo.blockSignals(False)
            return
        for run in runs:
            self.run_combo.addItem(f"{run['label']}（{run['file_count']} 个文件）", run["run_dir"])
        self.run_combo.blockSignals(False)
        self._on_run_changed(0)

    def _on_run_changed(self, index):
        run_dir = self.run_combo.currentData()
        self._entries = read_manifest(run_dir) if run_dir else []
        self._populate_table()
        self.delete_run_btn.setEnabled(run_dir is not None)

    def _on_delete_run_clicked(self):
        run_dir = self.run_combo.currentData()
        if not run_dir:
            return
        label = self.run_combo.currentText()
        reply = QMessageBox.question(
            self, "确认删除备份批次",
            f"即将整批删除「{label}」，包括这一批次里备份的所有原文件和记录，删除后无法恢复。\n"
            f"（这只是删除备份副本，不影响图纸的当前实际文件）\n\n确定要删除吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        try:
            delete_backup_run(run_dir)
        except Exception as e:
            QMessageBox.critical(self, "删除失败", f"删除备份批次失败：{e}")
            return
        self._reload_runs()

    def _on_retention_changed(self, value):
        save_backup_max_runs(value)

    def _refresh_backup_root_label(self):
        self.backup_root_label.setText(f"备份保存位置：{get_backup_root()}")

    def _on_pick_backup_root(self):
        current = get_backup_root()
        start_dir = current if os.path.isdir(current) else ""
        new_root = QFileDialog.getExistingDirectory(self, "选择备份保存位置", start_dir)
        if not new_root:
            return
        save_backup_root(new_root)
        self._refresh_backup_root_label()
        self._log(f"---- 备份保存位置已改为：{new_root} ----")

    def _populate_table(self):
        self.table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            cb = QCheckBox()
            cb.setChecked(True)
            self.table.setCellWidget(row, 0, cb)

            original_path = entry.get("original_path", "")
            self.table.setItem(row, 1, QTableWidgetItem(os.path.basename(original_path)))
            self.table.setItem(row, 2, QTableWidgetItem(original_path))
            self.table.setItem(row, 3, QTableWidgetItem(entry.get("backed_up_at", "")))

            status = "原路径文件存在" if os.path.exists(original_path) else "原路径文件不存在（已被移动/删除）"
            status_item = QTableWidgetItem(status)
            if not os.path.exists(original_path):
                status_item.setForeground(Qt.darkYellow)
            self.table.setItem(row, 4, status_item)

    def _set_all_checked(self, checked):
        for row in range(self.table.rowCount()):
            cb = self.table.cellWidget(row, 0)
            if cb is not None:
                cb.setChecked(checked)

    # ------------------------------------------------------------------
    def _on_restore_clicked(self):
        selected = []
        for row in range(self.table.rowCount()):
            cb = self.table.cellWidget(row, 0)
            if cb is not None and cb.isChecked():
                selected.append((row, self._entries[row]))

        if not selected:
            QMessageBox.information(self, "提示", "请先勾选要恢复的文件")
            return

        overwrite = self.overwrite_cb.isChecked()
        warn_text = f"即将把 {len(selected)} 个文件恢复到各自的原始路径。\n"
        if overwrite:
            warn_text += "已勾选「覆盖原路径已存在的文件」，原路径现有内容会被直接替换掉。\n"
        warn_text += "确定要继续吗？"
        reply = QMessageBox.question(self, "确认恢复", warn_text,
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        self.log_area.clear()
        self.restore_btn.setEnabled(False)
        self._restore_thread = RestoreThread(selected, overwrite)
        self._restore_thread.progress_signal.connect(self._log)
        self._restore_thread.finished_signal.connect(self._on_restore_finished)
        self._restore_thread.start()

    def _log(self, text):
        self.log_area.appendPlainText(text)

    def _on_row_double_clicked(self, row, column):
        if row < 0 or row >= len(self._entries):
            return
        entry = self._entries[row]
        original_path = entry.get("original_path", "")
        backup_path = entry.get("backup_path", "")
        if os.path.exists(original_path):
            self._open_containing_folder(original_path)
        elif os.path.exists(backup_path):
            self._log(f"原路径文件不存在，改为打开备份文件所在位置：{backup_path}")
            self._open_containing_folder(backup_path)
        else:
            QMessageBox.warning(self, "打不开", "原文件和备份文件都已经不存在了，找不到可以打开的位置。")

    def _open_containing_folder(self, path):
        """在资源管理器里打开某个文件所在的文件夹，并高亮选中这个文件。
        只在 Windows 上有意义（这个软件面向的就是 Windows + AutoCAD 用户），
        非 Windows 环境下退化为提示，不报错。"""
        if not sys.platform.startswith("win"):
            QMessageBox.information(self, "提示", f"文件位置：{path}")
            return
        try:
            subprocess.Popen(f'explorer /select,"{path}"')
        except Exception as e:
            QMessageBox.warning(self, "打开失败", f"无法打开文件所在位置：{e}")

    def _on_restore_finished(self, ok_count, fail_count):
        self.restore_btn.setEnabled(True)
        QMessageBox.information(self, "恢复完成", f"成功 {ok_count} 个，失败 {fail_count} 个。\n详情见上方日志。")
        # 恢复完之后原路径的"当前状态"列可能变了（文件从不存在变存在），刷新一下
        self._populate_table()

    def closeEvent(self, event):
        # 恢复操作跑在独立线程里，关闭弹窗前等它跑完，避免线程还在写文件
        # 的时候对象就被销毁，引发莫名其妙的崩溃
        if self._restore_thread is not None and self._restore_thread.isRunning():
            QMessageBox.warning(self, "请稍候", "恢复操作正在进行，请等待完成后再关闭")
            event.ignore()
            return
        super().closeEvent(event)