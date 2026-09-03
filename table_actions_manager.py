# table_actions_manager.py
"""
结果表格的右键菜单和文件操作：打开文件、打开所在位置、复制路径、复制
文件本身、全选、批量替换所选文件。原来这些都直接挂在 MainWindow 上，
现在拆成 TableActionsManager，用法跟 IndexManager/SearchManager/
PreviewManager 一样：MainWindow 只留一个
`self.table_actions = TableActionsManager(self)` 的引用。
"""
import os
import csv
import subprocess
from PyQt5.QtWidgets import QMessageBox, QMenu, QApplication, QFileDialog
from PyQt5.QtCore import QUrl, QMimeData, Qt

from helpers import open_file_with_default_app, open_containing_folder
from replace_dialog import ReplaceDialog


class TableActionsManager:
    def __init__(self, window):
        self.window = window

    def export_results_to_csv(self):
        """菜单栏"文件 -> 导出列表"：把当前结果表格里显示的内容（文件名/
        文件路径/创建日期/修改日期/DWG版本/大小）导出成一份 CSV。

        导出的是表格当前显示的内容和顺序——包括用户已经点过的排序，
        所见即所得，不用再自己拿到文件之后重新排一遍。

        如果表里当前一行都没有（还没搜索过，或者搜索结果是空的），
        直接提示，不生成一份只有表头的空文件出来。
        """
        window = self.window
        model = window.results_model
        row_count = model.rowCount()
        if row_count == 0:
            QMessageBox.information(window, "提示", "当前结果列表是空的，没有可导出的内容。")
            return

        default_name = "搜索结果.csv"
        file_path, _ = QFileDialog.getSaveFileName(
            window, "导出列表", default_name, "CSV 文件 (*.csv)"
        )
        if not file_path:
            return
        if not file_path.lower().endswith(".csv"):
            file_path += ".csv"

        col_count = model.columnCount()
        headers = [model.headerData(col, Qt.Horizontal) for col in range(col_count)]
        try:
            # utf-8-sig 带 BOM，Excel 直接双击打开中文列名/内容才不会
            # 显示成乱码——这是 Excel 自己对无 BOM 的 UTF-8 CSV 的老毛病，
            # 跟这个项目本身没关系，但不加这个用户打开导出文件第一眼
            # 就是乱码，体验上等于没做好。
            with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                for row in range(row_count):
                    row_values = [
                        model.index(row, col).data() or ""
                        for col in range(col_count)
                    ]
                    writer.writerow(row_values)
        except Exception as e:
            QMessageBox.warning(window, "导出失败", f"无法写入文件：\n{e}")
            return

        window.status_label.setText(f"已导出 {row_count} 条结果到：{file_path}")

    def open_file_from_table(self, row, column):
        window = self.window
        file_path = window.results_model.path_at(row)
        if file_path:
            open_file_with_default_app(file_path)

    def on_rename_succeeded(self, old_path, new_path):
        """文件名这一列被编辑之后，真正把磁盘上的文件改名这件事现在整个
        搬进了 ResultsTableModel.setData()（见 results_table_model.py）——
        校验非法字符、检查目标位置是否已有同名文件、真正调用
        os.rename()、失败时弹提示并让编辑框自动恢复原文字，这些全部
        在 Model 层一次性做完，不再需要 table_actions_manager 这边跟着
        table.itemChanged 信号手忙脚乱地校验+改名+失败退回。

        这个方法只在真正改名成功之后才会被调用（连着
        model.rename_succeeded 信号，见 layout.py），现在唯一要做的
        "收尾工作"就是把状态栏提示一下，跟原来 rename_file_from_table()
        最后一行的效果保持一致。

        注意：这里不需要手动去同步文字账本索引——后台文件监控
        （file_watcher.py 的 on_moved）本来就会把"重命名"当成"旧路径
        删除 + 新路径新增"处理，自己会在下一轮检查时同步过去。
        """
        window = self.window
        new_name = os.path.basename(new_path)
        window.status_label.setText(f"已重命名为：{new_name}")

    def select_all_table_rows(self):
        self.window.table.selectAll()

    def open_replace_for_selected(self):
        window = self.window
        selected_rows = set(idx.row() for idx in window.table.selectedIndexes())
        if not selected_rows:
            QMessageBox.information(window, "提示", "请先在结果表格里选中要替换的文件（可以先点“全选”）")
            return
        file_paths = []
        for row in selected_rows:
            file_path = window.results_model.path_at(row)
            if file_path:
                file_paths.append(file_path)
        dialog = ReplaceDialog(window, initial_files=file_paths)
        dialog.exec_()

    def open_replace_tool(self):
        dialog = ReplaceDialog(self.window, initial_files=None)
        dialog.exec_()

    # =========================================================
    # 右键菜单
    # =========================================================
    def show_context_menu(self, pos):
        window = self.window
        selected_rows = window.table.selectionModel().selectedRows()

        menu = QMenu(window)
        # "全选"不依赖是否已经选中行，右键点表格任意位置（包括空白处）
        # 都能用，不用先选中什么才能触发。
        menu.addAction("全选").triggered.connect(self.select_all_table_rows)

        if selected_rows:
            row = selected_rows[0].row()
            file_path = window.results_model.path_at(row) or ""

            # "复制文件"这类操作可以对多选生效，把所有选中行的路径都收集起来
            all_paths = []
            for idx in selected_rows:
                p = window.results_model.path_at(idx.row())
                if p:
                    all_paths.append(p)

            menu.addSeparator()
            menu.addAction("打开文件").triggered.connect(
                lambda: open_file_with_default_app(file_path))
            menu.addAction("打开文件所在位置").triggered.connect(
                lambda: self.open_file_location(file_path))
            menu.addAction("复制文件路径").triggered.connect(
                lambda: self.copy_file_path(file_path))
            menu.addAction("复制文件").triggered.connect(
                lambda: self.copy_files(all_paths))
            menu.addSeparator()
            menu.addAction("批量替换所选...").triggered.connect(
                self.open_replace_for_selected)

        menu.exec_(window.table.viewport().mapToGlobal(pos))

    def open_file_location(self, file_path):
        window = self.window
        open_containing_folder(file_path, parent=window)

    def copy_file_path(self, file_path):
        QApplication.clipboard().setText(file_path)
        self.window.status_label.setText("路径已成功复制到剪贴板！")

    def copy_files(self, file_paths):
        """
        把选中的文件本身放进剪贴板（不是路径文本），效果等同于在资源管理器里
        Ctrl+C 这几个文件，之后可以在任意文件夹里 Ctrl+V 粘贴出真正的文件副本。
        """
        window = self.window
        valid_paths = [p for p in file_paths if os.path.exists(p)]
        missing_count = len(file_paths) - len(valid_paths)
        if not valid_paths:
            QMessageBox.warning(window, "错误", "选中的文件不存在，无法复制。")
            return

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in valid_paths])
        QApplication.clipboard().setMimeData(mime)

        if missing_count:
            window.status_label.setText(
                f"已复制 {len(valid_paths)} 个文件到剪贴板"
                f"（{missing_count} 个文件不存在已跳过），可在资源管理器里粘贴"
            )
        else:
            window.status_label.setText(
                f"已复制 {len(valid_paths)} 个文件到剪贴板，可在资源管理器里粘贴"
            )