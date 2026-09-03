# replace_worker.py
#
# "文字替换"功能的后台逻辑。
#
# 核心设计：
#   - 用 DispatchEx 建一个完全独立、隐藏（Visible=False）的 AutoCAD 实例，
#     跟用户自己手动打开的 AutoCAD 互不相关，用户全程感知不到。
#   - dry_run=True 时只扫描、记录，不真正写入保存，务必先预览确认。
#   - 非 dry_run 时，写入保存之前一定先把原文件备份一份。
#
# 🌟 AutoCAD 隐藏实例改成"跟对话框同生命周期"的常驻实例，不再是
# "预览"和"确认执行"各自起一个新实例。DispatchEx 启动一个全新 AutoCAD
# 进程本身就慢（轮询等对象模型就绪最多30秒 + 额外5秒缓冲，正常情况下
# 也要几秒到十几秒），之前预览、执行各建一次，等于同一批文件、同一条
# 替换规则要付两次这个启动成本，纯属浪费。现在对话框一打开、后台线程
# 一启动就预热一个实例，预览和执行共用它，直到用户关闭对话框才真正
# Quit() 掉。
#
# 结构上参照老版本 IndexThread（V4.7，已验证很可靠）：ReplaceThread 是
# 一个简单的 QThread 子类，DispatchEx / 处理文件 / Quit() 全部发生在
# 同一个 run() 函数体内、同一个调用栈里，天然保证只在这一个线程里操作
# COM 对象（COM 对象跟创建它的线程绑定，不能跨线程直接用）。GUI 线程
# 要提交任务、请求关闭，靠一个线程安全的 queue.Queue 传递，不依赖 Qt
# 的跨线程信号槽去触发"关闭"这个动作——早先试过 QObject+moveToThread+
# 跨线程信号的写法，实测关不掉隐藏的 AutoCAD 进程，换回这套结构更简单、
# 已经被验证过确实好用的做法。
#
# 已知限制（暂时没做）：单个文件处理时如果弹出意外提示卡住，
# 整个批次会跟着卡住，需要手动去任务管理器结束隐藏的 acad.exe 进程。
# 原本设计了一版"开子线程+超时强杀"的保护机制，但 COM 对象不能跨线程
# 直接使用，会报错，所以先去掉了，等核心替换功能先跑稳了，
# 再考虑用"每个文件单独开一个系统进程"这种更彻底的隔离方式补上。

import os
import queue
import shutil
import subprocess
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import winreg
except ImportError:  # 非 Windows 环境（比如开发机上跑单元测试）没有 winreg
    winreg = None

from PyQt5.QtCore import QThread, pyqtSignal

from database import DWGDatabase
from helpers import extract_dwg_text_via_exe, read_dwg_version_tag
from config import (
    get_extractor_path, get_replace_engine, get_accoreconsole_plugin_root,
    get_accoreconsole_manual_path, get_dwg_replacer_path,
)
from backup_manager import compute_mirrored_backup_path, append_manifest_entry
from accoreconsole_detect import resolve_accoreconsole_engine
from accoreconsole_engine import process_one_file_accoreconsole
from acadsharp_engine import process_one_file_acadsharp

# 这几种实体类型的文字，用 .TextString 属性读写
TEXT_ENTITY_TYPES = ("AcDbText", "AcDbMText")
# 标注、引线类实体名里通常带这几个词，用 .TextOverride 属性读写
# 注：这个属性名没有实测过，如果运行时报错找不到这个属性，需要反馈调整。
DIMENSION_ENTITY_KEYWORDS = ("Dimension", "Leader")


RPC_E_CALL_REJECTED = -2147418111  # "被呼叫方拒绝接收呼叫"，通常是暂时性的，重试一下就好


def _is_rpc_rejected(e):
    """判断是不是那种"AutoCAD暂时忙、拒绝调用"的错误，这类错误值得重试"""
    args = getattr(e, "args", None)
    if args and len(args) > 0 and args[0] == RPC_E_CALL_REJECTED:
        return True
    return False


def _is_dimension_like(entity_name):
    return any(kw in entity_name for kw in DIMENSION_ENTITY_KEYWORDS)


def _create_dbx_obj(acad_app):
    """
    创建一个新的 ObjectDBX 实例——纯数据库级接口，不创建任何文档窗口，
    不会有 Documents.Open() 那种牵动窗口/焦点的副作用（详见本文件顶部
    说明，以及旧版本 helpers.py 里"不抢焦点、超稳定"那条注释）。
    优先用带版本号的接口，失败则兜底用通用接口。
    """
    dbx_ver = acad_app.Version.split('.')[0]
    for attempt in range(3):
        try:
            dbx_obj = acad_app.GetInterfaceObject(f"ObjectDBX.AxDbDocument.{dbx_ver}")
            if dbx_obj:
                return dbx_obj
        except Exception:
            time.sleep(0.2)
    try:
        return acad_app.GetInterfaceObject("ObjectDBX.AxDbDocument")
    except Exception:
        raise RuntimeError("初始化 ObjectDBX 接口失败")


