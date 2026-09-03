# folder_scope_bar.py
"""
"搜索目录"那一行的多目录范围选择器：一排可勾选的"目录标签"，替代原来
只能填一个目录的下拉框（QComboBox path_edit）。

设计取舍（对应"分两步走"里的第一步）：
- 只做"标签栏 + 加号 + 悬停删除 + 空白处点击/粘贴添加"，不做展开态的
  完整磁盘容量浏览器（带每个盘总大小/可用空间进度条的那个下拉面板）。
  那部分是可选的第二步，跟"支持多目录搜索"这个核心目的关系不大，工作
  量却是这里的好几倍，先不做——真正需要的时候再单独加。
- 一个标签都没有 = 全部目录，跟以前 path_edit 里"🖥 全部目录"是同一个
  语义，不需要单独一个"全部目录"标签占位。这也是为什么空白状态本身
  就是合法、有意义的状态，不是"用户还没设置"。

目录标签的点击手势完全照搬 Windows 资源管理器的手感（选中态 + 原地
改名），不是"猜时间间隔"那种容易误判的做法：
- 点一下图标/名字 → 选中这个标签（高亮变色），跟别的标签互斥，同一时刻
  只有一个能被选中。
- 已经选中的标签再点一下 → 进入编辑态，显示可编辑的完整路径。
- 正常速度的双击（Qt 自己就能识别成 MouseButtonDblClick 的那种）→
  不管选没选中，直接用资源管理器打开这个目录。
- 编辑态下点框外任何地方（不只是这个标签外面，是整个程序窗口范围内
  只要不是正在编辑的输入框/浏览按钮）→ 立刻提交这次编辑，不需要按回车。
"""
import os
from PyQt5.QtWidgets import (
    QWidget, QHBoxLayout, QCheckBox, QLabel, QToolButton, QFileDialog,
    QSizePolicy, QFrame, QApplication, QLineEdit
)
from PyQt5.QtCore import Qt, pyqtSignal, QEvent, QMimeData
from PyQt5.QtGui import QFontMetrics, QKeySequence, QCursor, QDrag

# 拖拽排序用的自定义 MIME 类型：拖起来的时候把这个标签的目录路径（作为
# 唯一标识）装进去，松手那一刻在目标位置反查出是拖的哪一个标签。用路径
# 当标识而不是内存地址/索引，是因为标签本身在拖拽过程中不会被销毁重
# 建，路径又天然唯一（FolderScopeBar 本来就不允许重复路径），不需要
# 另外发明一套 id 机制。
_FOLDER_CHIP_MIME_TYPE = "application/x-dwgsearch-folderchip-path"


class _ChipPathEdit(QLineEdit):
    """编辑目录路径用的输入框，就多包一个 Esc 取消编辑的信号——
    QLineEdit 原生没有这个，回车用自带的 returnPressed 就够了。"""
    cancelled = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
            return
        super().keyPressEvent(event)


