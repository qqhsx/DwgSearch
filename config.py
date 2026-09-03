# config.py
import os
import sys
import json
from PyQt5.QtWidgets import QComboBox


def _get_app_base_dir():
    r"""程序自身所在目录（不是当前工作目录 CWD）。

    之前 CONFIG_FILE 直接写死成裸文件名 "config.json"，Python 会把它
    按"当前工作目录"解析——正常双击打开软件时 CWD 刚好就是程序所在
    目录，看不出问题；但通过右键菜单集成（context_menu_integration.py）
    触发时，Windows 传给新进程的工作目录是**被右键点击的那个目标目录**，
    不是程序安装目录，"config.json" 就会被解析成"目标目录\config.json"，
    在用户随便一个图纸文件夹里凭空多出一个配置文件、还读不到程序自己
    那份真正的配置。

    这里改成跟 get_extractor_path()/get_accoreconsole_plugin_root() 一样
    的取法：跟 CWD 完全无关，永远指向程序自身安装目录（打包成 exe 后
    是 exe 所在目录，源码直接跑的开发模式下是这个 .py 文件所在目录）。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_FILE = os.path.join(_get_app_base_dir(), "config.json")

# 菜单栏"关于"、窗口标题这些地方都要用到同一个版本号/程序名，统一定义
# 在这里，改版本号只用改这一处，不会出现标题栏写的是 V6.2、"关于"弹窗
# 却忘了同步改还是旧版本号这种两处对不上的问题。
APP_NAME = "DwgSearch"
APP_VERSION = "V2.19.0"


def get_app_data_dir():
    """应用私有数据目录（数据库、程序日志等都统一放这里）：
    Windows 上是 %APPDATA%\\DWGSearch，非 Windows 退化到用户主目录下的
    .DWGSearch，跟 get_default_db_path() 原来的取法保持一致，这里单独
    抽出来是因为日志文件（log_utils.py）现在也要用同一个目录，不想
    在两个模块里各写一份"取 APPDATA 目录"的逻辑。
    """
    app_data = os.environ.get("APPDATA", os.path.expanduser("~"))
    data_dir = os.path.join(app_data, "DWGSearch")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def get_log_file_path():
    """程序日志文件路径，供菜单栏"帮助 -> 程序日志"直接打开用。"""
    return os.path.join(get_app_data_dir(), "app.log")

def path_is_within(path, ancestor):
    """判断 path 是否等于 ancestor，或者是 ancestor 目录下的真子路径。

    全项目任何地方只要涉及"这个路径是不是落在那个目录下面"，都应该
    调用这个统一函数，不要自己再写一遍 `startswith`。原因：直接拿
    字符串做 `path.startswith(ancestor)` 会把 "C:\\FooBar" 误判成落在
    "C:\\Foo" 下面——两者只是字符串前缀凑巧一样，实际上是毫不相干的
    两个目录。这里按路径分隔符严格对齐：要么完全相等，要么是 ancestor
    后面紧跟一个路径分隔符再往下的真子路径，才算数。

    两个参数都会在内部自己做 normpath + lower，调用方不需要预先处理。
    """
    norm_path = os.path.normpath(path).lower()
    norm_ancestor = os.path.normpath(ancestor).lower()
    return norm_path == norm_ancestor or norm_path.startswith(norm_ancestor.rstrip(os.sep) + os.sep)


def is_path_excluded(path, exclude_folders):
    """判断 path 是否命中 exclude_folders 里的某一条排除规则。

    具体的路径包含关系判断统一交给 path_is_within()，这里只负责
    "跟列表里每一条规则挨个比对一遍"这一层。
    """
    return any(path_is_within(path, ex) for ex in exclude_folders)


# 默认数据库路径：用户 AppData\Roaming 目录下
def get_default_db_path():
    return os.path.join(get_app_data_dir(), "dwg_index.db")


def get_db_path():
    """读取当前配置的数据库路径，未设置则返回默认路径"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            p = config.get("db_path", "")
            if p and os.path.isdir(os.path.dirname(p)):
                return p
        except Exception:
            pass
    return get_default_db_path()

def save_db_path(new_path):
    """单独保存数据库路径到配置文件"""
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["db_path"] = new_path
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


