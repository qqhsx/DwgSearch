# context_menu_integration.py
#
# "集成到右键菜单"：在 Windows 资源管理器里，右键点一个文件夹（或者在
# 文件夹内部空白处右键），菜单里加一项"用 DWG 图纸搜索工具搜索此目录"，
# 点了直接把这个目录作为搜索范围打开本软件，不用先手动打开软件、再
# 手动把目录加进"搜索目录"。
#
# 实现方式是往注册表写两条 shell 扩展：
#   HKCU\Software\Classes\Directory\shell\<name>\command
#       —— 右键点在"某个文件夹"上时触发，Windows 传的是 %1（那个
#          文件夹的路径）
#   HKCU\Software\Classes\Directory\Background\shell\<name>\command
#       —— 在文件夹"内部空白处"右键时触发（这时候没有具体点中哪个
#          文件夹，Windows 传的是 %V，代表"当前正浏览着的这个目录"）
# 两条注册的显示文字、指向的程序命令都一样，只是触发场景不同，两个
# 都注册上体验才完整（只注册第一条的话，进到一个目录里面之后，在
# 空白处右键是看不到这一项的，只能在目录还没打开、从上一级目录看着
# 这个文件夹图标时右键才有）。
#
# 特意写在 HKEY_CURRENT_USER 下而不是 HKEY_CLASSES_ROOT / HKEY_LOCAL_
# MACHINE——后两个是系统级注册表，写入需要管理员权限，用户得对着UAC
# 弹窗点"是"才行，体验很重；HKCU 是当前用户自己的注册表分支，普通权限
# 就能读写，Windows 在解析右键菜单时本来就会同时查 HKCR（系统级）和
# HKCU\Software\Classes（用户级，且优先级更高），效果完全一样，是目前
# 各种桌面软件做"添加右键菜单"这种用户级功能时的标准做法。
import os
import sys

try:
    import winreg
except ImportError:  # 非 Windows 环境（比如开发机跑单元测试）没有 winreg
    winreg = None

_MENU_KEY_NAME = "DWGSearchTool_V6"
_MENU_DISPLAY_TEXT = "用 DWG 图纸搜索工具搜索此目录"

_REGISTRY_BASES = (
    (r"Software\Classes\Directory\shell", "%1"),
    (r"Software\Classes\Directory\Background\shell", "%V"),
)


def _build_command(placeholder):
    """拼出注册表里 command 键要写的那条完整命令行。

    源码直接跑（没打包成 exe）的情况下，要把 python 解释器路径和脚本
    路径都带上，不然 Windows 不知道用什么去执行这个 .py 文件；打包成
    exe 之后 sys.executable 本身就是这个程序，直接带参数就行。
    """
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return f'"{exe}" --search-folder "{placeholder}"'
    script = os.path.abspath(sys.argv[0])
    return f'"{sys.executable}" "{script}" --search-folder "{placeholder}"'


def _get_icon_path():
    """右键菜单这一项要显示的图标，格式是 Windows 认的
    "文件路径,图标索引"（跟 cmd_here/Git GUI/Git Bash 这些右键菜单项
    用的是同一套写法）。找不到可用图标就返回 None，调用方会跳过
    Icon 这个值不写——不写就是 Windows 默认的空白占位图标，跟以前
    的表现一样，不会因为找不到图标就把整条菜单注册搞失败。

    - 打包成 exe 之后：build.spec 里 `icon=app.ico` 那行已经把
      app.ico 编译进了 exe 自己的图标资源，直接指向 exe 本身、
      取第 0 个图标资源即可，不需要单独再找一份 .ico 文件、也不用
      担心 exe 挪动位置——`sys.executable` 永远是它自己当前的真实
      路径。
    - 源码直接跑的开发模式下：`sys.executable` 是 python.exe，指过去
      只会显示 Python 的图标，不是这个程序的图标，这时候改成直接
      指向项目目录下的 app.ico 文件本身（构建打包时用的就是这一份，
      跟打包后显示的图标是同一个，开发模式下预览效果跟正式安装后
      一致）。
    """
    if getattr(sys, "frozen", False):
        return f"{sys.executable},0"
    ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")
    if os.path.exists(ico_path):
        return f"{ico_path},0"
    return None


def is_available():
    """当前环境支不支持这个功能（非 Windows 环境下 winreg 不存在，
    这个功能本身也没有意义——Explorer 右键菜单是 Windows 特有的东西）。"""
    return winreg is not None


def is_context_menu_enabled():
    """两条注册表项是不是都已经写上了。只要有一条没写上就算"未启用"，
    这样"启用"按钮的行为始终是幂等的——不会出现"点了没反应，因为其中
    一条其实已经存在了"这种半吊子状态。"""
    if not is_available():
        return False
    for base_path, _ in _REGISTRY_BASES:
        key_path = f"{base_path}\\{_MENU_KEY_NAME}\\command"
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path)
            winreg.CloseKey(key)
        except FileNotFoundError:
            return False
        except Exception:
            return False
    return True


def enable_context_menu():
    """写入两条注册表项。返回 (ok, error_message)。"""
    if not is_available():
        return False, "当前环境不支持（这个功能只在 Windows 上有意义）"
    try:
        icon_path = _get_icon_path()
        for base_path, placeholder in _REGISTRY_BASES:
            key_path = f"{base_path}\\{_MENU_KEY_NAME}"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, _MENU_DISPLAY_TEXT)
                # Icon 是 shell 扩展这一层的可选值，跟"(默认)"显示文字
                # 写在同一个键下，不是写在 \command 子键里——放错位置
                # Explorer 不会报错，只是图标不会显示，容易踩坑。
                if icon_path:
                    winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon_path)
            command = _build_command(placeholder)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path + "\\command") as cmd_key:
                winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, command)
        return True, None
    except Exception as e:
        return False, str(e)


def disable_context_menu():
    """删掉两条注册表项。返回 (ok, error_message)。command 子键要先删，
    再删它的父键——Windows 注册表 API 不允许直接删一个还有子键的键。
    两条里有一条本来就不存在（FileNotFoundError）算正常，不当成失败。
    """
    if not is_available():
        return False, "当前环境不支持（这个功能只在 Windows 上有意义）"
    try:
        for base_path, _ in _REGISTRY_BASES:
            key_path = f"{base_path}\\{_MENU_KEY_NAME}"
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path + "\\command")
            except FileNotFoundError:
                pass
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
            except FileNotFoundError:
                pass
        return True, None
    except Exception as e:
        return False, str(e)