def _open_dbx_with_retry(acad_app, abs_path, max_retries=3):
    """
    带重试的 DBX Open。遇到"被呼叫方拒绝"（AutoCAD 暂时忙）直接抛出让
    上层的 RPC 重试逻辑统一处理；遇到其他错误（文件损坏/被占用/实例
    偶尔状态不对）重建一个新的 DBX 实例后重试。
    """
    last_exc = None
    for attempt in range(max_retries):
        dbx_obj = _create_dbx_obj(acad_app)
        try:
            dbx_obj.Open(abs_path)
            return dbx_obj
        except Exception as e:
            last_exc = e
            if _is_rpc_rejected(e):
                raise
            time.sleep(1.0)
    raise RuntimeError(f"ObjectDBX 打开文件失败（可能文件被占用或损坏）：{last_exc}")


def _safe_update(obj):
    """
    改完 .TextString / .TextOverride 之后主动调一次 .Update()。

    背景：Left 对齐的文字，插入点是固定的，改变长度只是往右延伸，不存在
    对不齐的问题。但 Middle/Center/Right/Aligned/Fit 这几种对齐方式，理论上
    AutoCAD 会在渲染时按当前文字内容围绕对齐点重新计算位置，不需要额外
    干预；只是通过 COM/ActiveX 改属性之后，有的 AutoCAD 版本不会立刻刷新
    这个实体的显示范围（extents），需要显式调一次 Update() 才保险。
    这是个低成本、无副作用的防御性调用：真的不需要也不会有任何负面影响，
    所以没做"判断是否需要"这层区分，统一都调一下。
    """
    try:
        obj.Update()
    except Exception:
        # 不是所有实体类型都支持 Update()，或者极少数情况下调用失败，
        # 这都不该影响替换本身已经成功写入的结果，忽略即可。
        pass


def _list_acad_pids():
    """当前系统里正在跑的所有 acad.exe 进程 PID 集合，用来跟 DispatchEx
    前后做差集，找出这次新启动的隐藏实例对应哪个 PID。"""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq acad.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        pids = set()
        for line in result.stdout.splitlines():
            parts = [p.strip().strip('"') for p in line.split(",")]
            if len(parts) >= 2 and parts[1].isdigit():
                pids.add(int(parts[1]))
        return pids
    except Exception:
        return set()


def _is_autocad_com_registered():
    """查一下 Windows 注册表里有没有 "AutoCAD.Application" 这个 COM
    ProgID，用来在真正调用 DispatchEx 之前快速判断"这台电脑到底有没
    有装 AutoCAD"。

    这跟 accoreconsole_detect.py 那套"探测 accoreconsole.exe 装在哪"
    是两种不同性质的探测：accoreconsole 要找的是文件系统里的具体路径
    （装哪个盘、哪个目录都可能），必须主动扫描/查注册表卸载信息才能
    知道；而 COM 组件是 AutoCAD 安装时自动注册进 Windows 系统级 COM
    组件表的，直接查 HKEY_CLASSES_ROOT 下有没有这个 ProgID 键即可，
    不需要猜路径，成本也低得多（一次注册表读取，不涉及扫盘符）。

    返回 True/False；查询本身出问题（比如极端权限受限环境）时保守
    返回 True，不让预检的异常挡住后面真正的 DispatchEx 调用——预检
    只是为了给更快、更友好的报错，不能变成新的失败点。
    """
    if winreg is None:
        return True
    try:
        key = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "AutoCAD.Application")
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
    except Exception:
        return True


