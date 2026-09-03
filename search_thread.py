# search_thread.py
import os
import time
import json
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from PyQt5.QtCore import QThread, pyqtSignal
from database import DWGDatabase
from log_utils import log
from searcher import Searcher
from helpers import extract_dwg_text_via_exe, normalize_diameter_symbol, read_dwg_version_tag
from config import get_extractor_path, get_exclude_folders, get_extract_workers, is_path_excluded

FLUSH_INTERVAL = 10


# =========================================================================
# IndexThread：只负责建索引，完全不做搜索
#
# V4.8：彻底移除了 AutoCAD 隐藏实例/COM/ObjectDBX 那一整套逻辑。
# 改用 ACadSharp 编译出的独立 DwgTextExtractor.exe 做纯 .NET 离线解析，
# 索引扫描不再需要 AutoCAD 进程参与，也就从根上不存在"抢占用户 AutoCAD /
# 拖慢用户画图"这个问题了——不是隔离得更好，是压根没有共享的对象可抢。
# =========================================================================
class IndexThread(QThread):
    finished_signal  = pyqtSignal()
    # 统计信号：(已扫总数, 全盘预估总数, 剩余秒数)
    stats_signal     = pyqtSignal(int, int, int)
    # 单个顶层目录真正扫完（没有被中途打断/超时放弃）就发一次，folder 是
    # 具体哪个目录。之前"这个目录算不算扫过"只在整条线程自然跑完时一次性
    # 判定——一旦扫描中途被打断（比如用户改了搜索目录触发了优先级重排），
    # 哪怕 C、D、E 这些目录当时已经老老实实扫完了，也会因为"整条线程没有
    # 自然跑完"而全部不被计入，下次又要从头重新扫一遍，纯属浪费。改成
    # 每个目录扫完就单独报一次，不用等整条线程的最终状态。
    folder_done_signal = pyqtSignal(str)

    def __init__(self, folder_list):
        """
        folder_list: 目录列表，按优先级排列（第一个是用户当前目录）
        """
        super().__init__()
        self._is_running = True
        # 过滤掉不存在的目录，去重保持顺序
        seen = set()
        self.folder_list = []
        for f in folder_list:
            f = os.path.abspath(f)
            if f not in seen and os.path.isdir(f):
                seen.add(f)
                self.folder_list.append(f)

        # 正在跑的 DwgTextExtractor.exe 子进程句柄登记表。extract_one 的
        # 提取 worker（线程池里的原生线程）会在启动/结束子进程时通过
        # _register_proc / _unregister_proc 往这里增删，stop() 就能在
        # 用户点退出的瞬间直接把它们全部 kill 掉，不用干等它们自己跑完
        # 才能让 run() 返回。
        self._active_procs = set()
        self._procs_lock = threading.Lock()

    def _register_proc(self, proc):
        with self._procs_lock:
            self._active_procs.add(proc)

    def _unregister_proc(self, proc):
        with self._procs_lock:
            self._active_procs.discard(proc)

    def _kill_active_procs(self):
        """立即杀掉当前所有还在跑的提取子进程。这几个子进程本来是阻塞在
        subprocess.communicate() 里等外部 .exe 跑完的，杀掉之后
        communicate() 会几乎立刻返回，worker 线程随即结束，run() 里的
        as_completed 循环也就能马上检查到 _is_running=False 并退出——
        不需要再干等一个可能耗时好几秒的大图纸解析自然完成，这是让
        "点退出"能在秒级内响应、不用每次都等满强制超时上限的关键。"""
        with self._procs_lock:
            procs = list(self._active_procs)
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass

    def stop(self):
        self._is_running = False
        self._kill_active_procs()

    def run(self):
        db = DWGDatabase()
        try:
            log(f"IndexThread 启动，目录数: {len(self.folder_list)}")
            log(f"前3个目录: {self.folder_list[:3]}")

            exe_path = get_extractor_path()
            extractor_available = bool(exe_path and os.path.isfile(exe_path))
            if extractor_available:
                log(f">>> DWG 提取工具就绪: {exe_path}")
            else:
                log(f">>> 未找到 DwgTextExtractor.exe（期望路径: {exe_path}），"
                      f"本次索引只会记录文件名，不提取图纸内容文字")

            # 初始预估：数据库已有记录数，随扫描动态增长
            try:
                stats = db.get_index_stats()
                db_count = stats['total_count']
            except Exception:
                db_count = 0

            # 用已有记录数或1作为起点
            self._total_pending   = 0   # 全盘待扫描总数（各目录 pending 累加）
            self._total_scanned   = 0   # 已扫完数量
            self._scan_start_time = time.time()

            log(f"开始扫描循环，_is_running={self._is_running}，目录数={len(self.folder_list)}")
            for folder in self.folder_list:
                if not self._is_running:
                    break
                try:
                    self._index_folder(db, folder, exe_path, extractor_available)
                except Exception as fe:
                    import traceback
                    log(f"索引目录失败，跳过 {folder}: {fe}")
                    traceback.print_exc()

        except Exception as e:
            import traceback
            log(f"IndexThread 异常: {e}")
            traceback.print_exc()
        finally:
            try:
                db.flush_batch()
            except Exception:
                pass
            try:
                db.close()
            except Exception:
                pass
            self.finished_signal.emit()

    def _enumerate_dwg_files(self, folder, exclude_ref, progress_ref):
        """实际执行 os.walk 遍历，收集这个目录下所有 DWG 文件路径。
        单独抽出来是为了能被 _walk_folder_with_timeout() 扔进独立线程、
        套上"卡死检测"——见那边的说明。exclude_ref、progress_ref 都是
        一元素列表，用来在这个函数内部更新排除规则、汇报遍历进度，
        避免用 self. 属性在多个遍历线程之间互相干扰。
        """
        EXCLUDE_RELOAD_INTERVAL = 1.0
        last_exclude_reload = time.time()
        all_dwg_files = []
        for root, dirs, files in os.walk(folder, onerror=lambda e: None):
            # 每访问一层目录（哪怕这层目录底下一张 DWG 都没有）就更新一次
            # "最后有进展"的时间戳——只要这个时间戳还在动，不管盘有多大、
            # 总共要扫多久，就说明它是真的在往前走，不会被下面的卡死检测
            # 误伤。只有当这个时间戳长时间停在原地不动，才说明是真的卡住了
            # （比如卡在某一层目录的底层文件系统调用上，根本没往下走）。
            progress_ref[0] = time.time()
            if not self._is_running:
                return None
            now_walk = time.time()
            if now_walk - last_exclude_reload >= EXCLUDE_RELOAD_INTERVAL:
                exclude_ref[0] = get_exclude_folders()
                last_exclude_reload = now_walk
            dirs[:] = [
                d for d in dirs
                if not is_path_excluded(os.path.join(root, d), exclude_ref[0])
            ]
            for f in files:
                # 过滤 AutoCAD 临时文件（~$ 开头，打开图纸时自动生成）
                if f.lower().endswith(".dwg") and not f.startswith("~$"):
                    all_dwg_files.append(os.path.join(root, f))
        return all_dwg_files

    def _walk_folder_with_timeout(self, folder, exclude_folders, stall_timeout=90):
        """在独立线程里跑目录遍历，加一层"卡死检测"（不是简单粗暴的总耗时
        超时）。

        背景：Windows 上访问某些磁盘（没放盘的光驱、断开的网络盘、休眠的
        移动硬盘、失效的映射盘符等）时，底层文件系统调用可能会卡住很久
        甚至完全不返回——一旦扫描列表里排到这么一个盘，不加保护的话会
        直接把整个索引线程卡死，后面排队的其它目录全都没法继续。

        但盘符本身很大、要扫很久是完全正常的情况（几十万文件的盘扫上
        十几分钟都不奇怪），不能因为"总共耗时长"就一刀切放弃——那样反而
        会把大盘、正常盘也误伤掉，扫不完整。真正该判断的是"它是不是还
        在往前走"：只要目录遍历还在持续产生新的进展（每访问一层目录都会
        更新一次时间戳），不管总共花多久都不打断；只有连续 stall_timeout
        秒完全没有任何新进展（包括从一开始就卡在第一层，一次都没往前走
        过），才认定是真的卡死了，放弃这个目录、继续扫后面排队的目录——
        那个卡住的线程没法被真正强制杀掉（Python 没有安全的线程强制终止
        手段），只能让它在后台自己耗着，反正它操作的是自己局部的列表，
        不会碰共享状态，不会造成任何数据错乱。
        """
        holder = {"result": None, "done": False}
        exclude_ref = [exclude_folders]
        progress_ref = [time.time()]

        def _run():
            try:
                holder["result"] = self._enumerate_dwg_files(folder, exclude_ref, progress_ref)
            finally:
                holder["done"] = True

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        while True:
            t.join(2)  # 每 2 秒醒一次检查进度和停止信号，不是一口气等到底
            if holder["done"]:
                return holder["result"]
            if not self._is_running:
                return None
            if time.time() - progress_ref[0] > stall_timeout:
                return "__TIMEOUT__"

    def _index_folder(self, db, folder, exe_path, extractor_available):
        exclude_folders = get_exclude_folders()

        # 扫描起点 folder 本身也要过一遍排除检查——下面 os.walk 内部的
        # dirs[:] 裁剪只会拦住"子目录"，如果 folder 自己就是被排除的
        # 目录（比如整块被排除的磁盘根目录 H:\，而它又被
        # _get_index_folder_list 当作顶层目录直接传进来），排除规则永远
        # 没机会拦截它，会导致整个目录照常被完整扫描、文件依然能搜到。
        if is_path_excluded(folder, exclude_folders):
            log(f"目录 {folder} 命中排除规则，跳过整个目录")
            # 注意：这里不发 folder_done_signal——"因为被排除而跳过"跟
            # "真的把内容扫了一遍"是两回事，不能划等号。如果发了，这个
            # 目录会被记进"本次运行期间已扫描"的记忆里；等它之后被取消
            # 排除、真正需要扫描时，这份记忆完全不会自动失效（排除范围
            # 变了，但记忆没人去更新），会被 _is_folder_already_covered()
            # 误判成"早就扫过了，不用扫"，取消排除之后要是这一轮扫描
            # 又被别的操作打断了一次，就再也没有机会真正扫到它——表现
            # 出来就是取消排除了、但内容始终没有被真正收录进去。排除检查
            # 本身几乎零成本，每次重新判断一次也无所谓，没必要靠"记住"
            # 来省这点开销，换来的是排除状态变了就立刻是准的，不会有
            # 滞后的错误记忆。
            return

        # 收集所有 DWG，跳过排除目录、无权限目录和 AutoCAD 临时文件。
        # exclude_folders 改成节流读取（见 _enumerate_dwg_files 内部），
        # 目录树再深也不会因为频繁重读配置文件被拖慢。
        #
        # 开始遍历之前先打一行日志——如果这个目录恰好是没插盘的光驱、
        # 断开的网络盘之类会导致 os.walk 长时间卡住的情况，至少能从
        # 日志上看出"确实是卡在这一步"，而不是"什么都没发生"，方便
        # 排查到底是真慢还是真卡死。真卡住（连续 90 秒完全没有任何新
        # 进展）的话，下面的检测会放弃这个目录、继续扫后面排队的目录；
        # 只是单纯很大、很慢但一直在往前走的盘，不会被误伤，该等多久
        # 还是等多久。
        log(f"开始遍历目录 {folder} ...")
        all_dwg_files = self._walk_folder_with_timeout(folder, exclude_folders, stall_timeout=90)
        if all_dwg_files == "__TIMEOUT__":
            log(f">>> 目录 {folder} 连续 90 秒没有任何新进展，判断为卡死"
                f"（常见原因：光驱没放盘、网络盘断开或无响应、移动硬盘休眠、"
                f"映射盘符失效等），本轮先跳过这个目录，继续处理后面排队的"
                f"目录。如果这个目录本身是正常能用的盘，可以先确认一下当前"
                f"能不能在资源管理器里正常打开，能打开的话再单独针对它重新"
                f"扫一次。")
            return
        if all_dwg_files is None:
            return  # 被用户主动停止

        if not all_dwg_files:
            log(f"目录 {folder} 未找到任何 DWG，跳过")
            self.folder_done_signal.emit(folder)
            return

        if not self._is_running:
            return
        db.remove_deleted_files(all_dwg_files, folder)

        if not self._is_running:
            return
        # 批量读取该目录所有已索引文件的 mtime（一次 SQL，比逐一查快很多）
        db_mtimes = db.get_folder_mtimes(folder)

        # 找出需要扫描的（内存比对，不再逐一查数据库）
        pending = []
        for path in all_dwg_files:
            if not self._is_running:
                return
            mtime = os.path.getmtime(path)
            if db_mtimes.get(path) != mtime:
                pending.append((path, mtime))

        if not pending:
            log(f"目录 {folder} 所有文件已是最新，跳过")
            self.folder_done_signal.emit(folder)
            return

        # 累加待扫描总数
        self._total_pending += len(pending)
        log(f"目录 {folder} 找到 {len(all_dwg_files)} 张，其中 {len(pending)} 张需更新索引")

        last_flush_time = time.time()
        # 界面刷新节流：并发之后处理速度快很多，如果还是"每处理完一张就
        # 刷新一次界面"，刷新频率会跟着涨到一秒十几二十次，这部分频繁的
        # 界面重绘本身会占掉主线程一点点额外CPU。改成最多每200毫秒刷新
        # 一次，进度条看起来依然是流畅在动，但省掉不必要的重绘次数。
        last_stats_emit_time = 0
        STATS_EMIT_INTERVAL = 0.2
        # 用最近完成的时间戳算吞吐率（每秒处理几张），而不是像之前那样
        # 记录"单张耗时"——并发之后单张耗时不能直接代表整体速度了（8个
        # 同时在跑，单张本身的时长看着没变，但吞吐量是几倍的），用一段
        # 时间窗口内"完成了几张"来算实际速度更准。
        completed_times = deque(maxlen=40)

        # 提取失败的节流打印 + 归类统计：只逐条打印前 FAILURE_PRINT_LIMIT
        # 条，之后同一批里的失败只计数，扫完这个目录后按错误原因分类打印
        # 一次汇总。避免"一整个目录都是老版本 AC1009 图纸"这种场景下，
        # 几千条"提取失败"逐行刷屏、真正有用的信息反而被淹没。
        FAILURE_PRINT_LIMIT = 5
        failure_counts = {}
        printed_failures = 0
        last_error_flush_time = 0.0
        # 失败记录也要落盘，避免万一软件在扫描过程中被强制关掉（用户直接
        # 关终端/Ctrl+C），刚才控制台打印过的失败还没来得及落盘。但不能像
        # 之前那样"每失败一张就立刻刷盘一次"——一个目录里连续几千张老图纸
        # 提取失败时，那样会变成几千次高频独立小事务连轴转占着数据库写锁，
        # 导致"排除目录管理"想更新排除标记的后台线程迟迟抢不到写锁，报出
        # "database is locked"。改成失败发生时最多每隔 ERROR_FLUSH_INTERVAL
        # 秒才真正刷盘一次，durability 和"不占着写锁不撒手"之间取一个平衡。
        ERROR_FLUSH_INTERVAL = 3

        def _extract_one(dwg_path):
            """线程池worker：只做提取（调外部exe），不碰数据库——
            SQLite 一个连接不适合被多个线程同时写，数据库写入还是
            放回主线程里串行做。"""
            filename = os.path.basename(dwg_path)
            error_msg = None
            if extractor_available:
                try:
                    text_list = extract_dwg_text_via_exe(
                        dwg_path, exe_path,
                        register_process=self._register_proc,
                        unregister_process=self._unregister_proc,
                        is_cancelled=lambda: not self._is_running,
                    )
                except Exception as e:
                    # 单张图纸解析失败（损坏/格式过旧过新等）只跳过内容提取，
                    # 文件名索引照常写入，不影响这一批其余文件继续处理。
                    # 把失败原因带回主线程，写进数据库的 extract_failed/
                    # error_msg 两列，供"索引管理"里的失败列表查看。
                    # 注意：这里不再直接 print——失败量大的目录（比如一整个
                    # 目录都是老版本 AC1009 图纸）会导致这里被高频调用，
                    # 逐条打印只会把控制台刷屏刷成没法看；改成把失败原因
                    # 带回主线程（消费 as_completed 结果那一侧），统一节流
                    # 打印 + 按错误类型归类汇总，见下方消费循环。
                    error_msg = str(e)
                    text_list = []
            else:
                text_list = []
            # 顺手把版本标识也读出来存进数据库，跟提取内容共用这同一次
            # "已经在访问这个文件"的机会——只读文件头6个字节，开销跟一次
            # os.stat() 差不多，比后面搜索结果表格再单独现读一次划算得多
            # （见 search_manager.py 填表那段的耗时打点分析）。就算上面
            # 提取正文失败（图纸损坏/格式过旧），版本标识依然独立尝试读，
            # 两者不互相影响。
            dwg_version = read_dwg_version_tag(dwg_path)
            return dwg_path, filename, text_list, error_msg, dwg_version

        # 每次真正开始这一批之前才读一次配置，而不是启动时读一次存死——
        # 这样用户在索引管理里改了并发数之后，不用重启程序，下一次
        # 索引/重建就能生效。
        worker_count = get_extract_workers()
        executor = ThreadPoolExecutor(max_workers=worker_count)
        try:
            futures = {}
            for dwg_path, mtime in pending:
                if not self._is_running:
                    break
                futures[executor.submit(_extract_one, dwg_path)] = mtime

            for future in as_completed(futures):
                if not self._is_running:
                    # 停止信号来了：不再处理后续完成的结果、不再往数据库写
                    break

                # 整个顶层目录（folder 参数，比如 G:\）中途重新被排除了：
                # 下面那个单文件写库前的排除检查只能保证已经提取完的文件不被
                # 写进数据库，但还排队等待提取的数千张图纸依然会一张张跑完——
                # 白白浪费时间去解析根本不会被保存的图纸。此时就跟收到 stop()
                # 信号一样处理：撤销还没轮到的任务、杀掉正在跑的解析子进程，直接
                # 跳到下一个顶层目录，不干等这几千张跟自己无关的图纸自然跑完。
                #
                # 这里只读一次配置，同时用于上面的顶层目录检查和下面的单文件检查，
                # 避免每处理一张图纸就重复读两次本地 json 配置文件。
                current_excludes = get_exclude_folders()
                if is_path_excluded(folder, current_excludes):
                    unfinished = sum(1 for f in futures if not f.done())
                    log(f">>> 目录 {folder} 扫描中途被重新排除，取消剩余 {unfinished:,} 个任务")
                    for f in futures:
                        if not f.done():
                            f.cancel()
                    self._kill_active_procs()
                    # 把这一批还没跑到的任务从总预估里扣除，不然进度条会因为
                    # 这批被追加的待处理量永远补不完而卡在未完成的百分比。
                    self._total_pending = max(0, self._total_pending - unfinished)
                    return

                mtime = futures[future]
                try:
                    dwg_path, filename, text_list, error_msg, dwg_version = future.result()
                except Exception as e:
                    log(f">>> 提取任务异常，跳过: {e}")
                    continue

                # 此时重新检查一次排除规则：这张图纸可能在提交给线程池时
                # 还不在排除范围内，但在它提取完成、准备写库的这段时间里，
                # 用户在“排除目录管理”里新排除了它所在的目录。不再写进数据库，
                # 避免刚刚设置好的排除规则被这一批正在飞的任务绕过去，这样不需要
                # 中断重起整个扫描，即使排除设置是在扫描过程中途改的也能立即生效。
                if is_path_excluded(dwg_path, current_excludes):
                    self._total_scanned += 1
                    completed_times.append(time.time())
                    continue

                db.update_file_index(dwg_path, filename, text_list, mtime, error_msg=error_msg, dwg_version=dwg_version)
                self._total_scanned += 1
                now = time.time()
                completed_times.append(now)

                if error_msg:
                    failure_counts[error_msg] = failure_counts.get(error_msg, 0) + 1
                    if printed_failures < FAILURE_PRINT_LIMIT:
                        log(f">>> 提取失败: {filename} ({error_msg})")
                    elif printed_failures == FAILURE_PRINT_LIMIT:
                        log(f">>> 目录 {folder} 提取失败较多，后续同类失败改为计数汇总，"
                            f"不再逐条打印控制台（完整清单可在“索引管理→查看失败文件”里查看）")
                    printed_failures += 1

                    # 节流刷盘：保证失败记录最终会落盘（万一软件中途被强制
                    # 关掉），但不会因为连续失败就变成高频独立小事务连轴转
                    # 占着写锁。
                    if now - last_error_flush_time >= ERROR_FLUSH_INTERVAL:
                        db.flush_batch()
                        last_error_flush_time = now

                # 用窗口内的吞吐率估算剩余时间
                if len(completed_times) >= 2:
                    span = completed_times[-1] - completed_times[0]
                    rate = (len(completed_times) - 1) / span if span > 0 else 0
                else:
                    rate = 0
                remaining_count = max(0, self._total_pending - self._total_scanned)
                remaining_secs = int(remaining_count / rate) if rate > 0 else 0

                # 节流：200ms内已经发过一次的话就跳过，但最后一张（进度到底）
                # 必须发，不然进度条可能停在99%不动、看着像卡住了
                is_last = (self._total_scanned >= self._total_pending)
                if is_last or (now - last_stats_emit_time >= STATS_EMIT_INTERVAL):
                    self.stats_signal.emit(self._total_scanned, self._total_pending, remaining_secs)
                    last_stats_emit_time = now

                if now - last_flush_time >= FLUSH_INTERVAL:
                    db.flush_batch()
                    last_flush_time = now

            if not self._is_running:
                # 主动取消所有还排队中、还没轮到开始跑的任务——这一批可能
                # 提交了几千个，同时只有配置里设定的并发数个真正在跑，
                # 剩下排队的这些没必要傻等它们一个个轮到再跑完，直接从
                # 队列里撤掉。已经在跑的最多几个没法真正中途打断，就让
                # 它们在后台自然跑完，不强制等待（不影响主线程往下走）。
                for f in futures:
                    f.cancel()
        finally:
            # wait=False：不阻塞等待已经在跑的少数任务完成，函数可以立刻
            # 返回。这几个任务会在后台线程里自己跑完自然结束，它们的结果
            # 反正也没人再去取了（上面已经不再消费 as_completed 的结果），
            # 不影响主线程继续往下走、也不会遗留任何数据库写入（提取worker
            # 本身不碰数据库）。
            executor.shutdown(wait=False)

        db.flush_batch()

        # 目录扫完，按错误原因汇总打印一次失败统计——前面消费循环里
        # 逐条打印被节流掉的失败，都在这里能看到总数，不会真的丢信息，
        # 只是不再刷屏。
        if failure_counts:
            total_failed = sum(failure_counts.values())
            log(f"目录 {folder} 本轮共 {total_failed} 张提取失败，按原因归类：")
            for reason, count in sorted(failure_counts.items(), key=lambda kv: -kv[1]):
                log(f"    {count:>5} 张：{reason}")

        if not self._is_running:
            return
        self.folder_done_signal.emit(folder)


