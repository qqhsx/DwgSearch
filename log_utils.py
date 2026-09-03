# log_utils.py
"""
统一的带时间戳日志输出。

背景：索引扫描线程、实时监控线程、排除标记后台更新线程会交替往控制台
打印信息，光看内容本身很难分清谁先谁后、两条日志之间隔了多久——比如
"排除设置更新失败" 弹窗到底是因为跟哪一轮扫描撞车了、撞了多久，原来的
纯文本日志完全看不出来。统一在这里包一层，给每一条日志自动加上
"[HH:MM:SS.毫秒]" 前缀，不改变原来"直接 print"的调用方式，只是让
所有关心索引/监控/排除流程的模块都从这里导入同一个 log() 函数，
输出的时间线就能对得上。

V6.3：除了 print 到控制台，同时追加写一份到磁盘上的日志文件——之前
只打印到控制台，程序不是从命令行启动的话，这些信息其实谁都看不到，
用户报问题只能干等着重现。现在菜单栏"帮助 -> 程序日志"能直接打开
这份文件，索引/监控出过什么状况一目了然，不用非得复现问题时刚好开着
控制台。
"""
import os
import time

_LOG_FILE_PATH = None  # 懒加载，避免 log_utils 被导入的那一刻就依赖 config.py
_LOG_MAX_BYTES = 5 * 1024 * 1024  # 单个日志文件超过5MB就轮转，避免无限增长


def _get_log_file_path():
    global _LOG_FILE_PATH
    if _LOG_FILE_PATH is None:
        from config import get_log_file_path
        _LOG_FILE_PATH = get_log_file_path()
    return _LOG_FILE_PATH


def _rotate_if_too_large(path):
    try:
        if os.path.exists(path) and os.path.getsize(path) > _LOG_MAX_BYTES:
            old_path = path + ".old"
            if os.path.exists(old_path):
                os.remove(old_path)
            os.rename(path, old_path)
    except Exception:
        # 日志本身不能因为轮转失败就把主流程带崩，静默跳过就好，下次
        # 写入会在原文件基础上继续追加。
        pass


def log(msg):
    """替代 print()，自动加上精确到毫秒的时间戳前缀，同时追加写入磁盘
    日志文件。"""
    now = time.time()
    ts = time.strftime("%H:%M:%S", time.localtime(now))
    ms = int((now % 1) * 1000)
    line = f"[{ts}.{ms:03d}] {msg}"
    print(line)
    try:
        path = _get_log_file_path()
        _rotate_if_too_large(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        # 日志文件写不进去（比如目录被占用、磁盘满）不应该影响索引/
        # 监控这些主流程继续跑，控制台那份 print 出去的还在，不算
        # 完全丢失信息。
        pass