def _create_hidden_acad():
    """
    建一个全新的、独立的、隐藏的 AutoCAD 实例。
    返回 (acad_app, pid)：pid 是这个实例对应的系统进程号，拿不到时为 None。
    """
    import win32com.client

    # 预检：真正调用 DispatchEx 之前，先看一眼系统里有没有注册
    # "AutoCAD.Application" 这个 COM 组件。没装 AutoCAD 的情况下
    # DispatchEx 本身也会失败，但抛出来的是 pywintypes 的原始 COM
    # 错误码（类似 "(-2147221005, '无效的类字符串', ...)"），普通
    # 用户看不懂是什么意思；这里提前判断一次，能给出人话提示，
    # 且不用真的去拉起一个进程再等它失败。
    if not _is_autocad_com_registered():
        raise RuntimeError(
            "未检测到本机安装的 AutoCAD（COM 方式需要先安装 AutoCAD，"
            "而且要用管理员权限运行过一次，让它完成 COM 组件注册）"
        )

    # 先记一次快照，等新实例真正就绪之后再记一次，用差集找出这次新增
    # 的那个 PID——比读 .HWND 反查更可靠：隐藏实例（Visible=False）
    # 有的 AutoCAD 版本取 .HWND 会失败或者返回 0，尤其是这种情况下，
    # PID 就拿不到，后面 shutdown() 那层"确认退出/强杀"的兜底就完全
    # 用不上了，等于形同虚设。
    before_pids = _list_acad_pids()

    try:
        acad_app = win32com.client.DispatchEx("AutoCAD.Application")
    except Exception as e:
        # 预检通过了但 DispatchEx 还是失败，通常不是"没装"，而是
        # 别的原因：许可证没激活、首次启动需要手动接受协议弹窗、
        # 权限不足等。给个更有指向性的提示，比原始 COM 异常好排查。
        raise RuntimeError(
            f"已检测到本机安装的 AutoCAD，但启动隐藏实例失败：{e}\n"
            "常见原因：AutoCAD 许可证未激活、首次运行需要手动打开一次"
            "接受协议、或没有足够权限。建议先手动打开一次 AutoCAD 确认"
            "能正常启动。"
        )
    try:
        acad_app.Visible = False
    except Exception:
        pass

    # 关键：DispatchEx 刚拉起来的是一个全新进程，内部组件初始化需要几秒钟，
    # 这时候立刻调用 .Documents 之类的接口大概率会失败。
    # 轮询等它真正就绪，最多等 30 秒（正常几秒内就会好）。
    ready = False
    for _ in range(60):
        try:
            _ = acad_app.Documents  # 能正常访问这个属性，说明对象模型已经就绪
            ready = True
            break
        except Exception:
            time.sleep(0.5)
    if not ready:
        raise RuntimeError("等待 AutoCAD 隐藏实例就绪超时（30秒），实例可能启动失败")

    # 能读到 .Documents 属性只能说明基本就绪，不代表能立刻扛住 Open()
    # 这种更重的调用——之前实测发现每次新建实例处理的第一个文件，
    # 经常会遇到"暂时拒绝接收呼叫"，多等几秒缓冲一下能明显改善。
    time.sleep(5)

    after_pids = _list_acad_pids()
    new_pids = after_pids - before_pids
    if len(new_pids) == 1:
        pid = new_pids.pop()
    else:
        # 前后没能精确对上恰好一个新增 PID（比如这段时间用户自己手动
        # 也开了一个 AutoCAD，或者短时间内并发起了不止一个隐藏实例），
        # 退一步试试 HWND 反查，实在不行就是 None——这种情况下 shutdown()
        # 会退化回"只调 Quit()，没有强杀兜底"，跟改之前一样，不会更差。
        pid = None
        try:
            import win32process
            hwnd = acad_app.HWND
            if hwnd:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            pid = None

    return acad_app, pid


def _process_alive(pid):
    """用 tasklist 查一下这个 PID 是否还在跑，不用额外装 psutil。"""
    if not pid:
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        return str(pid) in result.stdout
    except Exception:
        # 查不到就当它可能还在跑，交给上层按"还没退出"处理，宁可多等
        # 一轮也别漏杀。
        return True


def _force_kill_process(pid):
    """
    结束一个完全独立的外部系统进程（隐藏的 acad.exe）——注意这跟这次
    改动之前那种"杀本进程内正卡在 COM 调用里的线程"完全是两回事：
    那种会把当前 Python 进程自己的 COM/解释器状态搞坏，直接崩掉；
    这里结束的是另一个独立进程，对本程序自身的稳定性没有任何影响，
    是安全的兜底手段。
    """
    if not pid:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    except Exception:
        pass


class ReplaceLogEntry:
    def __init__(self, dwg_path, source, old_val, new_val, is_block_definition=False,
                 block_name=None, note=None):
        self.dwg_path = dwg_path
        self.source = source
        self.old_val = old_val
        self.new_val = new_val
        self.is_block_definition = is_block_definition
        self.block_name = block_name
        self.note = note  # 排版相关的额外提示，比如"MText变长较多，注意换行重排"

    def display(self):
        scope = f"[块定义:{self.block_name}，此块所有实例都会变] " if self.is_block_definition else ""
        note_suffix = f"  ⚠️ {self.note}" if self.note else ""
        return f"{scope}[{self.source}] '{self.old_val}' -> '{self.new_val}'{note_suffix}"


MTEXT_GROW_WARN_RATIO = 0.3  # MText 新文字比原文字长超过这个比例，提示注意定宽换行重排

# 界面上"要替换哪些类型"勾选框对应的 key，全部为 True 时等价于改动前的
# 行为（所有类型都替换），保证没有主动去改设置的老用户体验不变。
DEFAULT_SCAN_OPTIONS = {
    "text": True,           # 单行文字 AcDbText
    "mtext": True,           # 多行文字 AcDbMText
    "dimension": True,       # 标注/引线覆盖文字（TextOverride）
    "block_attr": True,      # 块参照的属性值
    "scan_space": True,          # 范围开关：是否搜索模型空间/图纸空间（摆出来的实体）
    "include_block_defs": True,  # 范围开关：是否连块定义内部（模板本身）也一起改
}


