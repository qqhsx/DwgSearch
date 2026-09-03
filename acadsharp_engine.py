# acadsharp_engine.py
#
# 批量替换的第三个引擎：基于 ACadSharp（纯 .NET 实现的 DWG 读写库）
# 编译出的独立 DwgTextReplacer.exe，跟 accoreconsole / AutoCAD COM 两个
# 引擎最大的区别——这个引擎不需要本机装 AutoCAD。
#
# 跟 accoreconsole_engine.py 是同一个接口约定：process_one_file_acadsharp()
# 返回 (ok, entries, err)，entries 是 ReplaceLogEntry 列表，这样
# ReplaceThread 才能做到"只换内部实现，外部行为不变"。
#
# params.txt / result.txt 的文件格式故意跟 accoreconsole 引擎完全一致
# （见 _write_params/_parse_result），DwgTextReplacer.exe（C# 侧）读写
# 的也是同一套格式——这不是巧合，是特意设计成这样，两个引擎共用一套
# 协议，不用为新引擎单独发明格式、单独测一遍格式的正确性。
#
# 已知限制（跟 COM/accoreconsole 两个引擎完全一致，不是这个引擎独有的）：
# 标注(DIMENSION)类型只会替换"设置过覆盖文字"的标注，纯粹显示测量值、
# 没有手动设置过覆盖文字的标注不会被替换——具体原因见 DwgTextReplacer/
# Program.cs 顶部的说明。

import os
import subprocess
import tempfile


DEFAULT_TIMEOUT_SECONDS = 180


def _write_params(params_path, pairs, dry_run, scan_options):
    """跟 accoreconsole_engine.py 的 _write_params 是同一份格式，
    故意保持字段顺序完全一致（DwgTextReplacer.exe 按这个顺序读取）。"""
    scan_options = scan_options or {}
    with open(params_path, "w", encoding="utf-8") as f:
        f.write(("1" if dry_run else "0") + "\n")
        f.write(str(len(pairs)) + "\n")
        for old_text, new_text in pairs:
            f.write(old_text + "\n")
            f.write(new_text + "\n")
        for key in ("text", "mtext", "dimension", "block_attr", "scan_space", "include_block_defs"):
            f.write(("1" if scan_options.get(key, True) else "0") + "\n")


# 跟 accoreconsole_engine.py 的 _TYPE_NAME_MAP 是同一份映射，保证三个
# 引擎产出的日志/CSV 里"来源类型"这一列格式统一，用户不用理解三套命名。
_TYPE_NAME_MAP = {
    "TEXT": "AcDbText",
    "MTEXT": "AcDbMText",
    "DIMENSION": "AcDbDimension",
    "BLOCK_ATTR": "AcDbAttributeReference",
}


def _parse_result(result_path):
    """返回 (status_ok, err_msg, hits)，hits 是
    [{"type":..., "old":..., "new":..., "block": 或 None}, ...]。
    跟 accoreconsole_engine.py 的 _parse_result 逻辑完全一致（读的是
    同一套文件格式），这里单独保留一份，是为了让这个引擎模块本身
    保持自包含——不产生跟 accoreconsole_engine.py 之间的相互 import，
    两边各自独立、互不影响，改一个引擎不会有牵连另一个引擎的风险。"""
    if not os.path.exists(result_path):
        return False, "DwgTextReplacer.exe 没有生成 result.txt（可能中途异常退出）", []

    with open(result_path, "r", encoding="utf-8") as f:
        lines = [ln.rstrip("\n").rstrip("\r") for ln in f.readlines()]

    if not lines:
        return False, "result.txt 是空的", []

    status_line = lines[0]
    if status_line.startswith("STATUS:ERROR:"):
        return False, status_line[len("STATUS:ERROR:"):], []
    if not status_line.startswith("STATUS:OK"):
        return False, f"result.txt 第一行格式不认识：{status_line}", []

    hits = []
    for ln in lines[1:]:
        if not ln.startswith("HIT:"):
            continue
        body = ln[len("HIT:"):]
        parts = body.split("|")
        if len(parts) < 3:
            continue
        entry_type, old_val, new_val = parts[0], parts[1], parts[2]
        block_name = None
        for extra in parts[3:]:
            if extra.startswith("block:"):
                block_name = extra[len("block:"):]
        hits.append({"type": entry_type, "old": old_val, "new": new_val, "block": block_name})

    return True, None, hits


def process_one_file_acadsharp(dwg_path, pairs, dry_run, exe_path,
                                scan_options=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    """跟 replace_worker._process_one_file / accoreconsole_engine.
    process_one_file_accoreconsole 接口对等：返回 (ok, entries, err)。
    pairs: [(old_text, new_text), ...]，支持一次传多组替换规则。
    ReplaceLogEntry 延迟 import，避免跟 replace_worker.py 产生循环引用。"""
    from replace_worker import ReplaceLogEntry

    work_dir = tempfile.mkdtemp(prefix="acadsharp_replace_")
    params_path = os.path.join(work_dir, "params.txt")
    result_path = os.path.join(work_dir, "result.txt")

    try:
        abs_path = os.path.abspath(dwg_path)
        _write_params(params_path, pairs, dry_run, scan_options)

        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        cmd = [exe_path, abs_path, params_path, result_path]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, **kwargs)
        except subprocess.TimeoutExpired:
            return False, [], f"处理超时（{timeout}秒），进程已被终止"

        status_ok, err_msg, hits = _parse_result(result_path)
        if not status_ok:
            stderr_tail = ""
            try:
                stderr_tail = (proc.stderr or b"").decode("utf-8", errors="replace")
            except Exception:
                pass
            if stderr_tail.strip():
                err_msg = f"{err_msg}\n---- DwgTextReplacer.exe 输出（末尾部分）----\n{stderr_tail.strip()[-800:]}"
            return False, [], err_msg

        # 标注(DIMENSION)替换在三个引擎里都只对"已设置覆盖文字"的标注生效
        # （见 DwgTextReplacer/Program.cs 顶部说明），这里不需要额外加提示：
        # 能出现在 hits 里就说明这条标注确实设置过覆盖文字，行为跟另外
        # 两个引擎一致，不是这个引擎"打折扣"的结果。
        entries = []
        for h in hits:
            source = _TYPE_NAME_MAP.get(h["type"], h["type"])
            entries.append(ReplaceLogEntry(
                None, source, h["old"], h["new"],
                is_block_definition=bool(h["block"]),
                block_name=h["block"],
            ))
        return True, entries, None

    except Exception as e:
        return False, [], str(e)
    finally:
        for p in (params_path, result_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        try:
            os.rmdir(work_dir)
        except Exception:
            pass