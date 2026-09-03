# index_manager.py
"""
索引管理：建索引、实时监控变动、排除目录、切换数据库存储路径、清空重建
索引。原来这些状态和方法都直接挂在 MainWindow 上，跟窗口生命周期/搜索/
预览这些完全不相关的职责混在一个 1500+ 行的大文件里，不好找也不好改。
拆出来单独成一个 IndexManager 对象，MainWindow 只保留一个
`self.index_manager = IndexManager(self)` 的引用。

IndexManager 自己不是 QWidget，不能弹窗/不能被别的信号直接连接到它身上
展示界面——所有需要"父窗口"的地方（QDialog(self.window)、
QMessageBox.question(self.window, ...)）、以及需要读写的界面控件
（status_label、tray_icon、index_count_label 等），都通过传进来的
window（也就是 MainWindow 实例）访问，不复制一份、不重新造轮子。
"""
import os
from PyQt5.QtWidgets import QMessageBox, QFileDialog
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor

from search_thread import IndexThread, ClearIndexThread
from file_watcher import FileWatcherThread
from database import DWGDatabase
from config import get_db_path, save_db_path
from log_utils import log


class IndexManager:
    def __init__(self, window):
        self.window = window
        # 支持"多开窗口"（同一进程内多个 MainWindow 共用一个 IndexManager，
        # 见 main_window.py 的 open_new_window()）：新开的窗口只是额外
        # 注册进这个列表，索引/监控本身还是只有这一份，不会被重复建、
        # 重复监控；状态更新（状态栏文字、索引统计这几个标签）会广播
        # 给列表里所有还活着的窗口，不是只更新最早创建它的那一个。
        # self.window 本身（主窗口）永远是 linked_windows[0]，托盘图标、
        # "搜索目录"范围这些只有主窗口才有的东西，还是只认 self.window，
        # 不会尝试对着别的窗口去操作。
        self.linked_windows = [window]

        self.index_thread = None
        self.watcher_thread = None
        # "清空并重建索引"用的后台清空线程（DELETE + VACUUM），留一个引用
        # 防止线程还在跑的时候对象被垃圾回收，同时用来判断是否已经有一次
        # 清空正在进行中，避免用户手快连点触发重复清空。
        self._clear_index_thread = None
        # 排除范围现在是纯内存过滤（config.json 是唯一真相来源，搜索/
        # 扫描/监控三处都现查），保存排除设置不再需要一个专门的后台线程
        # 去追平数据库里的一份"排除标记"——这里不再需要类似
        # _exclude_diff_worker/_exclude_diff_queue 这样的状态。
        # 取消排除某个目录后，如果这个目录之前从没被扫描过，还是需要
        # 主动补扫一次才能真正找到里面的图纸——这个队列专门收集"等当前
        # 索引线程跑完之后要补扫的目录"，见 _scan_folders_now()。
        self._pending_targeted_scans = []
        # “待退休”的后台线程收容所：凡是调用了非阻塞的 stop()、但没有
        # 老老实实 wait() 等它真正退出就马上把引用丢掉的线程对象，都先扔
        # 进这里养着，等它真正跑完（QThread 内置的 finished 信号）再放手。
        # 背景：如果调用 stop() 之后立即把仅有的 Python 引用置为 None，
        # 线程的 run() 其实还没走完（比如 watchdog 的 observer 还在收尾），
        # 这时候如果没有其他引用挡着，垃圾回收器可能会在线程还在跑的时候
        # 就把这个 QThread 对象销毁掉，直接触发 Qt 的
        # “QThread: Destroyed while thread is still running”致命错误，
        # 严重时会把整个程序进程带崩溃退出（不是弹个警告那么简单）。
        self._retiring_threads = []
        # 记录"本次运行期间，哪些目录已经被完整扫描过"。这些目录扫完后
        # 会被实时监控接手（start_watcher 覆盖 _get_index_folder_list()
        # 返回的全部目录），所以只要在这个集合里，就不需要因为切换了
        # 搜索目标而被重新拉去陪跑一遍——它们后续的变动已经在被实时
        # 监控同步了，重新扫一遍纯粹是浪费。这个集合只存在内存里，不
        # 持久化，每次软件重启都会清空，重新走一遍完整流程（补上离线差价）。
        self._fully_scanned_folders = set()

    # =========================================================
    # 多开窗口支持：注册/注销共用这个 IndexManager 的窗口，以及把状态
    # 更新广播给全部还活着的窗口
    # =========================================================
    def add_linked_window(self, window):
        if window not in self.linked_windows:
            self.linked_windows.append(window)

    def remove_linked_window(self, window):
        if window in self.linked_windows:
            self.linked_windows.remove(window)

    def _broadcast_status(self, text):
        for w in self.linked_windows:
            try:
                w.status_label.setText(text)
            except RuntimeError:
                pass  # 窗口对应的 C++ 对象已经被销毁（窗口刚好在这一刻被关闭）

    def _broadcast_stats(self, count_text=None, size_text=None, time_text=None, failed_text=None):
        for w in self.linked_windows:
            try:
                if count_text is not None:
                    w.index_count_label.setText(count_text)
                if size_text is not None:
                    w.index_size_label.setText(size_text)
                if time_text is not None:
                    w.index_time_label.setText(time_text)
                if failed_text is not None:
                    w.index_failed_btn.setText(failed_text)
            except RuntimeError:
                pass

    # =========================================================
    # 索引扫描
    # =========================================================
    def _get_current_search_paths(self):
        """返回"搜索目录"标签栏里当前勾选的、真实存在的目录（规范化后）
        列表，可能有 0 个、1 个或多个。单独抽出来，因为 start_index()
        需要用它来判断"排在前面的目录到底是不是用户真的在搜的那几个"，
        跟 _get_index_folder_list() 用同一份取值逻辑。"""
        if not hasattr(self.window, "folder_scope_bar"):
            return []
        result = []
        for p in self.window.folder_scope_bar.get_checked_paths():
            if p and os.path.isdir(p):
                norm = os.path.normpath(p)
                if norm not in result:
                    result.append(norm)
        return result

    def _get_index_folder_list(self):
        """
        构建索引目录优先级列表：
        1. 当前标签栏里勾选的目录（最高优先，可能有多个）——保留这一档，
           是个几乎零成本的保险：不管数据库处于什么状态，只要用户在搜索，
           就该让他正在用的目录优先出结果，不用排在系统盘这种大目录后面等。
        2. 本地固定硬盘根目录（闲时扫，跳过网络盘、光驱、U盘）

        原来还有"历史搜索目录"这一档，现在去掉了——它存在的意义是
        "猜用户接下来可能搜哪个目录，提前给它优先权"，但真正搜索这个
        动作本身就会触发动态提权（ensure_folder_indexed_first），
        加上"已扫过就跳过"的记忆机制，这种预先猜测性质的排序已经没有
        实际收益，只会徒增复杂度。
        """
        folders = []

        # 当前勾选的目录优先，按标签栏里的显示顺序
        for current in self._get_current_search_paths():
            if current not in folders:
                folders.append(current)

        # 只枚举本地固定硬盘（DRIVE_FIXED = 3），跳过网络盘、光驱、U盘
        import ctypes
        import string
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for letter in string.ascii_uppercase:
            if bitmask & 1:
                drive = f"{letter}:\\"
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(drive)
                if drive_type == 3 and drive not in folders:  # 3 = DRIVE_FIXED
                    folders.append(drive)
            bitmask >>= 1

        return folders

    def start_index(self, force_target_rescan=True):
        """启动后台索引线程。

        force_target_rescan：是否无条件强制重扫"排在前面、且确实是用户
        当前勾选的那几个目录"（见下面的说明）。正常调用（首次启动、
        用户主动搜索触发 ensure_folder_indexed_first 之类）都应该保持
        默认的 True——这是"用户正在搜的目录永远最新"这份保险。

        但 _on_index_finished() 里"每扫完一轮就顺手检查一下是不是还有
        目录没扫完"的收尾调用必须传 False：那个收尾调用是每次扫描一
        结束就会自动触发的，如果还坚持"只要目标目录等于用户当前勾选的
        目录就无条件强制重扫"，只要用户一直没换勾选，就会变成"扫完
        →强制重扫目标目录→又扫完→又强制重扫……"的死循环，而且其它
        真正需要补扫的目录反而因为这个死循环一次都轮不到——这正是上一版
        真实出现过的问题。收尾场景只应该捞"确实还没被扫过"的目录，不该
        无条件强制重扫已经扫过的目标目录。
        """
        if self.index_thread and self.index_thread.isRunning():
            return
        folders = self._get_index_folder_list()
        if not folders:
            return

        # 排在前面的目录只有在它"确实是用户当前勾选的目录"这种情况下，
        # 才值得无条件优先重扫一遍——这是给用户正在用的目录一个"保证
        # 最新"的保险，这几个目录通常不大，重扫成本可以忽略。
        #
        # 但如果一个目录标签都没勾选，_get_index_folder_list() 会退化成
        # "本地固定硬盘里第一个盘符"（一般是 C:\），这纯粹是枚举顺序的
        # 副产品，不代表用户真的要优先看它。之前这里对 folders[0] 一律
        # 无条件强制重扫，导致只要 start_index() 被触发一次（哪怕只是
        # "排除目录管理"里改了一个跟 C 盘毫不相关的排除规则），C 盘就会
        # 被当成"目标目录"重新完整遍历一遍——即使这一盘早就扫过、没有
        # 任何文件变化（日志上表现为"目录 C:\ 所有文件已是最新，跳过"，
        # 看着像是在反复扫 C 盘，其实是白白多做了一次几十万文件的目录
        # 遍历，只是最后每个文件都被 mtime 比对跳过了而已）。现在改成：
        # 只有那些真的出现在用户当前勾选列表里的目录才强制重扫；否则跟
        # 其余目录一视同仁，也要检查是不是本次运行期间已经完整扫描过，
        # 扫过了就不再重新遍历一遍。
        current_search_paths = self._get_current_search_paths()
        if force_target_rescan and current_search_paths:
            target_list = [f for f in folders if os.path.normpath(f) in current_search_paths]
            candidates = [f for f in folders if os.path.normpath(f) not in current_search_paths]
        else:
            target_list = []
            candidates = folders

        # 当前处于排除状态的目录，压根不该算进"还需要扫描的候选"——
        # 上一版把"因为被排除而跳过"从 _fully_scanned_folders 里去掉之后
        # （见 search_thread.py 里的说明），这里如果不额外把"当前就是
        # 排除状态"的目录也过滤掉，会导致一个真正的死循环：一个目录只要
        # 处于排除状态，就永远不会被标记为"已扫描"，于是 _on_index_finished()
        # 里那个"扫完一轮顺手看看还有没有没扫完的"收尾调用，会一直把它
        # 当成"还没扫过、需要扫"，每次都重新拉起一轮索引线程，线程一启动
        # 就因为排除规则立刻跳过，然后又触发下一轮收尾检查……这样每秒
        # 能空转几十上百次。改成：不管有没有被扫描过，只要现在是排除
        # 状态，直接不列入候选——排除和"已扫描"是两个独立维度，不该混在
        # 一起判断。
        from config import get_exclude_folders, is_path_excluded
        exclude_folders = get_exclude_folders()
        target_list = [t for t in target_list if not is_path_excluded(t, exclude_folders)]
        remaining = [
            f for f in candidates
            if not self._is_folder_already_covered(f) and not is_path_excluded(f, exclude_folders)
        ]
        folders = target_list + remaining
        if not folders:
            # 所有候选目录本次运行期间都已经扫过了，没有真正需要新扫的
            # 目录——不需要为了"走个流程"空转一轮索引线程。
            log(">>> start_index() 被触发，但候选目录本次运行期间都已扫描过，跳过本轮")
            return

        self._launch_index_thread(folders)

    def _launch_index_thread(self, folders):
        """真正创建并启动 IndexThread 的公共逻辑：暂停实时监控、建线程、
        接信号、启动、更新托盘提示。start_index()（按目录优先级列表常规
        扫描）和 _flush_pending_targeted_scans()（排除设置变化后针对性
        补扫某几个具体目录）都走这一个入口，保证两条路径的收尾动作
        完全一致，不用维护两份几乎一样的代码。
        """
        # 索引任务要开始了，先暂停实时监控——扫描期间会大量写数据库，
        # 跟监听线程同时写容易互相打架；索引跑完（_on_index_finished）
        # 会重新启动监听，接手后续的变动侦测。
        self.stop_watcher()
        self.index_thread = IndexThread(folders)
        self.index_thread.finished_signal.connect(self._on_index_finished)
        self.index_thread.stats_signal.connect(self._on_index_stats)
        self.index_thread.folder_done_signal.connect(self._on_folder_done)
        self.index_thread.start()
        self.window.tray_icon.setToolTip("DWG 图纸搜索系统（索引中...）")

    def stop_index(self):
        """发送停止信号，不阻塞等待（线程会在处理完当前图纸后自然退出）"""
        if self.index_thread and self.index_thread.isRunning():
            self.index_thread.stop()

    def _scan_folders_now(self, folders):
        """直接扫描这几个指定目录，不经过 _get_index_folder_list() 的
        常规目录列表、也不查"本次运行期间是否已扫过"的记忆——目前只用
        在"取消排除某个目录后，立刻把这个目录本身补扫一遍"这一种场景，
        跟其它没变化的盘完全无关（不管 C、D、E、F 这次要不要扫，只
        精确针对这几个刚被取消排除的目录）。

        扫描本身走的还是 mtime 增量比对，已经索引过、没变化的文件
        几乎零开销，只有真正新纳入、以前从没扫过的文件才需要真正提取
        内容——所以不需要像以前那样为了"该重扫多大范围"去维护一套
        "祖先目录剔除"的记忆（原来的 _invalidate_scanned_ancestors）：
        这里直接、明确地告诉索引线程"就扫这几个目录"，不用去猜。

        如果这几个目录不在本地固定硬盘范围内（比如网络盘/U盘），
        软件不会主动扫它们——保持跟其它网络盘目录一样的行为：需要
        手动搜一下那个目录才会触发索引。
        """
        folders = [os.path.normpath(f) for f in folders if os.path.isdir(f)]
        if not folders:
            return
        for f in folders:
            if f not in self._pending_targeted_scans:
                self._pending_targeted_scans.append(f)

        if self.index_thread and self.index_thread.isRunning():
            # 当前有一轮扫描在跑，不强行打断它——排队等它自然跑完退出后
            # 再补这一轮，避免把正在进行的扫描过程硬生生打断。
            try:
                self.index_thread.finished_signal.disconnect(self._flush_pending_targeted_scans)
            except Exception:
                pass
            self.index_thread.finished_signal.connect(self._flush_pending_targeted_scans)
            return
        self._flush_pending_targeted_scans()

    def _flush_pending_targeted_scans(self):
        folders = self._pending_targeted_scans
        self._pending_targeted_scans = []
        if not folders:
            return
        self._launch_index_thread(folders)

    def _on_index_stats(self, scanned, estimated, remaining_secs):
        """收到索引统计信号，更新状态栏和托盘提示"""
        if estimated > 0:
            pct = min(100, int(scanned / estimated * 100))
        else:
            pct = 0

        if remaining_secs >= 3600:
            remain_str = f"剩余约 {remaining_secs // 3600} 小时 {(remaining_secs % 3600) // 60} 分钟"
        elif remaining_secs >= 60:
            remain_str = f"剩余约 {remaining_secs // 60} 分钟"
        elif remaining_secs > 0:
            remain_str = f"剩余约 {remaining_secs} 秒"
        else:
            remain_str = "即将完成"

        pct = min(100, int(scanned / estimated * 100)) if estimated > 0 else 0
        status = (
            f"后台索引中 | "
            f"{scanned:,}/{estimated:,} 张 ({pct}%) | "
            f"{remain_str} | 可随时搜索"
        )
        self._broadcast_status(status)

        tray_tip = (
            f"DWG 图纸搜索系统\n"
            f"{scanned:,}/{estimated:,} 张 ({pct}%)，{remain_str}"
        )
        self.window.tray_icon.setToolTip(tray_tip)

    def _on_folder_done(self, folder):
        """某个顶层目录真正扫完了（没有被中途打断/超时放弃），
        IndexThread 那边发的 folder_done_signal 接到这里。跟整条线程
        什么时候结束完全无关——哪怕这一轮扫描后面还要继续扫别的目录、
        甚至整条线程随后被打断，这个目录已经拿到的"扫过了"不会被
        收回。"""
        self._fully_scanned_folders.add(os.path.normpath(folder))

    def _on_index_finished(self):
        # 索引全部跑完后，尽力把 -wal 截断归零，释放磁盘占用。
        # 这只在索引任务结束这一刻触发一次，不影响索引过程中的读写速度；
        # 如果此刻正好有预览查询占着读快照，会自动放弃、不报错，不影响正常使用。
        try:
            db = DWGDatabase()
            db.checkpoint_wal()
            db.close()
        except Exception:
            pass
        self.refresh_stats()
        self.window.tray_icon.setToolTip("DWG 图纸搜索系统（就绪）")
        # 注意：这里不再有"整条线程自然跑完才批量标记 folder_list 里每个
        # 目录为已扫描"这一步了——哪个目录真正扫完，是在 _index_folder()
        # 里刚扫完那一刻就通过 folder_done_signal 实时上报、由 _on_folder_done()
        # 加进 _fully_scanned_folders 的（见那边的说明）。这样即使这一轮
        # 扫描中途被打断（比如用户搜了别的目录触发优先级重排），已经真正
        # 扫完的那几个目录也不会因为"整条线程没有自然跑完"而白白扫了个寂寞、
        # 下次还要重新再扫一遍。
        # 如果状态栏还在显示"后台索引中"才更新，否则保持现有状态
        current = self.window.status_label.text()
        if "后台索引中" in current:
            self._broadcast_status("索引完成，就绪 | 可随时搜索")
        elif "就绪 | 可随时搜索" in current:
            self._broadcast_status("索引已是最新，就绪 | 可随时搜索")
        # 500ms 后把 index_thread 引用清空，纯粹是给 Qt 一点缓冲时间，
        # 避免这个刚跑完的线程对象在还没被 Qt 内部完全收尾之前就被 Python
        # 垃圾回收掉。但下面马上就可能因为 start_index() 追一轮扫描而把
        # self.index_thread 重新指向一个全新的、正在跑的线程对象——如果
        # 到时候不加判断地直接 setattr(None)，会把这个新线程的引用给
        # 冲掉，后面所有 isRunning() 检查都会误判成"没在跑"。这里先把
        # "这一刻要清空的到底是哪个线程对象"锁定下来，500ms 后只有
        # self.index_thread 还是这同一个（没被新一轮扫描替换掉）才真的
        # 清空。
        finished_thread = self.index_thread
        QTimer.singleShot(
            500,
            lambda: setattr(self, 'index_thread', None)
            if self.index_thread is finished_thread else None
        )

        # 收尾追一次 start_index()：如果刚跑完这一轮是被中途叫停的（比如
        # 用户在扫描过程中又去搜了别的目录，触发了"当前搜索目录优先"把
        # 这一轮打断——见 ensure_folder_indexed_first），排在后面、还没
        # 轮到的目录这时候并不会被标记成"已扫描"（上面第 261-264 行的
        # 判断刻意排除了这种情况），但也没有任何自动重试的机制去把它们
        # 捞回来——之前这里就单纯忘了处理"打断之后怎么办"，导致取消排除
        # 一个目录、还没扫完就被别的操作打断的话，这个目录会一直卡在
        # "既没扫完、也没人再理它"的状态，永远不会真正被收录进去。
        # start_index() 本身是幂等的：真没有需要扫的目录时，只会打一行
        # "跳过本轮"的日志，不会白白空转一轮索引线程；只有真的还有目录
        # 没扫完时，才会真正接着扫（它内部会自己调用 stop_watcher()）。
        # 只有确认这次没有触发新一轮扫描，才轮到这里启动实时监控——
        # 不然会出现"刚启动监控又立刻被下一轮扫描叫停"这种没意义的空转。
        # 注意这里必须传 force_target_rescan=False，见 start_index() 的
        # 参数说明——否则只要用户的搜索框内容没变，每扫完一轮都会被这里
        # 强制重扫一遍当前搜索目录，变成停不下来的死循环。
        self.start_index(force_target_rescan=False)
        if not (self.index_thread and self.index_thread.isRunning()):
            self.start_watcher()

    # =========================================================
    # 实时文件监控
    # =========================================================
    def start_watcher(self):
        """启动实时监控，监视范围沿用索引目录列表（含排除目录规则）"""
        folders = self._get_index_folder_list()
        if not folders:
            return
        self.watcher_thread = FileWatcherThread(folders)
        self.watcher_thread.file_updated_signal.connect(self._on_watcher_update)
        self.watcher_thread.start()

    def stop_watcher(self):
        """停止实时监控。不在主线程阻塞等待它真正退出——之前用
        watcher.wait(3000) 会让主界面线程原地等最多3秒，Qt的界面重绘
        也是靠主线程处理的，主线程被卡住那几秒里，连状态栏文字都没机会
        刷新，表现出来就是"界面卡死、什么都不动"，比单纯索引慢得多更
        容易被当成软件卡住了。改成只发停止信号，让监听线程自己在后台
        默默收尾，不用主界面陪它等。
        收尾期间监听线程可能跟紧接着启动的新索引线程有极短暂的重叠
        写库，SQLite本身对这种短暂的并发写有重试机制兜底，风险比让
        界面冻结几秒要小得多。
        """
        watcher = self.watcher_thread
        self.watcher_thread = None
        if watcher and watcher.isRunning():
            watcher.stop()
            # 不能在这里就彻底放开引用：此时 run() 里的循环可能还没真正
            # 退出（还在等 observer 收尾），如果没其他引用挡着，垃圾
            # 回收器可能会在它还在跑的时候就把这个 QThread 对象销毁掉，
            # 直接触发 Qt 的 "QThread: Destroyed while thread is still running"
            # 致命错误，严重时会把整个进程带崩溃退出。先把它扔进
            # _retiring_threads 养着，接上它的 finished 信号（QThread 内置，
            # run() 真正返回后才会发），等它真正跑完了再从收容所里移除，
            # 这时候才真正允许被垃圾回收。
            self._retiring_threads.append(watcher)
            watcher.finished.connect(lambda w=watcher: self._retire_done(w))

    def _retire_done(self, thread_obj):
        """收容所里的线程真正跑完了，从列表里移除，让它可以被正常垃圾回收。"""
        if thread_obj in self._retiring_threads:
            self._retiring_threads.remove(thread_obj)

    def _on_watcher_update(self, message):
        """收到实时监控的变动同步提示，显示在状态栏"""
        self._broadcast_status(message)

    # =========================================================
    # 索引管理面板
    # =========================================================
    def refresh_stats(self):
        try:
            db = DWGDatabase()
            stats = db.get_index_stats()
            db.close()
            self._broadcast_stats(
                count_text=f"已索引图纸：{stats['total_count']:,} 张",
                size_text=f"数据库大小：{stats['db_size_mb']:.2f} MB",
                time_text=f"最后更新：{stats['last_update_str']}",
            )
            failed = stats.get('failed_count', 0)
            if hasattr(self.window, 'index_failed_btn'):
                self._broadcast_stats(failed_text=f"查看失败文件 ({failed:,})")
        except Exception as e:
            self._broadcast_stats(
                count_text="已索引图纸：读取失败",
                size_text="数据库大小：—",
                time_text=f"错误：{e}",
            )

    def show_failed_files(self):
        """弹窗展示"上次索引提取内容失败"的图纸清单及失败原因。
        这里只负责读库和展示，不做任何"自动重试"之类的操作——
        失败原因五花八门（图纸本身损坏、格式过新过旧、解析超时等），
        用户看到列表后自己决定要不要处理（比如用 AutoCAD 打开修复、
        或者确认这就是个坏文件，不用管）。每次打开都现查一次库，不缓存，
        保证看到的总是最新状态（比如刚刚重新索引完一批之后）。
        """
        from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                                     QTableWidget, QTableWidgetItem, QPushButton,
                                     QLabel, QHeaderView, QAbstractItemView)

        window = self.window
        try:
            db = DWGDatabase()
            failed_list = db.get_failed_files()
            db.close()
        except Exception as e:
            QMessageBox.warning(window, "错误", f"读取失败文件列表出错：\n{e}")
            return

        dialog = QDialog(window)
        dialog.setWindowTitle(f"提取失败的图纸（{len(failed_list)} 张）")
        dialog.setMinimumSize(760, 420)
        dialog.setWindowModality(Qt.ApplicationModal)
        layout = QVBoxLayout(dialog)

        hint = QLabel("这些图纸的文件名已正常索引（能被文件名搜到），只是图纸内容"
                     "解析失败，所以内容搜索找不到里面的文字。常见原因：图纸本身已"
                     "损坏、版本过新/过旧解析工具不支持、或单张解析超时。")
        hint.setStyleSheet("color: gray; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        if not failed_list:
            layout.addWidget(QLabel("目前没有提取失败的图纸。"))
            close_btn = QPushButton("关闭")
            close_btn.clicked.connect(dialog.accept)
            row = QHBoxLayout()
            row.addStretch()
            row.addWidget(close_btn)
            layout.addLayout(row)
            dialog.exec_()
            return

        table = QTableWidget()
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["文件路径", "失败原因"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table.setColumnWidth(0, 380)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setRowCount(len(failed_list))
        for i, item in enumerate(failed_list):
            path_item = QTableWidgetItem(item["dwg_path"])
            path_item.setToolTip(item["dwg_path"])
            table.setItem(i, 0, path_item)
            err_item = QTableWidgetItem(item["error_msg"])
            err_item.setToolTip(item["error_msg"])
            table.setItem(i, 1, err_item)
        layout.addWidget(table, 1)

        def _open_containing_folder():
            selected = table.selectedItems()
            if not selected:
                return
            row_idx = selected[0].row()
            path = table.item(row_idx, 0).text()
            # 直接把文件本身的完整路径交给 open_containing_folder()，
            # 不要在这里先用 os.path.dirname() 截成目录再传——传目录的话
            # Explorer 只会打开这个目录、不会选中里面那个文件，跟搜索
            # 结果列表右键"打开文件所在位置"的效果不一样（之前就是这么
            # 两边不一致的）。统一调用同一个函数，行为才能保证一致。
            from helpers import open_containing_folder
            open_containing_folder(path, parent=dialog)

        table.cellDoubleClicked.connect(lambda *_: _open_containing_folder())

        btn_row = QHBoxLayout()
        open_folder_btn = QPushButton("打开所在目录")
        open_folder_btn.clicked.connect(_open_containing_folder)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dialog.accept)
        btn_row.addWidget(open_folder_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        dialog.exec_()

    def change_db_path(self):
        """弹窗显示当前数据库路径，并提供更改选项"""
        from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                                     QLabel, QPushButton, QLineEdit)
        from config import get_default_db_path

        window = self.window
        dialog = QDialog(window)
        dialog.setWindowTitle("数据库存储路径")
        dialog.setMinimumWidth(540)
        dialog.setWindowModality(Qt.ApplicationModal)
        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)

        # 当前路径
        layout.addWidget(QLabel("当前存储路径："))
        current_path_edit = QLineEdit(get_db_path())
        current_path_edit.setReadOnly(True)
        current_path_edit.setStyleSheet("color: gray;")
        layout.addWidget(current_path_edit)

        # 默认路径提示
        default_path = get_default_db_path()
        hint = QLabel(f"默认路径：{default_path}")
        hint.setStyleSheet("font-size: 11px; color: gray;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 按钮行
        btn_row = QHBoxLayout()
        change_btn = QPushButton("更改路径")
        reset_btn  = QPushButton("恢复默认")
        close_btn  = QPushButton("关闭")
        btn_row.addWidget(change_btn)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        def do_change():
            current = get_db_path()
            new_dir = QFileDialog.getExistingDirectory(
                dialog, "选择数据库存储目录", os.path.dirname(current)
            )
            if not new_dir:
                return
            new_path = os.path.join(new_dir, "dwg_index.db")
            if new_path == current:
                return
            reply = QMessageBox.question(
                dialog, "确认更改存储路径",
                f"数据库将存储到：\n{new_path}\n\n"
                f"注意：切换路径后原数据库不会自动迁移，\n"
                f"需要重新建立索引。是否继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
            self.stop_index()
            save_db_path(new_path)
            current_path_edit.setText(new_path)
            # 切换到了一个全新的数据库，原来的数据不会带过去，
            # "哪些目录扫过了"这个记忆是针对旧数据库的，必须一起作废
            self._fully_scanned_folders.clear()
            self.refresh_stats()
            self._broadcast_status("数据库路径已更新，正在重新建立索引...")
            QTimer.singleShot(1000, self.start_index)

        def do_reset():
            if get_db_path() == default_path:
                QMessageBox.information(dialog, "提示", "当前已是默认路径，无需恢复。")
                return
            reply = QMessageBox.question(
                dialog, "确认恢复默认",
                f"将恢复到默认路径：\n{default_path}\n\n"
                f"需要重新建立索引。是否继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
            self.stop_index()
            save_db_path(default_path)
            current_path_edit.setText(default_path)
            self._fully_scanned_folders.clear()
            self.refresh_stats()
            self._broadcast_status("已恢复默认路径，正在重新建立索引...")
            QTimer.singleShot(1000, self.start_index)

        change_btn.clicked.connect(do_change)
        reset_btn.clicked.connect(do_reset)
        close_btn.clicked.connect(dialog.accept)
        dialog.exec_()

    def manage_exclude_folders(self):
        from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                                     QListWidget, QListWidgetItem, QPushButton, QLabel, QCheckBox)
        from config import (get_exclude_folders, save_exclude_folders, DEFAULT_EXCLUDE_FOLDERS,
                            get_user_exclude_folders, get_exclude_system_dirs_enabled,
                            save_exclude_system_dirs_enabled, get_exclude_custom_enabled,
                            save_exclude_custom_enabled)

        window = self.window
        dialog = QDialog(window)
        dialog.setWindowTitle("排除目录管理")
        dialog.setMinimumWidth(520)
        dialog.setMinimumHeight(380)
        dialog.setWindowModality(Qt.ApplicationModal)
        layout = QVBoxLayout(dialog)

        # 排除之前的"实际生效"状态，用来跟保存时的新状态做对比，
        # 决定哪些目录要新增排除、哪些要恢复。
        current_excludes = get_exclude_folders()

        hint = QLabel("内置默认排除目录不可删除（下方仍会列出）；下面两个开关分别控制"
                      "系统目录/自定义目录排除是否生效，关闭后对应条目会变灰（不会被删除，"
                      "重新勾选即可恢复）。保存后立即生效，不用重启软件：新排除的目录立刻从"
                      "搜索结果里隐藏；新取消排除的目录会自动触发重新扫描把里面的图纸补上（仅限本地"
                      "固定硬盘或当前搜索框里的目录，网络盘/U盘需要手动搜一下才会触发）。")
        hint.setStyleSheet("color: gray; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        toggle_row = QHBoxLayout()
        chk_system = QCheckBox("排除系统目录")
        chk_system.setChecked(get_exclude_system_dirs_enabled())
        chk_custom = QCheckBox("启用自定义")
        chk_custom.setChecked(get_exclude_custom_enabled())
        toggle_row.addWidget(chk_system)
        toggle_row.addWidget(chk_custom)
        toggle_row.addStretch()
        layout.addLayout(toggle_row)

        list_widget = QListWidget()

        NORMAL_COLOR = QColor("#000000")    # 开关开着（默认）：正常黑字
        DISABLED_COLOR = QColor("#999999")  # 开关关掉：变灰，表示暂时不生效

        def _refresh_item_colors():
            for i in range(list_widget.count()):
                item = list_widget.item(i)
                is_builtin = item.data(Qt.UserRole) == "builtin"
                enabled = chk_system.isChecked() if is_builtin else chk_custom.isChecked()
                item.setForeground(NORMAL_COLOR if enabled else DISABLED_COLOR)

        for p in DEFAULT_EXCLUDE_FOLDERS:
            item = QListWidgetItem(p)
            item.setData(Qt.UserRole, "builtin")
            item.setFlags(item.flags() & ~Qt.ItemIsSelectable)  # 内置不可选
            list_widget.addItem(item)
        for p in get_user_exclude_folders():
            item = QListWidgetItem(p)
            item.setData(Qt.UserRole, "custom")
            list_widget.addItem(item)
        _refresh_item_colors()
        layout.addWidget(list_widget)

        chk_system.toggled.connect(_refresh_item_colors)
        chk_custom.toggled.connect(_refresh_item_colors)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("添加目录")
        del_btn = QPushButton("删除选中")
        del_btn.setStyleSheet("color: red;")
        close_btn = QPushButton("确定")

        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        def add_folder():
            new_dir = QFileDialog.getExistingDirectory(dialog, "选择要排除的目录")
            if not new_dir:
                return
            new_dir = os.path.normpath(new_dir)
            # 检查是否已存在
            for i in range(list_widget.count()):
                if os.path.normpath(list_widget.item(i).text()) == new_dir:
                    QMessageBox.information(dialog, "提示", "该目录已在排除列表中。")
                    return
            item = QListWidgetItem(new_dir)
            item.setData(Qt.UserRole, "custom")
            list_widget.addItem(item)
            _refresh_item_colors()

        def del_folder():
            selected = list_widget.selectedItems()
            if not selected:
                return
            for item in selected:
                if item.data(Qt.UserRole) != "builtin":
                    list_widget.takeItem(list_widget.row(item))

        def save_and_close():
            new_custom_list = [
                list_widget.item(i).text() for i in range(list_widget.count())
                if list_widget.item(i).data(Qt.UserRole) != "builtin"
            ]

            save_exclude_folders(new_custom_list)
            save_exclude_system_dirs_enabled(chk_system.isChecked())
            save_exclude_custom_enabled(chk_custom.isChecked())

            # 用"实际生效"的新旧列表做对比（考虑了两个开关的状态），
            # 而不是单纯比列表条目本身有没有增删——关掉开关也要让对应
            # 目录的搜索可见性跟着恢复，不是只有增删条目才算变化。
            new_effective_excludes = get_exclude_folders()
            newly_excluded = [p for p in new_effective_excludes if p not in current_excludes]
            newly_included = [p for p in current_excludes if p not in new_effective_excludes]

            dialog.accept()  # 配置已经存盘了，弹窗可以先关

            if not newly_excluded and not newly_included:
                return

            log(f">>> 排除设置已保存，新增排除 {len(newly_excluded)} 个目录"
                f"{newly_excluded if newly_excluded else ''}，"
                f"取消排除 {len(newly_included)} 个目录"
                f"{newly_included if newly_included else ''}")

            # 排除范围是纯内存过滤：config.json 在上面几行已经存盘了，
            # 这一刻搜索/扫描/监控三处只要现查一下就已经全部生效，不需要
            # 再等任何后台线程去追平数据库——不会再有"数据库标记还没
            # 更新成功"这种半吊子状态，自然也不会有相关的报错弹窗。
            parts = []
            if newly_excluded:
                parts.append(f"{len(newly_excluded)} 个目录已排除")
            if newly_included:
                parts.append(f"{len(newly_included)} 个目录已恢复，正在重新扫描...")
            self._broadcast_status("，".join(parts))

            # 结果表格里如果正显示着上一次搜索的结果，排除范围一变，里面
            # 完全可能混着刚被排除掉的目录。不重新搜一次的话，用户看到的
            # 列表跟"排除已经生效"这件事对不上，容易误以为没生效。这里
            # 静默重跑一次上一次搜索（复用当前搜索框里的关键词/目录/
            # 筛选条件），如果用户还没搜过（表格是空的），什么都不做，
            # 避免无意义地弹出"请输入关键词"这种警告。
            if window.results_model.rowCount() > 0:
                log(">>> 排除范围已更新，重新执行上一次搜索以刷新结果列表")
                window.search_manager.start_search()

            if newly_included:
                # 取消排除不代表数据库里已经有这个目录的记录——如果它之前
                # 一直被排除、从未被扫描过，数据库里根本没它的内容，必须
                # 真正触发一次扫描才能把里面的图纸收录进来。这里不等软件
                # 重启，也不等下次手动搜索，直接针对这几个目录补扫一次。
                # _scan_folders_now() 内部会重启实时监控线程，newly_excluded
                # 的部分不需要另外处理了。
                self._scan_folders_now(newly_included)
            elif newly_excluded:
                # 只有新增排除、没有取消排除的情况：不需要触发扫描，但
                # 实时监控线程监听哪些目录是它自己线程启动那一刻定死的
                # （见 FileWatcherThread.run() 里的说明），不会因为配置文件
                # 变了就自动更新——不重启的话，刚排除的这个目录会继续被
                # 监听，唯一区别是它产生的文件变动会在写入数据库那一步被
                # 过滤掉（不会写错数据），但监听本身的开销（尤其是大盘）
                # 白白浪费了，而且直觉上"排除了却看着还在监听"也容易让人
                # 怀疑是不是没生效。这里直接重启一次监听，让它按最新的
                # 排除范围重新决定要不要监听这个目录。
                self.stop_watcher()
                self.start_watcher()

        add_btn.clicked.connect(add_folder)
        del_btn.clicked.connect(del_folder)
        close_btn.clicked.connect(save_and_close)

        dialog.exec_()

    def clear_index(self):
        if self._clear_index_thread and self._clear_index_thread.isRunning():
            QMessageBox.information(self.window, "提示", "清空索引正在进行中，请稍候...")
            return

        reply = QMessageBox.question(
            self.window, "确认清空",
            "确定要清空全部索引吗？\n清空后将重新扫描所有图纸，耗时较长。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        # ==== 打点：诊断"点清空并重建索引后卡在提示上不动"用，排查完
        # 可以删掉，但建议留着——这几行往后遇到类似反馈时排查会方便很多 ====
        log("[清空索引] 用户确认清空，index_thread 是否在跑: "
            f"{bool(self.index_thread and self.index_thread.isRunning())}")

        if self.index_thread and self.index_thread.isRunning():
            # 后台索引正在跑：不能一边让旧线程继续写数据库、一边在主线程
            # 清空同一个数据库文件，两边会打架。之前这里是发完停止信号后
            # 固定等1秒就硬着头皮清空+重启，但旧线程有没有真正退出跟这
            # 1秒毫无关系（尤其提取内容现在是8个并发，一批没跑完之前线程
            # 退不出来），等不到就导致界面卡在"正在重新建立索引..."，
            # 旧线程实际还在后台孤零零地继续跑。改成真正等旧线程发出
            # "我已经退出了"这个信号之后，再清空、再启动新线程，不靠猜。
            self._broadcast_status("正在停止当前索引，准备清空...")
            try:
                self.index_thread.finished_signal.disconnect()
            except Exception:
                pass
            self.index_thread.finished_signal.connect(self._do_clear_index_and_restart)
            log("[清空索引] 已发送停止信号，等待 index_thread.finished_signal...")
            self.stop_index()
        else:
            log("[清空索引] index_thread 当前没在跑，直接开始清空")
            self._do_clear_index_and_restart()

    def _do_clear_index_and_restart(self):
        """真正执行清空+重建索引。如果之前有旧的索引线程，这个函数只有
        在旧线程确认已经完全退出（发出 finished_signal）之后才会被调用，
        避免清库跟旧线程残留的写操作互相打架。

        清空动作（DELETE 全表 + VACUUM）本身放到 ClearIndexThread 后台
        线程里跑，不在这里（主线程/UI线程）同步执行——VACUUM 需要重写
        整个数据库文件，库越大越慢，同步跑会把 Qt 事件循环卡死，界面
        表现为"未响应"。这里只负责启动后台线程、更新状态文字，真正的
        收尾（作废扫描记忆、刷新统计、开始重新建索引）挪到
        _on_clear_index_done 里，等后台线程真正跑完再做。"""
        if self.index_thread is not None:
            try:
                self.index_thread.finished_signal.disconnect(self._do_clear_index_and_restart)
            except Exception:
                pass

        log("[清空索引] 旧索引线程已确认退出（或本来就没在跑），"
            "开始启动 ClearIndexThread 清空数据库...")
        self._broadcast_status("正在清空索引数据库，请稍候...")
        self._clear_index_thread = ClearIndexThread()
        self._clear_index_thread.finished_signal.connect(self._on_clear_index_done)
        self._clear_index_thread.error_signal.connect(self._on_clear_index_error)
        self._clear_index_thread.start()

    def _on_clear_index_done(self):
        """ClearIndexThread 真正清空完数据库之后才会被调用。"""
        log("[清空索引] ClearIndexThread 清空完成，准备重新开始建立索引")
        # 数据库已经清空，之前记的"哪些目录扫过了"这个记忆也必须一起
        # 作废——不然会误判成"不用扫"，那部分目录在数据库里其实已经
        # 什么都没有了，会一直空着，直到下次软件重启才会被补上
        self._fully_scanned_folders.clear()
        self.refresh_stats()
        self._broadcast_status("索引已清空，正在重新建立...")
        self.start_index()

    def _on_clear_index_error(self, error_msg):
        QMessageBox.warning(self.window, "错误", f"清空索引失败：\n{error_msg}")

    # =========================================================
    # 供搜索流程调用：确保正在搜的目录被优先扫描
    # =========================================================
    def _is_folder_already_covered(self, folder):
        """判断某个目录是不是已经在"本次运行期间已完整扫描"的记忆范围内。
        不只是精确匹配同一个目录，还要考虑"这个目录其实是某个已经扫过的
        更大目录的子目录"——扫描是递归遍历的，扫一个大目录的时候，
        它旗下所有子目录早就一起被摸过一遍了，子目录没必要再单独扫一次。
        """
        from config import path_is_within
        return any(path_is_within(folder, scanned) for scanned in self._fully_scanned_folders)

    def ensure_folder_indexed_first(self, folder):
        """确保当前搜索目录是索引优先级最高的"""
        norm_folder = os.path.normpath(folder)

        # 先判断这个目录是不是已经真正扫过了（自己扫过，或者它的某个
        # 上级目录已经扫过），不管当前有没有别的扫描正在跑，只要已经
        # 覆盖了就什么都不用做——之前这个判断只在"当前没有扫描线程在跑"
        # 这一个分支里做，下面"有扫描线程正在跑"的分支完全没检查，
        # 导致的实际后果是：只要背景里恰好有一轮扫描在跑（哪怕扫的是
        # 别的、更需要花时间的大盘），随手搜一个早就扫过的目录（哪怕是
        # 一个已经扫过的大目录下面的某个子目录）都会把它硬生生打断、
        # 重新扫一遍这个其实根本不需要扫的目录——白白浪费时间不说，
        # 被打断的那个真正需要扫完的大盘还要再等下一轮才能继续。
        if self._is_folder_already_covered(norm_folder):
            return

        if self.index_thread and self.index_thread.isRunning():
            if self.index_thread.folder_list and \
               os.path.normpath(self.index_thread.folder_list[0]) == norm_folder:
                return  # 已经是第一个，不需要重启
            # 不是第一个：非阻塞停止，等线程自然退出后 _on_index_finished 会收尾
            self.index_thread.stop()
            # 连接 finished 信号，线程退出后自动重启（带新优先级）
            try:
                self.index_thread.finished_signal.disconnect(self._on_index_finished)
            except Exception:
                pass
            self.index_thread.finished_signal.connect(self._on_index_restarted)
            return

        QTimer.singleShot(300, self.start_index)

    def _on_index_restarted(self):
        """旧索引线程退出后，重新连接信号并启动新线程"""
        try:
            self.index_thread.finished_signal.disconnect(self._on_index_restarted)
        except Exception:
            pass
        self.refresh_stats()
        self.start_index()  # 重启，当前目录已是最高优先级

    # =========================================================
    # 程序退出
    # =========================================================
    def shutdown(self):
        """程序退出前调用，把索引/监控/清空线程妥善收尾。search_thread、
        预览数据库连接、托盘图标这些不属于索引管理的收尾工作，由调用方
        （MainWindow._quit_app）自己处理。"""
        self.stop_watcher()

        if self.index_thread and self.index_thread.isRunning():
            self._broadcast_status("正在安全退出，清理后台进程...")
            self.index_thread.stop()
            # 给线程最多8秒时间走完 finally 块
            if not self.index_thread.wait(8000):
                # 超时了才强制终止。注意：IndexThread.run() 内部用
                # ThreadPoolExecutor 并发跑提取子进程，QThread.terminate()
                # 只能杀掉 Qt 这一层线程本体，杀不到线程池里那几个还在
                # 阻塞等 subprocess.run() 返回的 worker 线程——这几个是
                # Python 原生线程，不受 Qt 管辖。terminate() 这里仍然调用，
                # 是为了让 Qt 自己这层尽快标记为已停止，但不能指望它能让
                # 进程真正退出干净。
                self.index_thread.terminate()
                self.index_thread.wait(1000)

        if self._clear_index_thread and self._clear_index_thread.isRunning():
            # 清空索引（DELETE + VACUUM）正在往数据库文件里写数据，不能像
            # 上面那样直接 terminate/kill——VACUUM 中途被硬生生打断有损坏
            # 数据库文件的风险。这里老老实实等它自己跑完，给足15秒
            # （VACUUM 再慢也很少超过这个量级）。
            self._broadcast_status("正在等待索引清空完成，请稍候...")
            self._clear_index_thread.wait(15000)

        # 收容所里养着的那些（比如刚刚 stop_watcher 发完停止信号、还没真正
        # 退出的监听线程）程序退出前也简单等一下，避免在退出这个时间点也撞上
        # 同样的 "QThread: Destroyed while thread is still running" 问题。不用等很久，
        # 监听线程本身收尾很快，这里只是多一层保险。
        for t in list(self._retiring_threads):
            t.wait(2000)