def _apply_pairs_chained(original_val, pairs):
    """把多组「旧文字->新文字」依次应用到同一个字符串上（链式）：
    第2组是在第1组替换完的结果上继续找替换，不是各自独立作用在原文上。
    这跟大多数"批量查找替换"工具的直觉一致——比如先把"一号楼"换成
    "1号楼"，再把"1号楼"里的"号"换成"#"，两条规则是可以接力生效的。

    返回 (final_val, changed)：changed 为 True 表示至少有一组命中过、
    最终值和原始值不一样。"""
    current = original_val
    for old_text, new_text in pairs:
        if old_text and old_text in current:
            current = current.replace(old_text, new_text)
    return current, (current != original_val)


def _scan_entity(obj, pairs, dry_run, log_list,
                  is_block_definition=False, block_name=None,
                  scan_options=None):
    """pairs: [(old_text, new_text), ...]，至少一对，按顺序链式应用（见
    _apply_pairs_chained）。日志里对每个实体只记一条最终结果（原始值 ->
    最终值），不再按"哪一对命中"拆成多条，这样命中数、日志和 CSV 都是
    "改动了多少个实体"，跟改动前的语义保持一致，只是每次能命中更多组
    关键词而已。"""
    scan_options = scan_options or DEFAULT_SCAN_OPTIONS
    try:
        entity_name = obj.EntityName
    except Exception:
        return

    if entity_name in TEXT_ENTITY_TYPES:
        # 单行/多行文字是两个独立的勾选项，分开判断要不要跳过
        if entity_name == "AcDbText" and not scan_options.get("text", True):
            return
        if entity_name == "AcDbMText" and not scan_options.get("mtext", True):
            return
        try:
            text_val = obj.TextString
        except Exception:
            return
        new_val, changed = _apply_pairs_chained(text_val, pairs)
        if changed:
            note = None
            if entity_name == "AcDbMText" and len(text_val) > 0 and len(new_val) > len(text_val):
                grow_ratio = (len(new_val) - len(text_val)) / len(text_val)
                if grow_ratio > MTEXT_GROW_WARN_RATIO:
                    note = "文字变长较多，如果这个 MText 是定宽的，可能触发自动换行，建议替换后肉眼确认排版"

            log_list.append(ReplaceLogEntry(None, entity_name, text_val, new_val,
                                             is_block_definition, block_name, note=note))
            if not dry_run:
                obj.TextString = new_val
                _safe_update(obj)

    elif _is_dimension_like(entity_name):
        if not scan_options.get("dimension", True):
            return
        try:
            override_val = obj.TextOverride
        except Exception:
            override_val = ""
        if override_val:
            new_val, changed = _apply_pairs_chained(override_val, pairs)
            if changed:
                log_list.append(ReplaceLogEntry(None, entity_name, override_val, new_val,
                                                 is_block_definition, block_name))
                if not dry_run:
                    obj.TextOverride = new_val
                    _safe_update(obj)

    elif entity_name == "AcDbBlockReference":
        if not scan_options.get("block_attr", True):
            return
        try:
            has_attrs = obj.HasAttributes
        except Exception:
            has_attrs = False
        if has_attrs:
            for attr in obj.GetAttributes():
                try:
                    attr_val = attr.TextString
                except Exception:
                    continue
                new_val, changed = _apply_pairs_chained(attr_val, pairs)
                if changed:
                    log_list.append(ReplaceLogEntry(None, "AcDbAttributeReference", attr_val, new_val))
                    if not dry_run:
                        attr.TextString = new_val
                        _safe_update(attr)


def _process_one_file(acad_app, dwg_path, pairs, dry_run, scan_options=None):
    """
    处理单个文件。

    🌟 改用 ObjectDBX（AxDbDocument）而不是 Application.Documents.Open()：
    后者会牵动 AutoCAD 完整的文档窗口生命周期，即便 Application.Visible
    =False，依然会做窗口/焦点相关的处理——这正是"AutoCAD 实例启动过程
    会抢输入法"、以及偶尔出现 <unknown>.Open 这种诡异失败的根源。
    ObjectDBX 是纯数据库级接口，不创建任何文档窗口，这也是这个项目里
    "提取文字"功能一直用它、从没出现过这两个问题的原因（老版本
    helpers.py 里"不抢焦点、超稳定"那条注释）。

    ⚠️ 注意：这条写入路径（dbx_obj.SaveAs()）在这个项目里是第一次真正
    使用——之前 ObjectDBX 只用来只读提取文字，没有在真实图纸上实测过
    写入/保存这一步。原理上 ObjectDBX 支持完整的读写访问，但请务必先
    用预览模式多测几个有代表性的文件，正式执行后打开保存过的图纸检查
    一下（标注、块属性这些复杂对象尤其要重点看一下），确认没问题再放
    心批量跑，有异常随时告诉我。

    必须在创建 acad_app 的那个线程里调用——COM 对象是跟创建它的线程
    绑定的，不能跨线程直接用，见文件最上面的说明。

    scan_options: 界面上勾选的"要替换哪些类型"，见 DEFAULT_SCAN_OPTIONS。
    其中 include_block_defs 控制要不要连块定义内部（模板本身）也一起扫，
    不是传给 _scan_entity 的普通类型开关，在这个函数里单独处理。

    pairs: [(old_text, new_text), ...]，支持一次传多组替换规则，
    每个实体内部按顺序链式应用（见 _apply_pairs_chained）。

    返回 (ok, entries, error_msg)
    """
    scan_options = scan_options or DEFAULT_SCAN_OPTIONS
    log_list = []
    dbx_obj = None
    try:
        abs_path = os.path.abspath(dwg_path)

        # 跟原来一样：AutoCAD 刚启动完那一小段时间，偶尔会对这类调用
        # 返回"暂时拒绝接收呼叫"（RPC_E_CALL_REJECTED），这是暂时性的，
        # 稍等一下重试就好。
        max_attempts = 8
        for attempt in range(1, max_attempts + 1):
            try:
                dbx_obj = _open_dbx_with_retry(acad_app, abs_path)
                break
            except Exception as e:
                if _is_rpc_rejected(e) and attempt < max_attempts:
                    time.sleep(2.0 * attempt)
                    continue
                raise

        # scan_space 和 include_block_defs 是两个对等、独立的范围开关，
        # 不是"模型空间默认无条件搜、块定义可选追加"这种不对称关系——
        # 用户应该能自由组合出"只搜块定义"这种以前做不到的场景。
        if scan_options.get("scan_space", True):
            for space in (dbx_obj.ModelSpace, dbx_obj.PaperSpace):
                for obj in space:
                    _scan_entity(obj, pairs, dry_run, log_list,
                                  scan_options=scan_options)

        # include_block_defs 是另一个范围开关，跟 scan_space 对等：
        # 决定要不要连块定义模板本身也一起改，这条影响的是所有用到该块
        # 的地方，所以单独拎出来控制，而不是塞进 _scan_entity 内部判断。
        if scan_options.get("include_block_defs", True):
            for block in dbx_obj.Blocks:
                block_name = block.Name
                if block_name.startswith("*"):
                    continue
                for obj in block:
                    _scan_entity(obj, pairs, dry_run, log_list,
                                  is_block_definition=True, block_name=block_name,
                                  scan_options=scan_options)

        for entry in log_list:
            entry.dwg_path = dwg_path

        if not dry_run and log_list:
            # ObjectDBX 没有"跟原文件绑定的 Save()"这个概念（不像
            # Documents.Open() 打开的文档那样知道自己是从哪来的），
            # 用 SaveAs() 存回原路径，等效于原地保存。备份已经在这一步
            # 之前做过了，就算这里出问题也有原文件兜底。
            dbx_obj.SaveAs(abs_path)

        return True, log_list, None
    except Exception as e:
        return False, log_list, str(e)
    finally:
        # ObjectDBX 对象没有 Documents.Close() 那种概念，用完直接释放
        # Python 侧的引用，交给 COM 引用计数自己清理即可。
        dbx_obj = None


