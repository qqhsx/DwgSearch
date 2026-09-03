# backup_manager.py
#
# 批量替换执行前的备份，以及备份完成后的恢复，核心逻辑都放这里，
# replace_worker.py（写备份）和 backup_restore_dialog.py（读备份、
# 恢复）共用同一套函数，避免两边各写一份、路径规则慢慢就对不上了。
#
# 设计要点：
#   1) 每次正式执行都是独立一个"备份批次"，用时间戳命名文件夹，
#      不同批次互不覆盖，出问题了可以挑某一次的备份来恢复。
#   2) 备份文件不是拍平堆在一个目录里按文件名存的——不同来源文件夹
#      里可能有同名文件（比如两个项目都有"标题栏.dwg"），拍平存会
#      互相覆盖，等于白备份。这里按"盘符 + 完整目录结构"在备份批次
#      目录下镜像一份，天然不会重名，而且人工翻备份目录时也能一眼
#      看出某个文件原来在哪，不用非得靠软件才能恢复。
#   3) 每备份一个文件，立刻在 manifest.jsonl 里追加一行记录（原始
#      路径 -> 备份路径），而不是等一批全处理完再一次性写。这样任务
#      中途被取消/崩溃/断电，已经备份过的文件依然有据可查，不会因为
#      manifest 没来得及写完而变成"备份了但找不到对应关系"的孤儿文件。

import os
import shutil
import json
from datetime import datetime

MANIFEST_FILENAME = "manifest.jsonl"
BACKUP_RUN_PREFIX = "backup_"


def new_backup_run_dir(backup_root):
    """在 backup_root 下新建一个以当前时间命名的备份批次目录，返回其
    完整路径。命名格式 backup_20260818_153022，同一秒内不会有两次
    正式执行冲突（正式执行本身耗时通常远大于1秒），足够用。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(backup_root, f"{BACKUP_RUN_PREFIX}{stamp}")
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def compute_mirrored_backup_path(original_path, run_dir):
    """按原始文件的盘符 + 完整目录结构，在 run_dir 下镜像出对应路径。

    比如 D:\\图纸\\A项目\\标题栏.dwg 会备份成
    run_dir\\D\\图纸\\A项目\\标题栏.dwg —— 这样不同来源目录里哪怕有
    同名文件，备份出来也不会互相覆盖；而且人工打开备份目录，看着这套
    目录结构就能直接认出原文件在哪，不依赖 manifest 也能大致找回去。
    """
    drive, tail = os.path.splitdrive(os.path.abspath(original_path))
    drive_label = drive.rstrip(":\\/") or "NODRIVE"  # 极少数无盘符路径（比如网络路径）兜底
    tail = tail.lstrip("\\/")
    return os.path.join(run_dir, drive_label, tail)


def append_manifest_entry(run_dir, original_path, backup_path, extra=None):
    """备份完一个文件，立刻追加一行记录到 manifest.jsonl。用 jsonl
    （每行一个独立 JSON 对象）而不是一次性写一个大 JSON 数组，是因为
    追加只需要在文件末尾写一行，不需要先读回整个文件、改完再整个重写
    ——任务中途中断的话，已经写完的行不会因为最后一次重写没完成而
    整个文件损坏。"""
    entry = {
        "original_path": original_path,
        "backup_path": backup_path,
        "backed_up_at": datetime.now().isoformat(timespec="seconds"),
    }
    if extra:
        entry.update(extra)
    manifest_path = os.path.join(run_dir, MANIFEST_FILENAME)
    with open(manifest_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_manifest(run_dir):
    """读一个备份批次目录下的 manifest.jsonl，返回记录列表。单行解析
    失败（比如任务恰好在写这一行时被强制中断，导致这一行不完整）会
    跳过这一行，不影响其它已经完整写入的记录。"""
    manifest_path = os.path.join(run_dir, MANIFEST_FILENAME)
    entries = []
    if not os.path.exists(manifest_path):
        return entries
    with open(manifest_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except Exception:
                continue
    return entries


def list_backup_runs(backup_root):
    """列出 backup_root 下全部备份批次，按时间从新到旧排序，每项带上
    这一批次里实际备份了多少个文件（用于恢复弹窗里给用户看概况）。
    没有 manifest 或者 manifest 是空的批次不列出来——大概率是那次
    执行还没真正备份任何文件就中断了/或目录不是本工具产生的。"""
    runs = []
    if not os.path.isdir(backup_root):
        return runs
    for name in os.listdir(backup_root):
        full = os.path.join(backup_root, name)
        if not os.path.isdir(full) or not name.startswith(BACKUP_RUN_PREFIX):
            continue
        entries = read_manifest(full)
        if not entries:
            continue
        stamp_str = name[len(BACKUP_RUN_PREFIX):]
        try:
            dt = datetime.strptime(stamp_str, "%Y%m%d_%H%M%S")
            label = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            label = stamp_str
        runs.append({"run_dir": full, "label": label, "file_count": len(entries)})
    runs.sort(key=lambda r: r["run_dir"], reverse=True)
    return runs


def delete_backup_run(run_dir):
    """删除一个备份批次整个目录（连同镜像的原文件、manifest、CSV 记录一起
    删掉）。调用方要保证 run_dir 确实是从 list_backup_runs() 拿到的合法
    批次目录，这里不做二次校验——校验交给 UI 层用下拉框限定候选范围，
    用户不可能手输任意路径进来，没必要在这里重复防一遍。"""
    shutil.rmtree(run_dir)


def enforce_backup_retention(backup_root, max_runs):
    """按"最多保留 max_runs 个批次"清理旧备份，超出的部分从最旧的开始
    删除。max_runs <= 0 表示不限制，直接跳过不清理。

    返回被删除的批次目录列表，方便调用方记一笔日志告诉用户"清理了哪几
    批"，而不是静默删除让人摸不着头脑。单个批次删除失败（比如某个文件
    正被别的程序占用锁着）不影响其它批次继续清理，失败的那个留到下次
    再试。"""
    if not max_runs or max_runs <= 0:
        return []
    runs = list_backup_runs(backup_root)  # 已经是新到旧排序
    to_delete = runs[max_runs:]
    deleted = []
    for run in to_delete:
        try:
            delete_backup_run(run["run_dir"])
            deleted.append(run["run_dir"])
        except Exception:
            pass
    return deleted


def restore_entry(entry, overwrite=True):
    """把 manifest 里的一条记录恢复回原始路径。

    返回 (ok, msg)。设计上不在这里做"批量恢复要不要继续"的决策——
    那是调用方（恢复弹窗）的事，这里只管把单个文件恢复好、把失败原因
    说清楚，方便调用方汇总展示给用户。
    """
    backup_path = entry.get("backup_path")
    original_path = entry.get("original_path")
    if not backup_path or not original_path:
        return False, "备份记录缺少路径信息，跳过"
    if not os.path.exists(backup_path):
        return False, f"备份文件已不存在（可能被手动删除过）：{backup_path}"
    if os.path.exists(original_path) and not overwrite:
        return False, "原路径已存在文件，且未勾选覆盖，跳过"
    try:
        original_dir = os.path.dirname(original_path)
        if original_dir:
            os.makedirs(original_dir, exist_ok=True)
        shutil.copy2(backup_path, original_path)
        return True, "已恢复"
    except Exception as e:
        return False, f"恢复失败：{e}"