# results_table_model.py
"""
搜索结果表格的新实现——用 QAbstractTableModel + QTableView 替换旧的
QTableWidget（helpers.py 的 FrozenColumnTableWidget）。

背景：QTableWidget 是 item-based 控件，填多少行就要立刻在主线程创建
多少个真实的 QTableWidgetItem 对象（6列 x 2万行 = 12万个对象），不管
用户看不看得到；这是搜索结果填表耗时 1.5 秒左右、且这个耗时随命中数量
线性增长的根本原因（详见项目内部讨论记录）。QAbstractTableModel 是
纯数据接口，视图只在真正要绘制某个格子时才调用一次 data()，不管命中
2万条还是20条，界面填充耗时理论上应该跟总数基本无关，只跟可见行数
有关。

这是一份**独立、自包含**的新实现，目前不接入 layout.py / search_manager.py
等现有代码——先单独把这一整套（Model + 主视图 + 冻结列 + 排序 + 关键字
高亮 + 内联改名）在这个文件里跑通、跑对，等真机上验证过视觉效果和交互
手感没问题，再考虑替换掉现有的 window.table。

跟现有实现共用（原样复用，不重复造轮子，避免以后两边行为跑偏）：
  - helpers.FilenameHighlightDelegate：纯粹基于 index.data(Qt.DisplayRole)
    取值来绘制高亮，不依赖具体是 QTableWidgetItem 还是自定义 Model，
    可以直接套在这个新 Model 上用，不用改一行。
  - helpers._FrozenColumnSync：负责冻结列宽度双向同步、随主视图垂直
    滚动、贴合几何位置——用到的都是 QAbstractScrollArea/QAbstractItemView
    通用的 API（frameWidth()/viewport()/horizontalHeader()/
    verticalScrollBar()），不是 QTableWidget 专属的，同样可以直接复用。

跟现有实现不同、需要重新写的部分：
  - ResultsTableModel：全新的数据模型，取代 QTableWidgetItem 逐格存储。
  - MainResultsTableView：取代 FrozenColumnTableWidget，職責一样（重写
    scrollTo() 避免选中第0列时把用户手动横向滚动的位置强制复位），但
    继承自 QTableView 而不是 QTableWidget。
  - 排序触发方式：QTableWidget 有原生的 table.sortItems(col, order)；
    QTableView + 自定义 Model 对应的是 table.sortByColumn(col, order)，
    这会调用 model.sort(col, order)，需要在 ResultsTableModel 里实现
    sort() 方法（内部对 self._rows 重新排序，并且要用
    layoutAboutToBeChanged/changePersistentIndexList/layoutChanged 这套
    机制保留住"哪一行还是选中状态"，而不是"哪个视觉位置还是选中状态"——
    效果要跟原来 QTableWidget 排序后选中行不丢失保持一致）。
  - 内联改名：不再是"先改 item 文字、改名失败再手动 setText() 退回原文字"
    这种命令式写法，而是标准 Qt Model/View 模式：setData() 里做真正的
    改名（校验、os.rename()、失败弹窗），失败时直接 return False——
    Qt 收到 False 就不会把编辑器里的新文字提交进模型，视图会自动重新从
    模型读回原来的文字，不需要手动"改回去"这一步，比原来的写法更省
    代码、也更不容易漏掉某个分支忘记回退。
"""
import os
from PyQt5.QtWidgets import (
    QTableView, QAbstractItemView, QHeaderView, QLineEdit, QMessageBox, QFrame,
)
from PyQt5.QtCore import Qt, QAbstractTableModel, QModelIndex, QDateTime, pyqtSignal, QObject, QEvent

from helpers import (
    human_readable_size, FilenameHighlightDelegate, _FrozenColumnSync,
)

COLUMN_NAMES = ["文件名", "文件路径", "创建日期", "修改日期", "DWG版本", "大小"]
COL_NAME, COL_PATH, COL_CTIME, COL_MTIME, COL_VERSION, COL_SIZE = range(6)


