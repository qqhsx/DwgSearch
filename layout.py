# layout.py
import os
from PyQt5.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QCheckBox, QPlainTextEdit, QWidget, QSizePolicy,
    QHeaderView, QSplitter, QDialog, QSpinBox, QMenuBar, QAction,
    QActionGroup, QMessageBox, QFrame, QInputDialog, QMenu, QAbstractItemView
)
from PyQt5.QtCore import Qt, QObject, QEvent, QTimer, QByteArray
from PyQt5.QtGui import QKeySequence, QPixmap
from config import (
    get_extract_workers, save_extract_workers, get_search_filter_options,
    get_search_filter_row_expanded, save_search_filter_row_expanded,
    get_db_path, get_log_file_path, APP_NAME, APP_VERSION,
    get_main_table_column_widths, save_main_table_column_widths,
    get_main_table_hidden_columns, save_main_table_hidden_columns,
    get_main_search_splitter_sizes, save_main_search_splitter_sizes,
    get_minimize_to_tray_enabled, save_minimize_to_tray_enabled,
)
from donate_assets import WECHAT_QR_BASE64, ALIPAY_QR_BASE64
from context_menu_integration import (
    is_context_menu_enabled, enable_context_menu, disable_context_menu,
)
from autostart_manager import (
    is_autostart_enabled, enable_autostart, disable_autostart,
)
from bookmark_manager import add_bookmark, find_bookmark_by_content, find_bookmark_by_name
from bookmark_manager_dialog import BookmarkManagerDialog
from helpers import (
    build_native_search_combo,
    open_containing_folder, open_file_with_default_app,
    make_bookmark_status_icon, refresh_bookmark_icon_everywhere,
)
from results_table_model import create_results_table
from folder_scope_bar import FolderScopeBar
from database import TEXT_TYPE_LABELS, SPACE_LABELS, SCOPE_LABELS, ALL_TEXT_TYPES, ALL_SPACES, ALL_SCOPES