# 批量替换的备份根目录：跟数据库默认路径同一个思路——默认放在软件
# 私有的 AppData 目录下（不需要用户每次执行前手动选一遍），但允许
# 改到别的位置（比如想统一存在某个团队共享盘上）。每次正式执行会在
# 这个根目录下新建一个带时间戳的子目录，具体见 backup_manager.py。
def get_default_backup_root():
    return os.path.join(get_app_data_dir(), "replace_backups")


def get_backup_root():
    """读取当前配置的备份根目录，未设置则返回默认路径"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            p = config.get("backup_root", "")
            if p:
                return p
        except Exception:
            pass
    return get_default_backup_root()


def save_backup_root(new_path):
    """单独保存备份根目录到配置文件"""
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["backup_root"] = new_path
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


# 备份批次保留数量：每次正式执行都会新建一个时间戳批次目录，长期不清理
# 会一直堆积占用磁盘。0 表示不限制（不自动清理，交给用户自己在"从备份
# 恢复"弹窗里手动删）。
def get_backup_max_runs():
    default = 20
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            v = config.get("backup_max_runs", None)
            if isinstance(v, int) and v >= 0:
                return v
        except Exception:
            pass
    return default


def save_backup_max_runs(n):
    """单独保存备份批次保留数量到配置文件"""
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["backup_max_runs"] = int(n)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


# 批量替换主界面"文件列表"表格的列宽记忆：用户手动拖动调整过之后，
# 希望下次打开还是上次调的样子，不用每次都重新拖一遍。存成一个按列
# 序号对齐的整数列表，[]（空列表）表示还没保存过，交给控件自己的
# 默认列宽。
def get_replace_file_table_column_widths():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            v = config.get("replace_file_table_column_widths", [])
            if isinstance(v, list):
                return v
        except Exception:
            pass
    return []


def save_replace_file_table_column_widths(widths):
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["replace_file_table_column_widths"] = list(widths)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


# 主界面搜索结果表格的列宽记忆，跟上面"文字替换"对话框里文件列表的
# 记忆是同一套模式，只是存到另一个配置字段里，两边互不影响。
def get_main_table_column_widths():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            v = config.get("main_table_column_widths", [])
            if isinstance(v, list):
                return v
        except Exception:
            pass
    return []


def save_main_table_column_widths(widths):
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["main_table_column_widths"] = list(widths)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


# 主界面搜索结果表格里，"没有任何配置记录时"默认隐藏的列——目前只有
# "DWG版本"一项，见 get_main_table_hidden_columns() 里的说明。
_DEFAULT_HIDDEN_COLUMNS = ["DWG版本"]


def get_main_table_hidden_columns():
    """主界面搜索结果表格里，用户手动隐藏掉的列（存列名，不存列序号——
    列序号会因为以后表格改版而错位，列名更稳妥）。

    "DWG版本"这一列默认是隐藏的（_DEFAULT_HIDDEN_COLUMNS）。这个默认值
    最初设置的原因是：读取版本号曾经比单纯 os.stat() 更重（要真的打开
    一次文件句柄，网络共享盘上尤其明显），所以默认不读、用户自己想看
    再去表头右键/设置里打开。

    V2.19.1 之后这个原因已经不成立了：版本号改成建索引时读一次、存进
    数据库（见 database.py 的 dwg_version 列 / search_dwg_index），搜索
    结果表格填这一列时直接从数据库查询结果里拿，不再需要临时开文件——
    这一列现在跟其余列一样"免费"，默认隐藏纯粹是历史遗留的产品选择，
    保留下来是为了不打乱老用户已经习惯的默认界面布局，不是因为性能
    顾虑。如果想把这一列默认改成显示，直接调整 _DEFAULT_HIDDEN_COLUMNS
    就行，不用再担心开销问题。

    这个默认值只在"配置文件里压根没存过这一项"（比如全新安装、或者
    升级前的老配置文件里还没有这个字段）时才生效——一旦用户自己在
    界面上调整过显示列（哪怕只是把某一列打开又关掉），
    save_main_table_hidden_columns() 就会把这一项真正写进配置文件，
    之后 config.get(...) 就能读到用户的真实选择，不会再被这个默认值
    覆盖。"""
    default_hidden = _DEFAULT_HIDDEN_COLUMNS
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            v = config.get("main_table_hidden_columns", default_hidden)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, str)]
        except Exception:
            pass
    return list(default_hidden)


def save_main_table_hidden_columns(hidden_names):
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["main_table_hidden_columns"] = list(hidden_names)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


def get_main_search_splitter_sizes():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            v = config.get("main_search_splitter_sizes", [])
            if isinstance(v, list) and len(v) == 2 \
                    and all(isinstance(x, int) and x > 0 for x in v):
                return v
        except Exception:
            pass
    return []


def save_main_search_splitter_sizes(sizes):
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["main_search_splitter_sizes"] = list(sizes)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


def get_default_extract_workers():
    """默认并发数按CPU核数推算：核数-1（给系统和界面留一个核不占满），
    封顶在2~8之间——8核以上的机器也不建议无脑往上堆，图纸提取主要
    受限于外部程序启动开销和磁盘/网络IO，核数堆得再高帮助也有限，
    反而更容易把磁盘/网络这些非CPU资源占满。用户自己觉得电脑还有
    余力的话，可以在界面里手动调大。"""
    cpu_count = os.cpu_count() or 4
    return max(2, min(8, cpu_count - 1))

def get_extract_workers():
    """读取当前配置的内容提取并发数，未设置（0）则按CPU核数推算默认值"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            v = int(config.get("extract_workers", 0) or 0)
            if v > 0:
                return v
        except Exception:
            pass
    return get_default_extract_workers()