class _FolderChip(QFrame):
    """单个目录标签：复选框 + 📁 图标 + 名字（过长省略号）+ 悬停才显现的
    "×"删除按钮。

    "×"按钮平时彻底隐藏（真正的 setVisible(False)，不是靠改颜色让它
    "看起来"透明）——之前试过用 `color: transparent` 让文字颜色跟背景
    融为一体来模拟隐藏效果，理论上能避免悬停时标签宽度跳动，但实测在
    Windows 原生渲染下这个技巧没有完全生效，"×"变成了不管鼠标在不在
    都一直显示的红色，等于没隐藏，比宽度跳动这点小瑕疵更糟。所以老老
    实实换回真正的显示/隐藏切换，代价是"×"从无到有出现的瞬间标签宽度
    会有一点点跳动，但至少行为是对的。

    显示/隐藏的判断不依赖 Qt 的 enterEvent/leaveEvent 在父子控件之间
    传递的顺序（鼠标从标签背景划到内部子控件——比如复选框——上面时，
    Qt 可能会先给父控件发 Leave、再给子控件发 Enter，如果单纯"进就显示
    退就隐藏"会导致划过子控件的瞬间"×"被误判成"离开了"而闪一下）。
    改成每次任何相关的 Enter/Leave 事件发生时，都直接用鼠标当前的
    全局坐标去做一次"是否落在整个标签矩形范围内"的几何判断——不管
    事件具体是从哪个子控件来的，判断结果都是准的，不会因为事件传递
    顺序的细节而误判。

    选中/编辑这两个状态的"决策"都交给 FolderScopeBar 统一调度（谁选中、
    谁在编辑，这些是标签栏级别的互斥状态，单个标签自己不知道兄弟标签
    的情况）；这个类本身只负责"图标/名字区域被点了一下"这个原始事件，
    以及选中态、编辑态各自的显示效果和编辑时的输入逻辑。
    """
    selectRequested = pyqtSignal()  # 图标/名字区域被点了一下，且当前未选中
    editRequested = pyqtSignal()    # 已经选中的情况下又点了一下，请求进入编辑
    openRequested = pyqtSignal()    # 正常速度的双击，请求用资源管理器打开
    toggled = pyqtSignal()          # 勾选状态变化
    removeRequested = pyqtSignal()  # 点了"×"，请求从条上移除
    pathChanged = pyqtSignal()      # 编辑路径之后，目录变了

    MAX_LABEL_WIDTH = 110  # 名字超过这个像素宽度就省略号截断

    _BASE_STYLE = (
        "QFrame#folderChip {"
        "    border: 1px solid #d5d9de;"
        "    background-color: #f7f8fa;"
        "}"
        "QFrame#folderChip QToolButton#chipCloseBtn {"
        "    border: none; background: transparent; color: #c0392b;"
        "}"
        "QFrame#folderChip QToolButton#chipCloseBtn:hover {"
        "    color: #ffffff; background-color: #c0392b; border-radius: 8px;"
        "}"
    )
    _SELECTED_STYLE = _BASE_STYLE.replace(
        "border: 1px solid #d5d9de;", "border: 1px solid #4a90d9;"
    ).replace(
        "background-color: #f7f8fa;", "background-color: #cfe4ff;"
    )

    def __init__(self, path, checked=True, parent=None):
        super().__init__(parent)
        self.path = os.path.normpath(path)
        self.duplicate_check = None  # FolderScopeBar 会注入这个回调，编辑时查重用
        self._selected = False
        self._editing = False
        self._edit_field = None
        self._browse_btn = None
        self._browsing = False  # "..."浏览对话框开着的时候，暂停全局点击监听

        # 拖拽排序相关的临时状态，只在"鼠标按下 -> 抬起/开始拖拽"这一
        # 小段时间内有意义，见 eventFilter 里的详细说明。
        self._press_global_pos = None
        self._press_was_selected = False
        self._drag_started = False

        self.setObjectName("folderChip")
        # 跟 FolderScopeBar 一样的道理，显式打开这个属性确保样式表里的
        # border/background-color 一定会被画出来，不依赖 QFrame 默认行为。
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(self._BASE_STYLE)

        self._chip_layout = QHBoxLayout(self)
        self._chip_layout.setContentsMargins(6, 2, 4, 2)
        self._chip_layout.setSpacing(4)

        self.checkbox = QCheckBox()
        self.checkbox.setChecked(checked)
        self.checkbox.toggled.connect(lambda _checked: self.toggled.emit())
        self._chip_layout.addWidget(self.checkbox)
        self.checkbox.installEventFilter(self)

        self._icon_label = QLabel("📁")
        self._chip_layout.addWidget(self._icon_label)

        display_name = os.path.basename(self.path.rstrip("\\/")) or self.path
        fm = QFontMetrics(self.font())
        elided = fm.elidedText(display_name, Qt.ElideRight, self.MAX_LABEL_WIDTH)
        self.name_label = QLabel(elided)
        self._chip_layout.addWidget(self.name_label)

        # 名字被省略号截断时，鼠标悬停能看到完整路径；标签整体也挂一份，
        # 划到复选框/图标上同样能看到。
        self.setToolTip(self.path)
        self.name_label.setToolTip(self.path)

        # 图标 + 名字这两个子控件要响应点击手势——直接在它们身上装事件
        # 过滤器，统一转发到 chip 自己处理，比每个都单独重写一遍简单，
        # 也避免了子控件的鼠标事件不会自动"冒泡"给父控件这个 Qt 特性
        # 带来的麻烦。不设手型光标，跟资源管理器里点文件名一样保持
        # 普通箭头光标，不用变手指。
        self._icon_label.installEventFilter(self)
        self.name_label.installEventFilter(self)

        self.close_btn = QToolButton()
        self.close_btn.setObjectName("chipCloseBtn")
        self.close_btn.setText("✕")
        self.close_btn.setAutoRaise(True)
        self.close_btn.setFixedSize(16, 16)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setToolTip("移除这个搜索目录")
        self.close_btn.clicked.connect(self.removeRequested.emit)
        self.close_btn.setVisible(False)  # 默认隐藏，鼠标划进标签范围才显现
        self._chip_layout.addWidget(self.close_btn)

    def is_checked(self):
        return self.checkbox.isChecked()

    # ------------------------------------------------------------------
    # "×"删除按钮的显隐：几何判断，不依赖 Enter/Leave 事件传递顺序
    # ------------------------------------------------------------------
    def _refresh_close_btn_visibility(self):
        if self._editing:
            self.close_btn.setVisible(False)
            return
        local_pos = self.mapFromGlobal(QCursor.pos())
        self.close_btn.setVisible(self.rect().contains(local_pos))

    def enterEvent(self, event):
        self._refresh_close_btn_visibility()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._refresh_close_btn_visibility()
        super().leaveEvent(event)

    # ------------------------------------------------------------------
    # 选中态：由 FolderScopeBar 统一调度调用，标签自己只负责显示效果
    # ------------------------------------------------------------------
    def set_selected(self, selected):
        if self._selected == selected:
            return
        self._selected = selected
        self.setStyleSheet(self._SELECTED_STYLE if selected else self._BASE_STYLE)

    def is_selected(self):
        return self._selected

    # ------------------------------------------------------------------
    # 图标/名字区域的点击手势 + 复选框/图标/名字的 Enter/Leave 也要联动
    # 刷新"×"的显隐（见类注释：不能只靠 chip 自己的 enterEvent/leaveEvent，
    # 鼠标划到子控件上时事件传递顺序不可靠，统一走同一个几何判断兜底）
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event):
        if obj in (self._icon_label, self.name_label, self.checkbox):
            if event.type() in (QEvent.Enter, QEvent.Leave):
                self._refresh_close_btn_visibility()

        if obj in (self._icon_label, self.name_label) and not self._editing:
            # 点击手势跟拖拽排序手势都是"在图标/名字区域按下鼠标"，光看
            # 按下这一刻没法区分用户是想点一下（选中/进编辑），还是想
            # 按住拖动去调整这个标签的排列顺序。原来的写法是一按下就
            # 立刻判定选中还是编辑——如果直接保留这个写法，"已经选中的
            # 标签再按一下"会立刻把图标/名字换成编辑输入框，拖拽这个
            # 动作根本来不及发生。这里改成"先记下按下的位置和当时是否
            # 已选中，按住不放移动超过系统的拖拽判定阈值就当成拖拽处理，
            # 抬起时如果全程没有触发拖拽，才按原来的规则决定选中还是
            # 进编辑"，两种手势不再互相抢占。
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._press_global_pos = event.globalPos()
                self._press_was_selected = self._selected
                self._drag_started = False
                return True
            elif event.type() == QEvent.MouseMove and (event.buttons() & Qt.LeftButton):
                if self._press_global_pos is not None and not self._drag_started:
                    moved = (event.globalPos() - self._press_global_pos).manhattanLength()
                    if moved >= QApplication.startDragDistance():
                        self._drag_started = True
                        self._start_reorder_drag()
                return True
            elif event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                if not self._drag_started and self._press_global_pos is not None:
                    # 干干净净的一次点击，没有被识别成拖拽：按原来的
                    # 规则走，选中还是进编辑看按下那一刻是不是已选中。
                    if self._press_was_selected:
                        self.editRequested.emit()
                    else:
                        self.selectRequested.emit()
                self._press_global_pos = None
                self._drag_started = False
                return True
            elif event.type() == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
                # 双击的第二次按下已经被上面的 MouseButtonPress 分支
                # 记过一次状态，这里清空掉，避免紧跟在双击后面的那次
                # Release 事件被误判成一次普通点击、多余地再触发一次
                # 选中/编辑请求。
                self._press_global_pos = None
                self._drag_started = False
                self.openRequested.emit()
                return True
        return super().eventFilter(obj, event)

    def _start_reorder_drag(self):
        """把自己（这个标签当前长相的一份截图 + 自己的目录路径）打包
        成一次拖拽操作发出去。落到哪、要不要真的挪动位置，交给
        FolderScopeBar.dropEvent() 统一处理——单个标签自己不知道兄弟
        标签都在哪，也不该知道，排序这件事是标签栏级别的调度，跟"谁
        被选中/谁在编辑"是同一个道理。
        """
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_FOLDER_CHIP_MIME_TYPE, self.path.encode("utf-8"))
        drag.setMimeData(mime)
        # 拖拽过程中鼠标旁边跟着一份这个标签当前长相的预览图，直观地
        # 告诉用户正在拖的是哪一个、拖没拖起来——不做的话，整个拖拽
        # 过程中鼠标附近没有任何视觉反馈，用户容易怀疑自己是不是根本
        # 没拖动。
        pixmap = self.grab()
        drag.setPixmap(pixmap)
        drag.setHotSpot(self.mapFromGlobal(QCursor.pos()))
        drag.exec_(Qt.MoveAction)

    def open_in_explorer(self):
        if os.path.isdir(self.path):
            try:
                os.startfile(self.path)
            except Exception:
                pass  # 目录可能刚好被移走/断开了，安静地什么都不做

    # ------------------------------------------------------------------
    # 编辑态：把名字换成可编辑的完整路径输入框 + "..."浏览按钮
    # ------------------------------------------------------------------
    def is_editing(self):
        return self._editing

    def is_click_inside_editor(self, widget):
        """widget 是不是"正在编辑用的输入框/浏览按钮"本身——全局点击
        监听靠这个方法判断"点的是不是编辑区域自己"，不是就要提交。"""
        return widget is self._edit_field or widget is self._browse_btn

    def is_browsing(self):
        return self._browsing

    def enter_edit_mode(self):
        if self._editing:
            return
        self._editing = True
        self._icon_label.setVisible(False)
        self.name_label.setVisible(False)
        self.close_btn.setVisible(False)  # 编辑输入框占了这块地方，删除按钮先让位

        self._edit_field = _ChipPathEdit(self.path)
        self._edit_field.setMinimumWidth(180)
        self._edit_field.selectAll()
        self._edit_field.returnPressed.connect(self.commit_edit)
        self._edit_field.cancelled.connect(self.cancel_edit)

        self._browse_btn = QToolButton()
        self._browse_btn.setText("...")
        self._browse_btn.setCursor(Qt.PointingHandCursor)
        # 不让这个按钮抢焦点——否则点它的瞬间会先让输入框失焦，如果那时
        # 还接了 editingFinished 之类"失焦即提交"的信号，会在对话框真正
        # 打开之前就把编辑态拆掉。现在提交这件事统一交给 FolderScopeBar
        # 的全局点击监听处理，这里更多是保险，避免焦点跳来跳去添乱。
        self._browse_btn.setFocusPolicy(Qt.NoFocus)
        self._browse_btn.clicked.connect(self._browse_new_path)

        insert_index = self._chip_layout.indexOf(self.close_btn)
        self._chip_layout.insertWidget(insert_index, self._edit_field)
        self._chip_layout.insertWidget(insert_index + 1, self._browse_btn)

        self._edit_field.setFocus()

    def _browse_new_path(self):
        self._browsing = True  # 对话框开着的时候，全局点击监听要先歇一下
        new_dir = QFileDialog.getExistingDirectory(self, "选择目录", self.path)
        self._browsing = False
        if new_dir:
            self._edit_field.setText(new_dir)
        self._edit_field.setFocus()

    def commit_edit(self):
        if not self._editing:
            return
        new_path = self._edit_field.text().strip()
        if new_path and os.path.isdir(new_path):
            norm = os.path.normpath(new_path)
            if norm.lower() != self.path.lower():
                # 别的标签已经在用这个目录了，安静地放弃这次修改——
                # 跟粘贴路径无效时的处理原则一样，不弹窗打断操作。
                if self.duplicate_check is None or not self.duplicate_check(norm):
                    self.path = norm
                    self._refresh_display()
                    self.pathChanged.emit()
        # 路径不合法、或者跟原来一样、或者跟别的标签重复，都不算错误，
        # 直接安静地退回原来的显示。
        self._exit_edit_mode()

    def cancel_edit(self):
        self._exit_edit_mode()

    def _exit_edit_mode(self):
        if not self._editing:
            return
        self._editing = False
        for w in (self._edit_field, self._browse_btn):
            if w is not None:
                self._chip_layout.removeWidget(w)
                w.deleteLater()
        self._edit_field = None
        self._browse_btn = None
        self._icon_label.setVisible(True)
        self.name_label.setVisible(True)
        self._refresh_close_btn_visibility()  # 退出编辑后，按鼠标当前实际位置重新判断该不该显示

    def _refresh_display(self):
        display_name = os.path.basename(self.path.rstrip("\\/")) or self.path
        fm = QFontMetrics(self.font())
        elided = fm.elidedText(display_name, Qt.ElideRight, self.MAX_LABEL_WIDTH)
        self.name_label.setText(elided)
        self.setToolTip(self.path)
        self.name_label.setToolTip(self.path)