def _prompt_add_bookmark(window):
    """"添加到书签"共用逻辑：菜单栏"书签 -> 添加到书签"和文件名搜索框
    里的书签按钮，点的是同一个入口，效果完全一样，只是触发方式不同——
    跟"搜索"这颗放大镜按钮和回车触发的是同一个 start_search 是同一个
    思路，一件事配多个顺手的入口，不重复写两遍逻辑。

    书签收藏的是"文件名+内容"两个搜索框各自的关键词和正则开关状态，
    不含搜索目录、筛选条件（扩展名/日期/大小）这些——书签主要是为了
    省得重新敲一遍写起来麻烦的关键词/正则，跟"这次要搜哪个目录、筛
    选什么范围"这种一次性的场景设置不是一回事，混在一起收藏反而容易
    导致应用书签的时候把用户当前正打算用的搜索目录/筛选条件意外
    覆盖掉。
    """
    filename_kw = window.filename_keyword_edit.currentText().strip()
    filename_regex = window.filename_regex_action.isChecked()
    content_kw = window.keyword_edit.currentText().strip()
    content_regex = window.content_regex_action.isChecked()

    if not filename_kw and not content_kw:
        QMessageBox.information(
            window, "添加到书签",
            "文件名和内容搜索框都是空的，没有可收藏的搜索条件。"
        )
        return

    # 当前这套"文件名+内容"搜索条件已经收藏过了（哪怕是用别的名字、
    # 或者在别的窗口收藏的——书签数据是全局共享的一份），没必要收藏
    # 出两条内容一模一样、只是名字不同的书签，那样书签管理列表里会
    # 越攒越多重复的东西，反而不好找。直接告诉用户已经收藏过、叫
    # 什么名字，问一下要不要顺手打开书签管理去看看，不重复新增。
    existing = find_bookmark_by_content(filename_kw, filename_regex, content_kw, content_regex)
    if existing is not None:
        reply = QMessageBox.question(
            window, "添加到书签",
            f"当前这套搜索条件已经收藏为书签「{existing.get('name', '')}」了，不用重复收藏。\n\n"
            "要打开「书签管理」看看吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            window._show_bookmark_dialog()
        return

    # 默认名字直接拿当前关键词拼一个，尽量让用户不用另外想名字就能
    # 保存；太长截断加省略号，避免书签管理列表里"名称"这一列被撑得
    # 很宽。用户觉得默认名字不够清楚，弹出的输入框里随时可以自己改。
    parts = [p for p in (filename_kw, content_kw) if p]
    suggested = " + ".join(parts)
    if len(suggested) > 30:
        suggested = suggested[:30] + "…"

    name_text = suggested
    while True:
        name, ok = QInputDialog.getText(window, "添加到书签", "书签名称：", text=name_text)
        if not ok:
            return
        name = name.strip() or suggested or "未命名书签"
        # 名字重复了：书签管理列表主要就是靠名字辨认是哪一条，两条
        # 叫同一个名字会让人分不清点哪个才是自己要的那条，这里挡下来
        # 让用户换一个名字，而不是悄悄允许重名、留一堆看着一样的条目。
        duplicate = find_bookmark_by_name(name)
        if duplicate is not None:
            QMessageBox.warning(
                window, "添加到书签",
                f"已经有一条书签叫「{name}」了，换一个名字吧。"
            )
            name_text = name
            continue
        break

    add_bookmark(
        name=name,
        filename_keyword=filename_kw,
        filename_regex=filename_regex,
        content_keyword=content_kw,
        content_regex=content_regex,
    )
    window.status_label.setText(f"书签「{name}」已添加")
    refresh_bookmark_icon_everywhere(window)


def _refresh_bookmark_icon_state(window):
    """兼容/便捷入口：只刷新这一个窗口自己的书签图标状态（不广播给
    其它窗口）。书签数据没有变化、只是这个窗口自己的搜索框内容变了
    的场景用这个就够了——真正接线是靠 filename_keyword_edit/
    keyword_edit 的文字变化信号自动触发的（见 create_main_layout()
    里的 _do_refresh_bookmark_icon），这里只是留一个可以主动调用的
    入口，给以后万一有别的地方需要手动触发一次刷新时用。
    """
    impl = getattr(window, "_refresh_bookmark_icon_impl", None)
    if impl is not None:
        impl()


def _build_index_management_dialog(window):
    """索引管理是偶尔才用一次的维护性功能，跟"搜索"这种高频操作不该占
    同样显眼的主界面位置，改成从菜单里弹出的独立对话框。"""
    dialog = QDialog(window)
    dialog.setWindowTitle("索引管理")
    dialog.setMinimumWidth(480)
    dlg_layout = QVBoxLayout(dialog)
    dlg_layout.setContentsMargins(14, 14, 14, 14)
    dlg_layout.setSpacing(12)

    info_layout = QHBoxLayout()
    index_count_label = QLabel("已索引图纸：— 张")
    index_size_label = QLabel("数据库大小：— MB")
    index_time_label = QLabel("最后更新：—")
    for lbl in (index_count_label, index_size_label, index_time_label):
        lbl.setStyleSheet("font-size: 12px;")
    info_layout.addWidget(index_count_label)
    info_layout.addSpacing(20)
    info_layout.addWidget(index_size_label)
    info_layout.addSpacing(20)
    info_layout.addWidget(index_time_label)
    info_layout.addStretch()
    dlg_layout.addLayout(info_layout)

    btn_layout = QHBoxLayout()
    refresh_index_btn = QPushButton("刷新信息")
    refresh_index_btn.clicked.connect(window.index_manager.refresh_stats)
    db_path_btn = QPushButton("存储路径")
    db_path_btn.clicked.connect(window.index_manager.change_db_path)
    exclude_folders_btn = QPushButton("排除目录管理")
    exclude_folders_btn.clicked.connect(window.index_manager.manage_exclude_folders)
    # 查看索引过程中提取内容失败的图纸清单（损坏/格式不支持/解析超时等）。
    # 按钮文字本身带上当前失败数量，refresh_stats 里会同步更新。
    failed_files_btn = QPushButton("查看失败文件")
    failed_files_btn.clicked.connect(window.index_manager.show_failed_files)
    btn_layout.addWidget(refresh_index_btn)
    btn_layout.addWidget(db_path_btn)
    btn_layout.addWidget(exclude_folders_btn)
    btn_layout.addWidget(failed_files_btn)
    btn_layout.addStretch()
    dlg_layout.addLayout(btn_layout)

    # 内容提取并发数：图纸数量大的电脑上，提取内容这一步默认会用多线程
    # 并发调用外部提取程序，加快索引速度；但具体开多少个合适，取决于
    # 每台电脑自己的CPU核数和硬盘类型（机械盘/网络盘并发太高反而更慢），
    # 不该写死一个数字，让用户按自己电脑的实际情况调。
    worker_layout = QHBoxLayout()
    worker_label = QLabel("内容提取并发数：")
    worker_spin = QSpinBox()
    worker_spin.setRange(1, 32)
    worker_spin.setValue(get_extract_workers())
    worker_spin.setToolTip(
        "同时开几个进程提取图纸内容。调大能加快索引速度，但会更占用\n"
        "CPU；如果索引时感觉电脑明显变卡（尤其做别的事也卡），调小一点。\n"
        "调整会在下一次索引/重建索引时生效，不影响正在进行中的这一次。"
    )
    worker_spin.valueChanged.connect(save_extract_workers)
    worker_hint = QLabel("(调大更快但更占CPU，卡的话调小)")
    worker_hint.setStyleSheet("color: gray; font-size: 11px;")
    worker_layout.addWidget(worker_label)
    worker_layout.addWidget(worker_spin)
    worker_layout.addWidget(worker_hint)
    worker_layout.addStretch()
    dlg_layout.addLayout(worker_layout)

    # "清空并重建索引"是高风险操作（会清空重建整个索引，耗时可能很长），
    # 单独隔一行放最下面、靠右对齐，跟上面几个"无害"按钮拉开距离，
    # 降低手滑误点的概率。
    danger_layout = QHBoxLayout()
    clear_index_btn = QPushButton("清空并重建索引")
    clear_index_btn.setStyleSheet("color: red;")
    clear_index_btn.clicked.connect(window.index_manager.clear_index)
    danger_layout.addStretch()
    danger_layout.addWidget(clear_index_btn)
    dlg_layout.addLayout(danger_layout)

    window.index_count_label = index_count_label
    window.index_size_label = index_size_label
    window.index_time_label = index_time_label
    window.index_failed_btn = failed_files_btn
    return dialog


def _show_donate_dialog(window):
    """"捐赠作者"弹窗：微信/支付宝二维码左右并排展示，不用切换就能看到两个。

    图片数据以 base64 字符串内嵌在 donate_assets.py 里（而不是放成
    assets/donate/ 下的独立 png 文件），原因见 donate_assets.py 顶部
    的说明：独立文件在 onedir 打包模式下谁都能直接右键替换，收款码
    类图片这样放风险是真实的；内嵌到代码里之后就不再是"文件夹里一眼
    能看到、随手能换"的东西了。
    """
    wechat_bytes = QByteArray.fromBase64(WECHAT_QR_BASE64.encode("ascii"))
    alipay_bytes = QByteArray.fromBase64(ALIPAY_QR_BASE64.encode("ascii"))

    dialog = QDialog(window)
    dialog.setWindowTitle("捐赠作者")
    dialog.resize(460, 320)

    outer_layout = QVBoxLayout(dialog)

    thanks_label = QLabel(
        "如果这个工具帮你省了时间，欢迎请作者喝杯咖啡——完全自愿，"
        "不给也完全不影响使用。"
    )
    thanks_label.setWordWrap(True)
    outer_layout.addWidget(thanks_label)

    def _build_qr_label(qr_bytes):
        img_label = QLabel()
        img_label.setAlignment(Qt.AlignCenter)
        pixmap = QPixmap()
        pixmap.loadFromData(qr_bytes, "PNG")
        if not pixmap.isNull():
            # 二维码原图是 300x300，这里统一缩放展示尺寸；并排展示要
            # 给两张图各留够宽度，比之前单图/Tab 展示时缩小一些。
            pixmap = pixmap.scaled(
                200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            img_label.setPixmap(pixmap)
        else:
            img_label.setText("图片加载失败")
        return img_label

    qr_row_layout = QHBoxLayout()
    qr_row_layout.addStretch()
    qr_row_layout.addWidget(_build_qr_label(wechat_bytes))
    qr_row_layout.addSpacing(20)
    qr_row_layout.addWidget(_build_qr_label(alipay_bytes))
    qr_row_layout.addStretch()
    outer_layout.addLayout(qr_row_layout)

    close_btn = QPushButton("关闭")
    close_btn.clicked.connect(dialog.accept)
    btn_layout = QHBoxLayout()
    btn_layout.addStretch()
    btn_layout.addWidget(close_btn)
    outer_layout.addLayout(btn_layout)

    dialog.exec_()


def _build_menu_bar(window):
    """顶部菜单栏：文字替换（直接触发，没有下拉，跟主界面原来那颗
    "文字替换工具"按钮是同一个入口）+ 文件/设置/帮助三个下拉菜单。

    主窗口是普通 QWidget，不是 QMainWindow，没有 setMenuBar() 这种
    专门的停靠位；QMenuBar 本身就是个普通控件，直接塞进最上面的
    垂直布局第一行就行，效果一样。
    """
    menu_bar = QMenuBar(window)

    # ---- 文字替换：菜单栏上的顶层项，点了直接触发，不是下拉菜单 ----
    text_replace_action = QAction("文字替换", window)
    text_replace_action.triggered.connect(window.table_actions.open_replace_tool)
    menu_bar.addAction(text_replace_action)

    # ---- 文件 ----
    file_menu = menu_bar.addMenu("文件")

    new_search_action = QAction("新建搜索", window)
    new_search_action.setShortcut(QKeySequence("Ctrl+N"))
    new_search_action.triggered.connect(window.search_manager.start_new_search)
    file_menu.addAction(new_search_action)

    # "新建窗口"：另外开一个完全独立的程序窗口（新进程），跟浏览器
    # "新建窗口"是一个意思——不是清空当前搜索，是同时开两个能各自
    # 独立操作的窗口。跟上面"新建搜索"是两件事，分开放，各司其职。
    new_window_action = QAction("新建窗口", window)
    new_window_action.setShortcut(QKeySequence("Ctrl+Shift+N"))
    new_window_action.triggered.connect(window.open_new_window)
    file_menu.addAction(new_window_action)

    export_list_action = QAction("导出列表", window)
    export_list_action.setShortcut(QKeySequence("Ctrl+E"))
    export_list_action.triggered.connect(window.table_actions.export_results_to_csv)
    file_menu.addAction(export_list_action)

    file_menu.addSeparator()

    exit_action = QAction("退出程序", window)
    exit_action.setShortcut(QKeySequence("Ctrl+Q"))
    exit_action.triggered.connect(window.close)
    file_menu.addAction(exit_action)

    # ---- 设置 ----
    settings_menu = menu_bar.addMenu("设置")

    index_manage_action = QAction("索引管理...", window)
    index_manage_action.triggered.connect(window._show_index_dialog)
    settings_menu.addAction(index_manage_action)

    # 语言：目前只做了中文界面，这里先把入口占位放出来，选项本身
    # 如实标成"开发中"，不要让用户以为点了英文真的能切换界面——没有
    # 实现的功能装作能用，比干脆不放这个入口体验更差。
    language_menu = settings_menu.addMenu("语言")
    language_group = QActionGroup(window)
    language_group.setExclusive(True)

    zh_action = QAction("简体中文", window)
    zh_action.setCheckable(True)
    zh_action.setChecked(True)
    language_group.addAction(zh_action)
    language_menu.addAction(zh_action)

    en_action = QAction("English（开发中）", window)
    en_action.setCheckable(True)
    en_action.setEnabled(False)
    language_group.addAction(en_action)
    language_menu.addAction(en_action)

    settings_menu.addSeparator()

    # "集成到右键菜单"：只在主窗口的"设置"菜单里放这一项——它是全局性
    # 的系统设置（改注册表），跟某一个具体窗口没关系，"新建窗口"打开的
    # 窗口不需要（也不应该）重复放一份同样的开关，点哪个窗口的这个开关
    # 效果都一样，放一份就够，放多份反而容易让人以为"这是每个窗口各自
    # 独立的设置"。
    if window.is_primary:
        context_menu_action = QAction("集成到右键菜单", window)
        context_menu_action.setCheckable(True)
        context_menu_action.setToolTip(
            "开启后，在资源管理器里右键点一个文件夹（或者在文件夹内部空白处右键），\n"
            "菜单里会多出一项「用 DWG 图纸搜索工具搜索此目录」，点了直接把这个目录\n"
            "当作搜索范围打开本软件，不用先手动开软件再手动加目录。"
        )

        def _refresh_context_menu_action_state():
            context_menu_action.blockSignals(True)
            context_menu_action.setChecked(is_context_menu_enabled())
            context_menu_action.blockSignals(False)

        def _on_context_menu_toggled(checked):
            if checked:
                ok, err = enable_context_menu()
                action_desc = "开启"
            else:
                ok, err = disable_context_menu()
                action_desc = "关闭"
            if not ok:
                QMessageBox.warning(
                    window, f"{action_desc}失败",
                    f"{action_desc}右键菜单集成失败：{err}\n\n"
                    "这个功能通过写入当前用户的注册表实现（HKEY_CURRENT_USER），\n"
                    "一般不需要管理员权限；如果反复失败，可能是杀毒软件拦截了\n"
                    "注册表写入，可以检查一下安全软件的拦截记录。"
                )
            # 不管成功与否都重新读一次注册表实际状态来刷新勾选框——避免
            # 界面显示的勾选状态跟注册表里的真实情况不一致（比如失败了
            # 一半：两条项目只写成功了一条）。
            _refresh_context_menu_action_state()

        context_menu_action.toggled.connect(_on_context_menu_toggled)
        _refresh_context_menu_action_state()
        settings_menu.addAction(context_menu_action)

        # "最小化为系统托盘图标"：点主窗口关闭按钮时是最小化到托盘（后台
        # 索引继续跑），还是直接退出程序。默认勾选——维持软件原来就有
        # 的行为。这个开关只影响主窗口的关闭行为（"新建窗口"打开的窗口
        # 本来就没有托盘、点关闭永远是真的关掉那个窗口，见 MainWindow.
        # closeEvent），跟"集成到右键菜单"一样是全局性设置，只在主窗口
        # 菜单栏放一份即可。
        minimize_to_tray_action = QAction("最小化为系统托盘图标", window)
        minimize_to_tray_action.setCheckable(True)
        minimize_to_tray_action.setChecked(get_minimize_to_tray_enabled())
        minimize_to_tray_action.setToolTip(
            "开启后，点窗口右上角的关闭按钮会把程序最小化到系统托盘，\n"
            "后台索引继续运行，双击托盘图标可重新打开窗口。\n"
            "关闭后，点关闭按钮会直接退出程序。"
        )
        minimize_to_tray_action.toggled.connect(save_minimize_to_tray_enabled)
        settings_menu.addAction(minimize_to_tray_action)

        # "随系统自启动"：跟"集成到右键菜单"一样通过写注册表实现（见
        # autostart_manager.py），默认不勾选——开机自启动会改变用户对
        # "开机后哪些软件会自己跑起来"的预期，应该是用户主动选择打开的
        # 行为，不能替用户做这个决定。
        autostart_action = QAction("随系统自启动", window)
        autostart_action.setCheckable(True)
        autostart_action.setToolTip(
            "开启后，每次开机登录 Windows 会自动在后台启动本程序（不会\n"
            "弹出主窗口，只会出现系统托盘图标），索引可以随开机自动开始\n"
            "更新，不用先手动打开软件。"
        )

        def _refresh_autostart_action_state():
            autostart_action.blockSignals(True)
            autostart_action.setChecked(is_autostart_enabled())
            autostart_action.blockSignals(False)

        def _on_autostart_toggled(checked):
            if checked:
                ok, err = enable_autostart()
                action_desc = "开启"
            else:
                ok, err = disable_autostart()
                action_desc = "关闭"
            if not ok:
                QMessageBox.warning(
                    window, f"{action_desc}失败",
                    f"{action_desc}开机自启动失败：{err}\n\n"
                    "这个功能通过写入当前用户的注册表实现（HKEY_CURRENT_USER），\n"
                    "一般不需要管理员权限；如果反复失败，可能是杀毒软件拦截了\n"
                    "注册表写入，可以检查一下安全软件的拦截记录。"
                )
            # 不管成功与否都重新读一次注册表实际状态来刷新勾选框，理由
            # 跟 _refresh_context_menu_action_state() 一样：避免界面显示
            # 的勾选状态跟注册表里的真实情况不一致。
            _refresh_autostart_action_state()

        autostart_action.toggled.connect(_on_autostart_toggled)
        _refresh_autostart_action_state()
        settings_menu.addAction(autostart_action)

        settings_menu.addSeparator()

    open_data_dir_action = QAction("打开数据存储目录", window)

    def _open_data_dir():
        open_containing_folder(get_db_path(), parent=window)

    open_data_dir_action.triggered.connect(_open_data_dir)
    settings_menu.addAction(open_data_dir_action)

    # ---- 书签 ----
    # 不像"集成到右键菜单"/"最小化到托盘"/"自启动"那几个全局系统设置
    # 只在主窗口放一份，书签菜单每个窗口（包括"新建窗口"打开的）都要有
    # 一份——书签本身是收藏"怎么搜"这个偏好，跟当前是哪个窗口没关系，
    # 数据存在同一个 bookmarks.json 里，哪个窗口点"添加到书签"/"书签
    # 管理"效果都一样，都是操作同一份数据。
    bookmark_menu = menu_bar.addMenu("书签")

    add_bookmark_action = QAction("添加到书签", window)
    add_bookmark_action.triggered.connect(lambda: _prompt_add_bookmark(window))
    bookmark_menu.addAction(add_bookmark_action)

    manage_bookmark_action = QAction("书签管理", window)
    manage_bookmark_action.triggered.connect(window._show_bookmark_dialog)
    bookmark_menu.addAction(manage_bookmark_action)

    # ---- 帮助 ----
    help_menu = menu_bar.addMenu("帮助")

    about_action = QAction("关于", window)

    def _show_about():
        QMessageBox.about(
            window,
            f"关于 {APP_NAME}",
            f"<b>{APP_NAME} {APP_VERSION}</b><br><br>"
            "批量搜索 / 替换 DWG 图纸文件名与文字内容的桌面工具。<br>"
            "基于 accoreconsole 无界面引擎读取图纸内容，本地建立索引，"
            "支持按文件名、正文内容关键词（含正则表达式）快速检索。<br><br>"
            "<span style='color:#888;'>如果这个工具帮你省了时间，"
            "欢迎在\"帮助 -> 捐赠作者\"里请作者喝杯咖啡（完全自愿）。</span>"
        )

    about_action.triggered.connect(_show_about)
    help_menu.addAction(about_action)

    # ---- 捐赠作者 ----
    # 独立小工具的用户对"要钱"比较敏感，所以做成"想找就找得到、
    # 不想理会也完全不会撞见"的低打扰方式：只在帮助菜单里放一项，
    # 不做启动弹窗、不做使用次数提示。二维码图片路径见 config.py 的
    # get_donate_wechat_image_path()/get_donate_alipay_image_path()，
    # 图片本身不随代码提交，由使用者自行放进 assets/donate/ 目录。
    donate_action = QAction("捐赠作者", window)
    donate_action.triggered.connect(lambda: _show_donate_dialog(window))
    help_menu.addAction(donate_action)

    check_update_action = QAction("检查更新", window)

    def _check_update():
        # 如实说明：这个工具目前没有接入任何自动更新通道，不去假装
        # 发了一次网络请求、装模作样地告诉用户"已是最新版本"——那种
        # 提示除了让人误以为真的检查过，没有别的意义。
        QMessageBox.information(
            window, "检查更新",
            f"当前版本：{APP_VERSION}\n\n"
            "本工具暂未接入自动更新通道，如需要新版本请联系软件分发者获取。"
        )

    check_update_action.triggered.connect(_check_update)
    help_menu.addAction(check_update_action)

    log_action = QAction("程序日志", window)

    def _open_log():
        log_path = get_log_file_path()
        if not os.path.exists(log_path):
            QMessageBox.information(
                window, "程序日志",
                "日志文件还没有生成，请让程序正常运行一会儿（比如等索引扫描开始）之后再试。"
            )
            return
        open_file_with_default_app(log_path)

    log_action.triggered.connect(_open_log)
    help_menu.addAction(log_action)

    # 暴露给 create_main_layout() 在表格创建完之后往里加"显示列"子菜单
    # ——这里构建菜单栏时表格（table）还没创建出来（表格在 layout.py
    # 靠后的位置才 new 出来），没法在这里直接引用 table 对象，所以
    # 先把这个菜单对象挂到 window 上，稍后表格创建完了再回填内容。
    window.settings_menu = settings_menu

    return menu_bar


class _AutoRepositionLabel(QLabel):
    """跟普通 QLabel 一样，只是每次 setText() 改内容之后会顺便通知外面
    "这块浮层里的文字变了，需要重新算一下浮层整体该多宽、摆在哪"。

    背景：浮层（previewOverlayToolbar）自己不是被外层布局管理的普通
    控件，是手动 move() 摆放置在 text_display_area 右上角的，宽度/
    位置只有在我们主动调用 _reposition() 时才会重新计算。"字数：xxx"
    "命中 x/y"这些文字每次切换文件、翻页都可能变长变短（比如"字数：5"
    变成"字数：123456"），文字一变宽，如果没人告诉浮层"该重新量一下
    尺寸了"，浮层还是停在上一次算出来的（可能偏窄的）框子里，新内容会
    被挤裁在里面显示不全——用这个类包一层，不管以后是谁、在哪里调用
    这两个标签的 setText()，都会自动触发一次重新定位，不用满项目找
    有多少处调用点、生怕漏掉一个。
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_text_changed = None  # 由 _add_preview_overlay_toolbar() 事后接上

    def setText(self, text):
        super().setText(text)
        if self._on_text_changed:
            self._on_text_changed()


def _add_preview_overlay_toolbar(window, text_display_area):
    """把"字数：xxx"统计和"上一个/命中 x·y/下一个"关键词导航，做成一块
    浮在预览文本框右上角的小工具条，而不是文本框上方单独占一整行。

    做法：工具条本身是 text_display_area 的子控件（不是加进外层布局），
    平时贴着 viewport 右上角悬浮；文本框本身独占预览区整块空间。
    QPlainTextEdit 不会帮子控件自动排版/跟着一起缩放，所以用一个装在
    text_display_area 上的事件过滤器，监听 Resize 把工具条重新摆到
    右上角——viewport()（不是 text_display_area 本身）的宽度已经刨掉了
    竖排滚动条占的宽度，工具条不会遮住滚动条或者超出滚动条外面。
    """
    overlay = QFrame(text_display_area)
    overlay.setObjectName("previewOverlayToolbar")
    overlay.setStyleSheet("""
        QFrame#previewOverlayToolbar {
            background-color: rgba(255, 255, 255, 235);
            border: 1px solid #d5d9de;
            border-radius: 6px;
        }
        QFrame#previewOverlayToolbar QLabel {
            color: #666;
            font-size: 11px;
            background: transparent;
            border: none;
        }
        QFrame#previewOverlayToolbar QPushButton {
            color: #444;
            font-size: 12px;
            background: transparent;
            border: none;
            border-radius: 3px;
            padding: 1px 6px;
        }
        QFrame#previewOverlayToolbar QPushButton:hover {
            background-color: #e8edf3;
        }
        QFrame#previewOverlayToolbar QPushButton:disabled {
            color: #c2c6cb;
        }
    """)

    overlay_layout = QHBoxLayout(overlay)
    overlay_layout.setContentsMargins(8, 3, 6, 3)
    overlay_layout.setSpacing(4)

    preview_stats_label = _AutoRepositionLabel("字数：—")
    overlay_layout.addWidget(preview_stats_label)
    window.preview_stats_label = preview_stats_label

    # 竖线做视觉分隔，跟右边"上一个/命中数/下一个"这组导航控件区分开，
    # 不然纯靠间距挤在一起，看着像同一组东西。
    divider = QFrame()
    divider.setFrameShape(QFrame.VLine)
    divider.setStyleSheet("color: #d5d9de;")
    overlay_layout.addWidget(divider)

    # 浮在小小一块工具条里，没有原来一整行那么宽的空间，"上一个/下一个"
    # 这种四个字的按钮会把工具条撑得很宽、快赶上半个文本框了；换成箭头
    # 图标+文字提示（悬停显示"上一个匹配"/"下一个匹配"），意思不丢、
    # 占地方却小很多，是悬浮工具条这种寸土寸金的场景里更合适的写法。
    prev_match_btn = QPushButton("‹")
    prev_match_btn.setFixedWidth(22)
    prev_match_btn.setToolTip("上一个匹配")
    # 要等 preview_manager 创建之后才能连信号，见 create_main_layout 里
    # table 那处同样的注释——这里只负责摆出控件，交给 main_window.py 连接。
    window.prev_match_btn = prev_match_btn
    overlay_layout.addWidget(prev_match_btn)

    match_nav_label = _AutoRepositionLabel("")
    match_nav_label.setAlignment(Qt.AlignCenter)
    match_nav_label.setMinimumWidth(36)
    window.match_nav_label = match_nav_label
    overlay_layout.addWidget(match_nav_label)

    next_match_btn = QPushButton("›")
    next_match_btn.setFixedWidth(22)
    next_match_btn.setToolTip("下一个匹配")
    window.next_match_btn = next_match_btn
    overlay_layout.addWidget(next_match_btn)

    overlay.adjustSize()
    window.preview_overlay_toolbar = overlay
    # 一开始（还没打开任何文件）先藏起来——这时候文本框中间显示的是
    # "单击左侧表格中的文件以显示其文本内容..."这行占位提示，浮层这时候
    # 露出来只会挡住这行提示文字的一部分，没有任何实际信息量（字数还是
    # "—"，也没有命中可导航），不如干脆不露出来，等真的加载出内容了
    # （见 preview_manager.py 的 on_content_ready）再显示。
    overlay.setVisible(False)

    MARGIN = 8

    def _reposition():
        vp = text_display_area.viewport()
        overlay.adjustSize()
        x = vp.width() - overlay.width() - MARGIN
        overlay.move(max(0, x), MARGIN)
        overlay.raise_()

    # 见 _AutoRepositionLabel 的说明：文字内容一变就顺便重新摆一次位置。
    preview_stats_label._on_text_changed = _reposition
    match_nav_label._on_text_changed = _reposition

    class _OverlayRepositionFilter(QObject):
        def eventFilter(self, watched, event):
            if event.type() in (QEvent.Resize, QEvent.Show):
                # 不能在这里直接摆位置：text_display_area 自己的 Resize
                # 事件先于它内部 viewport 的几何更新触发，这一刻读到的
                # viewport().width() 还是缩放前的旧值，摆出来的位置会
                # 慢一拍、对不上最新宽度。用 QTimer.singleShot(0, ...)
                # 拖到这一轮事件处理完之后再摆，那时候 viewport 的宽高
                # 已经刷新好了。
                QTimer.singleShot(0, _reposition)
            return False

    reposition_filter = _OverlayRepositionFilter(text_display_area)
    text_display_area.installEventFilter(reposition_filter)
    # 浮层刚创建出来的时候先设成隐藏，这时候窗口往往还没被恢复到上次
    # 关闭时的真实大小（_restore_window_geometry() 是主窗口初始化最后
    # 才跑的），下面 _reposition() 这一次算出来的位置很可能是"临时的
    # 小尺寸窗口"那会儿的位置。之后 preview_manager.py 每次要真的显示
    # 浮层（调用 overlay.setVisible(True)）的时候，因为没有跟着触发
    # text_display_area 自己的 Resize/Show 事件，摆放位置不会重新算，
    # 用的还是那个过时的、可能对不上当前窗口大小的老位置——表现出来就是
    # 浮层"字数"这些信息该出现却好像没显示到正确地方，非要用户手动
    # 拖一下窗口大小、触发一次真正的 Resize 事件才会摆正。这里把同一个
    # 过滤器也装到浮层自己身上、盯着它自己的 Show 事件，这样每次
    # setVisible(True) 弹出来的那一刻都会强制重新摆一次位置，不会再
    # 用过时的旧坐标。
    overlay.installEventFilter(reposition_filter)
    # 事件过滤器对象本身没有别的引用持有它，得手动挂在 window 身上，
    # 不然 Python 这边没人拿着这个对象，很快被垃圾回收，装上去的事件
    # 过滤器也就跟着失效了。
    window._preview_overlay_reposition_filter = reposition_filter

    _reposition()
    return overlay


def create_main_layout(window):
    overall_vertical_layout = QVBoxLayout()
    overall_vertical_layout.setContentsMargins(0, 0, 0, 0)
    overall_vertical_layout.setSpacing(0)

    # =========================================================
    # 索引管理弹窗：只创建对话框本体和显示函数，"索引管理"这一项现在
    # 挂在菜单栏"设置"下面，同时下面表格上方也保留了一份同样触发这个
    # 对话框的按钮（高频用到，留在主界面更顺手，两处不冲突）。
    # =========================================================
    index_dialog = _build_index_management_dialog(window)

    def _show_index_dialog():
        index_dialog.show()
        index_dialog.raise_()
        index_dialog.activateWindow()

    window.index_dialog = index_dialog
    window._show_index_dialog = _show_index_dialog

    # =========================================================
    # 书签管理弹窗：同样只创建对话框本体和显示函数，"书签管理"这一项
    # 挂在菜单栏"书签"下面。每次弹出都先 reload() 一次，保证看到的是
    # 最新数据（比如刚点了旁边的"添加到书签"按钮，或者别的窗口改过）。
    # =========================================================
    bookmark_dialog = BookmarkManagerDialog(window, parent=window)

    def _show_bookmark_dialog():
        bookmark_dialog.reload()
        bookmark_dialog.show()
        bookmark_dialog.raise_()
        bookmark_dialog.activateWindow()

    window.bookmark_dialog = bookmark_dialog
    window._show_bookmark_dialog = _show_bookmark_dialog

    # 菜单栏放在整个窗口最顶上，比搜索框还靠上。
    menu_bar = _build_menu_bar(window)
    window.menu_bar = menu_bar
    overall_vertical_layout.setMenuBar(menu_bar)

    # =========================================================
    # 顶部搜索工具栏，改成两行：
    #   第一行（主行，加高更醒目）：文件名 + 内容，各占一半宽度，
    #   是每次搜索都要填的核心输入，理应比目录框更显眼。
    #   第二行：搜索目录单独占一整行——"浏览"不再单独放一个按钮，
    #   收进下拉框本身，选中即弹出选择对话框，省掉一个常驻按钮的空间。
    # =========================================================
    MAIN_ROW_HEIGHT = 34  # 主行（文件名/内容）比其他行更高，突出这是主要输入
    MAIN_ROW_FONT_SIZE = 14  # 之前是13，跟"搜索目录"那行的默认字号（约12px）太接近，看着像一样大；这次调开一点

    # 两个搜索框改回原生 QComboBox 样式：放大镜/正则图标嵌在输入框
    # 内部，清空按钮、下拉箭头都是 Qt 原生实现，不额外包一层自绘容器
    # （详见 helpers.py 里 build_native_search_combo() 的说明）。
    ICON_GRAY = "#6b7280"

    filename_keyword_edit, filename_search_action, filename_regex_action, filename_bookmark_action = build_native_search_combo(
        placeholder="搜索文件名", icon_color=ICON_GRAY,
        height=MAIN_ROW_HEIGHT, font_size=MAIN_ROW_FONT_SIZE,
        with_bookmark_button=True,
    )

    keyword_edit, keyword_search_action, content_regex_action, _content_bookmark_action = build_native_search_combo(
        placeholder="搜索文件内容", icon_color=ICON_GRAY,
        height=MAIN_ROW_HEIGHT, font_size=MAIN_ROW_FONT_SIZE,
    )

    window.filename_regex_action = filename_regex_action
    window.content_regex_action = content_regex_action

    filename_search_action.triggered.connect(window.search_manager.start_search)
    keyword_search_action.triggered.connect(window.search_manager.start_search)
    # 书签按钮只在文件名框那一侧出现（内容框那份 with_bookmark_button
    # 没传，_content_bookmark_action 恒为 None），但收藏的是"文件名+
    # 内容"两个框合起来的搜索条件——见 _prompt_add_bookmark()。
    filename_bookmark_action.triggered.connect(lambda: _prompt_add_bookmark(window))

    # 书签按钮的图标要能动态在"空心/实心"之间切换（当前搜索条件是否
    # 已经收藏过），实际的绘制细节封装成一个闭包挂在 window 上——
    # _refresh_bookmark_icon_state() 只是个到处都能调用的统一入口，
    # 具体怎么画、按钮引用是谁，都在这里就地确定好，不用把
    # filename_bookmark_action/ICON_GRAY/MAIN_ROW_HEIGHT 这些局部变量
    # 到处传递。
    def _do_refresh_bookmark_icon():
        filename_kw = filename_keyword_edit.currentText().strip()
        content_kw = keyword_edit.currentText().strip()
        matched = find_bookmark_by_content(
            filename_kw, filename_regex_action.isChecked(),
            content_kw, content_regex_action.isChecked(),
        )
        is_bookmarked = matched is not None
        filename_bookmark_action.setIcon(
            make_bookmark_status_icon(ICON_GRAY, MAIN_ROW_HEIGHT, filled=is_bookmarked)
        )
        filename_bookmark_action.setToolTip(
            f"已收藏为书签「{matched.get('name', '')}」"
            if is_bookmarked else
            "把当前的文件名/内容搜索条件收藏为书签"
        )

    window._refresh_bookmark_icon_impl = _do_refresh_bookmark_icon
    # 文件名/内容任一输入框的文字变化、任一正则开关切换，都要重新
    # 判断一次"当前条件是否已收藏"——用户可能是手动改成了跟某条旧
    # 书签一样的内容，也可能是从书签管理里应用了一条书签，图标都要
    # 跟着实时对上，不是只在点完"添加到书签"那一下才更新。
    filename_keyword_edit.editTextChanged.connect(lambda _t: _do_refresh_bookmark_icon())
    keyword_edit.editTextChanged.connect(lambda _t: _do_refresh_bookmark_icon())
    filename_regex_action.toggled.connect(lambda _c: _do_refresh_bookmark_icon())
    content_regex_action.toggled.connect(lambda _c: _do_refresh_bookmark_icon())
    _do_refresh_bookmark_icon()

    # "筛选"这颗按钮本身永远显示、不会跟着收起——它是唯一能把下面那排
    # 筛选框重新打开的入口，如果把它也放进会被隐藏的那个容器里，收起后
    # 就再也点不到了。按钮文字用 ▾/▸ 提示当前是展开还是收起状态。
    filter_toggle_btn = QPushButton()
    filter_toggle_btn.setMinimumWidth(70)
    filter_toggle_btn.setMinimumHeight(MAIN_ROW_HEIGHT)

    main_row_layout = QHBoxLayout()
    main_row_layout.setContentsMargins(8, 8, 8, 4)
    main_row_layout.setSpacing(6)

    # 文件名/内容两个搜索框中间加一条可拖拽的分隔线，用户可以按需要
    # 自己调整两边宽度比例，不用被迫接受写死的1:1——跟下面表格/预览区
    # 之间那条分隔线是同一套做法（见本文件后面 splitter.setHandleWidth(2)
    # 那处），细手柄、不额外画顶点样式，观感统一。
    search_splitter = QSplitter(Qt.Horizontal)
    search_splitter.setHandleWidth(2)
    search_splitter.setChildrenCollapsible(False)
    search_splitter.addWidget(filename_keyword_edit)
    search_splitter.addWidget(keyword_edit)
    search_splitter.setStretchFactor(0, 1)
    search_splitter.setStretchFactor(1, 1)

    # 记住用户手动拖动过的左右宽度比例，下次打开软件不用重新拖一遍。
    # 跟下面表格列宽记忆是同一套思路（拖动 300ms 内没有新动作才真正
    # 落盘，退出时如果计时器还没跑到，_quit_app() 里会主动 flush 一次，
    # 见 main_window.py），这里也挂在 window 上方便退出时统一收尾。
    saved_sizes = get_main_search_splitter_sizes()
    if saved_sizes:
        search_splitter.setSizes(saved_sizes)
    else:
        search_splitter.setSizes([1, 1])  # 没存过，左右各半，具体像素由 Qt 按比例分配

    def _save_search_splitter_sizes():
        save_main_search_splitter_sizes(search_splitter.sizes())

    window._save_search_splitter_sizes = _save_search_splitter_sizes
    window._search_splitter_save_timer = QTimer(window)
    window._search_splitter_save_timer.setSingleShot(True)
    window._search_splitter_save_timer.timeout.connect(_save_search_splitter_sizes)
    search_splitter.splitterMoved.connect(
        lambda *args: window._search_splitter_save_timer.start(300)
    )

    window.main_search_splitter = search_splitter

    main_row_layout.addWidget(search_splitter, 1)
    main_row_layout.addSpacing(6)
    main_row_layout.addWidget(filter_toggle_btn)

    overall_vertical_layout.addLayout(main_row_layout)

    # 搜索目录单独一行，不再带"搜索目录："这个前缀标签——标签栏本身
    # 空的时候会显示"点击此处添加搜索路径。"这句提示，已经足够说明这
    # 一整行是干什么用的，不需要额外再重复一遍。
    path_row_layout = QHBoxLayout()
    path_row_layout.setContentsMargins(8, 0, 8, 8)
    path_row_layout.setSpacing(6)

    folder_scope_bar = FolderScopeBar()

    path_row_layout.addWidget(folder_scope_bar, 1)

    overall_vertical_layout.addLayout(path_row_layout)

    window.folder_scope_bar = folder_scope_bar
    window.filename_keyword_edit = filename_keyword_edit
    window.keyword_edit = keyword_edit
    window.filename_search_action = filename_search_action
    window.keyword_search_action = keyword_search_action

    # =========================================================
    # 搜索筛选行：搜索哪些类型的文字 + 在哪个空间搜 + 是否算块定义内部。
    # 只在填了"内容"关键词时才生效（纯文件名搜索跟这三个筛选无关）。
    # 勾选状态会记住上次的选择（跟"批量文字替换"的 scan_options 是同一套
    # 持久化机制），没有历史记录时默认全部勾选，等价于不筛选。
    #
    # 整排装进一个独立的容器 QWidget（filter_row_widget）里，是为了能用
    # setVisible() 整体收起/展开——平时这排不常调整，收起来能省一行的
    # 界面空间，需要调整的时候点"筛选"按钮随时展开，勾选之后也不用
    # 关闭什么东西，效果立刻生效（这条跟展开/收起状态本身没关系，之前
    # 就已经是这样）。是否展开的状态本身也会记住，下次启动恢复成上次
    # 收起/展开前的样子。
    # =========================================================
    saved_filter_options = get_search_filter_options()

    filter_row_widget = QWidget()
    filter_row_layout = QHBoxLayout(filter_row_widget)
    filter_row_layout.setContentsMargins(8, 0, 8, 4)
    filter_row_layout.setSpacing(6)

    type_label = QLabel("文字类型：")
    filter_row_layout.addWidget(type_label)
    window.filter_type_checkboxes = {}
    for code in ALL_TEXT_TYPES:
        cb = QCheckBox(TEXT_TYPE_LABELS[code])
        cb.setChecked(saved_filter_options["entity_types"].get(code, True))
        window.filter_type_checkboxes[code] = cb
        filter_row_layout.addWidget(cb)

    filter_row_layout.addSpacing(16)

    space_label = QLabel("搜索位置：")
    filter_row_layout.addWidget(space_label)
    window.filter_space_checkboxes = {}
    for code in ALL_SPACES:
        cb = QCheckBox(SPACE_LABELS[code])
        cb.setChecked(saved_filter_options["spaces"].get(code, True))
        window.filter_space_checkboxes[code] = cb
        filter_row_layout.addWidget(cb)

    filter_row_layout.addSpacing(16)

    # "块定义内部 vs 摆放的实体"是独立于"模型/图纸空间"的另一个维度——
    # 跟"查找替换"功能里 scan_space / include_block_defs 是同一对概念，
    # 只是这里是搜索筛选、不是替换范围开关，用勾选框而不是互斥单选。
    scope_label = QLabel("块定义范围：")
    filter_row_layout.addWidget(scope_label)
    window.filter_scope_checkboxes = {}
    # Qt 的 tooltip 有个不太直观的特点：纯文本一律显示成一整行，不会自动
    # 换行，只有富文本（HTML）才会按指定宽度换行——所以这里特意包一层
    # <html><body style='width: ...'> 让 Qt 把它当成富文本处理，说明文字
    # 长的话就能按这个宽度自动折成多行，不用整行拉得老长。
    def _wrap_tooltip(text, width_px=320):
        return f"<html><body style='width:{width_px}px'>{text}</body></html>"

    SCOPE_TOOLTIPS = {
        "PLACED": _wrap_tooltip(
            "图纸里直接看得到、能点选到的内容——包括直接画的文字/标注，"
            "以及块实例里显示出来的属性值。日常搜图纸内容，勾这个就够了。"
        ),
        "BLOCK_DEF": _wrap_tooltip(
            "块的“设计模板”本身自带的固定文字，不是某次插入产生的。"
            "这个块不管在图上插入了多少次，这些文字只算一份，搜到时"
            "对应的是模板本身、不是某个具体插入位置；模板里如果还"
            "嵌套了别的块，也会一起搜进去。"
        ),
    }
    for code in ALL_SCOPES:
        cb = QCheckBox(SCOPE_LABELS[code])
        cb.setChecked(saved_filter_options["scopes"].get(code, True))
        if code in SCOPE_TOOLTIPS:
            cb.setToolTip(SCOPE_TOOLTIPS[code])
        window.filter_scope_checkboxes[code] = cb
        filter_row_layout.addWidget(cb)

    filter_row_layout.addStretch(1)

    # 筛选框状态一变：1) 如果右边正好选中着某个文件的预览，跟着刷新一遍，
    # 保证预览内容跟当前勾选状态对得上；2) 顺手把这次的勾选状态存到配置
    # 文件里，下次启动直接按上次的勾选状态还原，不用每次重开都重新勾一遍。
    # display_selected_file_content 在没有选中行时会直接 return，
    # 所以这里无脑连接不会有副作用。
    all_filter_checkboxes = (
        list(window.filter_type_checkboxes.values())
        + list(window.filter_space_checkboxes.values())
        + list(window.filter_scope_checkboxes.values())
    )
    for cb in all_filter_checkboxes:
        cb.stateChanged.connect(lambda _state, w=window: w.preview_manager.display_selected_file_content())
        cb.stateChanged.connect(lambda _state, w=window: w.search_manager.save_search_filter_state())

    overall_vertical_layout.addWidget(filter_row_widget)

    # 恢复上次的展开/收起状态，并让"筛选"按钮的文字跟当前状态保持同步
    def _set_filter_row_expanded(expanded):
        filter_row_widget.setVisible(expanded)
        filter_toggle_btn.setText("筛选 ▴" if expanded else "筛选 ▾")

    def _toggle_filter_row():
        expanded = not filter_row_widget.isVisible()
        _set_filter_row_expanded(expanded)
        save_search_filter_row_expanded(expanded)

    filter_toggle_btn.clicked.connect(_toggle_filter_row)
    _set_filter_row_expanded(get_search_filter_row_expanded())

    # 保留控件实例（防止 main_window.py 报空指针），但不加入任何布局——
    # DXF自定义输出路径这个功能目前主界面没有入口，先占位保留兼容
    window.custom_checkbox = QCheckBox("自定义DXF输出路径")
    window.output_label = QLabel("DXF输出路径：")
    window.dxf_output_edit = QLineEdit()
    window.dxf_output_edit.setPlaceholderText("默认输出到当前程序目录/converted_dxf")
    window.output_btn = QPushButton("浏览")

    # =========================================================
    # 左侧：结果表格 + 操作按钮
    # =========================================================
    left_widget = QWidget()
    left_vertical_layout = QVBoxLayout(left_widget)
    left_vertical_layout.setContentsMargins(6, 4, 2, 6)
    left_vertical_layout.setSpacing(2)

    # "文字替换工具"/"索引管理..."原来在这里放两个按钮，现在都已经进了
    # 顶部菜单栏（文字替换 / 设置->索引管理...），这里不用再重复放一份
    # 入口，表格直接顶到左侧面板最上面，跟右侧预览区（文本框也是顶到
    # 面板最上面）保持一致，不会显得左边比右边矮一截。
    # "全选"已挪到右键菜单里，不用独立按钮。

    model, table, frozen_filename_view, filename_highlight_delegate = create_results_table(window)
    window.results_model = model
    window.filename_highlight_delegate = filename_highlight_delegate
    window.frozen_filename_view = frozen_filename_view

    table.setColumnWidth(0, 150)
    table.setColumnWidth(1, 310)
    table.setColumnWidth(2, 150)
    table.setColumnWidth(3, 150)
    table.setColumnWidth(4, 130)
    table.setColumnWidth(5, 80)

    # 记住用户手动拖动调整过的列宽，下次打开软件不用重新调一遍。最后
    # 一列"大小"开了 setStretchLastSection(True)，会自动占满剩余空间，
    # 不需要也没法记它的宽度，只记前面几列。
    #
    # 这里跟"文字替换"对话框里文件列表的列宽记忆是同一套思路（拖动
    # 300ms 内没有新动作才真正落盘，避免拖动过程中每移动一像素就写
    # 一次配置文件），但这个主窗口关闭按钮点了并不会真正退出程序、
    # 只是最小化到托盘（见 MainWindow.closeEvent），真正退出走的是
    # 托盘"退出程序" -> MainWindow._quit_app()，那条路径为了确保干净
    # 退出用的是 os._exit(0) 直接砍掉进程，不会给还没来得及触发的
    # QTimer 一个跑完的机会。所以这里的计时器对象和保存函数都挂在
    # window 上（window._table_column_width_save_timer /
    # window._save_table_column_widths），方便 main_window.py 的
    # _quit_app() 在真正退出前主动 flush 一次，不依赖计时器自己跑到。
    saved_widths = get_main_table_column_widths()
    if saved_widths:
        for col, w in enumerate(saved_widths):
            if col < table.model().columnCount() - 1 and isinstance(w, int) and w > 0:  # 最后一列跳过，交给 stretchLastSection
                table.setColumnWidth(col, w)

    # ---- 表头右键菜单：选择要显示/隐藏的列 ----
    # 第0列"文件名"不开放隐藏：冻结列这套机制（见 results_table_model.py
    # 的 create_frozen_first_column_view）是按"第0列"这个固定逻辑位置
    # 做的，隐藏第0列会破坏这套机制，所以不开放。
    #
    # 其余5列（含"大小"）都可以隐藏。"大小"这一列虽然开了
    # setStretchLastSection(True)，但这不是它不能隐藏的理由——实测过
    # Qt 的这个属性是跟着"当前视觉上排在最后的那一可见列"走的，隐藏掉
    # 它之后拉伸效果会自动转移到新的最后一列，不会留空、也不会让拖动
    # 列宽的行为跟着变别扭（哪怕把其余列也全部隐藏、只剩第0列，拉伸
    # 效果一样会正确转移到第0列身上，且宽度同步信号照常正常触发）。
    #
    # 用列名而不是列序号做标识、存配置，以后万一调整了列的先后顺序，
    # 用户存好的"隐藏了哪几列"设置不会跟着错位失效。
    _HIDEABLE_COLUMNS = [(1, "文件路径"), (2, "创建日期"), (3, "修改日期"), (4, "DWG版本"), (5, "大小")]

    def _apply_hidden_columns():
        hidden_names = set(get_main_table_hidden_columns())
        for col_idx, col_name in _HIDEABLE_COLUMNS:
            table.setColumnHidden(col_idx, col_name in hidden_names)

    def _toggle_column_visibility(col_idx, visible):
        table.setColumnHidden(col_idx, not visible)
        still_hidden = [name for idx, name in _HIDEABLE_COLUMNS if table.isColumnHidden(idx)]
        save_main_table_hidden_columns(still_hidden)

    def _build_column_visibility_actions(menu, parent):
        # 被表头右键菜单、"设置"菜单里的"显示列"子菜单两处共用，保证
        # 两个入口显示的选中状态、点击后的行为完全一致，不用维护两份
        # 重复逻辑——当前是否勾选，统一以 table.isColumnHidden() 这个
        # 唯一真实状态源为准，不额外自己记一份状态，不会出现两处不同步。
        for col_idx, col_name in _HIDEABLE_COLUMNS:
            action = QAction(col_name, parent)
            action.setCheckable(True)
            action.setChecked(not table.isColumnHidden(col_idx))
            action.toggled.connect(
                lambda checked, idx=col_idx: _toggle_column_visibility(idx, checked)
            )
            menu.addAction(action)

    _apply_hidden_columns()

    def _show_header_context_menu(pos):
        menu = QMenu(window)
        _build_column_visibility_actions(menu, menu)
        menu.exec_(table.horizontalHeader().mapToGlobal(pos))

    table.horizontalHeader().setContextMenuPolicy(Qt.CustomContextMenu)
    table.horizontalHeader().customContextMenuRequested.connect(_show_header_context_menu)

    # "设置"菜单里也放一份同样的入口（用户不一定会想到去表头右键）。
    # 这个子菜单在这里（表格创建完之后）才能建，因为要读 table 的当前
    # 隐藏状态——_build_menu_bar() 执行的时候表格还没造出来，所以之前
    # 把 settings_menu 挂在了 window 上，这里回填内容。
    #
    # 每次子菜单弹出前都重新构建一遍菜单项（而不是常驻同一份、只更新
    # 勾选状态），是为了偷懒复用 _build_column_visibility_actions()
    # 这同一份"读当前状态建菜单项"的逻辑，不用再单独写一套"打开菜单前
    # 手动刷新每个 action 勾选状态"的同步代码——两处入口改的是同一个
    # table 对象，天然不会不同步。
    columns_menu = window.settings_menu.addMenu("显示列")

    def _rebuild_columns_menu():
        columns_menu.clear()
        _build_column_visibility_actions(columns_menu, columns_menu)

    columns_menu.aboutToShow.connect(_rebuild_columns_menu)

    def _save_table_column_widths():
        widths = [table.columnWidth(c) for c in range(table.model().columnCount())]
        save_main_table_column_widths(widths)

    window._save_table_column_widths = _save_table_column_widths
    window._table_column_width_save_timer = QTimer(window)
    window._table_column_width_save_timer.setSingleShot(True)
    window._table_column_width_save_timer.timeout.connect(_save_table_column_widths)
    table.horizontalHeader().sectionResized.connect(
        lambda *args: window._table_column_width_save_timer.start(300)
    )

    table.setContextMenuPolicy(Qt.CustomContextMenu)
    table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # 斑马纹（隔行变色）：几千行数据横向对齐扫读时，交替底色比纯白底
    # 更容易一眼定位到某一行，不容易看串行；选中行颜色也换成柔和一点
    # 的浅蓝，不用 Qt 默认那种偏刺眼的深蓝。create_results_table() 已经
    # 开了 setAlternatingRowColors(True)，这里只需要补一份具体配色的
    # 样式表——选择器从原来的 QTableWidget 换成 QTableView（新的主表格
    # 类型），其余没有变化。
    table.setStyleSheet(
        "QTableView {"
        "    alternate-background-color: #f4f6f9;"
        "    background-color: #ffffff;"
        "    gridline-color: #e3e6ea;"
        "    border: 1px solid #d5d9de;"
        "}"
        "QTableView::item:selected {"
        "    background-color: #cfe4ff;"
        "    color: black;"
        "}"
    )

    table.customContextMenuRequested.connect(window.table_actions.show_context_menu)
    # 文件名列的右键菜单：这一列的鼠标事件现在被 frozen_filename_view
    # 接住了，主表格本身的 customContextMenuRequested 收不到发生在这
    # 一列上的右键事件，得给 frozen_filename_view 单独接一份。位置坐标
    # 是相对于 frozen_filename_view 自己 viewport 的局部坐标，跟主表格
    # 那份处理函数（show_context_menu）预期的"相对于 table.viewport()
    # 的局部坐标"不是一回事，直接传过去弹出的菜单位置会对不上鼠标——
    # 这里先转成全局屏幕坐标、再转回主表格 viewport 的坐标系，这样
    # show_context_menu 内部继续按老逻辑处理，不用改它本身的代码。
    frozen_filename_view.setContextMenuPolicy(Qt.CustomContextMenu)

    def _frozen_context_menu(pos):
        global_pos = frozen_filename_view.viewport().mapToGlobal(pos)
        table_local_pos = table.viewport().mapFromGlobal(global_pos)
        window.table_actions.show_context_menu(table_local_pos)

    frozen_filename_view.customContextMenuRequested.connect(_frozen_context_menu)

    # 注：cellClicked 和 itemSelectionChanged 在正常点击行为下会同时触发，
    # 之前两个都接了同一个槽函数，导致点一次预览逻辑（读库+渲染+高亮）跑两遍，
    # 大文件时这个重复代价会被放大。QTableView 没有 itemSelectionChanged
    # 这个信号（那是 QTableWidget 专属的），对应的是 selectionModel() 的
    # selectionChanged 信号，同样覆盖鼠标点击和键盘上下键切换选中这两种
    # 场景，效果一致。这个信号要连到 window.preview_manager 身上，但
    # create_main_layout 执行的这一刻 preview_manager 还没创建出来（它得
    # 等布局搭完、text_display_area 有了之后才能构造），所以这里先不连，
    # 交给 MainWindow.__init__ 在创建完 preview_manager 之后再连
    # （连接对象从 table.itemSelectionChanged 换成了
    # table.selectionModel().selectionChanged，主文件那边接线的地方也
    # 要跟着改）。
    #
    # 双击打开文件：文件名列的双击现在被冻结视图接住了，其余列（路径/
    # 日期/大小）的双击还是走主表格自己的信号——QTableWidget 用的是
    # cellDoubleClicked(row, col)，QTableView 对应的是 doubleClicked(index)，
    # 两边都接到同一个处理函数上，效果保持一致——双击这一行的任何位置
    # 都能打开。
    table.doubleClicked.connect(
        lambda index: window.table_actions.open_file_from_table(index.row(), index.column())
    )
    frozen_filename_view.doubleClicked.connect(
        lambda index: window.table_actions.open_file_from_table(index.row(), index.column())
    )

    # 文件名被编辑（单击已选中的行再点一下、按F2、或者直接打字）之后，
    # 真正把磁盘上的文件改名——这部分逻辑现在整个搬进了
    # ResultsTableModel.setData()（见 results_table_model.py），不再需要
    # 像原来 QTableWidget 的 itemChanged 信号那样在外部（
    # table_actions_manager.rename_file_from_table）单独校验、
    # os.rename()、失败了再手动把文字改回去——Model 层直接做完这一切，
    # 改名失败时 setData() 返回 False，Qt 自己就会让编辑框显示的文字
    # 恢复原状，不用额外接线。这里只需要监听改名成功之后的通知信号，
    # 用来做原来 itemChanged 槽函数里"顺带"的部分（如果有需要做的
    # 收尾工作，比如刷新状态栏），具体处理逻辑挪到 table_actions_manager.py。
    model.rename_succeeded.connect(window.table_actions.on_rename_succeeded)

    left_vertical_layout.addWidget(table, 1)
    window.table = table

    # =========================================================
    # 右侧：文件内容预览区
    # =========================================================
    right_widget = QWidget()
    right_vertical_layout = QVBoxLayout(right_widget)
    right_vertical_layout.setContentsMargins(2, 4, 6, 6)
    right_vertical_layout.setSpacing(2)

    text_display_area = QPlainTextEdit()
    text_display_area.setReadOnly(True)
    text_display_area.setPlaceholderText("单击左侧表格中的文件以显示其文本内容...")
    text_display_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    # 跳转到关键词命中位置时，尽量把那一行滚到视口正中间，而不是贴着底边，
    # 这样命中处上下都能看到点上下文，不用手动再滚一下。
    text_display_area.setCenterOnScroll(True)
    # 换成细而扁平的边框，跟左边表格保持一致的视觉风格，去掉 Qt 默认
    # 那种带凹陷阴影效果的厚重"3D"边框。
    text_display_area.setStyleSheet("QPlainTextEdit { border: 1px solid #d5d9de; }")
    window.text_display_area = text_display_area
    right_vertical_layout.addWidget(text_display_area, 1)

    # "字数：xxx"和"上一个/命中x·y/下一个"原来占独立一整行，现在改成
    # 浮在文本框右上角的一小块半透明工具条——效果类似浏览器/编辑器里
    # 常见的那种"查找结果"悬浮条，不用再单独占一整行的高度，文本框本身
    # 也能顶到预览区最上面，跟左边表格顶到面板最上面对齐。
    _add_preview_overlay_toolbar(window, text_display_area)

    # =========================================================
    # 左右两栏改成可拖拽的分隔条：用户可以按需要自己调整表格/预览区的
    # 比例，不用被迫接受写死的1:1。初始给左边（表格）多一点空间，因为
    # 表格信息密度更高（5列数据），预览区只是一整块纯文字，不需要跟
    # 表格一样宽。
    # =========================================================
    splitter = QSplitter(Qt.Horizontal)
    # 中间那条分隔线之前看着太宽，是左右面板各自预留的边距(3+3px)加上
    # QSplitter默认手柄宽度叠在一起的结果；这里把手柄本身调细一点，
    # 让中间分隔看起来跟普通的细边框差不多粗细，不再那么显眼。
    splitter.setHandleWidth(2)
    splitter.addWidget(left_widget)
    splitter.addWidget(right_widget)
    splitter.setSizes([600, 400])
    splitter.setChildrenCollapsible(False)
    window.main_splitter = splitter

    overall_vertical_layout.addWidget(splitter, 1)

    # 底部状态栏
    status_label = QLabel("就绪 | 可随时搜索")
    status_label.setStyleSheet("color: gray; font-size: 11px; padding: 3px 6px;")
    overall_vertical_layout.addWidget(status_label)
    window.status_label = status_label

    window.setLayout(overall_vertical_layout)
    # 标题栏文字改成跟 config.py 的 APP_NAME/APP_VERSION 联动，避免以后
    # 升版本号时又只改了 config.py、这里的硬编码字符串忘了同步——这也是
    # 之前 V6.6 版本号在"关于"弹窗生效了、标题栏却还停在 V6.5 的原因。
    window.setWindowTitle(f"{APP_NAME}_{APP_VERSION}")