class PreviewLoadThread(QThread):
    """
    右侧预览内容改成后台线程加载，不再在主线程里同步查数据库。

    db 参数：调用方传入一个已经开好、常驻的 DWGDatabase 连接，这里
    只管拿它查询，不在这个线程里新建连接。原因：DWGDatabase() 每次
    实例化都会跑一遍 create_table()（检查各张表、索引、FTS5 虚拟表
    是否存在），这是一笔跟"这次查的文件内容多少"完全无关的固定开销——
    之前的写法是每次选中一行就新建一个 DWGDatabase()，相当于把这笔
    固定开销从"每次搜索一次"摊薄成了"每次点一下都要交一次"，这才是
    "所有文件都一样卡、跟内容多少无关"这个现象的真正来源，不是查询
    本身慢。改成复用同一个常驻连接后，这里就只剩下真正的查询开销。

    这个连接对象是从主线程创建、但在这里（工作线程）里使用——
    DWGDatabase 内部用 check_same_thread=False 打开连接，配合 SQLite
    默认的"serialized"线程安全模式，跨线程复用同一个连接对象本身是
    安全的，只是不同线程严禁真正同时并发地对它发起查询（这里的场景
    是"前一次预览查询早跑完了才会有下一次"，天然满足这个前提）。
    """
    content_ready = pyqtSignal(str, list, list, bool)  # (dwg_path, texts, keywords_for_highlight, is_regex)
    content_error = pyqtSignal(str, str)         # (dwg_path, error_message)

    def __init__(self, db, dwg_path, keywords, entity_types=None, spaces=None, scopes=None,
                 content_regex=False):
        super().__init__()
        self.db = db
        self.dwg_path = dwg_path
        self.keywords = keywords or []
        self.entity_types = entity_types or []
        self.spaces = spaces or []
        self.scopes = scopes or []
        self.content_regex = content_regex

    def run(self):
        if not os.path.exists(self.dwg_path):
            self.content_error.emit(self.dwg_path, f"错误: 图纸物理文件不存在: {self.dwg_path}")
            return
        try:
            texts = self.db.get_single_file_content(
                self.dwg_path, entity_types=self.entity_types,
                spaces=self.spaces, scopes=self.scopes
            )
            # 正则模式下不能小写化——会破坏 \D / \S / \W / [A-Z] 这类
            # 大小写敏感的元字符语义，交给下游用 re.IGNORECASE 处理
            # 大小写不敏感匹配，而不是在这里粗暴转小写。
            # 同理，正则模式下也不能做直径符号归一化——用户正则里如果
            # 精确写了 ∅ 或 Ø，不该被悄悄替换成 ⌀，改变匹配语义。
            # 非正则模式下必须做归一化：texts 是从数据库读出来的，
            # 提取阶段（helpers.py _parse_extracted_line）已经把 ∅/Ø/ø
            # 统一转成 ⌀ 了，如果这里高亮关键词不做同样归一化，用户
            # 搜索时输入的 ∅ 能匹配到文件（database.py 查询时也做了归一化），
            # 但预览区文本里其实是 ⌀，doc.find() 按原始 ∅ 找就会一个都
            # 高亮不到——明明命中了，右边预览却看着像没高亮上。
            if self.content_regex:
                keywords_for_highlight = [kw for kw in self.keywords if kw.strip()]
            else:
                keywords_for_highlight = [normalize_diameter_symbol(kw.lower()) for kw in self.keywords if kw.strip()]
            if not texts:
                texts = ["[提示] 当前筛选条件下，该图纸没有匹配的文字内容（或该图纸暂未建立文字账本索引，请重新点击“搜索”激活增量扫描）。"]
            self.content_ready.emit(self.dwg_path, texts, keywords_for_highlight, self.content_regex)
        except Exception as e:
            self.content_error.emit(self.dwg_path, f"读取账本预览缓存失败: {e}")