class _RowData:
    """一行的全部数据，格式化字符串在装载数据的时候就算好、存起来——
    不要留到 data() 里现算：data() 是每次绘制每个可见格子都会被调用的
    热路径，日期格式化（QDateTime 构造 + toString）、体积单位换算这些
    有一定开销的操作，不应该在滚动、重绘的时候反复重复执行。

    __slots__ 而不是普通类属性：这个对象会被创建几万个（命中多少行就有
    多少个），普通类每个实例都要带一份 __dict__，几万个实例累加起来是
    看得见的内存开销；__slots__ 能省掉这部分。"""
    __slots__ = (
        "path", "filename", "dirname",
        "ctime_raw", "mtime_raw", "size_raw",
        "ctime_str", "mtime_str", "size_str",
        "version", "version_tooltip",
    )

    def __init__(self, path, stat_result, dwg_version):
        self.path = path
        self.filename = os.path.basename(path)
        self.dirname = os.path.dirname(path)

        if stat_result is not None:
            self.ctime_raw = stat_result.st_ctime
            self.mtime_raw = stat_result.st_mtime
            self.size_raw = stat_result.st_size
            self.ctime_str = QDateTime.fromTime_t(int(self.ctime_raw)).toString("yyyy-MM-dd hh:mm:ss")
            self.mtime_str = QDateTime.fromTime_t(int(self.mtime_raw)).toString("yyyy-MM-dd hh:mm:ss")
            self.size_str = human_readable_size(self.size_raw)
        else:
            # 跟原来 update_table_row() 的 except 分支行为一致：stat 拿不到
            # 时这三列显示"未知"，排序时这类"未知"的行统一垫底处理（见
            # ResultsTableModel.sort() 里 sort_key 对 None 的处理）。
            self.ctime_raw = None
            self.mtime_raw = None
            self.size_raw = None
            self.ctime_str = "未知"
            self.mtime_str = "未知"
            self.size_str = "未知"

        self.version = dwg_version if dwg_version else "未知"
        self.version_tooltip = dwg_version if dwg_version else "未能识别到 DWG 版本信息"


class ResultsTableModel(QAbstractTableModel):
    """搜索结果表格的数据模型。外部只需要调用 load_rows() 灌入
    (path, stat_result, dwg_version) 三元组的列表，其余（格式化显示、
    排序、改名）都在这个类内部处理。"""

    # 改名真正落盘成功之后发出去，外部（比如状态栏提示、文字索引库
    # 同步——虽然目前是靠 file_watcher.py 的 on_moved 自动处理）可以按
    # 需要监听这个信号；不发起点也不强制要求有人接。
    rename_succeeded = pyqtSignal(str, str)  # (old_path, new_path)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []  # List[_RowData]
        self._sort_column = None
        self._sort_order = Qt.AscendingOrder

    # ---------------- 数据装载 ----------------

    def load_rows(self, path_stat_version_list):
        """path_stat_version_list: [(path, stat_result_or_None, dwg_version_or_None), ...]
        整批替换当前数据——对应原来 search_manager.py 里
        table.setRowCount(0) 之后重新填充这一整套流程。"""
        self.beginResetModel()
        self._rows = [_RowData(p, s, v) for p, s, v in path_stat_version_list]
        self._sort_column = None  # 新一轮搜索结果，排序状态跟着重置
        self.endResetModel()

    def clear(self):
        self.beginResetModel()
        self._rows = []
        self.endResetModel()

    def path_at(self, row):
        if 0 <= row < len(self._rows):
            return self._rows[row].path
        return None

    # ---------------- QAbstractTableModel 必须实现的接口 ----------------

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMN_NAMES)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            if 0 <= section < len(COLUMN_NAMES):
                return COLUMN_NAMES[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == COL_NAME:
            # 只有文件名这一列可编辑，用来做内联改名——跟原来
            # update_table_row() 里"item_name 显式声明 ItemIsEditable，
            # 其余列去掉 ItemIsEditable"的意图完全一致。
            base |= Qt.ItemIsEditable
        return base

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()

        if role in (Qt.DisplayRole, Qt.EditRole):
            if col == COL_NAME:
                return row.filename
            elif col == COL_PATH:
                return row.path
            elif col == COL_CTIME:
                return row.ctime_str
            elif col == COL_MTIME:
                return row.mtime_str
            elif col == COL_VERSION:
                return row.version
            elif col == COL_SIZE:
                return row.size_str

        elif role == Qt.ToolTipRole:
            if col == COL_NAME:
                return row.filename
            elif col == COL_PATH:
                return row.path
            elif col == COL_VERSION:
                return row.version_tooltip
            elif col == COL_CTIME:
                return row.ctime_str
            elif col == COL_MTIME:
                return row.mtime_str
            elif col == COL_SIZE:
                return row.size_str

        return None

    def setData(self, index, value, role=Qt.EditRole):
        """内联改名的真正入口。文件名列被编辑提交时（回车/点别处/Tab）
        Qt 会调用这里，value 是编辑框里的最终文字。

        返回 False：Qt 认为这次编辑"没有被接受"，不会把 value 写进模型，
        视图会自动重新从 data() 读回原来的文字显示——不需要像原来
        QTableWidgetItem 那套写法一样，手动 blockSignals() + setText()
        把文字改回去，Qt 自己就会做这件事。
        """
        if role != Qt.EditRole or index.column() != COL_NAME:
            return False

        row_idx = index.row()
        row = self._rows[row_idx]
        old_path = row.path
        old_name = row.filename
        new_name = (value or "").strip()

        if not new_name or new_name == old_name:
            return False  # 没有实际改动，视图自动显示回原文字，不用弹提示

        invalid_chars = set('\\/:*?"<>|')
        if any(ch in invalid_chars for ch in new_name):
            QMessageBox.warning(None, "文件名不合法",
                                 '文件名不能包含以下字符：\\ / : * ? " < > |')
            return False

        new_path = os.path.join(row.dirname, new_name)

        if os.path.exists(new_path) and os.path.normcase(new_path) != os.path.normcase(old_path):
            QMessageBox.warning(None, "无法重命名", f"目标位置已经存在同名文件：\n{new_name}")
            return False

        try:
            os.rename(old_path, new_path)
        except Exception as e:
            QMessageBox.warning(None, "重命名失败", f"无法重命名这个文件：\n{e}")
            return False

        # 改名成功：更新这一行的数据（文件名 + 完整路径都要跟着变），
        # 同时通知第1列（文件路径）也要重绘——虽然编辑动作发生在第0列，
        # 但改名会连带影响第1列显示的完整路径，两列都要发 dataChanged。
        row.path = new_path
        row.filename = new_name
        path_index = self.index(row_idx, COL_PATH)
        self.dataChanged.emit(index, index, [Qt.DisplayRole])
        self.dataChanged.emit(path_index, path_index, [Qt.DisplayRole])
        self.rename_succeeded.emit(old_path, new_path)
        return True

    # ---------------- 排序 ----------------

    def sort(self, column, order=Qt.AscendingOrder):
        """QTableView.sortByColumn() / 表头点击排序（开了setSortingEnabled
        之后）最终都会调用到这里。用 layoutAboutToBeChanged +
        changePersistentIndexList + layoutChanged 这套标准做法，保证排序
        之后"原来选中的是哪一行数据"还是选中状态（而不是"选中的第几个
        视觉位置"还是选中状态）——效果对齐原来 QTableWidget.sortItems()
        排序后选中行不丢失的行为。"""
        if not self._rows:
            return

        reverse = (order == Qt.DescendingOrder)

        def sort_key_for(row_data):
            if column == COL_NAME:
                return (row_data.filename or "").lower()
            elif column == COL_PATH:
                return (row_data.path or "").lower()
            elif column == COL_CTIME:
                return row_data.ctime_raw
            elif column == COL_MTIME:
                return row_data.mtime_raw
            elif column == COL_VERSION:
                return (row_data.version or "").lower()
            elif column == COL_SIZE:
                return row_data.size_raw
            return None

        def sort_key_wrapper(row_data):
            """(是否是"空值", 真实排序值) 这样一个元组：True > False，
            所以拿不到数据的行（None）不管升降序都会自然排在最后，不用
            另外写分支特殊处理"未知"这几行该排在哪。真实值按字符串还是
            数字比较，取决于这一列本身的 sort_key_for 返回的是什么类型，
            同一列内部类型是统一的（要么全是字符串，要么全是数字/None），
            混合列内不会出现"字符串跟数字互相比较"这种 Python 会直接
            报错的情况。"""
            key = sort_key_for(row_data)
            is_empty = key is None or key == ""
            return (is_empty, key if not is_empty else "")

        self.layoutAboutToBeChanged.emit()
        old_persistent_indexes = self.persistentIndexList()
        old_paths_by_row = {idx.row(): self._rows[idx.row()].path
                             for idx in old_persistent_indexes if idx.isValid()}

        self._rows.sort(key=sort_key_wrapper, reverse=reverse)

        path_to_new_row = {row_data.path: i for i, row_data in enumerate(self._rows)}
        new_persistent_indexes = []
        for idx in old_persistent_indexes:
            if not idx.isValid():
                new_persistent_indexes.append(QModelIndex())
                continue
            old_path = old_paths_by_row.get(idx.row())
            new_row = path_to_new_row.get(old_path)
            if new_row is None:
                new_persistent_indexes.append(QModelIndex())
            else:
                new_persistent_indexes.append(self.index(new_row, idx.column()))
        self.changePersistentIndexList(old_persistent_indexes, new_persistent_indexes)

        self._sort_column = column
        self._sort_order = order
        self.layoutChanged.emit()


class MainResultsTableView(QTableView):
    """对应原来 helpers.FrozenColumnTableWidget：重写 scrollTo()，避免
    用户点击冻结列（叠加视图）选中某一行时，Qt 内置的"自动横向滚动到
    当前选中格子可见"这个行为把用户手动横向拖动过的滚动位置强制复位
    到最左边——只拦第0列（文件名）这一种情况，其余列该怎么自动滚动
    还是怎么滚动，原因详见 helpers.py 里 FrozenColumnTableWidget 的
    说明，这里是同一个问题、同一个解法，只是基类换成了 QTableView。"""

    def scrollTo(self, index, hint=QAbstractItemView.EnsureVisible):
        if index.isValid() and index.column() == COL_NAME:
            return
        super().scrollTo(index, hint)


class _F2RenameEventFilter(QObject):
    """F2 触发改名编辑，要转发给冻结浮层去处理，不能让主表格自己处理。

    背景：第0列（文件名）在屏幕上实际显示的是冻结浮层（frozen_view）
    盖在主表格（table）上面那一层——两者是层叠关系，frozen_view 在
    z 轴顺序上盖住了 table 对应的那块区域。如果编辑触发（F2/双击/
    点击已选中行）落在 table 自己身上，Qt 会在 table 自己的坐标系统里
    创建编辑框，这个编辑框虽然位置算得没错，但是会被叠在上面的
    frozen_view 挡住——用户看到的、能操作的还是 frozen_view 画出来的
    旧文字，编辑框其实存在，只是"藏"在下面，等于摸不到、看不见。

    "点击已选中行触发编辑"这条路径天然不会有这个问题，因为鼠标点击
    事件本来就是先落在最上层的 frozen_view 身上，由它自己触发编辑，
    编辑框自然创建在正确的、看得见的那一层。但 F2 是键盘事件，走的是
    "当前拥有焦点的控件"——frozen_view 为了不抢用户在主表格上的操作
    焦点，特意设成了 NoFocus（见下方 frozen_view.setFocusPolicy），
    所以键盘事件永远落在 table 身上，不会自动转发给 frozen_view。

    这个事件过滤器专门补上这一环：拦截 table 收到的 F2 按键，不让它
    在 table 自己身上触发编辑（table 的 editTriggers 也确实没有开
    EditKeyPressed，双保险），而是主动调用 frozen_view.edit()，把
    编辑动作显式地转发到真正看得见、摸得着的那一层。"""

    def __init__(self, table, frozen_view, parent=None):
        super().__init__(parent)
        self._table = table
        self._frozen_view = frozen_view

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_F2:
            selection_model = self._table.selectionModel()
            current = selection_model.currentIndex()
            if current.isValid():
                name_index = self._table.model().index(current.row(), COL_NAME)
                selection_model.setCurrentIndex(
                    name_index, selection_model.SelectCurrent | selection_model.Rows
                )
                self._frozen_view.edit(name_index)
            return True  # 事件到此为止，不再往下传给 table 自己处理
        return False


def create_frozen_first_column_view(table):
    """跟 helpers.create_frozen_first_column() 是同一个功能（横向滚动时
    第0列固定不动、拖宽表头会推挤右边的列、点文件名表头可以排序），
    针对 table 是 QTableView（而不是 QTableWidget）做了适配。

    跟原函数的唯一实质区别：排序触发从 QTableWidget 专有的
    table.sortItems(col, order) 换成了 QTableView 通用的
    table.sortByColumn(col, order)（这个调用最终会转发到
    model.sort(col, order)，也就是 ResultsTableModel.sort()）。
    其余逻辑（宽度双向同步、滚动同步、排序箭头显示/收起的状态管理）
    原样照抄，没有改动。"""
    frozen_view = QTableView(table)
    frozen_view.setModel(table.model())
    frozen_view.setSelectionModel(table.selectionModel())
    frozen_view.setFocusPolicy(Qt.NoFocus)
    frozen_view.setFrameShape(QFrame.NoFrame)

    for col in range(1, table.model().columnCount()):
        frozen_view.setColumnHidden(col, True)

    frozen_view.verticalHeader().setVisible(False)
    frozen_view.horizontalHeader().setFixedHeight(table.horizontalHeader().height())
    frozen_view.horizontalHeader().setHighlightSections(False)
    frozen_view.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
    frozen_view.horizontalHeader().setSectionsClickable(True)
    frozen_view.horizontalHeader().setSortIndicatorShown(False)
    frozen_view.setShowGrid(table.showGrid())
    frozen_view.setAlternatingRowColors(table.alternatingRowColors())
    frozen_view.setStyleSheet(
        "QTableView {"
        "    border: none;"
        "    alternate-background-color: #f4f6f9;"
        "    background-color: #ffffff;"
        "    gridline-color: #e3e6ea;"
        "}"
        "QTableView::item:selected {"
        "    background-color: #cfe4ff;"
        "    color: black;"
        "}"
    )
    frozen_view.setSelectionBehavior(table.selectionBehavior())
    frozen_view.setSelectionMode(table.selectionMode())
    frozen_view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    frozen_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    frozen_view.setColumnWidth(0, table.columnWidth(0))
    frozen_view.setEditTriggers(QAbstractItemView.SelectedClicked)
    frozen_view.show()
    frozen_view.raise_()

    table._filename_sort_order = None

    def _on_frozen_header_clicked(logical_index):
        if logical_index != 0:
            return
        if table._filename_sort_order == Qt.AscendingOrder:
            new_order = Qt.DescendingOrder
        else:
            new_order = Qt.AscendingOrder
        table._filename_sort_order = new_order
        table.sortByColumn(0, new_order)
        frozen_view.horizontalHeader().setSortIndicatorShown(True)
        frozen_view.horizontalHeader().setSortIndicator(0, new_order)
        table.horizontalHeader().setSortIndicator(0, new_order)

    frozen_view.horizontalHeader().sectionClicked.connect(_on_frozen_header_clicked)

    def _on_other_column_sorted(logical_index, order):
        if logical_index == 0:
            return
        frozen_view.horizontalHeader().setSortIndicatorShown(False)
        table._filename_sort_order = None

    table.horizontalHeader().sortIndicatorChanged.connect(_on_other_column_sorted)

    _FrozenColumnSync(table, frozen_view)

    f2_filter = _F2RenameEventFilter(table, frozen_view, parent=table)
    table.installEventFilter(f2_filter)
    table._f2_rename_filter = f2_filter  # 防止被垃圾回收，没有强引用这个过滤器对象很快会被回收失效

    return frozen_view


def create_results_table(parent=None):
    """一次性搭好整套结果表格：Model + 主视图 + 冻结列 + 排序 + 文件名
    关键字高亮委托。返回 (model, table, frozen_view)，调用方（目前只有
    本文件末尾的独立测试脚本；以后真正接入时会是 layout.py）拿到这三个
    对象后，跟原来 layout.py 里那段代码一样，接着设置列宽、连接右键菜单、
    双击、itemChanged 等事件——不过这次这些事件的连接方式在 QTableView
    下跟 QTableWidget 略有出入，接入的时候要对应换成 QTableView 的
    等价信号（比如 cellDoubleClicked → doubleClicked(index)），这一步
    留到真正替换 layout.py 时再做，不在这个独立测试阶段的范围内。"""
    model = ResultsTableModel()

    table = MainResultsTableView(parent)
    table.setModel(model)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setHighlightSections(False)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.ExtendedSelection)
    table.setEditTriggers(QAbstractItemView.SelectedClicked)
    # 主表格自己只保留 SelectedClicked（点击已选中的行再点一下）这一种
    # 编辑触发方式——这个场景下鼠标点击本来就是先落在最上层的
    # frozen_view 身上（第0列在屏幕上实际显示的是 frozen_view 盖在
    # table 上面那一层），所以 SelectedClicked 即使挂在 table 身上，
    # 实际触发编辑的也是 frozen_view，不会有"编辑框创建在被遮挡的
    # 底层，看不见摸不着"这个问题。
    #
    # 特意不用 Qt 默认值（DoubleClicked | EditKeyPressed | AnyKeyPressed）：
    #   - AnyKeyPressed 很危险：选中一行后随便按个字符键就会立刻进入
    #     改名编辑态，且这第一下按键会直接替换掉原文件名的第一个字符——
    #     手滑、习惯性按串了键，都可能在毫无预警的情况下触发一次真实的
    #     改名操作，风险跟"F2"或"点击已选中行"这两种主动操作完全不对等，
    #     宁可牺牲这个"跟 Windows 资源管理器一致"的小方便也要去掉。
    #   - EditKeyPressed（F2）不能留在 table 身上：跟 SelectedClicked
    #     不一样，F2 是键盘事件，走的是"当前拥有焦点的控件"——
    #     frozen_view 为了不抢主表格的操作焦点，特意设成了 NoFocus，
    #     键盘事件永远先落在 table 身上。如果让 table 自己响应
    #     EditKeyPressed，编辑框会创建在 table 自己的坐标系里，被叠在
    #     上面的 frozen_view 挡住，用户什么都看不见、摸不着（这正是
    #     "F2 进入编辑态，只会进入底下那一层"这个问题的根源）。F2 改成
    #     用下面的 _F2RenameEventFilter 主动转发给 frozen_view.edit()
    #     处理，效果上等同于把 F2 也变成了"由最上层去触发"，就没有这个
    #     隐患了。
    #   - DoubleClicked 也一并去掉：第0列的双击事件走的是 frozen_view
    #     自己的处理，跟这份 editTriggers 无关；其余列都不可编辑（见
    #     ResultsTableModel.flags()），去掉纯粹是为了明确表达意图。
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.setSortingEnabled(True)

    highlight_delegate = FilenameHighlightDelegate(table)
    table.setItemDelegateForColumn(COL_NAME, highlight_delegate)

    table.setColumnWidth(COL_NAME, 150)
    table.setColumnWidth(COL_PATH, 310)
    table.setColumnWidth(COL_CTIME, 150)
    table.setColumnWidth(COL_MTIME, 150)
    table.setColumnWidth(COL_VERSION, 130)
    table.setColumnWidth(COL_SIZE, 80)

    frozen_view = create_frozen_first_column_view(table)
    frozen_view.setItemDelegateForColumn(COL_NAME, highlight_delegate)

    return model, table, frozen_view, highlight_delegate