def save_extract_workers(worker_count):
    """单独保存内容提取并发数到配置文件"""
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["extract_workers"] = int(worker_count)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


def get_replace_scan_options():
    """读取"批量文字替换"上次记住的勾选状态，未设置过或读取失败就用
    DEFAULT_CONFIG 里的默认值（只勾单行/多行文字）。"""
    default = DEFAULT_CONFIG["replace_scan_options"].copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            saved = config.get("replace_scan_options")
            if isinstance(saved, dict):
                # 用 saved 里有的键覆盖默认值，缺的键（比如以后新增了
                # 类型）继续用默认值兜底，不会因为老配置文件缺字段报错。
                default.update({k: v for k, v in saved.items() if k in default})
        except Exception:
            pass
    return default


def save_replace_scan_options(options):
    """单独保存"批量文字替换"这次的勾选状态到配置文件。"""
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["replace_scan_options"] = dict(options)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


def get_search_regex_options():
    """读取"文件名/内容"两个搜索框的正则模式开关上次记住的状态。"""
    default = dict(DEFAULT_CONFIG["search_regex_options"])
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            saved = config.get("search_regex_options")
            if isinstance(saved, dict):
                default.update({k: v for k, v in saved.items() if k in default})
        except Exception:
            pass
    return default


def save_search_regex_options(options):
    """单独保存"文件名/内容"两个搜索框这次的正则模式开关状态到配置文件。
    options 结构：{"filename_regex": bool, "content_regex": bool}"""
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["search_regex_options"] = {
        "filename_regex": bool(options.get("filename_regex", False)),
        "content_regex": bool(options.get("content_regex", False)),
    }
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


