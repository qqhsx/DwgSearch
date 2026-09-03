# accoreconsole_engine.py
#
# 批量替换的新引擎：每个文件起一个独立的 accoreconsole.exe 子进程
# （NETLOAD 我们自己编译的插件），跟现有 COM/ObjectDBX 引擎
# （replace_worker.py 里的 _process_one_file）功能对等、接口对等——
# 返回同样的 (ok, entries, err)，entries 是 ReplaceLogEntry 列表，
# 这样 ReplaceThread 才能做到"只换内部实现，外部行为不变"。
#
# 这里的 scr/params/result 文件读写方式，是 Stage 0 用 POC 脚本反复
# 测试踩坑之后的结论，不是随手写的：
#   - stdout 不解析（那是 UTF-16，只用来人工排错，不参与业务逻辑）
#   - params.txt / result.txt 显式用 UTF-8（.NET 这边可以显式指定
#     编码，不像 AutoLISP 那样得去猜系统代码页）
#   - SECURELOAD 0 / QSAVE 走 scr 原生命令，不在插件代码里调
#     Database.SaveAs（会触发 eFilerError，见 Stage 0 记录）
#   - 每次调用用独立临时目录，就算将来做并行也不会互相踩文件

import os
import subprocess
import tempfile


# 单个文件给多长时间——accoreconsole 启动本身有几秒开销，复杂图纸
# 处理也需要时间，超时后会被干净杀掉（不需要像 COM 那版那样搞
# PID 差集+taskkill）。这个数字先给个保守默认，如果发现大图纸经常
# 超时，再调大或者做成可配置。
DEFAULT_TIMEOUT_SECONDS = 180


