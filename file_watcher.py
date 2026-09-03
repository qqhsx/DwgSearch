# file_watcher.py
"""
实时监控已索引目录里的 DWG 文件变动（新增/修改/删除/改名）。

跟启动时的增量扫描是互补关系，不是替代：
- 启动扫描负责补"软件关闭这段时间"发生的变动（离线期间的差价）
- 这个线程负责软件运行期间的"实时"更新，只处理真正发生变动的
  那一个文件，不用像扫描那样遍历整个目录树

依赖第三方库 watchdog（pip install watchdog），底层封装了 Windows
自带的目录变更通知机制——没有变动的时候完全静止、不占用CPU，只有
文件真的发生变化时，操作系统才会主动唤醒程序去处理。
"""
import os
import time
import traceback
import threading
from PyQt5.QtCore import QThread, pyqtSignal

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    # 没装 watchdog 库时，让实时监控功能优雅降级为"不可用"，
    # 不影响软件其他功能正常使用（毕竟这是可选的增强功能）
    WATCHDOG_AVAILABLE = False
    Observer = None
    FileSystemEventHandler = object

from database import DWGDatabase
from log_utils import log
from config import get_extractor_path, get_exclude_folders, is_path_excluded
from helpers import extract_dwg_text_via_exe, read_dwg_version_tag

# 文件改动后要"安静"这么久没有新动静，才认为它真的写完了，再去读取内容。
# 避免正好在别的程序还没保存完的中途，就抢先读到写一半的半成品文件。
DEBOUNCE_SECONDS = 2.0

# 每隔多久检查一次有没有"安静下来、可以处理"的变动
CHECK_INTERVAL_SECONDS = 1.0


class _DwgEventHandler(FileSystemEventHandler):
    """
    watchdog 原生事件回调，跑在 watchdog 自己开的操作系统线程里。
    这里只做最轻量的"记一笔时间戳"，不做任何耗时操作（不解析文件、
    不碰数据库），避免拖慢系统对文件事件的处理。真正的处理放在
    FileWatcherThread 自己的循环里，隔一段时间统一检查、批量处理。
    """
    def __init__(self, pending, lock):
        super().__init__()
        self._pending = pending  # dict: 文件路径 -> ('changed'|'deleted', 记录时间)
        self._lock = lock

    def _touch(self, path, kind):
        if not path.lower().endswith(".dwg"):
            return
        with self._lock:
            self._pending[path] = (kind, time.time())

    def on_created(self, event):
        if not event.is_directory:
            self._touch(event.src_path, "changed")

    def on_modified(self, event):
        if not event.is_directory:
            self._touch(event.src_path, "changed")

    def on_deleted(self, event):
        if not event.is_directory:
            self._touch(event.src_path, "deleted")

    def on_moved(self, event):
        if not event.is_directory:
            self._touch(event.src_path, "deleted")
            self._touch(event.dest_path, "changed")


