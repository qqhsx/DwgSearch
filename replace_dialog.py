# replace_dialog.py
#
# "文字替换"功能的界面。两种打开方式：
#   - 从搜索结果表格传入已经选好的文件列表（initial_files 不为空）
#   - 单独打开，自己在对话框里选文件/文件夹（initial_files 为 None）
#
# 流程设计成"必须先预览、才能正式执行"，不允许跳过预览直接写入保存，
# 这是故意的安全限制，不是疏漏。
#
# 🌟 隐藏的 AutoCAD 实例改成对话框打开时启动一次、常驻到关闭为止，
# 预览和确认执行共用同一个实例（完整设计说明见 replace_worker.py 头部
# 注释）。后台线程是 ReplaceThread（QThread 子类），提交任务/请求关闭
# 都是直接调用它的普通方法（submit_job / request_stop_current_job /
# request_shutdown）——这几个方法内部只是操作一个线程安全的
# queue.Queue 或者简单标志位，不触碰任何 COM 对象，所以可以放心从
# GUI 线程直接调用，不需要绕一圈信号槽。真正操作 COM 对象的代码全部
# 在 ReplaceThread.run() 这一个函数体内，天然保证只在它自己的线程里
# 执行。

import os
from datetime import datetime

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QPlainTextEdit, QFileDialog, QMessageBox, QProgressBar,
    QAbstractItemView, QCheckBox, QGroupBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QComboBox
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor

from replace_worker import ReplaceThread, ReindexThread
from helpers import ElidedLabel, expand_pairs_with_diameter_variants
from config import (
    get_replace_scan_options, save_replace_scan_options, get_replace_engine, save_replace_engine,
    get_backup_root, get_backup_max_runs,
    get_replace_file_table_column_widths, save_replace_file_table_column_widths,
)
from backup_manager import new_backup_run_dir, enforce_backup_retention
from backup_restore_dialog import BackupRestoreDialog
from accoreconsole_settings_dialog import AccoreconsoleSettingsDialog
from file_watcher import WATCHDOG_AVAILABLE

# 极少数情况下：对话框在隐藏 AutoCAD 实例还没预热完成时就被关闭，这时
# ReplaceThread 还得继续跑一段时间（等 DispatchEx 那次同步调用自己
# 返回）才能真正处理到关闭请求、退出 run()。这段时间它不能被 Python
# 当成"没人引用了"提前垃圾回收，也不能被 Qt 的父子关系带着一起销毁，
# 那正是 "QThread: Destroyed while thread is still running" 崩溃的
# 触发条件。用这个模块级列表兜底多持有一份强引用，线程真正跑完
# （finished 信号）之后再从这里移除，交还给正常的垃圾回收。
_ORPHANED_WORKERS = []

# 文件列表里的状态前缀 + 颜色，_on_file_done / _reset_file_statuses 共用，
# 集中定义方便以后统一调整观感。
_STATUS_PENDING_COLOR = QColor("#999999")
_STATUS_HIT_COLOR = QColor("#1e7e34")      # 命中 >0 处：绿色
_STATUS_NO_HIT_COLOR = QColor("#888888")   # 跑成功但没命中：灰色，跟"出错"区分开
_STATUS_ERROR_COLOR = QColor("#c0392b")    # 真正处理失败：红色


