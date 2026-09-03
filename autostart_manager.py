# autostart_manager.py
#
# "随系统自启动"：Windows 登录后自动把本程序拉起来，不用用户自己手动
# 双击图标。跟 context_menu_integration.py 的右键菜单集成是同一类"系统
# 级开关"，实现思路也一样——写注册表，而不是往"启动"文件夹里放快捷方式
# （放快捷方式还要额外生成 .lnk 文件、处理路径变化后快捷方式失效的问题，
# 注册表这条路更简单、也是大多数桌面软件的标准做法）。
#
# 写在：
#   HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run
# 而不是 HKEY_LOCAL_MACHINE 下同名位置——后者是"给这台电脑上所有用户
# 自启动"，写入需要管理员权限；HKCU 只对当前登录用户生效，普通权限
# 就能读写，跟右键菜单集成选 HKCU 的理由完全一样。
#
# 启动时额外带一个 --minimized 参数（见 main.py）：开机自启动这种场景，
# 用户通常是希望软件"悄悄在后台把索引跑起来"，不是希望一开机就弹出
# 一个主窗口糊在桌面上挡着看不见的地方——托盘图标照常出现，双击就能
# 唤出主窗口，跟手动从托盘打开是一回事。
import os
import sys

try:
    import winreg
except ImportError:  # 非 Windows 环境（比如开发机跑单元测试）没有 winreg
    winreg = None

_RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE_NAME = "DWGSearchTool_V6"


def _build_command():
    """拼出注册表里要写的那条完整启动命令行，带上 --minimized 参数。

    源码直接跑（没打包成 exe）的情况下，要把 python 解释器路径和脚本
    路径都带上；打包成 exe 之后 sys.executable 本身就是这个程序，直接
    带参数就行——跟 context_menu_integration.py 的 _build_command() 是
    同一套逻辑。
    """
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return f'"{exe}" --minimized'
    script = os.path.abspath(sys.argv[0])
    return f'"{sys.executable}" "{script}" --minimized'


def is_available():
    """当前环境支不支持这个功能（非 Windows 环境下 winreg 不存在，
    这个功能本身也没有意义——"开机自启动"是 Windows 特有的概念）。"""
    return winreg is not None


def is_autostart_enabled():
    """注册表里是不是已经写上了自启动项。"""
    if not is_available():
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH)
        try:
            winreg.QueryValueEx(key, _RUN_VALUE_NAME)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        return False
    except Exception:
        return False


def enable_autostart():
    """写入自启动注册表项。返回 (ok, error_message)。"""
    if not is_available():
        return False, "当前环境不支持（这个功能只在 Windows 上有意义）"
    try:
        command = _build_command()
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH) as key:
            winreg.SetValueEx(key, _RUN_VALUE_NAME, 0, winreg.REG_SZ, command)
        return True, None
    except Exception as e:
        return False, str(e)


def disable_autostart():
    """删掉自启动注册表项。返回 (ok, error_message)。项本来就不存在
    （FileNotFoundError）算正常，不当成失败——跟 context_menu_integration
    里 disable_context_menu() 的处理方式一致。"""
    if not is_available():
        return False, "当前环境不支持（这个功能只在 Windows 上有意义）"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, _RUN_VALUE_NAME)
            except FileNotFoundError:
                pass
        return True, None
    except FileNotFoundError:
        return True, None
    except Exception as e:
        return False, str(e)