class ReplaceThread(QThread):
    """
    常驻的替换后台线程。整个"批量文字替换"对话框只创建一次，run() 只
    执行一次，从预热到真正退出全程只有一次 DispatchEx。

    🔧 结构改成跟老版本 IndexThread（V4.7，已验证很可靠）对齐：一个
    简单的 QThread 子类，内部用一个线程安全的 queue.Queue 阻塞等待
    GUI 线程发来的任务，处理完继续等下一个，收到"关闭"哨兵值才真正
    退出 run() 里的 while 循环——退出循环后，Quit() 和 CoUninitialize()
    都在 run() 自己的 finally 块里做，跟创建 acad_app 是同一个函数、
    同一次调用栈，不再依赖 Qt 的跨线程信号槽去触发关闭这个动作。

    之前 QObject + moveToThread + 跨信号槽（_request_shutdown 信号）
    的写法在实测中就是关不掉隐藏的 AutoCAD 进程，具体是信号排队机制
    哪个环节出的问题没能100%定位，索性换回结构上更简单、老版本已经
    验证过确实好用的做法：普通的 obj.submit_job(...) / obj.request_
    shutdown() 方法调用，跨线程直接调用是安全的——它们只是往
    queue.Queue 里放一条数据，不触碰任何 COM 对象，Python 的 GIL
    保证这种操作是线程安全的。真正操作 COM 对象的代码全部留在 run()
    这一个函数体内，天然保证只在这一个线程里执行，不需要借助信号槽来
    保证线程亲和性。
    """
    progress_signal       = pyqtSignal(str)                  # 一行一行的日志文本
    file_done_signal      = pyqtSignal(str, bool, int, str)  # (文件路径, 是否成功, 命中数, 错误信息)
    finished_signal       = pyqtSignal(int, int, int)        # (总文件数, 总命中数, 失败文件数)
    instance_ready_signal = pyqtSignal(bool, str)             # (是否就绪, 失败原因)

    _SHUTDOWN = object()  # 队列里的哨兵值，取到它就说明该退出 run() 了

    def __init__(self):
        super().__init__()
        self._task_queue = queue.Queue()
        self._stop_current_job = False
        # 引擎选择在线程启动时（run() 一开始）确定一次，整个线程生命周期
        # 内不再重新判断——避免一个批次跑到一半，用户在设置里切换了引擎，
        # 导致同一批文件被两种引擎混着处理这种不一致的情况。
        self._engine = "com"
        self._accoreconsole_path = None
        self._plugin_dll_path = None
        self._acadsharp_path = None

    # ------------------------------------------------------------------
    # 下面几个方法可以从任何线程（通常是 GUI 线程）安全地直接调用，
    # 不需要走信号槽——都只是简单的队列/标志位操作，不碰 COM 对象。
    # ------------------------------------------------------------------
    def submit_job(self, file_paths, pairs, dry_run, backup_dir, log_csv_path,
                   scan_options=None):
        """pairs: [(old_text, new_text), ...]，支持一次提交多组替换规则。"""
        self._stop_current_job = False
        self._task_queue.put((file_paths, pairs, dry_run, backup_dir,
                               log_csv_path, scan_options))

    def request_stop_current_job(self):
        self._stop_current_job = True

    def request_shutdown(self):
        self._task_queue.put(self._SHUTDOWN)

    # ------------------------------------------------------------------
    def run(self):
        import pythoncom
        pythoncom.CoInitialize()

        acad_app = None
        acad_pid = None
        try:
            configured_engine = get_replace_engine()

            if configured_engine == "accoreconsole":
                plugin_root = get_accoreconsole_plugin_root()
                manual_path = get_accoreconsole_manual_path()
                exe_path, dll_path, reasons = resolve_accoreconsole_engine(plugin_root, manual_path)
                if exe_path:
                    self._engine = "accoreconsole"
                    self._accoreconsole_path = exe_path
                    self._plugin_dll_path = dll_path
                    self.progress_signal.emit(f"（accoreconsole 引擎：{exe_path}）")
                    self.progress_signal.emit(f"（插件：{dll_path}）")
                    # 这个引擎不需要常驻的 AutoCAD 实例——每个文件都是独立
                    # 的 accoreconsole 子进程，acad_app 全程保持 None，
                    # 下面的预热/重建逻辑天然不会碰它。
                    self.instance_ready_signal.emit(True, "")
                else:
                    reason_text = "；".join(reasons) if reasons else "未知原因"
                    self.progress_signal.emit(
                        f"⚠️ 无法使用 accoreconsole 引擎（{reason_text}），"
                        f"本次自动回退到 COM 引擎"
                    )
                    self._engine = "com"
            elif configured_engine == "acadsharp":
                exe_path = get_dwg_replacer_path()
                if exe_path and os.path.isfile(exe_path):
                    self._engine = "acadsharp"
                    self._acadsharp_path = exe_path
                    self.progress_signal.emit(f"（ACadSharp 引擎：{exe_path}，本机无需安装 AutoCAD）")
                    self.progress_signal.emit(
                        "（提示：标注类型只会替换已设置过覆盖文字的标注，"
                        "纯测量值标注不受影响——这跟另外两个引擎行为一致）"
                    )
                    # 跟 accoreconsole 一样，这个引擎不需要常驻的 AutoCAD
                    # 实例——每个文件是独立的 DwgTextReplacer.exe 子进程。
                    self.instance_ready_signal.emit(True, "")
                else:
                    self.progress_signal.emit(
                        f"⚠️ 未找到 DwgTextReplacer.exe（期望路径: {exe_path}），"
                        f"本次自动回退到 COM 引擎"
                    )
                    self._engine = "com"
            else:
                self._engine = "com"

            if self._engine == "com":
                # 线程一启动就立即预热实例，把 DispatchEx 的启动开销藏在
                # 用户添加文件、输入替换文字这段操作界面的时间背后。
                try:
                    acad_app, acad_pid = _create_hidden_acad()
                    if acad_pid:
                        self.progress_signal.emit(f"（隐藏 AutoCAD 实例 PID：{acad_pid}）")
                    self.instance_ready_signal.emit(True, "")
                except Exception as e:
                    self.progress_signal.emit(f"❌ 无法启动隐藏的 AutoCAD 实例：{e}")
                    self.instance_ready_signal.emit(False, str(e))
                    acad_app, acad_pid = None, None

            while True:
                item = self._task_queue.get()  # 没任务时线程在这里挂起，不占CPU
                if item is self._SHUTDOWN:
                    break

                file_paths, pairs, dry_run, backup_dir, log_csv_path, scan_options = item

                # 预热阶段失败过（比如那次 AutoCAD 没启动起来），这里
                # 趁真正要用的时候再试一次，不至于预热失败就整个对话框
                # 期间都用不了替换功能。accoreconsole 引擎不需要这个
                # 重建逻辑——没有常驻实例这回事。
                if self._engine == "com" and acad_app is None:
                    try:
                        acad_app, acad_pid = _create_hidden_acad()
                    except Exception as e:
                        self.progress_signal.emit(f"❌ 无法启动隐藏的 AutoCAD 实例：{e}")
                        self.finished_signal.emit(0, 0, len(file_paths))
                        continue

                self._run_job(acad_app, file_paths, pairs,
                               dry_run, backup_dir, log_csv_path, scan_options)
        finally:
            if acad_app is not None:
                try:
                    acad_app.Quit()
                except Exception as e:
                    print(f">>> [replace_worker] Quit() 抛出异常：{e}")

                # Quit() 正常情况下几乎是瞬间的事，但万一它背后卡着什么
                # 看不见的弹窗、请求被无声地忽略了，进程会一直占着不
                # 退出——这里等几秒确认它真的退出了，没退出就直接结束
                # 这个进程本身兜底。
                if acad_pid:
                    exited = False
                    for _ in range(6):
                        if not _process_alive(acad_pid):
                            exited = True
                            break
                        time.sleep(1)
                    if not exited:
                        print(f">>> [replace_worker] PID={acad_pid} 等待6秒后仍存活，执行 taskkill 强制结束")
                        _force_kill_process(acad_pid)
            pythoncom.CoUninitialize()

    def _run_job(self, acad_app, file_paths, pairs, dry_run, backup_dir,
                 log_csv_path, scan_options=None):
        """预览和正式执行走的是同一个方法，靠 dry_run 区分是否真正写入
        保存——acad_app 复用同一个实例，不再每次任务现建现拆。
        pairs: [(old_text, new_text), ...]，一次任务可以带多组替换规则。"""
        mode_desc = "预览模式（不会保存）" if dry_run else "正式执行（会写入并保存）"
        engine_desc = {
            "accoreconsole": "accoreconsole",
            "acadsharp": "ACadSharp（无需 AutoCAD）",
        }.get(self._engine, "AutoCAD COM")
        self.progress_signal.emit(f"当前模式：{mode_desc}　引擎：{engine_desc}")
        pairs_desc = "；".join(f"'{o}' -> '{n}'" for o, n in pairs)
        self.progress_signal.emit(f"共 {len(file_paths)} 个文件，{len(pairs)} 组替换规则：{pairs_desc}")

        total_hits = 0
        error_count = 0
        all_entries = []

        for dwg_path in file_paths:
            if self._stop_current_job:
                self.progress_signal.emit("用户已取消，停止处理剩余文件")
                break

            filename = os.path.basename(dwg_path)
            self.progress_signal.emit(f"处理中：{filename}")

            if not os.path.exists(dwg_path):
                self.progress_signal.emit(f"  ❌ 文件不存在，跳过")
                self.file_done_signal.emit(dwg_path, False, 0, "文件不存在")
                error_count += 1
                continue

            # 备份：只在正式执行、且文件确实存在的情况下做。按原始盘符
            # + 目录结构镜像存放（而不是拍平按文件名存），不同来源
            # 文件夹里的同名文件不会互相覆盖；备份完立刻写一行 manifest
            # 记录，方便之后用"从备份恢复"功能按原路径找回去，见
            # backup_manager.py 顶部的设计说明。
            if not dry_run and backup_dir:
                try:
                    backup_path = compute_mirrored_backup_path(dwg_path, backup_dir)
                    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                    if not os.path.exists(backup_path):
                        shutil.copy2(dwg_path, backup_path)
                    append_manifest_entry(backup_dir, dwg_path, backup_path)
                except Exception as e:
                    self.progress_signal.emit(f"  ⚠️ 备份失败（{e}），为安全起见跳过这个文件")
                    self.file_done_signal.emit(dwg_path, False, 0, f"备份失败: {e}")
                    error_count += 1
                    continue

            # 直接在当前线程处理。COM 引擎下 acad_app 是在这个线程创建的，
            # COM 对象必须在创建它的同一个线程里用，不能跨线程直接调用，
            # 这意味着 COM 引擎暂时没有"单个文件卡住自动跳过"的超时保护，
            # 如果某张图纸处理时弹出意外提示，整个批次会卡在这里，
            # 需要你手动去任务管理器结束隐藏的 acad.exe 进程才能恢复。
            #
            # accoreconsole / acadsharp 两个引擎都不受这个限制——每个文件
            # 是独立子进程，自带超时+进程级强杀（分别见 accoreconsole_
            # engine.py / acadsharp_engine.py），单个文件卡住不会拖累
            # 整个批次，这是换引擎最主要的收益之一。
            if self._engine == "accoreconsole":
                ok, entries, err = process_one_file_accoreconsole(
                    dwg_path, pairs, dry_run,
                    self._accoreconsole_path, self._plugin_dll_path,
                    scan_options=scan_options,
                )
            elif self._engine == "acadsharp":
                ok, entries, err = process_one_file_acadsharp(
                    dwg_path, pairs, dry_run,
                    self._acadsharp_path,
                    scan_options=scan_options,
                )
            else:
                ok, entries, err = _process_one_file(acad_app, dwg_path, pairs, dry_run,
                                                      scan_options=scan_options)

            if not ok:
                self.progress_signal.emit(f"  ❌ 处理失败：{err}")
                error_count += 1
            elif not entries:
                self.progress_signal.emit(f"  ℹ️ 未命中")
            else:
                self.progress_signal.emit(
                    f"  ✅ 命中 {len(entries)} 处" + ("（预览，未保存）" if dry_run else "（已保存）")
                )
                for entry in entries:
                    self.progress_signal.emit(f"    {entry.display()}")

            total_hits += len(entries)
            all_entries.extend(entries)
            self.file_done_signal.emit(dwg_path, ok, len(entries), err or "")

        if log_csv_path and all_entries:
            try:
                import csv
                with open(log_csv_path, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.writer(f)
                    writer.writerow(["文件", "来源类型", "原文字", "新文字", "是否块定义", "块名", "提示"])
                    for entry in all_entries:
                        writer.writerow([
                            entry.dwg_path, entry.source, entry.old_val, entry.new_val,
                            "是" if entry.is_block_definition else "", entry.block_name or "",
                            entry.note or ""
                        ])
                self.progress_signal.emit(f"详细日志已写入：{log_csv_path}")
            except Exception as e:
                self.progress_signal.emit(f"⚠️ 写日志文件失败：{e}")

        self.progress_signal.emit(
            f"========== 完成：共 {len(file_paths)} 个文件，命中 {total_hits} 处，失败 {error_count} 个 =========="
        )
        self.finished_signal.emit(len(file_paths), total_hits, error_count)


class ReindexThread(QThread):
    """
    正式执行（非预览）替换完成后，只针对这次真正被改写并保存的文件，
    重新用 DwgTextExtractor.exe 提取一遍文字，写回 SQLite 索引。

    不用等下次启动程序触发的全盘 mtime 扫描，也不用手动点"清空并重建索引"——
    这里只处理刚被替换工具改过的这几个文件，量级小，跑起来很快。
    """
    progress_signal = pyqtSignal(str)
    finished_signal  = pyqtSignal(int, int)  # (成功数, 失败数)

    def __init__(self, file_paths):
        super().__init__()
        self.file_paths = list(file_paths)

    def run(self):
        exe_path = get_extractor_path()
        extractor_available = bool(exe_path and os.path.isfile(exe_path))
        if not extractor_available:
            self.progress_signal.emit(
                f"⚠️ 未找到 DwgTextExtractor.exe（期望路径: {exe_path}），"
                f"本次跳过索引同步，文件名和内容都不会更新"
            )
            self.finished_signal.emit(0, len(self.file_paths))
            return

        existing_paths = [p for p in self.file_paths if os.path.exists(p)]
        missing_count = len(self.file_paths) - len(existing_paths)
        for p in self.file_paths:
            if p not in existing_paths:
                self.progress_signal.emit(f"  ⚠️ 同步索引跳过（文件不存在）：{os.path.basename(p)}")

        # 提取文字调用的是相互独立的 DwgTextExtractor.exe 子进程，
        # 互不共享状态，可以并发跑，不用像 AutoCAD COM 那样只能单实例串行处理。
        # 线程数量给个不大的上限，避免文件很多时一次性拉起太多子进程。
        results = {}
        max_workers = min(8, len(existing_paths)) or 1
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_path = {
                executor.submit(extract_dwg_text_via_exe, p, exe_path): p
                for p in existing_paths
            }
            for future in as_completed(future_to_path):
                p = future_to_path[future]
                try:
                    results[p] = (future.result(), None)
                except Exception as e:
                    results[p] = (None, str(e))

        # SQLite 连接不是线程安全的，写数据库这一步统一放回单线程串行处理，
        # 并发只用在提取阶段（提取占了大头耗时，写库本身很快）。
        db = DWGDatabase()
        ok_count = 0
        error_count = missing_count
        try:
            for p in existing_paths:
                filename = os.path.basename(p)
                text_list, err = results[p]
                if err is not None:
                    self.progress_signal.emit(f"  ⚠️ 同步索引失败：{filename}（{err}）")
                    error_count += 1
                    continue
                try:
                    mtime = os.path.getmtime(p)
                    # 文件刚被 AutoCAD 重新保存过（文字替换会触发另存），
                    # 版本标识理论上可能跟着变（比如另存成了不同的
                    # AutoCAD 版本格式），不能沿用旧值，重新读一次。
                    dwg_version = read_dwg_version_tag(p)
                    db.update_file_index(p, filename, text_list, mtime, dwg_version=dwg_version)
                    ok_count += 1
                except Exception as e:
                    self.progress_signal.emit(f"  ⚠️ 写入数据库失败：{filename}（{e}）")
                    error_count += 1
            db.flush_batch()
        finally:
            db.close()

        self.progress_signal.emit(f"---------- 索引同步完成：成功 {ok_count} 个，失败 {error_count} 个 ----------")
        self.finished_signal.emit(ok_count, error_count)