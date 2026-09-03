# main_window.py
import os
import sys
from PyQt5.QtWidgets import (
    QWidget, QMenu, QApplication, QStyledItemDelegate,
    QSystemTrayIcon, QAction, QMessageBox
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor

from layout import create_main_layout
from config import (
    load_config, save_config, get_window_geometry, save_window_geometry,
    get_minimize_to_tray_enabled,
)
from index_manager import IndexManager
from search_manager import SearchManager
from preview_manager import PreviewManager
from table_actions_manager import TableActionsManager
from helpers import force_window_to_foreground

class CenterItemDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        if index.data(Qt.DisplayRole) == "清空记录":
            from PyQt5.QtGui import QPen
            painter.save()
            pen = QPen(QColor("#cccccc"))
            pen.setWidth(1)
            painter.setPen(pen)
            painter.drawLine(option.rect.left() + 8, option.rect.top(),
                           option.rect.right() - 8, option.rect.top())
            painter.restore()
            painter.save()
            painter.setPen(QColor("#cc3333"))
            painter.drawText(option.rect, Qt.AlignCenter, "清空记录")
            painter.restore()
        else:
            super().paint(painter, option, index)


class MainWindow(QWidget):
    # 同一个进程里，"新建窗口"打开的所有窗口（含主窗口自己）都注册在
    # 这里——托盘"退出程序"、扫描列宽保存计时器兜底这些"要对全部窗口
    # 做一遍"的操作要用到这份名单；不用它就只能操作某一个具体窗口，
    # 关掉别的窗口时容易漏掉收尾。
    _all_windows = []

    def __init__(self, is_primary=True, shared_index_manager=None):
        super().__init__()
        self.is_primary = is_primary
        MainWindow._all_windows.append(self)

        # 搜索（关键词/筛选条件解析、发起搜索、结果填表、搜索框下拉历史
        # 交互）这一整套状态和方法都在 SearchManager 里，不再直接挂在
        # self 上。每个窗口各自独立一份——不同窗口应该能各搜各的，互不
        # 干扰，这也是"多开窗口"最核心的价值。
        self.search_manager = SearchManager(self)
        # 索引建立/实时监控/排除目录/数据库路径切换/清空重建索引，这一
        # 整套状态和方法都在 IndexManager 里。索引本身是"整个软件只有
        # 一份"的东西（数据库、实时监控线程只能有一个在跑，重复了只是
        # 白白浪费资源、互相抢锁），所以"新建窗口"打开的窗口不会各自
        # 建一个新的 IndexManager，而是复用主窗口那一个（通过
        # shared_index_manager 传进来），只是把自己注册进它的
        # linked_windows 列表，这样索引统计、状态栏这些信息才能同步
        # 广播给这个新窗口，不是只有最早创建它的主窗口能看到更新。
        if is_primary:
            self.index_manager = IndexManager(self)
        else:
            assert shared_index_manager is not None, "非主窗口必须传入共用的 IndexManager"
            self.index_manager = shared_index_manager
            self.index_manager.add_linked_window(self)
        # 表格右键菜单/文件操作（打开、定位、复制路径、复制文件、批量
        # 替换所选）这些方法都在 TableActionsManager 里，不再直接挂在
        # self 上。这个不依赖布局搭好之后的控件才能构造（跟
        # index_manager/search_manager 一样，只是持有 window 引用），
        # 放哪都行，为了跟其它几个管理器放在一起就近声明。
        self.table_actions = TableActionsManager(self)
        self._last_filename_keywords = []  # 本次搜索用的文件名关键字，供结果表格高亮用

        # 1. 组装 UI
        create_main_layout(self)

        # 2. 内容预览：显示选中文件内容、关键词高亮、滚动条命中标记、
        # 上一个/下一个命中导航，这一整套状态和方法都在 PreviewManager
        # 里，不再直接挂在 self 上。PreviewManager 自己会在构造时把
        # 事件过滤器装到预览区的原生滚动条上。
        self.preview_manager = PreviewManager(self)
        # 这三个信号得等 preview_manager 创建出来之后才能连（layout.py
        # 搭界面的那一刻它还不存在），所以放到这里而不是 layout.py 里连。
        # QTableView 没有 itemSelectionChanged 这个信号（那是 QTableWidget
        # 专属的），对应的是 selectionModel() 的 selectionChanged 信号——
        # 同样覆盖鼠标点击和键盘上下键切换选中这两种场景，效果一致。
        # selectionChanged 传两个参数（selected, deselected），而
        # display_selected_file_content() 不需要用到这两个参数（它自己会
        # 重新去 table.selectionModel().selectedRows() 查当前选中状态），
        # 用 lambda 吃掉这两个参数，避免连接失败。
        self.table.selectionModel().selectionChanged.connect(
            lambda selected, deselected: self.preview_manager.display_selected_file_content()
        )
        self.prev_match_btn.clicked.connect(self.preview_manager.go_to_prev_match)
        self.next_match_btn.clicked.connect(self.preview_manager.go_to_next_match)

        # 3. 加载历史记录
        load_config(self)

        # "搜索目录"标签栏增删/勾选状态一变就立刻存盘，不用等到下次点
        # 搜索才存——万一加完目录就直接关掉软件，也不会丢。
        #
        # 多开出来的窗口各自都有一份独立的标签栏控件（Qt 控件没法真的
        # 被两个窗口共用同一个实例），但"哪些目录参与索引"这件事只能有
        # 一份真相——所以这里除了存盘，还要把改动广播给同一进程里其它
        # 全部窗口的标签栏，不然就会出现"在这个窗口加了个目录，那个
        # 窗口却看不到，两边各显示各的、跟真正在建索引的范围对不上"
        # 这种混乱。广播的时候要 blockSignals，不然对方窗口 set_folders()
        # 也会触发一次它自己的 scopeChanged，几个窗口来回广播就死循环了。
        def _on_scope_changed():
            save_config(self)
            folders = self.folder_scope_bar.get_folders()
            for w in MainWindow._all_windows:
                if w is self:
                    continue
                w.folder_scope_bar.blockSignals(True)
                w.folder_scope_bar.set_folders(folders)
                w.folder_scope_bar.blockSignals(False)
        self.folder_scope_bar.scopeChanged.connect(_on_scope_changed)

        # 恢复"文件名/内容"两个搜索框的正则表达式开关上次记住的状态；
        # 恢复的时候先断开信号，不然 setChecked() 触发的 toggled 会
        # 立刻反手又存一次盘，虽然存的是同一份值、无害，但没必要。
        from config import get_search_regex_options, save_search_regex_options
        saved_regex_options = get_search_regex_options()
        self.filename_regex_action.blockSignals(True)
        self.filename_regex_action.setChecked(saved_regex_options["filename_regex"])
        self.filename_regex_action.blockSignals(False)
        self.content_regex_action.blockSignals(True)
        self.content_regex_action.setChecked(saved_regex_options["content_regex"])
        self.content_regex_action.blockSignals(False)

        def _save_regex_options(*_):
            save_search_regex_options({
                "filename_regex": self.filename_regex_action.isChecked(),
                "content_regex": self.content_regex_action.isChecked(),
            })
        self.filename_regex_action.toggled.connect(_save_regex_options)
        self.content_regex_action.toggled.connect(_save_regex_options)

        # 4. 刷新索引统计（每个窗口打开时都刷一次，不用等下一次索引事件
        # 才能看到当前状态；IndexManager.refresh_stats() 内部会广播给
        # linked_windows 里全部窗口，多个窗口同时开着也不会互相踩）
        self.index_manager.refresh_stats()

        # 5. 系统托盘：整个进程只需要一个，只有主窗口创建。"新建窗口"
        # 打开的窗口共用主窗口那一个托盘图标，不会每开一个窗口就在
        # 系统托盘里多冒出一个图标。
        if self.is_primary:
            self._setup_tray()

        # 6. 恢复上次窗口大小和位置，首次启动取屏幕70%居中。
        # "新建窗口"打开的窗口不走这条路径——保存的那份窗口几何信息
        # 只有一份，如果每个窗口都套用同一份，新窗口会跟主窗口完全
        # 重叠在同一个位置，得手动拖开才能看到两个窗口，体验很差。
        # 改成跟主窗口错开一点摆放，见 _cascade_from_primary()。
        if self.is_primary:
            self._restore_window_geometry()
        else:
            self._cascade_from_primary()

        # 7. 启动时自动开始建索引（延迟3秒等界面稳定）。只有主窗口需要
        # 触发——"新建窗口"打开的窗口共用主窗口那一个 IndexManager，
        # 索引早就已经在跑或者跑完了，重复调用一次纯粹是浪费。
        if self.is_primary:
            QTimer.singleShot(3000, self.index_manager.start_index)

        # ─── 回车键触发搜索 ───
        for combo in ['filename_keyword_edit', 'keyword_edit']:
            w = getattr(self, combo, None)
            if w and w.lineEdit():
                w.lineEdit().returnPressed.connect(self.search_manager.start_search)

        # ─── 下拉清空记录 ───
        delegate = CenterItemDelegate(self)
        for combo in ['filename_keyword_edit', 'keyword_edit']:
            w = getattr(self, combo, None)
            if w:
                w.setItemDelegate(delegate)
                w.activated[str].connect(lambda text, c=w: self.search_manager.handle_combo_clear(c, text))

        # ─── 从下拉历史里选一个关键词，直接自动搜索，不用再手动点一次搜索图标 ───
        for combo in ['filename_keyword_edit', 'keyword_edit']:
            w = getattr(self, combo, None)
            if w:
                w.activated[str].connect(self.search_manager.auto_search_on_dropdown_pick)

        # ─── 文件名/内容关键词都被删空时，左侧结果表格和右侧预览要跟着
        # 立刻清掉，不然搜索框已经空了、界面上却还挂着上一次的搜索结果，
        # 看着像是"空关键词搜出来的"，状态不一致。用 editTextChanged
        # （而不是底层 QLineEdit 的 textChanged）是因为 load_config 在
        # 重建下拉历史时会临时把这两个框清空又填回去，期间用
        # combo.blockSignals() 挡住的正是 editTextChanged 这个信号，
        # 挂在这上面才不会被那次程序内部的临时清空误触发。 ───
        for combo in ['filename_keyword_edit', 'keyword_edit']:
            w = getattr(self, combo, None)
            if w:
                w.editTextChanged.connect(self.search_manager.on_keyword_text_changed)

        # 隐藏遗留控件
        for attr in ['custom_checkbox', 'dxf_output_edit', 'output_btn', 'output_label']:
            w = getattr(self, attr, None)
            if w:
                w.setVisible(False)

    # =========================================================
    # 系统托盘
    # =========================================================
    def _restore_window_geometry(self):
        """恢复上次窗口大小和位置，首次启动取屏幕70%大小居中"""
        geometry = get_window_geometry()
        if geometry:
            w, h, x, y = geometry
            self.resize(w, h)
            # 确保窗口在屏幕范围内
            screen = QApplication.primaryScreen().geometry()
            if 0 <= x <= screen.width() - 100 and 0 <= y <= screen.height() - 100:
                self.move(x, y)
            else:
                self._center_window()
        else:
            # 首次启动：屏幕70%大小居中
            screen = QApplication.primaryScreen().geometry()
            w = int(screen.width()  * 0.70)
            h = int(screen.height() * 0.75)
            self.resize(w, h)
            self._center_window()

    def _center_window(self):
        """将窗口居中显示"""
        screen = QApplication.primaryScreen().geometry()
        x = (screen.width()  - self.width())  // 2
        y = (screen.height() - self.height()) // 2
        self.move(x, y)

    def _cascade_from_primary(self):
        """"新建窗口"打开的窗口：大小跟主窗口一致，位置在主窗口基础上
        往右下方错开一段距离（"层叠"摆放，跟 Windows 里同时开好几个
        资源管理器窗口时系统自动摆放的效果类似），不会跟主窗口叠在
        完全相同的位置、看起来像没反应。多开几个窗口错位量会累加，
        避免第三、第四个窗口又叠回第一个窗口的位置上。"""
        primary = MainWindow._all_windows[0] if MainWindow._all_windows else None
        if primary is None or primary is self:
            self._center_window()
            return
        self.resize(primary.width(), primary.height())
        offset = 32 * ((len(MainWindow._all_windows) - 1) % 8)  # 叠够8层就从头开始叠，避免无限跑出屏幕
        screen = QApplication.primaryScreen().geometry()
        x = min(primary.x() + offset, screen.width() - self.width())
        y = min(primary.y() + offset, screen.height() - self.height())
        self.move(max(0, x), max(0, y))

    def _setup_tray(self):
        """初始化系统托盘图标"""
        self.tray_icon = QSystemTrayIcon(self)
        # 使用系统内置图标，无需外部图标文件
        self.tray_icon.setIcon(QApplication.style().standardIcon(
            QApplication.style().SP_FileDialogContentsView
        ))
        self.tray_icon.setToolTip("DWG 图纸搜索系统")

        tray_menu = QMenu()
        show_action = QAction("打开主窗口", self)
        show_action.triggered.connect(self._show_window)
        quit_action = QAction("退出程序", self)
        quit_action.triggered.connect(self._quit_app)

        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _on_tray_activated(self, reason):
        """双击托盘图标显示主窗口"""
        if reason == QSystemTrayIcon.DoubleClick:
            self._show_window()

    def _show_window(self):
        self.showNormal()
        force_window_to_foreground(self)

    def open_new_window(self):
        """菜单栏"文件 -> 新建窗口"：跟 Everything、Anytxt Searcher 这类
        工具的"新建窗口"效果一样——同一个进程里再开一个独立窗口，可以
        各自搜各的，但系统托盘图标始终只有一个，不会每开一个窗口就在
        右下角多冒出一个图标。

        做法是在当前进程里直接再构造一个 MainWindow（is_primary=False，
        共用主窗口那一个 IndexManager，见类里的详细说明），而不是像
        之前那版一样开一个全新的操作系统进程——单独开进程的话，新进程
        会自己再建一套索引管理器、再起一个托盘图标，跟"只有一个托盘
        图标"这个目标是矛盾的。

        双击程序图标/exe 重新打开的场景（不是从菜单点的，而是资源管理
        器里再点一次图标、或者命令行再跑一次）走的是另一条路：
        main.py 里的单实例检测会发现已经有一个实例在跑，把"新建窗口"
        的请求通过本地 IPC 转发给正在跑的这个进程，效果殊途同归——
        新窗口还是在已经在跑的这个进程里开出来的，不会真的启动第二个
        进程、也不会多一个托盘图标。
        """
        win = MainWindow(is_primary=False, shared_index_manager=self.index_manager)
        force_window_to_foreground(win)

    def open_search_for_folder(self, folder_path):
        """右键菜单"用 DWG 图纸搜索工具搜索此目录"触发（见
        context_menu_integration.py + main.py 里的 IPC 转发）：开一个
        新窗口，把这个目录设成这个窗口唯一勾选的搜索范围，光标定位到
        文件名搜索框，等用户输入关键词——不直接自动帮用户填关键词或者
        发起搜索，用户从 Explorer 右键点进来，目的是"来这个目录搜"，
        具体搜什么这一步没法替他们决定。

        不影响用户已经开着的其它窗口（包括主窗口自己）的搜索范围——
        用一个新窗口装这次"临时的、只针对这一个目录"的搜索，做完了
        关掉这个窗口就行，不会把主窗口原来勾选的那些目录搅乱。

        如果这个目录之前从来没加进过"搜索目录"列表，会顺手加进去并
        且勾选上；已经在列表里的话就只是把其它目录都取消勾选、单独
        勾上这一个，不会重复添加出两条一样的记录。这个目录改动会跟
        其它窗口的"搜索目录"标签栏同步（见 __init__ 里 folder_scope_bar
        .scopeChanged 那段说明），如果这个目录还没被索引过，也会被
        IndexManager 优先扫描（_get_current_search_paths() 读的就是
        当前勾选的目录）。
        """
        win = MainWindow(is_primary=False, shared_index_manager=self.index_manager)
        # 这个新窗口很可能是从右键菜单/双击图标这类"外部触发"场景开出来
        # 的（走 IPC 转发过来的请求），单靠 show()/raise_()/activateWindow()
        # 经常拉不到最前面（见 force_window_to_foreground() 里的详细
        # 说明），改用它来真正抢到前台。
        force_window_to_foreground(win)

        folders = win.folder_scope_bar.get_folders()
        normalized_target = os.path.normcase(os.path.normpath(folder_path))
        found = False
        for item in folders:
            is_target = os.path.normcase(os.path.normpath(item.get("path", ""))) == normalized_target
            item["checked"] = is_target
            found = found or is_target
        if not found:
            for item in folders:
                item["checked"] = False
            folders.append({"path": folder_path, "checked": True})
        # set_folders() 内部会重新构建标签栏、发出 scopeChanged，进而
        # 触发存盘、同步给其它窗口、（间接）让索引优先扫描这个目录，
        # 跟用户自己手动在标签栏上操作的效果完全一样，不需要额外处理。
        win.folder_scope_bar.set_folders(folders)
        win.filename_keyword_edit.setFocus()
        return win

    def _quit_app(self):
        """退出前确保索引线程的 AutoCAD 实例被正确关闭，避免进程残留。
        托盘"退出程序"只挂在主窗口上，只有主窗口能触发这个方法——但
        退出要收拾的是整个进程，所以这里要把 MainWindow._all_windows
        里全部窗口（不只是 self 这一个）都清理一遍，不能漏掉"新建窗口"
        开出来的那些。
        """
        # 退出前保存窗口大小和位置——只存主窗口的，"新建窗口"打开的
        # 窗口本来就没有单独的位置记忆机制（见 _cascade_from_primary），
        # 不需要存。
        save_window_geometry(
            self.width(), self.height(),
            self.x(), self.y()
        )

        for w in list(MainWindow._all_windows):
            # 表格列宽拖动后有 300ms 防抖才真正落盘（见 layout.py 里的
            # 说明），这里走的是 os._exit(0) 硬退出，不会给还没跑到的
            # QTimer 一个补跑的机会，所以退出前主动 flush 一次没保存完
            # 的列宽改动，不依赖计时器自己触发。每个窗口各有一份自己
            # 的计时器，要逐个处理。
            if getattr(w, "_table_column_width_save_timer", None) is not None \
                    and w._table_column_width_save_timer.isActive():
                w._table_column_width_save_timer.stop()
                w._save_table_column_widths()

            # 顶部搜索框分隔线位置同理，也要在硬退出前主动 flush 一次。
            if getattr(w, "_search_splitter_save_timer", None) is not None \
                    and w._search_splitter_save_timer.isActive():
                w._search_splitter_save_timer.stop()
                w._save_search_splitter_sizes()

            # search_manager / preview_manager 也是每个窗口各自一份，
            # 逐个收尾（主要是停掉可能还在跑的搜索/预览后台线程）。
            try:
                w.search_manager.shutdown()
            except Exception:
                pass
            try:
                w.preview_manager.shutdown()
            except Exception:
                pass

        # 索引/实时监控/清空索引这几个后台线程的收尾都在 IndexManager
        # 里，逻辑（等多久、要不要强制终止）跟索引管理本身强相关，别
        # 拆开放在两个文件里维护。IndexManager 全进程只有一份（多个
        # 窗口共用），只需要关一次。
        self.index_manager.shutdown()

        # 必须在 os._exit(0) 之前手动隐藏托盘图标。os._exit(0) 是直接在
        # 操作系统层面砍掉进程，会跳过 tray_icon 这个 QSystemTrayIcon
        # 对象正常的析构/清理流程——而 Windows 托盘图标本来就是靠这个
        # 清理动作（Shell_NotifyIcon NIM_DELETE）主动告诉系统"删掉"的。
        # 少了这一步，系统托盘里会留下一个僵尸图标：进程已经不在了，
        # 但图标占位还在，只有等鼠标划过去触发一次消息交互，系统才会
        # 顺便发现并清掉它——这正是"退出后角标要划一下才消失"的原因。
        # 这里主动 hide() 让它立刻发出删除通知，再用 processEvents()
        # 把这个原生调用从 Qt 的事件队列里强制推出去，确保它在进程真
        # 正终止前已经被 Windows 处理掉。
        try:
            self.tray_icon.hide()
            QApplication.processEvents()
        except Exception:
            pass

        QApplication.quit()

        # 兜底保险：如果全量重建索引期间残留的 ThreadPoolExecutor worker
        # 线程还卡在某个 subprocess.run() 调用里没返回（比如遇到一张
        # 解析异常慢、或者子进程本身没干净退出的图纸），这些线程不是
        # daemon 线程——Python 解释器正常退出时会经由 concurrent.futures
        # 内部注册的 atexit 钩子，等这些线程全部跑完才让进程真正终止，
        # 表现出来就是"点了退出、窗口消失了，但进程还挂着/托盘图标卡住
        # 未响应"，而且这个等待没有上限，不受上面 8 秒+1 秒这两次
        # wait() 约束。QApplication.quit() 只是让 Qt 事件循环退出，管不到
        # 这些残留线程。这里用 os._exit(0) 直接在操作系统层面终止进程，
        # 跳过 Python 正常的解释器关闭流程（包括那个会卡住的 atexit 联
        # 合等待），確保点"退出"之后进程一定会消失，不会无限期挂着。
        # 数据库这边不受影响：SQLite 的 WAL 文件本来就是为"进程随时可能
        # 异常终止"设计的，下次打开时会自动做 WAL 回放，不会损坏索引。
        os._exit(0)

    def closeEvent(self, event):
        """主窗口：点关闭默认是最小化到托盘（后台索引继续跑），不是真
        退出——这是原来就有的行为；现在这个行为可以在菜单栏"设置 ->
        最小化为系统托盘图标"里关掉，关掉之后点关闭按钮就是真的退出
        程序，跟托盘菜单"退出程序"效果一样。

        "新建窗口"打开的窗口：点关闭就是真的关掉这个窗口——它没有自己
        的托盘图标，"最小化到托盘"这个概念对它没有意义；关掉它不会
        影响主窗口和其它窗口，索引/实时监控该怎么跑还怎么跑（那是
        主窗口那份共用的 IndexManager 的事，不受某个窗口关闭影响）。
        自己的 search_manager / preview_manager 也要收尾一下，避免
        还有搜索线程在后台空转。
        """
        if self.is_primary:
            save_window_geometry(
                self.width(), self.height(),
                self.x(), self.y()
            )
            if not get_minimize_to_tray_enabled():
                event.accept()
                self._quit_app()
                return
            event.ignore()
            self.hide()
            self.tray_icon.showMessage(
                "DWG 图纸搜索系统",
                "程序已最小化到托盘，后台索引继续运行。\n双击托盘图标可重新打开窗口。",
                QSystemTrayIcon.Information,
                3000
            )
            return

        try:
            self.search_manager.shutdown()
        except Exception:
            pass
        try:
            self.preview_manager.shutdown()
        except Exception:
            pass
        self.index_manager.remove_linked_window(self)
        if self in MainWindow._all_windows:
            MainWindow._all_windows.remove(self)
        event.accept()

    # =========================================================
    # 索引管理相关的状态和方法搬去了 index_manager.py 的 IndexManager，
    # 搜索相关的（关键词解析、发起搜索、搜索框下拉历史交互）搬去了
    # search_manager.py 的 SearchManager，内容预览+高亮搬去了
    # preview_manager.py 的 PreviewManager，分别通过 self.index_manager /
    # self.search_manager / self.preview_manager 访问。
    # =========================================================
    # =========================================================
    # 表格右键菜单/文件操作
    # =========================================================
    # =========================================================
    # 表格右键菜单/文件操作相关的状态和方法搬去了
    # table_actions_manager.py 的 TableActionsManager，通过
    # self.table_actions 访问。
    # =========================================================