class FileWatcherThread(QThread):
    """
    对一批目录做实时监控。跟索引扫描线程（IndexThread）不是同时跑的关系：
    每次真正做全量/增量扫描前，应该先暂停这个线程，扫描完再重新启动，
    避免两边同时写同一个数据库文件。
    """
    # 参数：给状态栏显示用的提示文字
    file_updated_signal = pyqtSignal(str)

    def __init__(self, folders, parent=None):
        super().__init__(parent)
        self.folders = folders
        self._pending = {}
        self._lock = threading.Lock()
        self._is_running = True
        self._observer = None

    def stop(self):
        self._is_running = False
        if self._observer is not None:
            try:
                self._observer.stop()
            except Exception:
                pass

    def run(self):
        if not WATCHDOG_AVAILABLE:
            log(">>> 未安装 watchdog 库，实时监控功能不可用（pip install watchdog 后重启软件即可启用）")
            return

        exclude_folders = get_exclude_folders()
        handler = _DwgEventHandler(self._pending, self._lock)
        self._observer = Observer()

        watched_count = 0
        for folder in self.folders:
            if not os.path.isdir(folder):
                continue
            # 排除目录（跟索引管理那边共用同一份配置）不监听，省掉一批
            # 肯定不会有DWG图纸的目录的监听开销；按路径分隔符严格对齐
            # 判断（不用简单 startswith），避免把 "C:\\FooBar" 误判成
            # 命中排除规则 "C:\\Foo"。
            if is_path_excluded(folder, exclude_folders):
                continue
            try:
                self._observer.schedule(handler, folder, recursive=True)
                watched_count += 1
            except Exception as e:
                # 单个目录监听失败（比如权限不够、目录在扫描过程中被删了）
                # 不影响其余目录继续监听
                log(f">>> 监听目录失败，跳过: {folder} ({e})")

        if watched_count == 0:
            log(">>> 没有可监听的目录，本次实时监控未启动")
            return

        log(f">>> 实时监控已启动，正在监听 {watched_count} 个目录")
        self._observer.start()

        exe_path = get_extractor_path()
        extractor_available = os.path.exists(exe_path)
        db = DWGDatabase()

        # 用于定期打印"我还活着"心跳日志、以及定期检查 watchdog 底层
        # 监听线程是否还健康——这两个都是专门为了排查"监控忽然失效，
        # 但日志一片空白、看不出原因"这类问题加的诊断信息。不这样做的
        # 话，日志安静下来的时候没法区分到底是"真的没有文件变动"还是
        # "监控本身已经死了、只是没人告诉你"，这两种情况在日志上长得
        # 一模一样（都是"没有输出"）。
        last_heartbeat = time.time()
        HEARTBEAT_INTERVAL_SECONDS = 300  # 5分钟一次，避免日志刷屏

        try:
            while self._is_running:
                time.sleep(CHECK_INTERVAL_SECONDS)
                try:
                    self._process_settled_events(db, exe_path, extractor_available)
                except Exception:
                    # 这里之前没有 try/except：任何一次处理过程中冒出的、
                    # 没被内层代码单独捕获的异常（最典型的是数据库瞬时
                    # 写冲突），都会直接把这个 while 循环冲出去，整个
                    # 监控线程就此悄悄退出——不会有任何弹窗报错（QThread
                    # 内部异常默认不会冒泡成界面上能看到的崩溃提示），
                    # 日志里除了 finally 那句"已停止"，看不出真正原因，
                    # 而且监控从此彻底失效，不会自己恢复，直到下一次
                    # 完整索引扫描或者重启软件才会被重新拉起来。
                    # 现在改成捕获、打印完整堆栈、继续循环——不管是数据库
                    # 冲突还是别的什么意外，都不该让整个监控功能因为
                    # 处理某一批事件时出的问题就永久失效。
                    log(f">>> 实时监控处理事件时发生未预期异常，已忽略并继续监控：\n{traceback.format_exc()}")

                now = time.time()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                    last_heartbeat = now
                    if self._observer is not None and not self._observer.is_alive():
                        # watchdog 底层监听线程自己意外退出了（比如 Windows
                        # 的 ReadDirectoryChangesW 通知缓冲区溢出等已知的
                        # 边界情况），我们自己这层的 while 循环是感知不到
                        # 的——它只是每隔一秒检查一下 self._pending 里有没有
                        # 攒够可处理的事件；底层监听真的死了的话，
                        # self._pending 会一直空着，表现出来就是"安安静静、
                        # 没有任何变动"，实际上监控已经彻底停摆了，新文件
                        # 变动再也不会被感知到。
                        log(">>> ⚠️ 实时监控的底层监听线程已意外退出，监控实际已失效！"
                            "建议重新触发一次索引扫描来重启监控（或重启软件）。")
                    else:
                        log(">>> 实时监控运行正常（心跳）")
        finally:
            try:
                self._observer.stop()
                self._observer.join(timeout=3)
            except Exception:
                pass
            db.close()
            log(">>> 实时监控已停止")

    def _process_settled_events(self, db, exe_path, extractor_available):
        """只处理"已经安静了 DEBOUNCE_SECONDS 秒没有新动静"的文件。

        每次进来都重新读一次排除目录配置（而不是只在 run() 刚启动时读一次
        存死）——这个方法本身每隔 CHECK_INTERVAL_SECONDS（1秒）就会被调一次，
        相当于排除规则改了之后最多 1 秒内就能在实时监控这边生效，不需要
        重启监听线程。这个重新读取只是读本地 json 配置文件，开销微乎其微。
        """
        now = time.time()
        to_process = []
        with self._lock:
            for path, (kind, t) in list(self._pending.items()):
                if now - t >= DEBOUNCE_SECONDS:
                    to_process.append((path, kind))
                    del self._pending[path]

        if not to_process:
            return

        exclude_folders = get_exclude_folders()

        updated, deleted, skipped = 0, 0, 0
        for path, kind in to_process:
            if not self._is_running:
                break

            try:
                if kind == "deleted" or not os.path.exists(path):
                    # 删除事件不受排除规则影响，正常清理——就算这个文件之前
                    # 就在被排除的目录里、根本没被索引过，delete_single_file
                    # 对不存在的记录也是安全的无操作。
                    db.delete_single_file(path)
                    deleted += 1
                    continue

                # 新增/修改事件：如果这个文件现在正好落在排除范围内（可能是
                # 监听这个目录时还没被排除，中途用户在"排除目录管理"里新排除
                # 了它所在的目录），不写进数据库。之前监听线程只在启动时读一次
                # 排除配置，即使顶层目录未被整个排除、只是子目录中途新排除，
                # 监听线程也一概不知情、还会继续把排除子目录里的变动同步进数据库，
                # 相当于排除规则在实时监控这一块上形同虚设。
                if is_path_excluded(path, exclude_folders):
                    skipped += 1
                    continue

                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    # 处理这一刻文件又被删了/移走了，跳过——如果真的是被删除，
                    # watchdog 之后还会再发一次删除事件，不会漏掉
                    continue

                filename = os.path.basename(path)
                error_msg = None
                if extractor_available:
                    try:
                        text_list = extract_dwg_text_via_exe(path, exe_path)
                    except Exception as e:
                        # 同索引扫描一样，把失败原因带进数据库，不只是打印就丢了
                        error_msg = str(e)
                        log(f">>> 实时更新提取失败，仅记录文件名: {filename} ({error_msg})")
                        text_list = []
                else:
                    text_list = []

                # 文件既然变动了（新增/修改），版本标识也要跟着重新读一次，
                # 不能沿用数据库里的旧值——理由同 upsert_single_file 的说明。
                dwg_version = read_dwg_version_tag(path)
                db.upsert_single_file(path, filename, text_list, mtime, error_msg=error_msg, dwg_version=dwg_version)
                updated += 1
            except Exception:
                # 单个文件处理出问题（最常见是跟索引扫描线程/别的监控实例
                # 撞上了数据库瞬时写冲突），不该连累这一批里其余文件都没法
                # 处理——原来这里没有兜底，一旦某个文件在这个 for 循环里
                # 抛出未捕获异常，不仅这一批后面排队的文件全部被跳过不处理，
                # 这个异常还会继续往外冒、捅穿 run() 里的 while 循环，把
                # 整个监控线程直接冲死（见 run() 里新加的说明）。记下完整
                # 堆栈方便排查，跳过这一个文件，继续处理下一个。
                log(f">>> 实时监控处理单个文件时出错，已跳过: {path}\n{traceback.format_exc()}")
                continue

        if updated or deleted:
            parts = []
            if updated:
                parts.append(f"更新 {updated} 张")
            if deleted:
                parts.append(f"移除 {deleted} 张")
            self.file_updated_signal.emit("检测到图纸变动，已自动同步索引：" + "、".join(parts))