def get_search_filter_options():
    """
    读取主搜索栏"文字类型 / 搜索位置 / 块定义范围"三组筛选勾选框上次记住
    的状态，未设置过或读取失败就用 DEFAULT_CONFIG 里的默认值（全部勾选，
    等价于不筛选）。
    返回结构：{"entity_types": {...}, "spaces": {...}, "scopes": {...}}
    """
    default = {
        group: DEFAULT_CONFIG["search_filter_options"][group].copy()
        for group in ("entity_types", "spaces", "scopes")
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            saved = config.get("search_filter_options")
            if isinstance(saved, dict):
                for group in default:
                    saved_group = saved.get(group)
                    if isinstance(saved_group, dict):
                        # 只用 saved 里认识的键覆盖默认值，缺的键（比如以后
                        # 新增了类型）继续用默认值兜底，不会因为老配置文件
                        # 缺字段报错，也不会把不认识的旧键带进来。
                        default[group].update(
                            {k: v for k, v in saved_group.items() if k in default[group]}
                        )
        except Exception:
            pass
    return default


def save_search_filter_options(options):
    """单独保存主搜索栏三组筛选勾选框这次的勾选状态到配置文件。
    options 结构：{"entity_types": {...}, "spaces": {...}, "scopes": {...}}"""
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["search_filter_options"] = {
        "entity_types": dict(options.get("entity_types", {})),
        "spaces": dict(options.get("spaces", {})),
        "scopes": dict(options.get("scopes", {})),
    }
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


def get_search_filter_row_expanded():
    """读取搜索栏那排"文字类型/搜索位置/块定义范围"筛选框上次是展开
    还是收起，读取失败就用默认值（收起）。"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            return bool(config.get("search_filter_row_expanded",
                                    DEFAULT_CONFIG["search_filter_row_expanded"]))
        except Exception:
            pass
    return DEFAULT_CONFIG["search_filter_row_expanded"]


def save_search_filter_row_expanded(expanded):
    """单独保存筛选框这次是展开还是收起的状态。"""
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["search_filter_row_expanded"] = bool(expanded)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


REPLACE_ENGINE_CHOICES = ("accoreconsole", "com", "acadsharp")


def get_replace_engine():
    """读取"批量文字替换"当前用哪个引擎：
      - "accoreconsole"（默认，accoreconsole.exe + NETLOAD 插件——软件主要
        就是围绕这套方案设计的，所以默认选它，没设置过配置文件时也一样）
      - "com"（AutoCAD COM + ObjectDBX）
      - "acadsharp"（纯 .NET 的 ACadSharp 读写库，本机不需要装 AutoCAD，
        但标注类型的替换范围比另外两个引擎窄，见 acadsharp_engine.py
        顶部说明）
    三者都需要本机装 AutoCAD 才能用的前提只对前两个成立。"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            v = config.get("replace_engine", "accoreconsole")
            if v in REPLACE_ENGINE_CHOICES:
                return v
        except Exception:
            pass
    return "accoreconsole"


def save_replace_engine(engine):
    """单独保存"批量文字替换"引擎选择到配置文件。"""
    if engine not in REPLACE_ENGINE_CHOICES:
        return
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["replace_engine"] = engine
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


# 兼容单文件夹/单文件两种打包模式的资源目录定位：
# - 单文件模式（onefile）：datas 被解压到临时目录 sys._MEIPASS
# - 单文件夹模式（onedir，PyInstaller 5.x 平铺 / 源码运行）：_MEIPASS 不存在或
#   就等于程序目录，回退到 exe 所在目录 / 源码文件所在目录即可。
def _get_resource_base():
    import sys
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        return meipass
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# DwgTextExtractor.exe 固定放在主程序同目录下的固定子目录，
# 打包分发时一起拷走即可，不需要用户配置——普通用户不需要知道这是什么。
def get_extractor_path():
    return os.path.join(_get_resource_base(), "DwgTextExtractor", "DwgTextExtractor.exe")


# DwgTextReplacer.exe（批量替换的第三个引擎，见 acadsharp_engine.py）
# 跟 get_extractor_path() 是同一个思路：固定路径，不需要探测——它不依赖
# 本机装没装 AutoCAD，只是打包时跟主程序放在一起的一个独立子程序。
def get_dwg_replacer_path():
    return os.path.join(_get_resource_base(), "DwgTextReplacer", "DwgTextReplacer.exe")


# accoreconsole 插件（多目标编译产物）固定放在主程序同目录下的固定子
# 目录，跟 get_extractor_path() 是同一个思路——打包分发时整个文件夹
# 一起拷走，accoreconsole_detect.resolve_accoreconsole_engine() 会在
# 这个目录下按 .NET 目标框架子目录去找对应版本的 dll。
def get_accoreconsole_plugin_root():
    return os.path.join(_get_resource_base(), "AccoreconsolePlugin")


# accoreconsole.exe 的自动探测（注册表 + 盘符扫描，见 accoreconsole_
# detect.py）覆盖不了所有情况——用户可能把 AutoCAD 装在很奇怪的地方，
# 或者注册表信息缺失。这里存用户手动指定的路径作为最终兜底，一旦设置
# 过就优先信任，不用每次都重新自动探测。空字符串表示没设置过，交给
# 自动探测。
def get_accoreconsole_manual_path():
    """读取用户手动指定的 accoreconsole.exe 路径，没设置过返回空字符串。"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            v = config.get("accoreconsole_manual_path", "")
            if isinstance(v, str):
                return v
        except Exception:
            pass
    return ""


def save_accoreconsole_manual_path(path):
    """保存用户手动指定的 accoreconsole.exe 路径；传空字符串/None 表示
    清空设置，改回自动探测。"""
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["accoreconsole_manual_path"] = path or ""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass

# 默认的初始化白账本结构（增加了最后一次路径的槽位）
# 默认内置排除目录（系统目录，基本不含 DWG）
DEFAULT_EXCLUDE_FOLDERS = [
    "C:\\Windows",
    "C:\\Program Files",
    "C:\\Program Files (x86)",
    "C:\\ProgramData",
    "C:\\$Recycle.Bin",
    "C:\\System Volume Information",
]

DEFAULT_CONFIG = {
    # "文件名/内容"两个搜索框各自的"正则表达式模式"开关，默认都关闭——
    # 普通用户不了解正则语法，默认应该按普通子串关键词解析，不能让
    # 日常输入（文件名里常见的 . ( ) + 这些字符）被当成正则元字符处理。
    "search_regex_options": {
        "filename_regex": False,
        "content_regex": False,
    },
    # "搜索目录"那一排可勾选的目录标签（FolderScopeBar），按显示顺序存
    # [{"path": str, "checked": bool}, ...]。空列表 = 没有固定任何目录 =
    # 全部目录都搜，跟以前 path_edit 里"🖥 全部目录"是同一个语义，不需要
    # 单独记一个"是不是全部目录"的开关。
    "search_scope_folders": [],
    "filename_keyword_history": [],
    "content_keyword_history": [],
    "db_path": "",
    "backup_root": "",
    "backup_max_runs": 20,
    "replace_file_table_column_widths": [],
    "main_table_column_widths": [],
    # 主界面搜索结果表格里，用户手动隐藏掉的列名列表。这个键必须
    # 出现在 DEFAULT_CONFIG 里——本文件里所有 save_xxx() 函数都是
    # "以 DEFAULT_CONFIG 的键为准，把旧配置文件里同名的值捞回来，再
    # 覆盖写回整个文件"这个写法，任何一个不在 DEFAULT_CONFIG 里的键，
    # 会在下一次调用*别的*任意一个 save_xxx() 时被整体覆盖时悄悄丢失
    # （因为它不在"要捞回来"的键名列表里）。
    #
    # 默认值特意跟 _DEFAULT_HIDDEN_COLUMNS 保持一致（而不是空列表 []）：
    # 如果这里随便写成 []，一旦用户还没手动碰过"显示列"设置、但触发了
    # 任意*别的* save_xxx() 调用（这在正常使用中很容易发生——搜索关键
    # 词历史、分隔线位置等等随手一动就会存盘），DEFAULT_CONFIG 里这个
    # "假默认值" []（代表"全部列都显示"）就会被当成用户的真实选择，
    # 提前写进配置文件，"DWG版本默认隐藏"这个设计就整个失效了。
    "main_table_hidden_columns": list(_DEFAULT_HIDDEN_COLUMNS),
    # 顶部"搜索文件名/搜索文件内容"两个框中间那条可拖拽分隔线的位置，
    # 存 QSplitter.sizes() 原样返回的 [左宽, 右宽] 两个整数（单位像素）。
    # 空列表 = 没手动拖动过，用初始的左右各半。
    "main_search_splitter_sizes": [],
    "exclude_folders": [],
    # 排除目录管理弹窗里的两个开关：是否启用内置系统目录排除、是否
    # 启用用户自定义排除目录，默认都启用。关掉某个开关时，对应类别
    # 的条目会变灰、暂时不生效，但列表数据还在，不会被删掉。
    "exclude_system_dirs_enabled": True,
    "exclude_custom_enabled": True,
    "extract_workers": 0,  # 0 表示未设置，用get_extract_workers()里的默认推算逻辑
    "window_width": 0,
    "window_height": 0,
    "window_x": -1,
    "window_y": -1,
    # "批量文字替换"用哪个引擎：accoreconsole（默认，软件主要方案）或
    # com（旧的 AutoCAD COM + ObjectDBX，兼容/备用）。
    "replace_engine": "accoreconsole",
    # 用户手动指定的 accoreconsole.exe 路径，空字符串 = 没设置过，交给
    # 自动探测（注册表 + 盘符扫描）。见 get_accoreconsole_manual_path()。
    "accoreconsole_manual_path": "",
    # "批量文字替换"里勾选的替换范围，记住上次用户的选择。默认只勾
    # 单行/多行文字——标注覆盖文字、块属性、块定义模板这几个影响面
    # 更大/更容易误伤，默认不勾，让用户自己按需打开。
    "replace_scan_options": {
        "text": True,
        "mtext": True,
        "dimension": False,
        "block_attr": False,
        "scan_space": True,
        "include_block_defs": False,
    },
    # 主搜索栏"文字类型 / 搜索位置 / 块定义范围"三组筛选勾选框，记住上次
    # 用户的勾选状态。默认全部勾选，等价于不筛选，跟界面上的初始默认一致。
    "search_filter_options": {
        "entity_types": {
            "TEXT": True,
            "MTEXT": True,
            "DIMENSION": True,
            "BLOCK_ATTR": True,
        },
        "spaces": {
            "MODEL": True,
            "PAPER": True,
        },
        "scopes": {
            "PLACED": True,
            "BLOCK_DEF": True,
        },
    },
    # 搜索栏下面那排筛选勾选框是不是展开状态，记住上次的选择——默认收起，
    # 平时不常用的时候不占地方，需要的时候点"筛选"按钮展开。
    "search_filter_row_expanded": False,
    # 点主窗口的关闭按钮时，是最小化到系统托盘（后台索引继续跑），还是
    # 直接退出程序。默认最小化到托盘——这是软件原来就有的行为，不希望
    # 因为加了这个开关就悄悄改变老用户已经习惯的默认表现。
    "minimize_to_tray_on_close": True,
}

def load_config(window, refresh_scope_bar=True):
    """从本地 json 完美的、按最新优先顺序加载所有历史记录到下拉框中。

    refresh_scope_bar：要不要顺带重建"搜索目录"标签栏。启动时的正常
    调用要传 True；search_manager.start_search() 里那次"保存后重读，
    只为了刷新关键词下拉历史"的调用要传 False——标签栏状态在那个
    时间点根本没变过，没必要跟着清空重建一遍标签控件。
    """
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                # 确保读取出来的基础结构是完整的
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass

    # 临时断开信号，防止初始化填充数据时触发不必要的联动搜索
    _block_combo_signals(window, True)

    # 1. 恢复"搜索目录"标签栏——上次固定过哪些目录、各自是否勾选。
    # 不存在的目录（比如上次记的是移动硬盘上的路径，这次没插上）
    # set_folders() 内部会自动跳过，不会显示成一个打不开的空标签。
    if refresh_scope_bar and hasattr(window, "folder_scope_bar"):
        window.folder_scope_bar.set_folders(config["search_scope_folders"])

    # 2. 刷新文件名关键字历史（前台保持干净空白）
    _refresh_combo_box(window.filename_keyword_edit, config["filename_keyword_history"], has_clear_btn=True)
    window.filename_keyword_edit.setEditText("")
    
    # 3. 刷新内容关键字历史（前台保持干净空白）
    _refresh_combo_box(window.keyword_edit, config["content_keyword_history"], has_clear_btn=True)
    window.keyword_edit.setEditText("")

    _block_combo_signals(window, False)


def save_config(window):
    """保存当前输入的记录，并强制实现【最新使用词永远置顶】的逻辑"""
    # 读取当前前台输入框里活生生的字
    current_filename = window.filename_keyword_edit.currentText().strip()
    current_content = window.keyword_edit.currentText().strip()

    # 先读取已有的老账本
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass

    # 搜索目录标签栏当前是什么状态就直接整份存下来，不用像关键词历史
    # 那样做"去重置顶"——标签本身的顺序就是用户自己拖/加出来的显示
    # 顺序，不需要额外排序逻辑。
    if hasattr(window, "folder_scope_bar"):
        config["search_scope_folders"] = window.folder_scope_bar.get_folders()

    if current_filename and current_filename != "清空记录":
        if current_filename in config["filename_keyword_history"]:
            config["filename_keyword_history"].remove(current_filename)
        config["filename_keyword_history"].insert(0, current_filename)
        config["filename_keyword_history"] = config["filename_keyword_history"][:15]

    if current_content and current_content != "清空记录":
        if current_content in config["content_keyword_history"]:
            config["content_keyword_history"].remove(current_content)
        config["content_keyword_history"].insert(0, current_content)
        config["content_keyword_history"] = config["content_keyword_history"][:15]

    # 写入硬盘
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


def clear_all_history(window, target_combo):
    """💥 彻底摧毁并抹平指定的历史记录槽，解决清空失效的通病"""
    if not os.path.exists(CONFIG_FILE):
        return

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        config = DEFAULT_CONFIG.copy()

    # 判定是谁触发了"清空记录"，精准抹黑它对应的硬盘数据
    if target_combo == window.filename_keyword_edit:
        config["filename_keyword_history"] = []
    elif target_combo == window.keyword_edit:
        config["content_keyword_history"] = []

    # 灌回硬盘
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass

    # 强力刷新前台界面UI，让它立刻变干净
    _block_combo_signals(window, True)
    target_combo.clear()
    target_combo.addItem("清空记录")
    _block_combo_signals(window, False)


def _refresh_combo_box(combo, history_list, has_clear_btn=True):
    """前台下拉组件强力安全刷新器"""
    combo.clear()
    
    # 塞入历史数据
    for item in history_list:
        if item and item != "清空记录":
            combo.addItem(item)
            
    # 底部垫一个清空按钮
    if has_clear_btn:
        combo.addItem("清空记录")


def _block_combo_signals(window, block_state):
    """集中管控信号锁，防止刷新时发生死循环"""
    if hasattr(window, 'filename_keyword_edit'): window.filename_keyword_edit.blockSignals(block_state)
    if hasattr(window, 'keyword_edit'): window.keyword_edit.blockSignals(block_state)

def get_exclude_system_dirs_enabled():
    """内置系统目录（C:\\Windows 这类）排除功能是否启用，默认启用"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            return bool(config.get("exclude_system_dirs_enabled", True))
        except Exception:
            pass
    return True


def save_exclude_system_dirs_enabled(enabled):
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["exclude_system_dirs_enabled"] = bool(enabled)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_exclude_custom_enabled():
    """用户自定义排除目录功能是否启用，默认启用"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            return bool(config.get("exclude_custom_enabled", True))
        except Exception:
            pass
    return True


def save_exclude_custom_enabled(enabled):
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["exclude_custom_enabled"] = bool(enabled)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_user_exclude_folders():
    """只读用户自己加的排除目录（不含内置默认），不受"启用自定义"这个
    开关影响——开关只决定这些目录实际生不生效，不影响列表本身的
    增删，弹窗展示要用这个原始列表，不能用会被开关过滤掉的
    get_exclude_folders()。"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            return config.get("exclude_folders", [])
        except Exception:
            pass
    return []


def get_exclude_folders():
    """读取【实际生效的】排除目录列表，合并内置默认和用户自定义——会
    分别受"排除系统目录""启用自定义"这两个开关控制，关掉哪个，
    对应那部分就不会出现在这个列表里（但配置文件里的原始数据还在，
    随时可以重新打开开关恢复，不是删掉）。"""
    all_excludes = []
    if get_exclude_system_dirs_enabled():
        all_excludes.extend(DEFAULT_EXCLUDE_FOLDERS)
    if get_exclude_custom_enabled():
        for p in get_user_exclude_folders():
            if p not in all_excludes:
                all_excludes.append(p)
    return all_excludes


def save_exclude_folders(exclude_list):
    """保存用户自定义排除目录（不含内置默认）"""
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    # 只保存用户新增的，内置的不存到文件
    user_only = [p for p in exclude_list if p not in DEFAULT_EXCLUDE_FOLDERS]
    config["exclude_folders"] = user_only
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass

def get_minimize_to_tray_enabled():
    """点主窗口关闭按钮时是否最小化到系统托盘（而不是直接退出程序），
    默认启用——对应菜单栏"设置 -> 最小化为系统托盘图标"这一项。"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            return bool(config.get("minimize_to_tray_on_close", True))
        except Exception:
            pass
    return True


def save_minimize_to_tray_enabled(enabled):
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["minimize_to_tray_on_close"] = bool(enabled)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_window_geometry():
    """读取上次保存的窗口大小和位置，没有记录则返回 None"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            w = config.get("window_width", 0)
            h = config.get("window_height", 0)
            x = config.get("window_x", -1)
            y = config.get("window_y", -1)
            if w > 100 and h > 100:
                return w, h, x, y
        except Exception:
            pass
    return None


def save_window_geometry(width, height, x, y):
    """保存窗口大小和位置"""
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for key in config:
                    if key in loaded:
                        config[key] = loaded[key]
        except Exception:
            pass
    config["window_width"]  = width
    config["window_height"] = height
    config["window_x"]      = x
    config["window_y"]      = y
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass