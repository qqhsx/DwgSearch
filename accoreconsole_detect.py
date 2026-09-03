# accoreconsole_detect.py
#
# 负责两件事：
#   1) 探测本机装了哪个/哪些 AutoCAD（找 accoreconsole.exe，猜版本年份）
#   2) 根据版本年份，找到打包好的、对应 .NET 目标框架编译出来的插件 dll
#
# 背景（为什么需要这个模块）：
# NETLOAD 插件是编译时绑定 .NET 版本的，不像 COM 自动化那样"装哪个版本
# 都能用"。所以这次迁移到 accoreconsole 之后，必须在运行时探测用户机器
# 装的是哪个 AutoCAD，挑选出对应版本编译好的插件配对着用；查不到匹配
# 版本时不能直接报错崩溃，要能优雅降级（上层 replace_worker.py 负责在
# 拿不到可用配对时回退到旧的 COM 引擎）。
#
# ⚠️ ACCORECONSOLE_VERSION_MAP 这张表需要长期维护：Autodesk 差不多每年
# 会出一个新的 AutoCAD 大版本，底层 .NET 版本也可能跟着换（比如 2025~
# 2026 是 .NET 8，2027 又换成了 .NET 10），新版本出来时要往这张表里
# 补一行，并且实测一下用哪个 .NET 目标编译出来的插件能在新版本里正常
# NETLOAD——不能想当然地假设"跟上一年一样"。
#
# 探测 accoreconsole.exe 装在哪，按可信度从高到低分三层，前一层没结果
# 才落到下一层，而不是互相替代：
#   1) 用户手动指定的路径（resolve_accoreconsole_engine 的 manual_path
#      参数）——只要是用户自己选的、文件也确实存在，直接信任，不需要
#      再验证是不是"规规矩矩装在默认路径"，这是所有探测手段都失效时
#      的最终保底。
#   2) Windows 注册表（卸载信息里的 InstallLocation）——不管用户把
#      AutoCAD 装在哪个盘、起了什么目录名，安装程序都会把真实路径写
#      进注册表，这是最不依赖"猜路径规律"的办法，也是本模块的主力
#      探测方式。
#   3) 盘符 + 通配符扫描默认安装路径——注册表查不到时的兜底（比如非
#      标准安装方式、注册表被清理过等极端情况），也就是原来就有的
#      那套逻辑，现在补充了 Program Files (x86)。

import glob
import os
import re
import string

try:
    import winreg
except ImportError:  # 非 Windows 环境（比如开发机上跑单元测试）没有 winreg
    winreg = None


# 已知的 AutoCAD 版本年份 -> 编译该插件时应该用的 .NET 目标框架名。
# 这张表纯粹是"构建时的元信息"（告诉 C# 工程这个年份该用哪个 .NET
# 版本编译、日志里给人看的），不直接决定插件文件存放路径——插件文件
# 夹按年份命名（见 get_plugin_dll_path），不是按 TFM 名命名。这么拆开
# 是故意的：即使两个年份恰好用同一个 .NET 目标框架编译（比如下面
# 2025/2026 都是 net8.0-windows），也不代表这两年的 AutoCAD 托管 API
# 完全兼容、能共用同一份 dll——年份才是精确的身份标识，.NET 版本只是
# "怎么编译出这份 dll"的实现细节，不应该拿来当"这份 dll 是给谁用的"
# 这个问题的答案。
ACCORECONSOLE_VERSION_MAP = {
    2025: "net8.0-windows",
    2026: "net8.0-windows",
    2027: "net10.0-windows",
}
# 2025 之前的版本统一按 .NET Framework 4.8 处理（这条暂未实测，等有
# 老版本 AutoCAD 的机器时需要专门验证一次，不能光靠猜）。
LEGACY_NET_FRAMEWORK_TARGET = "net48"
LEGACY_CUTOFF_YEAR = 2025

PLUGIN_DLL_NAME = "AccoreconsolePoc.dll"


def _candidate_drives():
    """本机所有已挂载的固定盘符，比如 ['C:\\\\', 'D:\\\\']。
    用户的 AutoCAD 装在 D 盘就是个例子，不能只查 C 盘。"""
    drives = []
    try:
        from ctypes import windll
        bitmask = windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if bitmask & (1 << i):
                drives.append(f"{letter}:\\")
    except Exception:
        pass
    return drives or ["C:\\"]


def _guess_version_year(exe_path):
    """从路径里的 "AutoCAD 20XX" 这几个字猜版本年份。Autodesk 这个
    命名规律用了十几年没变过，是目前最不容易出错的判断依据；如果用户
    把安装目录改了名字，这里会猜不出来，返回 None（上层需要处理这种
    "查到了 exe，但猜不出版本"的情况，不能假设一定能拿到年份）。"""
    m = re.search(r"AutoCAD[\s_]*(\d{4})", exe_path, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _find_via_registry():
    """从 Windows 卸载信息里找 AutoCAD 的真实安装路径。

    AutoCAD 安装程序会在卸载注册表项里写 InstallLocation，这个值就是
    用户实际选的安装目录，不管装在哪个盘、目录名有没有改过都能找到，
    不依赖"路径长得像默认路径"这个假设。同时查 64 位和 32 位两个视图
    （WOW6432Node），因为不确定装机时走的是哪一套注册表视图。

    返回形如 [{"accoreconsole_path": "...", "year": 2027}, ...]，查不到
    或者不在 Windows 上跑（winreg 不存在）时返回空列表——这只是三层
    探测里的一层，允许查不到，由上层继续尝试其它层。
    """
    if winreg is None:
        return []

    uninstall_subpaths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]

    results = []
    for subpath in uninstall_subpaths:
        try:
            root_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subpath)
        except OSError:
            continue

        try:
            i = 0
            while True:
                try:
                    entry_name = winreg.EnumKey(root_key, i)
                except OSError:
                    break  # 没有更多子项了，正常结束
                i += 1

                try:
                    entry_key = winreg.OpenKey(root_key, entry_name)
                except OSError:
                    continue

                try:
                    try:
                        display_name, _ = winreg.QueryValueEx(entry_key, "DisplayName")
                    except FileNotFoundError:
                        continue
                    if "autocad" not in display_name.lower():
                        continue

                    try:
                        install_location, _ = winreg.QueryValueEx(entry_key, "InstallLocation")
                    except FileNotFoundError:
                        continue
                    if not install_location:
                        continue

                    exe_path = os.path.join(install_location, "accoreconsole.exe")
                    if not os.path.exists(exe_path):
                        continue

                    # 优先从卸载条目自身携带的版本年份猜，猜不到再退回
                    # 从安装路径里猜（两者理论上应该一致，但注册表里的
                    # DisplayName 有时候更干净，比如带斜杠/年份的机型号）
                    year = _guess_version_year(display_name) or _guess_version_year(exe_path)
                    results.append({"accoreconsole_path": exe_path, "year": year})
                finally:
                    entry_key.Close()
        finally:
            root_key.Close()

    return results


def _find_via_path_scan():
    """盘符 + 通配符扫描默认安装路径，注册表查不到时的兜底方案。

    只覆盖"规规矩矩装在默认路径"的情况；用户自定义了安装目录名或者
    装在非常规位置的话，这一层是找不到的，得靠注册表那一层或者用户
    手动指定。"""
    results = []
    program_files_dirs = ["Program Files", "Program Files (x86)"]
    for drive in _candidate_drives():
        for pf_dir in program_files_dirs:
            pattern = os.path.join(drive, pf_dir, "Autodesk", "AutoCAD *", "accoreconsole.exe")
            try:
                matches = glob.glob(pattern)
            except Exception:
                matches = []
            for exe_path in matches:
                results.append({
                    "accoreconsole_path": exe_path,
                    "year": _guess_version_year(exe_path),
                })
    return results


def find_accoreconsole_installations():
    """探测本机全部 accoreconsole.exe 及其推断出的版本年份。

    依次尝试注册表探测和盘符扫描，两边结果合并去重（按路径去重，同一
    个 exe 只保留一份）。返回形如
    [{"accoreconsole_path": "...", "year": 2027}, ...] 的列表，year 可能
    是 None（找到了 exe 但猜不出版本，见 _guess_version_year）。一台
    机器可能装了不止一个 AutoCAD 版本，这里返回全部结果，挑选逻辑交给
    resolve_accoreconsole_engine()。
    """
    combined = {}
    for item in _find_via_registry() + _find_via_path_scan():
        key = os.path.normcase(os.path.normpath(item["accoreconsole_path"]))
        if key not in combined or (combined[key]["year"] is None and item["year"] is not None):
            combined[key] = item
    return list(combined.values())


def get_plugin_target_for_year(year):
    """给定 AutoCAD 版本年份，返回这个版本"官方声称"应该用哪个 .NET
    目标框架编译（纯信息性，给日志/构建脚本参考）。查不到时返回
    (None, 原因说明)。注意：这个返回值不代表"能不能用"——真正判断
    能不能用，看的是 get_plugin_dll_path() 里对应年份文件夹下的 dll
    存不存在，两者是分开的两件事，见本文件顶部的说明。"""
    if year is None:
        return None, "无法从安装路径判断 AutoCAD 版本年份"
    if year < LEGACY_CUTOFF_YEAR:
        return LEGACY_NET_FRAMEWORK_TARGET, None
    target = ACCORECONSOLE_VERSION_MAP.get(year)
    if target is None:
        return None, (
            f"AutoCAD {year} 还没有登记在 ACCORECONSOLE_VERSION_MAP 里，"
            f"需要确认这个版本用的 .NET 版本、补充映射并验证插件能正常 NETLOAD"
        )
    return target, None


