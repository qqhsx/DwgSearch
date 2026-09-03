# main.py
import sys
import traceback
from PyQt5.QtWidgets import QApplication, QMessageBox
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from main_window import MainWindow  # ✅ 改这里：不再从 ui 子目录导入

# 单实例 + 进程内"新建窗口"/"从右键菜单搜索指定目录" IPC。
#
# 背景：Everything、Anytxt Searcher 这类工具"多开窗口但托盘图标只有
# 一个"，靠的不是什么特殊技巧，就是"根本没有真的多开进程"——第二次
# 双击桌面图标时，那个新进程一启动就会发现已经有一个实例在跑，把
# "帮我开一个新窗口"这句话捎给正在跑的那个实例，自己就退出了；新窗口
# 其实是已经在跑的那个进程自己在内部又开了一个而已，索引服务、托盘
# 图标全程只有那一份，不会被复制第二份。
#
# 右键菜单集成（见 context_menu_integration.py）触发本程序时也是一样
# 的场景：Windows 每次都是重新启动一个全新进程、带上 `--search-folder
# <路径>` 这个参数，不是"通知已经在跑的那个进程"，所以也要走同一套
# 单实例检测——已经有实例在跑的话，把这个目录路径转发过去，让已经在
# 跑的那个实例开一个新窗口来搜，而不是又起一个新进程、多一个托盘图标。
#
# 这里用 Qt 自带的 QLocalServer/QLocalSocket 实现同样的效果（Windows
# 上走的是命名管道，不需要额外装依赖）：
#   - 启动时先尝试以客户端身份连接一个约定好名字的本地服务器。
#     连得上 → 说明已经有实例在跑，把这次启动带的请求（新建空白窗口，
#     或者带着指定目录路径）发过去，这个新进程直接退出，不用再往下
#     初始化 QApplication/主窗口。
#     连不上 → 说明这是第一个实例，自己起一个服务器占住这个名字，
#     后面新进程发来的请求都由这个服务器接住转交给主窗口。
_IPC_SERVER_NAME = "DWGSearchTool_V6_SingleInstance"
_IPC_NEW_WINDOW_MSG = b"NEW_WINDOW"
_IPC_SEARCH_FOLDER_PREFIX = b"SEARCH_FOLDER:"


def _parse_search_folder_arg(argv):
    """从命令行参数里找 `--search-folder <路径>`，找不到返回 None。
    右键菜单注册的命令行就是这个格式（见 context_menu_integration.py
    的 _build_command()）。"""
    for i, arg in enumerate(argv):
        if arg == "--search-folder" and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _parse_minimized_arg(argv):
    """命令行里有没有带 `--minimized`。"随系统自启动"注册的命令行就是
    这个格式（见 autostart_manager.py 的 _build_command()）——开机自动
    拉起来的场景，不需要（也不应该）弹出主窗口糊在桌面上，托盘图标
    照常出现，双击就能唤出主窗口，跟手动从托盘打开是一回事。"""
    return "--minimized" in argv


def _try_notify_running_instance(search_folder=None):
    """返回 True：已经有实例在跑，本进程该做的（转发一条消息）已经
    做完了，接下来应该直接退出。返回 False：本进程是第一个实例，
    照常往下走完整启动流程。"""
    socket = QLocalSocket()
    socket.connectToServer(_IPC_SERVER_NAME)
    if socket.waitForConnected(500):
        if search_folder:
            message = _IPC_SEARCH_FOLDER_PREFIX + search_folder.encode("utf-8")
        else:
            message = _IPC_NEW_WINDOW_MSG
        socket.write(message)
        socket.waitForBytesWritten(500)
        socket.disconnectFromServer()
        return True
    return False


def _start_ipc_server(on_new_window_requested, on_search_folder_requested):
    """第一个实例专用：起一个本地服务器，接住"以后再启动一次本程序"
    发来的请求（新建空白窗口 / 搜索指定目录），转交给对应的回调去
    实际处理（都是主窗口上的方法）。

    先 removeServer() 一下——如果上次程序是被强制结束的（比如任务管理
    器强杀，没走到正常退出流程），系统上可能残留一个没人认领的同名
    管道，不清掉的话这次 listen() 会失败，会被误判成"已经有实例在跑"，
    导致这次启动直接静默退出、什么窗口都不出现。

    返回的 server 对象调用方必须自己留一个引用（比如存到某个变量里）
    ——它没有其它地方引用着的话会被 Python 垃圾回收掉，服务器也就
    跟着停了，之后再收不到任何请求。
    """
    QLocalServer.removeServer(_IPC_SERVER_NAME)
    server = QLocalServer()
    server.listen(_IPC_SERVER_NAME)

    def _on_new_connection():
        conn = server.nextPendingConnection()
        if conn is None:
            return

        def _on_ready_read():
            data = bytes(conn.readAll())
            if data.startswith(_IPC_SEARCH_FOLDER_PREFIX):
                folder = data[len(_IPC_SEARCH_FOLDER_PREFIX):].decode("utf-8", errors="replace")
                on_search_folder_requested(folder)
            elif data == _IPC_NEW_WINDOW_MSG:
                on_new_window_requested()
            conn.disconnectFromServer()

        conn.readyRead.connect(_on_ready_read)

    server.newConnection.connect(_on_new_connection)
    return server


def exception_hook(exc_type, exc_value, exc_traceback):
    err_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    print("未处理异常：\n", err_msg)
    QMessageBox.critical(None, "程序错误", f"发生未处理异常:\n{err_msg}")

if __name__ == "__main__":
    sys.excepthook = exception_hook

    search_folder_arg = _parse_search_folder_arg(sys.argv)
    minimized_arg = _parse_minimized_arg(sys.argv)

    # 单实例检测放在创建 QApplication 之前：如果已经有实例在跑，这个
    # 新进程只需要转发一条消息就能退出，没必要多花时间初始化一整个
    # QApplication/主窗口再销毁掉。
    if _try_notify_running_instance(search_folder=search_folder_arg):
        sys.exit(0)

    app = QApplication(sys.argv)

    # 窗口图标：与系统托盘图标保持一致，使用系统内置图标，无需外部图标文件
    app.setWindowIcon(app.style().standardIcon(app.style().SP_FileDialogContentsView))

    # 主窗口点"关闭"是最小化到托盘（不是真退出，见 MainWindow.
    # closeEvent），但"新建窗口"打开的窗口点关闭是真的会被销毁的——
    # 如果只开了一个新建的窗口、主窗口又正好被最小化到托盘，关掉那
    # 唯一可见的窗口时，Qt 默认的"最后一个窗口关闭就退出整个程序"
    # 会被意外触发，把还在后台运行的主窗口一起带崩。这里显式关掉这个
    # 默认行为，退出这件事只应该由托盘菜单的"退出程序"来决定。
    app.setQuitOnLastWindowClosed(False)

    # IPC 服务器要尽早起来监听，赶在 MainWindow() 构造完成之前——
    # MainWindow() 要开数据库、扫索引，慢的时候能到一两秒甚至更久。
    # 之前的写法是等 MainWindow() 完全造好、win.show() 都调用完了才
    # 起服务器（在最下面），这段"已经确定自己是第一个实例，但服务器
    # 还没真正开始监听"的窗口期里，如果这段时间内又有新进程启动（比如
    # 右键菜单短时间内被触发了两次），新进程 _try_notify_running_
    # instance() 连接会失败（服务器还没起来），也会误判成"我才是第一个
    # 实例"，于是两个进程都各自往下建了一个完整窗口——这正是"没打开
    # 软件时用右键菜单搜索，结果开出两个窗口"的真正原因。
    #
    # 这里用一个占位字典存 MainWindow 实例的引用，IPC 回调函数用闭包
    # 引用这个占位字典（而不是直接引用 win 变量）——这样可以让服务器
    # 在 win 真正被赋值之前就先监听起来，把"已经有实例在跑"这件事尽早
    # 对外公布，让竞态窗口从"MainWindow() 整个构造过程"缩小到几乎可以
    # 忽略不计的几行代码执行时间。
    win_holder = {}

    def _forward_new_window():
        if "win" in win_holder:
            win_holder["win"].open_new_window()

    def _forward_search_folder(folder):
        if "win" in win_holder:
            win_holder["win"].open_search_for_folder(folder)

    # 必须留一个引用（见 _start_ipc_server 的说明），不能只是调用完
    # 就丢掉返回值
    ipc_server = _start_ipc_server(_forward_new_window, _forward_search_folder)

    win = MainWindow()
    win_holder["win"] = win

    # 主窗口要不要在这次启动时弹出来，取决于这次启动是不是从右键菜单
    # 带着目录路径过来的：
    #   - 普通启动（双击图标/命令行直接运行，没有目录参数）：正常显示
    #     主窗口，这是用户平时打开软件的方式。
    #   - 带目录参数（从右键菜单触发）：主窗口不弹出来，只在后台跑着
    #     （系统托盘图标照样会正常出现，见 MainWindow.__init__ 里
    #     self.tray_icon.show()，不依赖 win.show()）。只显示下面
    #     open_search_for_folder() 专门开的那个目录搜索窗口——这样
    #     跟"软件已经在跑、从右键菜单发过来一条 IPC 消息"这个场景的
    #     表现完全一致（那种场景下主窗口本来就已经在跑或者被最小化在
    #     托盘里，不会跟着弹出来，用户只会看到新增的目录搜索窗口）。
    #     之前这里不管有没有目录参数都无条件 show()，导致从右键菜单
    #     触发时主窗口和目录搜索窗口两个一起弹出来，这才是"没打开软件
    #     时用右键菜单搜索，结果开出两个窗口"的真正原因——不是竞态，
    #     是这里的逻辑本来就没跟 IPC 转发那条路径的行为对齐。
    #   - 带 --minimized 参数（开机自启动触发，见 autostart_manager.py）：
    #     同样不弹出主窗口，只留系统托盘图标，索引/实时监控照常在后台
    #     跑起来，双击托盘图标随时可以唤出主窗口。
    if not search_folder_arg and not minimized_arg:
        win.show()

    # 这次启动本身就是从右键菜单带着目录路径过来的（本进程是第一个
    # 实例，前面 _try_notify_running_instance() 没能转发给别人，说明
    # 得自己处理这个请求）：开一个聚焦在这个目录的窗口。
    if search_folder_arg:
        win.open_search_for_folder(search_folder_arg)

    sys.exit(app.exec())