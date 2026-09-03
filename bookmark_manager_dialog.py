# bookmark_manager_dialog.py
#
# "书签管理"弹窗：列出所有收藏过的搜索条件（文件名/内容关键词各自的
# 文本+是否正则），双击某一行、或者选中后点"应用"，直接把这条书签的
# 内容填回主窗口的两个搜索框并立即执行一次搜索——书签存在的意义就是
# "省得重新敲一遍"，选中之后不该还要用户自己再点一次放大镜，那样等于
# 没帮上忙。
#
# 跟 backup_restore_dialog.py（"从备份恢复"弹窗）是同一类"列表+按钮"
# 结构，这里保持同样的写法习惯：QTableWidget 展示、下面一排按钮操作
# 当前选中行，双击行等价于点最主要的那个按钮（这里是"应用"）。
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QMessageBox, QInputDialog
)
from PyQt5.QtCore import Qt

from bookmark_manager import load_bookmarks, delete_bookmark, rename_bookmark, find_bookmark_by_name
from helpers import refresh_bookmark_icon_everywhere


class BookmarkManagerDialog(QDialog):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window  # 主窗口引用：应用书签要把值填回它的搜索框
        self.setWindowTitle("书签管理")
        self.resize(640, 420)

        self._bookmarks = []  # 跟表格的行一一对应，方便按行号取回原始记录

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        hint = QLabel("双击某一行，或选中后点「应用」，把这条书签的搜索条件填回搜索框并立即搜索。")
        hint.setStyleSheet("color: gray; font-size: 12px;")
        layout.addWidget(hint)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["名称", "文件名关键字", "内容关键字", "创建时间"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Interactive)
        self.table.setColumnWidth(0, 140)
        self.table.setColumnWidth(3, 150)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.setToolTip("双击：应用这条书签（填回搜索框并立即搜索）")
        self.table.cellDoubleClicked.connect(lambda *_: self._apply_selected())
        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        apply_btn = QPushButton("应用")
        apply_btn.setToolTip("把这条书签的搜索条件填回搜索框并立即搜索")
        apply_btn.clicked.connect(self._apply_selected)
        btn_layout.addWidget(apply_btn)

        rename_btn = QPushButton("重命名")
        rename_btn.clicked.connect(self._rename_selected)
        btn_layout.addWidget(rename_btn)

        delete_btn = QPushButton("删除")
        delete_btn.setStyleSheet("color: red;")
        delete_btn.clicked.connect(self._delete_selected)
        btn_layout.addWidget(delete_btn)

        btn_layout.addStretch()

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        self.reload()

    def reload(self):
        """重新从磁盘读一遍书签列表——每次弹窗打开都调用一次，保证
        看到的是最新数据（比如另一个窗口刚添加/删除过书签）。"""
        self._bookmarks = load_bookmarks()
        self.table.setRowCount(len(self._bookmarks))
        for row, b in enumerate(self._bookmarks):
            self.table.setItem(row, 0, QTableWidgetItem(b.get("name", "")))
            self.table.setItem(row, 1, QTableWidgetItem(self._format_keyword(
                b.get("filename_keyword", ""), b.get("filename_regex", False))))
            self.table.setItem(row, 2, QTableWidgetItem(self._format_keyword(
                b.get("content_keyword", ""), b.get("content_regex", False))))
            self.table.setItem(row, 3, QTableWidgetItem(b.get("created_at", "")))

    @staticmethod
    def _format_keyword(keyword, is_regex):
        if not keyword:
            return "（空）"
        return f"{keyword}  [正则]" if is_regex else keyword

    def _selected_bookmark(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._bookmarks):
            QMessageBox.information(self, "书签", "请先在列表里选中一条书签。")
            return None
        return self._bookmarks[row]

    def _apply_selected(self):
        b = self._selected_bookmark()
        if not b:
            return
        window = self.window
        # 正则开关要先设置好，再填文字——filename_regex_action/
        # content_regex_action 各自的 toggled 回调只负责切换图标外观，
        # 不影响文本内容，两者谁先谁后本来没有强依赖，这里保持"先定
        # 模式、再填内容"的顺序单纯是符合直觉（先选"按什么规则解析"，
        # 再看"解析的内容是什么"）。
        window.filename_regex_action.setChecked(bool(b.get("filename_regex", False)))
        window.content_regex_action.setChecked(bool(b.get("content_regex", False)))
        window.filename_keyword_edit.setCurrentText(b.get("filename_keyword", ""))
        window.keyword_edit.setCurrentText(b.get("content_keyword", ""))
        window.status_label.setText(f"已应用书签「{b.get('name', '')}」")
        self.close()
        window.search_manager.start_search()

    def _rename_selected(self):
        b = self._selected_bookmark()
        if not b:
            return
        new_name, ok = QInputDialog.getText(
            self, "重命名书签", "新名称：", text=b.get("name", "")
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name:
            QMessageBox.warning(self, "重命名书签", "名称不能为空。")
            return
        # 跟"添加到书签"时的规则一样，不允许改成跟别的书签重名——
        # exclude_id 传自己，允许"改成跟原来一样的名字"（等于没改）。
        duplicate = find_bookmark_by_name(new_name, exclude_id=b["id"])
        if duplicate is not None:
            QMessageBox.warning(
                self, "重命名书签", f"已经有一条书签叫「{new_name}」了，换一个名字吧。"
            )
            return
        rename_bookmark(b["id"], new_name)
        self.reload()
        refresh_bookmark_icon_everywhere(self.window)

    def _delete_selected(self):
        b = self._selected_bookmark()
        if not b:
            return
        reply = QMessageBox.question(
            self, "删除书签",
            f"确定删除书签「{b.get('name', '')}」吗？此操作不可恢复。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        delete_bookmark(b["id"])
        self.reload()
        refresh_bookmark_icon_everywhere(self.window)