def get_plugin_dll_path(year, plugin_root_dir):
    """给定版本年份和插件打包根目录，找该年份对应的插件 dll。

    插件文件夹按年份命名（plugin_root_dir/2027/AccoreconsolePoc.dll），
    不是按 .NET 目标框架命名——原因见本文件顶部的说明。"""
    if year is None:
        return None, "无法从安装路径判断 AutoCAD 版本年份"
    dll_path = os.path.join(plugin_root_dir, str(year), PLUGIN_DLL_NAME)
    if not os.path.exists(dll_path):
        target, _ = get_plugin_target_for_year(year)
        target_hint = f"（该用 {target} 编译）" if target else "（这个年份还没有已知的 .NET 目标框架映射）"
        return None, f"预期的插件文件不存在：{dll_path}{target_hint}，是否忘了编译/打包这个版本？"
    return dll_path, None


def resolve_accoreconsole_engine(plugin_root_dir, manual_path=None):
    """给 replace_worker.py 用的主入口：探测本机 AutoCAD 安装，挑一个
    能配对上插件的 accoreconsole + dll 组合。

    manual_path：用户在设置里手动指定的 accoreconsole.exe 路径（来自
    config.get_accoreconsole_manual_path()）。这是三层探测里优先级最高
    的一层——用户已经明确告诉我们路径了，就不需要再靠注册表/盘符扫描
    去猜，直接校验文件存在、猜一下版本年份、去配对插件 dll 即可。只有
    手动路径本身不存在或者用户没设置过（为 None/空字符串）时，才继续
    往下走自动探测。

    自动探测这块：多个版本都装了的情况下，优先选年份最新的（大概率是
    用户日常在用的那个）。一个都配不上时返回 (None, None, 原因列表)，
    上层应该据此回退到旧的 COM 引擎，而不是直接报错——这是版本适配这
    块必须有的兜底行为，"查不到匹配版本"应该是可预期的正常分支，不是
    异常状态。
    """
    reasons = []

    if manual_path:
        if os.path.exists(manual_path):
            year = _guess_version_year(manual_path)
            dll_path, reason = get_plugin_dll_path(year, plugin_root_dir)
            if dll_path:
                return manual_path, dll_path, None
            year_label = year if year is not None else "未知版本"
            reasons.append(f"手动指定的路径（AutoCAD {year_label}，{manual_path}）：{reason}")
            # 手动路径本身没问题，只是配不上插件 dll——不再退回自动探测，
            # 因为用户既然手动指定了，大概率就是想用这个版本，自动探测
            # 出别的版本反而会让人困惑；直接把原因报出去，让用户/上层
            # 决定要不要回退到 COM 引擎。
            return None, None, reasons
        else:
            reasons.append(f"手动指定的路径不存在：{manual_path}，回退到自动探测")

    installs = find_accoreconsole_installations()
    if not installs:
        reasons.append("本机没有探测到任何 AutoCAD（找不到 accoreconsole.exe）")
        return None, None, reasons

    installs.sort(key=lambda x: x["year"] or 0, reverse=True)

    for inst in installs:
        dll_path, reason = get_plugin_dll_path(inst["year"], plugin_root_dir)
        if dll_path:
            return inst["accoreconsole_path"], dll_path, None
        year_label = inst["year"] if inst["year"] is not None else "未知版本"
        reasons.append(f"AutoCAD {year_label}（{inst['accoreconsole_path']}）：{reason}")

    return None, None, reasons


if __name__ == "__main__":
    # 独立跑一下这个文件，方便在具体某台机器上直接看探测结果，
    # 不用接进主程序也能验证这个模块本身对不对。
    print("探测到的 AutoCAD 安装：")
    for inst in find_accoreconsole_installations():
        print(f"  年份={inst['year']}  路径={inst['accoreconsole_path']}")

    plugin_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AccoreconsolePlugin")
    exe_path, dll_path, reasons = resolve_accoreconsole_engine(plugin_root)
    print("\n匹配结果：")
    if exe_path:
        print(f"  accoreconsole = {exe_path}")
        print(f"  plugin dll    = {dll_path}")
    else:
        print("  没有可用的匹配，原因：")
        for r in reasons:
            print(f"    - {r}")