# =========================================================================
# SearchThread：只负责搜索，直接查数据库，毫秒级完成（未改动）
# =========================================================================
class SearchThread(QThread):
    progress_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(str, str)
    count_signal    = pyqtSignal(int, int, int)

    def __init__(self, dwg_folders, content_keywords, filename_keywords,
                 entity_types=None, spaces=None, scopes=None,
                 filename_regex=False, content_regex=False):
        super().__init__()
        # dwg_folders：勾选的目录标签列表，空列表 = 全部目录，不限定。
        self.dwg_folders = [os.path.abspath(f) for f in (dwg_folders or []) if f]
        self.content_keywords  = content_keywords  or []
        self.filename_keywords = filename_keywords or []
        # None 或空表示不筛选（全选）；只有传了内容关键词时这几个筛选才有意义
        self.entity_types = entity_types or []
        self.spaces = spaces or []
        self.scopes = scopes or []
        # 是否把对应的关键词列表当正则表达式解析（而不是普通子串匹配）。
        # search_manager.py 负责在正则模式下把关键词整理成"不拆词、
        # 已校验语法"的形式，这里只管原样透传给 Searcher。
        self.filename_regex = filename_regex
        self.content_regex = content_regex

    def run(self):
        start_time = time.time()
        db = DWGDatabase()
        try:
            searcher = Searcher(db)
            matched = searcher.search_keywords(
                dwg_folders=self.dwg_folders,
                content_keywords=self.content_keywords,
                filename_keywords=self.filename_keywords,
                filename_regex=self.filename_regex,
                content_regex=self.content_regex,
                progress_callback=self.progress_signal.emit,
                count_callback=self.count_signal.emit,
                total_files=0,
                entity_types=self.entity_types,
                spaces=self.spaces,
                scopes=self.scopes
            )
            elapsed = time.time() - start_time
            # matched 现在是 { dwg_path: dwg_version, ... }（见 searcher.py），
            # 不再是纯路径集合——直接把整个 dict 序列化传给主线程，
            # search_manager.py 填表时可以从这里直接拿到版本号，不用再
            # 对每个命中文件单独现读一次。
            result_json = json.dumps(matched)
            status_msg = f"搜索完成，匹配到 {len(matched)} 个，耗时 {elapsed:.2f} 秒"
            self.finished_signal.emit(status_msg, result_json)
        except Exception as e:
            # matched 现在是 dict 语义（path -> version），异常兜底也要用
            # "{}" 而不是旧版本的 "[]"，不然 search_manager.py 那边
            # json.loads 出来的类型会跟正常路径对不上。
            self.finished_signal.emit(f"搜索异常: {e}", "{}")
        finally:
            db.close()


# =========================================================================
# ClearIndexThread：后台执行"清空索引"（DELETE 全表 + VACUUM）
#
# VACUUM 需要把整个数据库文件重写一遍来真正收缩磁盘占用，数据库越大
# （图纸多、正文索引多）耗时越长，可能到几秒甚至几十秒。这个操作之前
# 是直接在主线程（UI线程）同步调用 db.clear_all_index()，执行期间 Qt
# 事件循环被整个堵死，界面无法重绘/响应任何点击，表现出来就是系统
# 弹"未响应"。挪到独立线程后，UI 线程只管显示"正在清空..."状态、
# 处理其他事件，真正的清空动作在后台跑完后通过信号通知主线程再继续
# 后续的重新建索引流程。
# =========================================================================
class ClearIndexThread(QThread):
    finished_signal = pyqtSignal()
    error_signal     = pyqtSignal(str)

    def run(self):
        db = DWGDatabase()
        try:
            db.clear_all_index()
        except Exception as e:
            self.error_signal.emit(str(e))
            return
        finally:
            db.close()
        self.finished_signal.emit()