class FolderScopeBar(QWidget):
    """整行控件：一串 _FolderChip + "+" 按钮 + 剩余空白区域（点击/
    Ctrl+V 粘贴都能加目录，对应"点击此处添加搜索路径。之后按 Ctrl+V
    从剪贴板粘贴路径。"这句提示）。

    同一时刻最多一个标签处于"选中"状态、最多一个标签处于"编辑"状态
    （这两者互斥性质不同：选中态之间互斥是产品逻辑要求；编辑态本身
    只是恰好通常也只会有一个，因为进编辑前必须先选中）——这两个状态
    的调度都在这个类里统一处理，靠一个装在 QApplication 上的全局点击
    监听来实现"点哪都能取消选中/提交编辑"，因为很多能点的地方（其它
    标签的文字、下面的空白区域）本身默认不接受键盘焦点，指望 Qt 原生
    的失焦事件是不够的。
    """

    # 目录被增删、或某一个目录的勾选状态变化时发出，外部（比如
    # main_window.py 接一个 save_config）可以借此立刻持久化，不用等到
    # 下次点搜索才存盘。
    scopeChanged = pyqtSignal()

    EMPTY_HINT = "点击此处添加搜索路径。之后按 Ctrl+V 从剪贴板粘贴路径。"
    EMPTY_HINT_SHORT = "点击此处添加搜索路径。"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chips = []  # [_FolderChip, ...]，显示顺序即索引优先级顺序
        self._selected_chip = None
        self._editing_chip = None

        self.setObjectName("folderScopeBar")
        # QWidget 默认不会把样式表里的 border/background-color 真正画
        # 出来（这是 Qt 的一个常见坑：QFrame 会自动画，纯 QWidget 不会），
        # 不开这个属性的话，下面整段 setStyleSheet 形同虚设——边框和
        # 背景色压根不会显示，只有里面的子控件（"+"按钮、标签）自己
        # 浮在父级背景上，效果就是"看起来完全没有框"。
        self.setAttribute(Qt.WA_StyledBackground, True)
        # 接受标签拖拽排序的drop——具体接不接、怎么处理见下面
        # dragEnterEvent/dragMoveEvent/dropEvent。
        self.setAcceptDrops(True)
        self.setStyleSheet(
            "QWidget#folderScopeBar {"
            "    border: 1px solid #c7ccd1;"
            "    background-color: #ffffff;"
            "}"
        )

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(6, 3, 6, 3)
        self._layout.setSpacing(6)

        self.add_btn = QToolButton()
        self.add_btn.setText("+")
        self.add_btn.setToolTip("添加搜索目录")
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.setFixedSize(20, 20)
        # 圆形浅蓝底 + 白色"+"，跟输入框里嵌入的放大镜图标一个思路，
        # 一眼就能看出"这里可以点"。
        self.add_btn.setStyleSheet(
            "QToolButton {"
            "    border: none; border-radius: 10px;"
            "    background-color: #4a90d9; color: #ffffff;"
            "    font-weight: bold; font-size: 13px;"
            "}"
            "QToolButton:hover { background-color: #3a7bc8; }"
            "QToolButton:pressed { background-color: #2f6bb0; }"
        )
        self.add_btn.clicked.connect(self._browse_and_add)
        self._layout.addWidget(self.add_btn)

        # 加号右边这一片区域：一个都没添加时，直接在框里常驻显示提示
        # 文字（不用等鼠标悬停才看到），点击/聚焦后 Ctrl+V 都能加目录；
        # 一旦有了至少一个标签，这片区域就变回纯粹的空白占位（文字清空），
        # 不会一直跟已经加好的标签抢着显示。不设手型光标，跟标签本身
        # 保持一致，都用普通箭头光标。
        self._blank_area = QLabel()
        self._blank_area.setStyleSheet("color: #9aa0a6;")
        self._blank_area.setToolTip(self.EMPTY_HINT)
        self._blank_area.setFocusPolicy(Qt.ClickFocus)
        self._blank_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._blank_area.mousePressEvent = self._on_blank_area_clicked
        self._blank_area.keyPressEvent = self._on_blank_area_key
        self._layout.addWidget(self._blank_area, 1)
        self._update_hint()

        # 全局点击监听：装在整个应用程序上，不管点窗口里哪个角落都能
        # 收到通知，用来实现"点哪都能取消选中态/提交编辑态"。装一次
        # 常驻即可，事件很轻量，没必要跟着选中/编辑状态动态装卸。
        QApplication.instance().installEventFilter(self)

    # ------------------------------------------------------------------
    # 全局点击监听：取消选中 / 提交编辑
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress:
            # "..."浏览对话框开着的时候，点击发生在对话框自己的窗口里，
            # 跟这个标签栏毫无关系，必须先放过，不然会被误判成"点了
            # 外面"，提前把还没选完路径的编辑态拆掉。
            if self._editing_chip is not None and self._editing_chip.is_browsing():
                return False

            clicked_widget = QApplication.widgetAt(event.globalPos())

            if self._editing_chip is not None:
                if not self._editing_chip.is_click_inside_editor(clicked_widget):
                    self._editing_chip.commit_edit()
                    self._editing_chip = None

            if self._selected_chip is not None:
                still_inside = (
                    clicked_widget is not None
                    and (clicked_widget is self._selected_chip
                         or self._selected_chip.isAncestorOf(clicked_widget))
                )
                if not still_inside:
                    self._selected_chip.set_selected(False)
                    self._selected_chip = None

        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # 交互：点击空白区 / Ctrl+V 粘贴
    # ------------------------------------------------------------------
    def _on_blank_area_clicked(self, event):
        self._blank_area.setFocus()
        self._browse_and_add()

    def _on_blank_area_key(self, event):
        if event.matches(QKeySequence.Paste):
            # 剪贴板里常见会带一对包裹的双引号（比如从资源管理器地址栏
            # 复制路径），先去掉再判断是不是真实存在的目录；不是目录的
            # 文本（用户可能只是手滑粘错了别的内容）直接安静地忽略掉，
            # 不弹错误框打断操作——粘贴本来就该是"对了就加，不对就没事"。
            text = QApplication.clipboard().text().strip().strip('"')
            if text and os.path.isdir(text):
                self.add_folder(text, checked=True)
            return
        QLabel.keyPressEvent(self._blank_area, event)

    def _browse_and_add(self):
        dir_path = QFileDialog.getExistingDirectory(self, "选择搜索目录")
        if dir_path:
            self.add_folder(dir_path, checked=True)

    def _update_hint(self):
        """一个标签都没有时，在空白区域常驻显示提示文字；有标签之后
        清空文字，变回纯粹的空白占位，不跟已经加好的标签抢地方。"""
        self._blank_area.setText("" if self._chips else self.EMPTY_HINT_SHORT)

    def _is_duplicate_path(self, path, exclude_chip):
        norm = path.lower()
        return any(c is not exclude_chip and c.path.lower() == norm for c in self._chips)

    # ------------------------------------------------------------------
    # 单个标签的选中 / 编辑 / 打开请求
    # ------------------------------------------------------------------
    def _on_chip_select_requested(self, chip):
        if self._selected_chip is not None and self._selected_chip is not chip:
            self._selected_chip.set_selected(False)
        chip.set_selected(True)
        self._selected_chip = chip

    def _on_chip_edit_requested(self, chip):
        chip.enter_edit_mode()
        self._editing_chip = chip

    def _on_chip_open_requested(self, chip):
        chip.open_in_explorer()

    # ------------------------------------------------------------------
    # 拖拽排序：接住 _FolderChip._start_reorder_drag() 发起的拖拽
    # ------------------------------------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(_FOLDER_CHIP_MIME_TYPE):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(_FOLDER_CHIP_MIME_TYPE):
            event.acceptProposedAction()

    def dropEvent(self, event):
        if not event.mimeData().hasFormat(_FOLDER_CHIP_MIME_TYPE):
            return
        dragged_path = bytes(event.mimeData().data(_FOLDER_CHIP_MIME_TYPE)).decode("utf-8")
        dragged_chip = next(
            (c for c in self._chips if c.path.lower() == dragged_path.lower()), None
        )
        if dragged_chip is None:
            return  # 理论上不该发生（路径唯一、标签也没被删过），保险起见还是判断一下
        target_index = self._compute_drop_index(event.pos(), dragged_chip)
        self._reorder_chip(dragged_chip, target_index)
        event.acceptProposedAction()

    def _compute_drop_index(self, pos, dragged_chip):
        """根据鼠标松开时的横坐标，算出应该落到第几个位置——比较的是
        每个标签中点的 x 坐标，鼠标落在哪个标签的左半边就排到它前面，
        落在右半边就排到它后面，符合"跟哪个标签更近就排哪附近"的直觉。
        遍历到最后都没找到比鼠标位置更靠右的标签，说明该排到最后面。
        """
        for index, chip in enumerate(self._chips):
            if chip is dragged_chip:
                continue  # 不跟自己原来的位置比较，不然拖拽过程中会有一格"死区"卡住挪不动
            if pos.x() < chip.geometry().center().x():
                return index
        return len(self._chips)

    def _reorder_chip(self, chip, target_index):
        """把 chip 挪到 self._chips（以及对应的 _layout 里的显示顺序）
        的第 target_index 个位置。target_index 是"排除 chip 自己之后"
        数出来的目标位置（见 _compute_drop_index），所以如果 chip 原本
        排在目标位置前面，先把它从列表里摘掉会导致后面的位置整体往
        前挪一位，这里要相应地修正一下，不然会多排偏一位。
        """
        if chip not in self._chips:
            return
        current_index = self._chips.index(chip)
        if target_index in (current_index, current_index + 1):
            return  # 拖回了原来的位置（或者紧邻原位置右边，等价于没动），不用真的重排一遍
        self._chips.remove(chip)
        if target_index > current_index:
            target_index -= 1
        self._chips.insert(target_index, chip)
        # _layout 里的顺序是 [chip0, chip1, ..., chipN-1, add_btn, blank_area]
        # ——每次新增标签都是往"+"按钮前面插（见 add_folder()），标签
        # 之间的相对顺序在 _layout 里跟 self._chips 列表里的顺序始终
        # 一一对应，所以 self._chips 里的目标下标可以直接当成 _layout
        # 里的目标下标使用，不需要另外换算。
        self._layout.removeWidget(chip)
        self._layout.insertWidget(target_index, chip)
        self.scopeChanged.emit()

    # ------------------------------------------------------------------
    # 增删目录
    # ------------------------------------------------------------------
    def add_folder(self, path, checked=True):
        norm = os.path.normpath(path)
        if not os.path.isdir(norm):
            return
        for chip in self._chips:
            if chip.path.lower() == norm.lower():
                return  # 已经存在，不重复添加
        chip = _FolderChip(norm, checked=checked)
        chip.duplicate_check = lambda p, c=chip: self._is_duplicate_path(p, c)
        chip.toggled.connect(self.scopeChanged.emit)
        chip.removeRequested.connect(lambda c=chip: self._remove_chip(c))
        chip.pathChanged.connect(self.scopeChanged.emit)
        chip.selectRequested.connect(lambda c=chip: self._on_chip_select_requested(c))
        chip.editRequested.connect(lambda c=chip: self._on_chip_edit_requested(c))
        chip.openRequested.connect(lambda c=chip: self._on_chip_open_requested(c))
        self._chips.append(chip)
        # 新标签插在"+"按钮之前，空白点击区之后不受影响（它本来就在
        # 布局最后、带拉伸因子，插入新标签不会挤到它前面去）。
        insert_index = self._layout.indexOf(self.add_btn)
        self._layout.insertWidget(insert_index, chip)
        self._update_hint()
        self.scopeChanged.emit()

    def _remove_chip(self, chip):
        if chip in self._chips:
            self._chips.remove(chip)
            self._layout.removeWidget(chip)
            if self._selected_chip is chip:
                self._selected_chip = None
            if self._editing_chip is chip:
                self._editing_chip = None
            chip.deleteLater()
            self._update_hint()
            self.scopeChanged.emit()

    def clear_folders(self):
        for chip in list(self._chips):
            self._remove_chip(chip)

    # ------------------------------------------------------------------
    # 状态读写（供 config.py 存取）
    # ------------------------------------------------------------------
    def get_folders(self):
        """[{"path": str, "checked": bool}, ...]，按显示顺序。"""
        return [{"path": c.path, "checked": c.is_checked()} for c in self._chips]

    def set_folders(self, folder_list):
        """folder_list: [{"path":..., "checked":...}, ...]，用于启动时
        从配置恢复。会先清空当前已有的标签；不存在的目录直接跳过（比如
        上次记的是移动硬盘上的目录，这次没插上），不会显示成一个打不开
        的空标签。"""
        self.clear_folders()
        for item in folder_list or []:
            path = item.get("path", "")
            checked = item.get("checked", True)
            if path and os.path.isdir(path):
                self.add_folder(path, checked=checked)

    def get_checked_paths(self):
        """当前勾选的目录路径列表，按显示顺序——传给搜索/索引优先级用。"""
        return [c.path for c in self._chips if c.is_checked()]

    def has_any_folder(self):
        return bool(self._chips)