class ReplaceDialog(QDialog):
    def __init__(self, parent=None, initial_files=None):
        super().__init__(parent)
        self.setWindowTitle("批量文字替换")
        self.resize(700, 560)

        self.file_paths = list(initial_files) if initial_files else []
        self._file_items = {}  # path -> (status_item, hits_item)，_on_file_done 靠这个更新对应行
        self.reindex_thread = None
        self.worker_thread = None
        self._preview_passed = False  # 必须先跑过一次预览，且预览没有异常，才允许正式执行
        self._modified_files = []  # 本次"正式执行"里真正被写入保存过的文件，用于替换完成后同步索引
        self._job_running = False    # 当前是否有预览/执行任务在 worker 线程里跑
        self._current_dry_run = True  # 当前这次任务是预览还是正式执行，_on_job_finished 等回调要用

        self._build_ui()
        self._refresh_file_list()
        self._start_worker()

    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ---- 文件来源 ----
        file_ops_layout = QHBoxLayout()
        self.file_count_label = QLabel("")
        add_files_btn = QPushButton("添加文件...")
        add_files_btn.clicked.connect(self._add_files)
        add_folder_btn = QPushButton("添加文件夹...")
        add_folder_btn.clicked.connect(self._add_folder)
        clear_files_btn = QPushButton("清空列表")
        clear_files_btn.clicked.connect(self._clear_files)
        file_ops_layout.addWidget(self.file_count_label)
        file_ops_layout.addStretch()
        file_ops_layout.addWidget(add_files_btn)
        file_ops_layout.addWidget(add_folder_btn)
        file_ops_layout.addWidget(clear_files_btn)
        layout.addLayout(file_ops_layout)

        self.file_table = QTableWidget(0, 5)
        self.file_table.setHorizontalHeaderLabels(["序号", "文件名", "文件路径", "状态", "命中数"])
        self.file_table.verticalHeader().setVisible(False)
        self.file_table.horizontalHeader().setHighlightSections(False)
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.file_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        self.file_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.file_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Interactive)
        self.file_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Interactive)
        self.file_table.setColumnWidth(0, 50)
        self.file_table.setColumnWidth(1, 160)
        self.file_table.setColumnWidth(3, 130)
        self.file_table.setColumnWidth(4, 70)
        # 记住用户手动拖动调整过的列宽（第2列"文件路径"是 Stretch 模式，
        # 会自动占满剩余空间，不需要也没法记它的宽度，只记其它几列）：
        # 有保存过就按保存的来，覆盖掉上面几行写死的默认宽度；没保存过
        # 就用上面的默认值。列宽变化本身通过下面的计时器合并连续多次
        # 拖动事件，300ms 内没有新变化才真正写一次配置文件，不会因为
        # 拖动过程中每移动一像素就触发一次磁盘写入。
        saved_widths = get_replace_file_table_column_widths()
        if saved_widths:
            for col, w in enumerate(saved_widths):
                if (col < self.file_table.columnCount()
                        and self.file_table.horizontalHeader().sectionResizeMode(col) != QHeaderView.Stretch
                        and isinstance(w, int) and w > 0):
                    self.file_table.setColumnWidth(col, w)
        self._column_width_save_timer = QTimer(self)
        self._column_width_save_timer.setSingleShot(True)
        self._column_width_save_timer.timeout.connect(self._save_file_table_column_widths)
        self.file_table.horizontalHeader().sectionResized.connect(
            lambda *args: self._column_width_save_timer.start(300)
        )
        # 文件路径那一列经常很长，宽度不够时用"..."省略而不是把整行撑爆；
        # 用 ElideMiddle 而不是默认的 ElideRight，这样前面的盘符/根目录和
        # 后面挨着文件名的那层目录都还留得住，只是中间省掉，更好辨认。
        self.file_table.setTextElideMode(Qt.ElideMiddle)
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.file_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.file_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # 跟主界面搜索结果表格用同一套观感（斑马纹 + 柔和浅蓝选中色），
        # 两个列表放在一起看不会觉得是两套风格拼出来的。
        self.file_table.setAlternatingRowColors(True)
        self.file_table.setStyleSheet(
            "QTableWidget {"
            "    alternate-background-color: #f4f6f9;"
            "    background-color: #ffffff;"
            "    gridline-color: #e3e6ea;"
            "    border: 1px solid #d5d9de;"
            "}"
            "QTableWidget::item:selected {"
            "    background-color: #cfe4ff;"
            "    color: black;"
            "}"
        )
        # 之前限高 100px 是因为下面日志区要占地方；日志区现在默认收起了，
        # 腾出来的空间给文件列表，给个明显更高的下限，太长的列表就滚动查看。
        self.file_table.setMinimumHeight(200)
        layout.addWidget(self.file_table, 1)

        remove_selected_btn = QPushButton("移除选中的文件")
        remove_selected_btn.clicked.connect(self._remove_selected_files)
        layout.addWidget(remove_selected_btn)

        # ---- 替换内容：支持一次填多组"旧文字->新文字"，按表格从上到下
        # 依次链式应用到同一段文字上（后一组是在前一组替换完的结果上继续
        # 找替换，不是各自独立作用在原文上），所以顺序是有意义的。 ----
        pairs_label_layout = QHBoxLayout()
        pairs_label_layout.addWidget(QLabel("替换内容（可添加多组，按顺序依次应用；空白行会被忽略）："))
        pairs_label_layout.addStretch()
        add_pair_btn = QPushButton("+ 添加一组")
        add_pair_btn.clicked.connect(lambda: self._add_pair_row())
        remove_pair_btn = QPushButton("删除选中组")
        remove_pair_btn.clicked.connect(self._remove_selected_pair_rows)
        pairs_label_layout.addWidget(add_pair_btn)
        pairs_label_layout.addWidget(remove_pair_btn)
        layout.addLayout(pairs_label_layout)

        self.pairs_table = QTableWidget(0, 2)
        self.pairs_table.setHorizontalHeaderLabels(["旧文字", "新文字"])
        self.pairs_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.pairs_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.pairs_table.setMaximumHeight(140)
        self.pairs_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.pairs_table.itemChanged.connect(self._on_pairs_changed)
        layout.addWidget(self.pairs_table)
        self._add_pair_row()  # 默认给一组空行，跟改动前"只有一组"的体验保持一致

        # ---- 要替换哪些类型（记住上次的勾选，默认只勾单行/多行文字）----
        saved_scan_options = get_replace_scan_options()
        type_group = QGroupBox("替换哪些类型的文字")
        type_layout = QHBoxLayout(type_group)
        self.type_text_cb = QCheckBox("单行文字")
        self.type_text_cb.setChecked(saved_scan_options.get("text", True))
        self.type_mtext_cb = QCheckBox("多行文字")
        self.type_mtext_cb.setChecked(saved_scan_options.get("mtext", True))
        self.type_dimension_cb = QCheckBox("标注/引线覆盖文字")
        self.type_dimension_cb.setChecked(saved_scan_options.get("dimension", False))
        self.type_dimension_cb.setToolTip("只改手动覆盖过的标注文字，自动生成的测量数值不受影响")
        self.type_block_attr_cb = QCheckBox("块参照属性值")
        self.type_block_attr_cb.setChecked(saved_scan_options.get("block_attr", False))
        self.type_block_attr_cb.setToolTip("插入的块实例上已经填好的属性值，比如图框里的图号、比例这类")
        for cb in (self.type_text_cb, self.type_mtext_cb,
                   self.type_dimension_cb, self.type_block_attr_cb):
            type_layout.addWidget(cb)
        layout.addWidget(type_group)

        # ---- 去哪些位置搜——跟上面"类型"是完全独立、对等的两个维度：
        # "类型"决定找什么，"位置"决定去哪找，两者自由组合。比如只勾
        # "块定义内部"、不勾"模型/图纸空间"，就能做到"只改块模板里的
        # 文字，不碰图纸里散落的同名文字"，这是以前做不到的场景。
        location_group = QGroupBox("在哪里搜索（至少选一项）")
        location_layout = QHBoxLayout(location_group)
        self.scan_space_cb = QCheckBox("模型空间/图纸空间")
        self.scan_space_cb.setChecked(saved_scan_options.get("scan_space", True))
        self.scan_space_cb.setToolTip("图纸里正常摆放的实体（不含块定义模板本身）")
        self.include_block_defs_cb = QCheckBox("块定义内部（含嵌套块）")
        self.include_block_defs_cb.setChecked(saved_scan_options.get("include_block_defs", False))
        self.include_block_defs_cb.setToolTip(
            "块定义是模板，勾选后会连模板本身一起改，影响所有用到该块的地方，请谨慎"
        )
        location_layout.addWidget(self.scan_space_cb)
        location_layout.addWidget(self.include_block_defs_cb)
        block_def_warn = QLabel("⚠️ 勾选「块定义内部」会影响所有用到该块的地方，请谨慎")
        block_def_warn.setStyleSheet("color: #b36b00;")
        location_layout.addWidget(block_def_warn)
        location_layout.addStretch()
        layout.addWidget(location_group)

        # 替换范围（类型 + 位置）改了之后：
        #   1）之前跑过的预览就不再准确反映实际会改动的内容了，强制重新预览
        #   2）顺手记住这次的选择，下次打开对话框直接沿用
        for cb in (self.type_text_cb, self.type_mtext_cb, self.type_dimension_cb,
                   self.type_block_attr_cb, self.scan_space_cb, self.include_block_defs_cb):
            cb.stateChanged.connect(self._on_scan_options_changed)

        # ---- 替换引擎 ----
        # 三选一下拉框：accoreconsole（默认）/ AutoCAD COM / ACadSharp。
        # 前两个都需要本机装 AutoCAD，只是调用方式不同；ACadSharp 是纯
        # .NET 实现，不需要装 AutoCAD，但标注类型的替换范围比另外两个
        # 窄（只替换已设置过覆盖文字的标注），具体说明见下面的 tooltip
        # 和 acadsharp_engine.py / DwgTextReplacer/Program.cs 顶部注释。
        #
        # 用 QComboBox 而不是三个互斥 checkbox：一次只能选一种引擎，
        # 下拉框天然保证互斥，不用额外写"选了这个就取消那个"的联动代码。
        engine_layout = QHBoxLayout()
        engine_layout.addWidget(QLabel("替换引擎："))
        self.engine_combo = QComboBox()
        # (下拉框显示文字, 引擎内部标识) 一一对应，索引跟下拉框选项顺序一致
        self._ENGINE_CHOICES = [
            ("accoreconsole 引擎（推荐）", "accoreconsole"),
            ("AutoCAD COM 引擎", "com"),
            ("ACadSharp 引擎（本机无需安装 AutoCAD）", "acadsharp"),
        ]
        for label, _engine_key in self._ENGINE_CHOICES:
            self.engine_combo.addItem(label)
        current_engine = get_replace_engine()
        for i, (_label, engine_key) in enumerate(self._ENGINE_CHOICES):
            if engine_key == current_engine:
                self.engine_combo.setCurrentIndex(i)
                break
        self.engine_combo.setToolTip(
            "accoreconsole：每个文件用独立的 accoreconsole.exe 进程处理，单个文件卡住不会拖累整批，\n"
            "也不会抢占你正在用的 AutoCAD 窗口。需要本机装 AutoCAD。\n\n"
            "AutoCAD COM：最早支持的引擎，启动一个隐藏的 AutoCAD 实例逐个处理。需要本机装 AutoCAD。\n\n"
            "ACadSharp：纯 .NET 实现，本机不需要装 AutoCAD 就能用；但标注(标注文字)类型只会\n"
            "替换已经设置过覆盖文字的标注，纯粹显示测量值的标注不会被改动（这一点其实三个引擎\n"
            "都一样，不是 ACadSharp 独有的限制）。\n\n"
            "切换后会重启后台实例，正在运行的任务不受影响，需要重新预览一次。"
        )
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        engine_layout.addWidget(self.engine_combo)
        self.accoreconsole_settings_btn = QPushButton("高级设置…")
        self.accoreconsole_settings_btn.setToolTip(
            "自动探测不到 accoreconsole.exe（比如 AutoCAD 装在了非常规目录）时，\n"
            "可以在这里手动指定路径。只在选择 accoreconsole 引擎时有用，平时用不上，不用管。"
        )
        self.accoreconsole_settings_btn.clicked.connect(self._on_open_accoreconsole_settings)
        self.accoreconsole_settings_btn.setEnabled(current_engine == "accoreconsole")
        engine_layout.addWidget(self.accoreconsole_settings_btn)
        engine_layout.addStretch()
        layout.addLayout(engine_layout)

        # ---- 备份：正式执行前自动备份到这里（按时间戳分批次存放），
        # 不需要每次执行都手动选一遍。具体存哪、批次怎么清理，都放进了
        # "从备份恢复"弹窗里统一管理——见下面第275行附近的说明和
        # backup_restore_dialog.py，这里主界面只留一个入口按钮，不用
        # 每次打开这个对话框都先看一遍备份路径细节。----

        # ---- 操作按钮 ----
        action_layout = QHBoxLayout()
        self.preview_btn = QPushButton("预览")
        self.preview_btn.setToolTip("先跑一遍看看会命中哪些内容，不会真正保存文件。可选，但建议先看一眼再执行。")
        self.preview_btn.clicked.connect(self._run_preview)
        self.execute_btn = QPushButton("确认执行")
        self.execute_btn.setToolTip("真正写入并保存所选文件。执行前会自动备份原文件。")
        self.execute_btn.setStyleSheet("color: red;")
        self.execute_btn.setEnabled(True)
        self.execute_btn.clicked.connect(self._run_execute)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setToolTip("取消当前正在进行的任务")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_running)
        self.restore_backup_btn = QPushButton("从备份恢复…")
        self.restore_backup_btn.setToolTip("挑一个之前的备份批次，把文件恢复回原始路径；备份保存位置等设置也在这里。")
        self.restore_backup_btn.clicked.connect(self._on_open_restore_dialog)
        action_layout.addWidget(self.preview_btn)
        action_layout.addWidget(self.execute_btn)
        action_layout.addWidget(self.cancel_btn)
        action_layout.addWidget(self.restore_backup_btn)
        layout.addLayout(action_layout)

        # ---- 进度 + 日志 ----
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(0)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        # 顶部列表已经能看到每个文件的状态和命中数了，这里的详细日志
        # （逐条命中记录、引擎切换提示等）平时不需要一直盯着，所以默认
        # 收起——但留一行"当前状态"常驻显示最新一条消息，不用展开也能
        # 知道跑到哪一步了；真要查完整记录（比如排查某个文件为什么失败）
        # 再点开。
        self.current_status_label = ElidedLabel("")
        self.current_status_label.setStyleSheet("color: #555555;")
        layout.addWidget(self.current_status_label)

        self.toggle_log_btn = QPushButton("▸ 查看详细日志")
        self.toggle_log_btn.setStyleSheet("text-align: left; border: none; color: #4a70b0;")
        self.toggle_log_btn.clicked.connect(self._toggle_log_area)
        layout.addWidget(self.toggle_log_btn)

        self.log_area = QPlainTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setVisible(False)
        layout.addWidget(self.log_area, 1)

    def _toggle_log_area(self):
        expanded = not self.log_area.isVisible()
        self.log_area.setVisible(expanded)
        self.toggle_log_btn.setText("▾ 收起详细日志" if expanded else "▸ 查看详细日志")

    # ------------------------------------------------------------------
    # worker 线程管理：整个对话框只在这里启动一次常驻线程，run() 内部
    # 自己完成预热（DispatchEx）
    # ------------------------------------------------------------------
    def _start_worker(self):
        self.worker_thread = ReplaceThread()
        self.worker_thread.progress_signal.connect(self._on_progress)
        self.worker_thread.file_done_signal.connect(self._on_file_done)
        self.worker_thread.finished_signal.connect(self._on_job_finished)
        self.worker_thread.instance_ready_signal.connect(self._on_instance_ready)
        self.worker_thread.start()

        self._log("正在后台准备处理实例，可以先添加文件、填写替换内容…")

    def _on_instance_ready(self, ok, err_msg):
        if ok:
            self._log("✅ 处理实例已就绪，预览/执行会立刻响应，不用再等启动。")
        else:
            self._log(f"⚠️ {err_msg}")

    # ------------------------------------------------------------------
    def _refresh_file_list(self):
        self.file_table.setRowCount(0)
        self._file_items = {}
        for p in self.file_paths:
            row = self.file_table.rowCount()
            self.file_table.insertRow(row)

            index_item = QTableWidgetItem(str(row + 1))
            index_item.setTextAlignment(Qt.AlignCenter)
            name_item = QTableWidgetItem(os.path.basename(p))
            path_item = QTableWidgetItem(os.path.dirname(p))
            status_item = QTableWidgetItem("⏳ 待处理")
            hits_item = QTableWidgetItem("-")
            hits_item.setTextAlignment(Qt.AlignCenter)
            status_item.setForeground(_STATUS_PENDING_COLOR)
            # 文件名/状态列也顺手放一份完整路径在 tooltip 里，路径列被
            # 省略号截断时，鼠标悬停照样能看到没删减的原文。
            for it in (index_item, name_item, path_item, status_item, hits_item):
                it.setToolTip(p)

            self.file_table.setItem(row, 0, index_item)
            self.file_table.setItem(row, 1, name_item)
            self.file_table.setItem(row, 2, path_item)
            self.file_table.setItem(row, 3, status_item)
            self.file_table.setItem(row, 4, hits_item)
            self._file_items[p] = (status_item, hits_item)

        self.file_count_label.setText(f"共 {len(self.file_paths)} 个文件")
        # 文件列表变了，之前的预览就不作数了，强制重新预览
        self._preview_passed = False

    def _reset_file_statuses(self):
        """每次预览/执行开始前，把所有行的状态打回"待处理"的灰色——不然
        重新跑一次之后，还没轮到的文件会一直显示上一轮的旧状态，容易
        误以为"这个文件已经处理过了"。顺便把列表滚回顶部，这样新一轮
        跑起来之后，滚动条会跟着当前处理到第几个自然往下走，用户不用
        自己先手动划回最上面。"""
        for status_item, hits_item in self._file_items.values():
            status_item.setText("⏳ 待处理")
            status_item.setForeground(_STATUS_PENDING_COLOR)
            hits_item.setText("-")
            hits_item.setForeground(_STATUS_PENDING_COLOR)
        self.file_table.scrollToTop()

    def _add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择 DWG 文件", "", "DWG 文件 (*.dwg)")
        for f in files:
            if f not in self.file_paths:
                self.file_paths.append(f)
        self._refresh_file_list()

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if not folder:
            return
        added = 0
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(".dwg") and not f.startswith("~$"):
                    full_path = os.path.join(root, f)
                    if full_path not in self.file_paths:
                        self.file_paths.append(full_path)
                        added += 1
        self._refresh_file_list()
        QMessageBox.information(self, "已添加", f"从文件夹中新增了 {added} 个 DWG 文件")

    def _clear_files(self):
        self.file_paths = []
        self._refresh_file_list()

    def _remove_selected_files(self):
        selected_rows = sorted({idx.row() for idx in self.file_table.selectedIndexes()}, reverse=True)
        for row in selected_rows:
            del self.file_paths[row]
        self._refresh_file_list()

    # ------------------------------------------------------------------
    def _save_file_table_column_widths(self):
        widths = [self.file_table.columnWidth(c) for c in range(self.file_table.columnCount())]
        save_replace_file_table_column_widths(widths)

    def _on_scan_options_changed(self):
        self._preview_passed = False
        save_replace_scan_options(self._get_scan_options())

    def _on_engine_changed(self, index):
        if index < 0 or index >= len(self._ENGINE_CHOICES):
            return
        _label, engine = self._ENGINE_CHOICES[index]
        save_replace_engine(engine)
        # "高级设置…"按钮（手动指定 accoreconsole.exe 路径）只在选中
        # accoreconsole 引擎时才有意义，切到别的引擎就禁用掉，避免用户
        # 以为改了这个设置对当前选中的引擎也有影响。
        self.accoreconsole_settings_btn.setEnabled(engine == "accoreconsole")
        self._preview_passed = False
        self._log(f"---- 切换引擎为「{engine}」，正在重启后台实例 ----")
        self._restart_worker()

    def _on_open_accoreconsole_settings(self):
        dlg = AccoreconsoleSettingsDialog(self)
        dlg.path_changed_signal.connect(self._on_accoreconsole_path_changed)
        dlg.exec_()

    def _on_accoreconsole_path_changed(self, new_path):
        """accoreconsole 高级设置弹窗里改了手动路径（指定或清除），
        路径变了意味着实际用哪个 accoreconsole+插件组合可能跟着变，
        跟切换引擎时一样，需要重启后台实例、并要求重新预览。"""
        if new_path:
            self._log(f"---- 手动指定 accoreconsole.exe 路径为：{new_path}，正在重启后台实例 ----")
        else:
            self._log("---- 已清除手动指定的路径，改回自动探测，正在重启后台实例 ----")
        self._preview_passed = False
        self._restart_worker()

    def _on_open_restore_dialog(self):
        dlg = BackupRestoreDialog(self)
        dlg.exec_()

    def _restart_worker(self):
        """引擎切换需要重新走一遍 ReplaceThread.run() 开头的引擎探测/
        实例预热逻辑（那部分只在线程启动时跑一次），所以整个后台线程
        重开一个——复用关闭对话框时那套"不阻塞、不强杀"的安全关闭
        流程（见 _shutdown_worker_async 的说明），新线程立刻接着启动，
        用户感知上就是"稍等一下、实例重新就绪"。"""
        self._shutdown_worker_async()
        self._start_worker()

    # ------------------------------------------------------------------
    # 替换内容表格：多组"旧文字->新文字"
    # ------------------------------------------------------------------
    def _add_pair_row(self, old="", new=""):
        row = self.pairs_table.rowCount()
        self.pairs_table.insertRow(row)
        # 新增行的时候先断开 itemChanged，避免 setItem 触发的"变更"误判成
        # 用户主动编辑，导致刚打开对话框、还没输入任何东西就把预览标脏。
        self.pairs_table.blockSignals(True)
        self.pairs_table.setItem(row, 0, QTableWidgetItem(old))
        self.pairs_table.setItem(row, 1, QTableWidgetItem(new))
        self.pairs_table.blockSignals(False)

    def _remove_selected_pair_rows(self):
        selected_rows = sorted({idx.row() for idx in self.pairs_table.selectedIndexes()}, reverse=True)
        if not selected_rows:
            return
        for row in selected_rows:
            self.pairs_table.removeRow(row)
        if self.pairs_table.rowCount() == 0:
            self._add_pair_row()  # 至少留一行，方便继续输入
        self._on_pairs_changed()

    def _on_pairs_changed(self, *_):
        # 替换内容改了之后，之前跑过的预览就不再准确反映实际会改动的内容，
        # 强制重新预览——跟"替换范围（类型/位置）改了要重新预览"是同一个道理。
        self._preview_passed = False

    def _get_pairs(self):
        """从表格里收集非空的 (旧文字, 新文字) 组，跳过旧文字为空的行
        （用户还没填完、或者删掉内容留下的空行，直接忽略，不算错误）。"""
        pairs = []
        for row in range(self.pairs_table.rowCount()):
            old_item = self.pairs_table.item(row, 0)
            new_item = self.pairs_table.item(row, 1)
            old_text = (old_item.text() if old_item else "").strip()
            new_text = (new_item.text() if new_item else "")
            if old_text:
                pairs.append((old_text, new_text))
        return pairs

    def _get_scan_options(self):
        return {
            "text": self.type_text_cb.isChecked(),
            "mtext": self.type_mtext_cb.isChecked(),
            "dimension": self.type_dimension_cb.isChecked(),
            "block_attr": self.type_block_attr_cb.isChecked(),
            "scan_space": self.scan_space_cb.isChecked(),
            "include_block_defs": self.include_block_defs_cb.isChecked(),
        }

    def _validate_inputs(self):
        if not self.file_paths:
            QMessageBox.warning(self, "提示", "还没有添加任何文件")
            return False
        pairs = self._get_pairs()
        if not pairs:
            QMessageBox.warning(self, "提示", "请至少填写一组要替换的旧文字")
            return False
        seen_old = set()
        for old_text, _ in pairs:
            if old_text in seen_old:
                QMessageBox.warning(self, "提示", f"「{old_text}」在多组里重复出现，请检查后再试")
                return False
            seen_old.add(old_text)
        if not any((self.type_text_cb.isChecked(), self.type_mtext_cb.isChecked(),
                    self.type_dimension_cb.isChecked(), self.type_block_attr_cb.isChecked())):
            QMessageBox.warning(self, "提示", "请至少勾选一种要替换的文字类型")
            return False
        if not any((self.scan_space_cb.isChecked(), self.include_block_defs_cb.isChecked())):
            QMessageBox.warning(self, "提示", "请至少勾选一个搜索位置（模型/图纸空间 或 块定义内部）")
            return False
        return True

    def _set_running_state(self, running):
        self.preview_btn.setEnabled(not running)
        self.execute_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self.progress_bar.setVisible(running)
        for cb in (self.type_text_cb, self.type_mtext_cb, self.type_dimension_cb,
                   self.type_block_attr_cb, self.scan_space_cb, self.include_block_defs_cb):
            cb.setEnabled(not running)
        self.engine_combo.setEnabled(not running)
        # "高级设置…"按钮需要同时满足两个条件才能点：没有任务在跑、
        # 且当前选中的就是 accoreconsole 引擎（对另外两个引擎无意义）。
        current_engine = self._ENGINE_CHOICES[self.engine_combo.currentIndex()][1]
        self.accoreconsole_settings_btn.setEnabled(not running and current_engine == "accoreconsole")
        self.restore_backup_btn.setEnabled(not running)
        self.pairs_table.setEnabled(not running)

    def _run_preview(self):
        if not self._validate_inputs():
            return
        self.log_area.clear()
        self._start_job(dry_run=True, backup_dir=None)

    def _run_execute(self):
        if not self._validate_inputs():
            return

        backup_root = get_backup_root()
        # 预览不再是执行的硬性前提——有执行前自动备份 + 快捷恢复兜底，
        # 不跑预览也可以直接执行；但没预览过的话还是提醒一句，让用户
        # 自己判断要不要先看一眼命中内容，而不是完全不提示。
        preview_hint = "" if self._preview_passed else (
            "⚠️ 你还没有跑过预览，不确定这次会命中哪些内容。\n\n"
        )
        reply = QMessageBox.question(
            self, "确认执行",
            f"{preview_hint}"
            "即将真正写入并保存所选文件。\n"
            f"操作前会自动把原文件备份到：\n{backup_root}\n"
            "（按本次执行的时间戳单独存放，不会覆盖之前的备份批次）\n\n"
            "确定要继续吗？这一步无法在软件内撤销，但可以用「从备份恢复」找回原文件。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        try:
            backup_dir = new_backup_run_dir(backup_root)
        except Exception as e:
            QMessageBox.critical(self, "备份目录创建失败",
                                  f"无法创建备份目录：{e}\n请检查「更改备份保存位置」设置的路径是否可写。")
            return

        self.log_area.clear()
        self._start_job(dry_run=False, backup_dir=backup_dir)

    def _start_job(self, dry_run, backup_dir):
        # 只在真正提交给替换引擎之前展开直径符号的等价变体——校验阶段
        # （_validate_inputs 里的重复项检查）用的是 _get_pairs() 原始结果，
        # 不受这里展开的影响，用户看到的重复提示仍然是他自己填的那份。
        pairs = expand_pairs_with_diameter_variants(self._get_pairs())

        # 预览阶段不落 CSV：还没写入任何文件，日志区里已经能看到全部命中内容，
        # 落一份文件纯粹是重复。正式执行才生成，作为这次不可逆操作的存档凭证，
        # 存到本次备份批次目录里，跟备份文件、manifest 放一起，方便找。
        csv_path = None
        if not dry_run:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_path = os.path.join(backup_dir, f"replace_result_{stamp}.csv")

        self._modified_files = []
        self._current_dry_run = dry_run
        self._current_backup_dir = backup_dir
        self._job_running = True
        self._set_running_state(True)
        self._reset_file_statuses()

        # 直接调用 worker_thread 的普通方法——submit_job 内部只是把这批
        # 参数塞进一个线程安全的 queue.Queue，不碰任何 COM 对象，从 GUI
        # 线程直接调用是安全的（具体原因见 replace_worker.py 头部说明）。
        self.worker_thread.submit_job(
            list(self.file_paths), pairs,
            dry_run, backup_dir, csv_path,
            scan_options=self._get_scan_options()
        )

    def _on_file_done(self, dwg_path, ok, hit_count, err):
        # 顶部列表里对应这个文件的那一行，状态列和命中数列分别更新，
        # 三种情况分开区分，不然"没命中"很容易被误看成"出错了"：
        #   ❌ 红色：真的处理失败（文件损坏、备份失败等），命中数列显示错误原因
        #   ✅ 绿色：跑成功且命中 >0 处
        #   ○  灰色：跑成功但没找到关键词，不算错误
        items = self._file_items.get(dwg_path)
        if items is not None:
            status_item, hits_item = items
            if not ok:
                status_item.setText("❌ 失败")
                status_item.setForeground(_STATUS_ERROR_COLOR)
                hits_item.setText(err or "未知错误")
                hits_item.setForeground(_STATUS_ERROR_COLOR)
                hits_item.setToolTip(err or "未知错误")
            elif hit_count > 0:
                status_item.setText("✅ 成功")
                status_item.setForeground(_STATUS_HIT_COLOR)
                hits_item.setText(str(hit_count))
                hits_item.setForeground(_STATUS_HIT_COLOR)
            else:
                status_item.setText("○ 无匹配")
                status_item.setForeground(_STATUS_NO_HIT_COLOR)
                hits_item.setText("0")
                hits_item.setForeground(_STATUS_NO_HIT_COLOR)

            # 数量一多，用户不太可能一直盯着列表手动往下拉；这里只有在
            # 当前这行已经不在可视区域内时才会滚动（EnsureVisible），
            # 不会每跑完一个文件就强制把视图拉到正中间，那样反而会
            # 打断用户想停下来细看某一行的操作。
            self.file_table.scrollToItem(status_item)

        # 只有正式执行（非预览）、处理成功、且确实命中了才会真正保存过文件，
        # 才需要记下来后面同步索引；预览模式和"未命中"都不会改动文件本身。
        if not self._current_dry_run and ok and hit_count > 0:
            self._modified_files.append(dwg_path)

    def _log(self, text):
        """所有需要写日志的地方都走这个方法，保证顶部常驻的状态行永远
        是最新一条消息，跟展开的详细日志区保持同步。"""
        self.current_status_label.setText(text)
        self.log_area.appendPlainText(text)

    def _on_progress(self, text):
        self._log(text)

    def _on_job_finished(self, total, hits, errors):
        self._job_running = False
        was_dry_run = self._current_dry_run
        self._set_running_state(False)
        if was_dry_run:
            # 预览没有整体失败（单个文件失败不算，那种情况日志里已经能看到），
            # 就允许解锁"确认执行"按钮
            self._preview_passed = True
            self.execute_btn.setEnabled(True)
            QMessageBox.information(
                self, "预览完成",
                f"共 {total} 个文件，预计命中 {hits} 处，处理失败 {errors} 个。\n"
                f"请检查上面的日志确认命中内容无误，再点击“确认执行”。"
            )
        else:
            QMessageBox.information(
                self, "执行完成",
                f"共 {total} 个文件，实际替换 {hits} 处，处理失败 {errors} 个。\n"
                f"原文件已备份到：\n{getattr(self, '_current_backup_dir', '') or '（备份目录未知）'}\n"
                f"如需找回原文件，可以点击「从备份恢复…」。"
            )
            self._cleanup_old_backups()
            self._start_reindex_if_needed()

    def _cleanup_old_backups(self):
        """按"备份保留批次数"设置清理最旧的备份批次，避免长期跑下来
        磁盘被越堆越多的历史备份占满。这个设置默认 20，在「从备份恢复」
        弹窗里可以改，见 backup_restore_dialog.py。"""
        try:
            deleted = enforce_backup_retention(get_backup_root(), get_backup_max_runs())
        except Exception:
            return
        if deleted:
            self._log(f"---- 已按保留数量设置清理 {len(deleted)} 个过期备份批次 ----")

    def _start_reindex_if_needed(self):
        """
        替换执行完成后，把这次真正被改写保存过的文件重新提取一遍文字写回数据库，
        不用等下次启动或手动重建索引，搜索结果能立刻反映最新内容。

        软件现在有实时监控（file_watcher.py）常驻后台，监视范围就是索引
        目录列表——这批被替换的文件本来就是从搜索结果里选出来的，必然
        落在索引目录范围内，保存动作本身也会触发它自动同步数据库，不需要
        这里再显式做一遍。真正显式重建的场景只剩下 watchdog 库没装
        （WATCHDOG_AVAILABLE=False，实时监控从一开始就没启动，见
        file_watcher.py 顶部说明）——这种情况下没有其它自动更新数据库
        的路径，不显式处理的话，得等下次手动重建索引才会反映最新内容，
        所以保留这一条路径作为兜底，而不是整个功能一起删掉。
        """
        if not self._modified_files:
            return
        if WATCHDOG_AVAILABLE:
            self._log("---- 实时监控会在后台自动同步数据库，无需在这里重复处理 ----")
            return
        self._log(f"---- 未安装 watchdog（实时监控不可用），手动同步索引数据库（{len(self._modified_files)} 个文件）----")
        self.preview_btn.setEnabled(False)
        self.execute_btn.setEnabled(False)
        self.reindex_thread = ReindexThread(self._modified_files)
        self.reindex_thread.progress_signal.connect(self._on_progress)
        self.reindex_thread.finished_signal.connect(self._on_reindex_finished)
        self.reindex_thread.start()

    def _on_reindex_finished(self, ok_count, error_count):
        self._modified_files = []
        self.preview_btn.setEnabled(True)
        self.execute_btn.setEnabled(self._preview_passed)

    def _cancel_running(self):
        if self._job_running:
            self.worker_thread.request_stop_current_job()
            self._log("正在停止，请等待当前文件处理完...")

    def closeEvent(self, event):
        # 列宽拖完之后有 300ms 防抖，这里没走完就关闭窗口的话，防抖计时器
        # 还没触发，本次拖动就白拖了。关闭前把"还没来得及落盘"的宽度立刻
        # 存一次，不依赖那个计时器最终会不会跑到。
        if self._column_width_save_timer.isActive():
            self._column_width_save_timer.stop()
            self._save_file_table_column_widths()

        if self._job_running:
            reply = QMessageBox.question(
                self, "任务正在进行", "有任务正在处理中，确定要关闭吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self.worker_thread.request_stop_current_job()

        if self.reindex_thread and self.reindex_thread.isRunning():
            reply = QMessageBox.question(
                self, "索引同步中",
                "替换结果正在同步到搜索数据库，现在关闭的话这几个文件的索引会停留在替换前的旧内容，"
                "需要下次启动或手动重建索引才会更新。确定现在关闭吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self.reindex_thread.wait(3000)

        self._shutdown_worker_async()
        event.accept()

    def _shutdown_worker_async(self):
        """
        请求隐藏的 AutoCAD 实例退出、结束常驻工作线程——但不阻塞界面等它
        真正跑完，也绝不强行终止它。

        背景：如果用户在实例还在预热（等 AutoCAD 启动，最长可能三十多
        秒）的时候就关闭对话框，这时 ReplaceThread 正卡在 run() 里那次
        同步的 win32com 调用上，没法立刻响应关闭请求。这里不同步 wait()
        干等，也绝不用 terminate() 强杀——强行终止一个正卡在 COM 调用里
        的线程非常危险，容易把 COM/pywin32 的内部状态弄坏，直接让整个
        Python 进程崩掉。

        改成：request_shutdown() 只是往 queue.Queue 里放一个哨兵值，
        线程该等多久就等多久，等它自己从 DispatchEx 那次调用返回、进到
        run() 里的 while 循环，取到这个哨兵值就会自然退出循环，触发
        run() 自己 finally 块里的 Quit() 清理——不需要界面这边等着它。
        """
        if self.worker_thread is None:
            return

        # 关闭流程一旦开始，对话框大概率很快就没了——这几个会往界面控件
        # 上写内容的连接如果线程在后台跑完之后才触发，操作的是已经被
        # 销毁的控件，会抛异常。先断开，避免这种延迟触发的问题。
        for signal, slot in (
            (self.worker_thread.progress_signal, self._on_progress),
            (self.worker_thread.instance_ready_signal, self._on_instance_ready),
            (self.worker_thread.file_done_signal, self._on_file_done),
            (self.worker_thread.finished_signal, self._on_job_finished),
        ):
            try:
                signal.disconnect(slot)
            except Exception:
                pass

        # 切断跟对话框的 Qt 父子关系，并用模块级列表兜底多持有一份强
        # 引用，防止线程还没退出就被当成"没人用了"提前回收——两者都是
        # 为了避免 "QThread: Destroyed while thread is still running"。
        self.worker_thread.setParent(None)
        _ORPHANED_WORKERS.append(self.worker_thread)

        def _release():
            try:
                _ORPHANED_WORKERS.remove(self.worker_thread)
            except ValueError:
                pass

        self.worker_thread.finished.connect(_release)
        self.worker_thread.request_shutdown()