def _write_scr(scr_path, dll_path, dry_run, result_path=None):
    """生成本次 accoreconsole 调用要跑的命令序列。

    QSAVE 不再是"只要不是 dry_run 就无条件跑"——之前是这么写的，
    实测证实了一个问题：哪怕 POCREPLACE 一个命中都没有（文档内容
    完全没被改过），只要脚本里排了 QSAVE 这条命令，AutoCAD 还是会
    老老实实存一遍，文件的修改时间照样会被刷新。批量替换如果扫一整个
    目录、但只有少数几份文件真正命中，这样搞下来会把目录里**所有**
    文件的修改时间都碰一遍——不仅仅是"日期不好看"这么简单，索引扫描
    是靠 mtime 判断"要不要重新提取内容"的，日期全被刷新之后，下次
    扫描会把这整个目录都当成"变了"，白白把内容提取再跑一遍。

    改成在 POCREPLACE 和 QSAVE 之间插一段 AutoLISP：读一下 POCREPLACE
    刚写出来的 result.txt，有没有 "HIT:" 开头的行（真的有命中、真的
    改了内容）；有才发 QSAVE，没有就跳过，文件原样不动、修改时间也
    不会被碰。

    这段判断刻意"失败时偏向于还是要保存"（_hit 默认给 T，只有确认
    读到 result.txt 且里面真的一条命中都没有，才会把 _hit 改成 nil）——
    宁可万一判断逻辑本身出岔子时多存一次（顶多是白白刷新一次修改
    时间，麻烦但不丢数据），也不要因为判断出错就悄悄跳过了一次本该
    真正落盘的保存、把已经做的替换结果丢在内存里没存下去。
    """
    dll_path_fwd = dll_path.replace("\\", "/")
    lines = [
        "FILEDIA 0",
        "CMDECHO 0",
        "EXPERT 5",
        "SECURELOAD 0",
        "NETLOAD",
        dll_path_fwd,
        "POCREPLACE",
    ]
    if not dry_run:
        result_path_fwd = result_path.replace("\\", "/")
        lines.append(
            '(progn (setq _hit T) '
            '(vl-catch-all-apply (function (lambda () '
            f'(setq _f (open "{result_path_fwd}" "r")) '
            '(setq _hit nil) '
            '(while (and (not _hit) (setq _ln (read-line _f))) '
            '(if (wcmatch _ln "HIT:*") (setq _hit T))) '
            '(close _f)))) '
            '(if _hit (command "._QSAVE")))'
        )
    lines += ["QUIT", "Y"]
    with open(scr_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _write_params(params_path, pairs, dry_run, scan_options):
    """pairs: [(old_text, new_text), ...]，至少一对。

    格式（跟 Commands.cs 里的读取顺序严格对应，改动时两边一起改）：
      第1行：dry_run          ("1"/"0")
      第2行：pair_count       (N，几组 旧文字/新文字)
      接下来 2*N 行：每组占两行，先 old 后 new
      再往后 6 行：scan_options（text/mtext/dimension/block_attr/scan_space/include_block_defs）
    """
    scan_options = scan_options or {}
    with open(params_path, "w", encoding="utf-8") as f:
        f.write(("1" if dry_run else "0") + "\n")
        f.write(str(len(pairs)) + "\n")
        for old_text, new_text in pairs:
            f.write(old_text + "\n")
            f.write(new_text + "\n")
        for key in ("text", "mtext", "dimension", "block_attr", "scan_space", "include_block_defs"):
            f.write(("1" if scan_options.get(key, True) else "0") + "\n")


# 插件那边 HIT: 前缀里的类型名，映射成跟 COM 引擎一致的实体类型名，
# 这样两个引擎产出的日志/CSV 里"来源类型"这一列格式是统一的，不用
# 让用户去理解两套不同的命名。
_TYPE_NAME_MAP = {
    "TEXT": "AcDbText",
    "MTEXT": "AcDbMText",
    "DIMENSION": "AcDbDimension",
    "BLOCK_ATTR": "AcDbAttributeReference",
}


def _parse_result(result_path):
    """返回 (status_ok, err_msg, hits)，hits 是
    [{"type":..., "old":..., "new":..., "block": 或 None}, ...]"""
    if not os.path.exists(result_path):
        return False, "插件没有生成 result.txt（可能中途异常退出，或弹出了意外对话框卡住）", []

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
            continue  # DIAG:/其它诊断行，不是命中记录，跳过
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


def _decode_console_output(raw_bytes):
    """accoreconsole 的控制台输出实测是 UTF-16（Stage 0 反复验证过），
    这里只用于出错时的诊断展示，不参与业务逻辑判断，所以做个简单的
    尽力而为解码就够，不需要像业务数据那样严格。"""
    if not raw_bytes:
        return ""
    for enc in ("utf-16", "utf-16-le", "gbk", "utf-8"):
        try:
            return raw_bytes.decode(enc)
        except Exception:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def process_one_file_accoreconsole(dwg_path, pairs, dry_run,
                                    accoreconsole_path, plugin_dll_path,
                                    scan_options=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    """跟 replace_worker._process_one_file 接口对等：返回 (ok, entries, err)。
    pairs: [(old_text, new_text), ...]，支持一次传多组替换规则。
    ReplaceLogEntry 放在函数内部延迟 import，避免跟 replace_worker.py
    产生循环引用（replace_worker 反过来要 import 这个模块）。"""
    from replace_worker import ReplaceLogEntry

    work_dir = tempfile.mkdtemp(prefix="accoreconsole_replace_")
    scr_path = os.path.join(work_dir, "job.scr")
    params_path = os.path.join(work_dir, "params.txt")
    result_path = os.path.join(work_dir, "result.txt")

    try:
        abs_path = os.path.abspath(dwg_path)
        _write_scr(scr_path, plugin_dll_path, dry_run, result_path=result_path)
        _write_params(params_path, pairs, dry_run, scan_options)

        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        # 关键：C# 插件是通过这两个环境变量拿到 params/result 文件路径的
        # （见 Commands.cs 里 Environment.GetEnvironmentVariable 那两行），
        # 不传的话插件会退化成写一个相对路径的 result.txt，我们在预期
        # 路径上就永远找不到文件，表现成"插件没有生成 result.txt"，
        # 容易被误判成插件卡死/崩溃——其实只是参数没传进去。
        env = os.environ.copy()
        env["POC_PARAMS_PATH"] = params_path
        env["POC_RESULT_PATH"] = result_path

        cmd = [accoreconsole_path, "/i", abs_path, "/s", scr_path]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env, **kwargs)
        except subprocess.TimeoutExpired:
            # subprocess 自己负责杀掉这个子进程，不需要额外的
            # PID 差集/taskkill 兜底——这是这次换引擎最主要的收益之一。
            return False, [], f"处理超时（{timeout}秒），进程已被终止"

        status_ok, err_msg, hits = _parse_result(result_path)
        if not status_ok:
            # 出错时把 accoreconsole 的控制台输出附上一段，方便排查——
            # 这个输出实测是 UTF-16 编码（Stage 0 验证过，不是瞎猜）。
            stdout_tail = _decode_console_output(proc.stdout)
            if stdout_tail:
                err_msg = f"{err_msg}\n---- accoreconsole 输出（末尾部分）----\n{stdout_tail[-800:]}"
            return False, [], err_msg

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
        # 临时目录用完即删，避免每个文件都在 temp 底下留垃圾。
        for p in (scr_path, params_path, result_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        try:
            os.rmdir(work_dir)
        except Exception:
            pass