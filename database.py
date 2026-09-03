# database.py
import os
import re
import sqlite3
import time
from functools import lru_cache

# 批量提交的阈值：每积累这么多条记录才一次性写入磁盘
BATCH_SIZE = 50

# 直径符号在 DWG 里可能以好几种不同的 Unicode 字符出现（∅ 空集符号、
# Ø/ø 带删除线的字母O），视觉上跟标准直径符号 ⌀ 分不清，但字符串比较时
# 是完全不同的字符。这份映射跟 helpers.py 里的 _DIAMETER_VARIANTS、
# DwgTextExtractor/Program.cs 里的 DiameterVariants 是同一套对应关系——
# 这里不直接 import helpers.py 复用，是因为 database.py 要保持不依赖
# PyQt5（有可能在非 GUI 场景下单独跑），所以自己存一份小映射表。
# 用途：用户在搜索框里手打 ∅/Ø 这类变体时，查询关键词先归一化成 ⌀
# 再去匹配索引里的文字（索引里的文字从 helpers.py 提取阶段起就已经
# 统一是 ⌀ 了），两边才能对得上。
_DIAMETER_VARIANTS = {
    "\u2205": "\u2300",  # ∅ EMPTY SET → ⌀
    "\u00d8": "\u2300",  # Ø LATIN CAPITAL LETTER O WITH STROKE → ⌀
    "\u00f8": "\u2300",  # ø LATIN SMALL LETTER O WITH STROKE → ⌀
}


def _normalize_diameter_symbol(text):
    """把 ∅ / Ø / ø 统一替换成标准直径符号 ⌀（U+2300）。"""
    if not text:
        return text
    for variant, canonical in _DIAMETER_VARIANTS.items():
        if variant in text:
            text = text.replace(variant, canonical)
    return text

# 这个数字变了，就说明 dwg_text_fts 的表结构变了（比如加了新列）。
# FTS5 虚拟表不支持 ALTER TABLE ADD COLUMN，表结构一变，唯一的办法是
# 整表 DROP 重建——同时必须强制下次全量重新提取，不然新表建好了也是空的。
SCHEMA_TEXT_TABLE_VERSION = "3"  # v3: 新增 dwg_text_lookup 索引表，给按文件查预览用

# 文字类型 / 空间 / 范围标签，跟 DwgTextExtractor.exe 的 Emit() 输出保持一致。
# 界面上的筛选勾选框用这份映射生成中文标签，避免代码里到处硬编码字符串。
TEXT_TYPE_LABELS = {
    "TEXT": "单行文字",
    "MTEXT": "多行文字",
    "DIMENSION": "标注",
    "BLOCK_ATTR": "块属性",
}
SPACE_LABELS = {
    "MODEL": "模型空间",
    "PAPER": "图纸空间（布局）",
}
# 跟查找替换功能的 scan_space / include_block_defs 是同一对概念：
# PLACED = 图纸里正常摆放的实体本身，BLOCK_DEF = 块定义模板内部（含嵌套块）。
# 这是独立于"空间(MODEL/PAPER)"的另一个维度，可以任意组合。
SCOPE_LABELS = {
    "PLACED": "摆放的实体",
    "BLOCK_DEF": "块定义内部（含嵌套块）",
}
ALL_TEXT_TYPES = list(TEXT_TYPE_LABELS.keys())
ALL_SPACES = list(SPACE_LABELS.keys())
ALL_SCOPES = list(SCOPE_LABELS.keys())


@lru_cache(maxsize=64)
def _compile_regex(pattern):
    """编译好的正则对象缓存起来——REGEXP 这个函数会被 SQLite 对每一行
    都调用一次，同一次搜索里模式不变，没必要每行都重新编译一遍。
    大小写不敏感，跟原来 LIKE 查询"默认不区分大小写"的体感保持一致。"""
    return re.compile(pattern, re.IGNORECASE)


def _sqlite_regexp(pattern, value):
    """注册给 SQLite 当 REGEXP 操作符用的函数。SQLite 里 `X REGEXP Y`
    等价于调用 `regexp(Y, X)`，所以这里的参数顺序是 (pattern, value)，
    不是 (value, pattern)，跟直觉正好相反，写反了会全表不匹配。"""
    if value is None:
        return False
    try:
        return _compile_regex(pattern).search(value) is not None
    except re.error:
        # 正则本身写错了（比如括号不配对）。search_manager.py 在发起
        # 搜索前已经会先校验一遍语法、提前弹窗拦住，这里只是双重保险——
        # 万一漏网了，让这一行"不匹配"而不是让整条 SQL 查询直接报错崩掉。
        return False


def _normalize_entries(content_list):
    """
    统一把 content_list 归一化成 (entity_type, space, scope, text) 四元组列表。
    兼容三种输入：
      - 当前格式：extract_dwg_text_via_exe 解析好的 (type, space, scope, text) 四元组
      - 旧格式：(type, space, text) 三元组（exe 还没加 scope 那一版），scope 按 PLACED 补齐
      - 更旧格式：纯字符串（历史调用路径或异常兜底），按 TEXT/MODEL/PLACED 补齐
    """
    normalized = []
    for item in content_list:
        if isinstance(item, (tuple, list)) and len(item) == 4:
            entity_type, space, scope, text = item
            if text and text.strip():
                normalized.append((entity_type or "TEXT", space or "MODEL", scope or "PLACED", text))
        elif isinstance(item, (tuple, list)) and len(item) == 3:
            entity_type, space, text = item
            if text and text.strip():
                normalized.append((entity_type or "TEXT", space or "MODEL", "PLACED", text))
        elif isinstance(item, str):
            if item.strip():
                normalized.append(("TEXT", "MODEL", "PLACED", item))
    return normalized


class DWGDatabase:
    """
    SQLite 数据库管理器：负责图纸文字账本的创建、读取、增量同步与闪电检索。

    表结构（V5.6 起）：
      - dwg_index：一个文件一行，只存路径/文件名/修改时间/排除标记，
        文件名搜索、mtime 比对这些"文件级"操作全部走这张表，行数少、天然快。
      - dwg_text_fts：一条文字一行的 FTS5(trigram) 全文索引表，存实际的
        图纸文字内容 + 类型(entity_type) + 空间(space) + 范围(scope)，供内容
        关键词搜索和"按类型/位置/是否块定义筛选"使用。trigram 分词器天然
        支持中文子串匹配，用 LIKE（而不是 MATCH）查询可以绕开"至少3字符"
        的限制。
      - 如果这台机器的 SQLite 版本太老、FTS5/trigram 不可用，自动降级成
        普通表 + LIKE 查询（能用，但没有索引加速，量大时会比较慢）。
    """
    def __init__(self, db_path=None):
        from config import get_db_path
        self.db_path = db_path or get_db_path()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        # 🌟 优化3：开启 WAL 模式，大幅提升并发写入速度
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")  # WAL 模式下 NORMAL 已足够安全
        self.conn.execute("PRAGMA busy_timeout=30000")  # 双保险，跟上面 connect(timeout=30) 对齐
        # 注册正则搜索支持：SQLite 原生没有 REGEXP 操作符，得靠这个回调
        # 实现。注册一次之后，这条连接上所有 SQL 里的 `... REGEXP ?`
        # 都会走这个函数（内部转发到 Python re 模块）。
        self.conn.create_function("REGEXP", 2, _sqlite_regexp)
        self.fts_available = False
        self.trigram_available = False
        self.create_table()
        # 批量写入缓冲区：元素是 (dwg_path, filename, mtime, entries)
        self._batch_buffer = []

    def create_table(self):
        """创建图纸索引核心账本表、文字全文索引表和元数据表"""
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS dwg_index (
                    dwg_path     TEXT PRIMARY KEY,
                    filename     TEXT,
                    file_content TEXT,
                    modify_time  REAL,
                    excluded     INTEGER DEFAULT 0
                )
            """)
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_filename ON dwg_index(filename)"
            )
            # 元数据表
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
        # 旧数据库迁移：如果没有 excluded 列则自动添加。
        # 排除范围现在已经改成纯内存过滤（现查 config.json，见
        # search_dwg_index），不再依赖这一列的值——但列本身还留着，
        # 单纯是为了兼容老数据库文件，不用为了删一列去跑有风险的
        # DROP COLUMN 迁移。新写入的记录不会再往这列塞值，SQLite 会
        # 自动用上面定义的 DEFAULT 0 补上，没有实际作用，纯粹是
        # 兼容旧库结构的历史遗留字段。
        try:
            self.conn.execute("ALTER TABLE dwg_index ADD COLUMN excluded INTEGER DEFAULT 0")
        except Exception:
            pass  # 列已存在则忽略

        # 旧数据库迁移：提取失败标记/原因这两列同样按需补上。extract_failed=1
        # 表示这张图纸上次索引时提取内容失败了（损坏/格式不支持/超时等），
        # error_msg 存具体原因，供“索引管理”里的失败列表面板显示。
        try:
            self.conn.execute("ALTER TABLE dwg_index ADD COLUMN extract_failed INTEGER DEFAULT 0")
            with self.conn:
                self.conn.execute("UPDATE dwg_index SET extract_failed=0 WHERE extract_failed IS NULL")
        except Exception:
            pass
        try:
            self.conn.execute("ALTER TABLE dwg_index ADD COLUMN error_msg TEXT")
        except Exception:
            pass
        try:
            with self.conn:
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_extract_failed ON dwg_index(extract_failed)"
                )
        except Exception:
            pass

        # 旧数据库迁移：新增 dwg_version 列，把"DWG版本"这一列从"搜索结果
        # 填表时现 open() 每个命中文件读一次"改成"建索引时顺手读一次、
        # 存进数据库，搜索时直接跟着 SELECT 出来"，省掉搜索这一侧重复的
        # 文件访问开销（背景见 search_manager.py 填表那段的打点分析）。
        #
        # ALTER TABLE ADD COLUMN 只有列真的不存在时才会成功——用这一点
        # 判断"这是不是第一次升级到带 dwg_version 列的版本"：如果成功，
        # 说明是从旧库升级上来的，老记录的 dwg_version 全是 NULL，得强制
        # 触发一次全量重新扫描才能把这些文件的版本号补上；如果失败（列已
        # 存在，走进 except），说明不是第一次运行，不需要再重复触发。
        # 强制重扫的手法跟 _reset_text_table_on_schema_change 一样：把
        # modify_time 全部改成 -1，真实 mtime 不可能等于 -1，下次
        # IndexThread 扫描时会把所有文件当成"需要更新"重新处理一遍。
        try:
            self.conn.execute("ALTER TABLE dwg_index ADD COLUMN dwg_version TEXT")
            with self.conn:
                self.conn.execute("UPDATE dwg_index SET modify_time = -1")
            # 不用 log_utils.log()：那边会连带把依赖 PyQt5 的 config.py
            # 一起导入进来，database.py 要保持能在非 GUI 场景下独立运行
            # （见文件顶部 _DIAMETER_VARIANTS 附近的说明），这里跟其余
            # 迁移代码一样，静默处理，不打印。
        except Exception:
            pass  # 列已存在，不是首次升级，跳过强制重扫

        self._reset_text_table_on_schema_change()
        self._create_text_table()

    def _reset_text_table_on_schema_change(self):
        """
        统一的文字表迁移入口，覆盖两种情况：
          1) 从更老的版本（文字整存在 dwg_index.file_content 一个大字符串里，
             压根没有 dwg_text_fts 这张表）升级上来；
          2) dwg_text_fts 已经存在，但列结构是旧版本的（比如这次新增了 scope
             列）——FTS5 虚拟表不支持 ALTER TABLE ADD COLUMN，只能整表重建。
        两种情况的后果是一样的：新表（重建后）是空的，但 dwg_index 里的
        modify_time 没变，IndexThread 靠"mtime 是否变化"判断要不要重新提取，
        mtime 没变就会跳过，新表会一直空着，搜索变成"什么都搜不到"。
        所以这里统一处理：只要检测到版本号对不上，就 DROP 旧表 + 强制清空
        所有 modify_time，下次扫描会把全部文件当成"需要更新"重新提取一遍。
        用 meta 表记版本号，保证同一个版本只触发一次。
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM meta WHERE key='schema_text_table_version'")
        row = cursor.fetchone()
        current_version = row[0] if row else None
        if current_version == SCHEMA_TEXT_TABLE_VERSION:
            return
        with self.conn:
            self.conn.execute("DROP TABLE IF EXISTS dwg_text_fts")
            self.conn.execute("DROP TABLE IF EXISTS dwg_text_lookup")
            self.conn.execute("UPDATE dwg_index SET modify_time = -1")
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_text_table_version', ?)",
                (SCHEMA_TEXT_TABLE_VERSION,)
            )

    def _create_text_table(self):
        """
        建立文字子表，按优先级依次尝试：
          1) FTS5 + trigram 分词器（最快，支持任意长度中文子串）
          2) FTS5 + 默认 unicode61 分词器（能建索引，但中文子串匹配能力弱）
          3) 普通表 + LIKE（兜底，能用但没有索引加速）
        探测方式是"实际尝试建表"，不是猜版本号——建表失败就换下一档，
        不假设任何机器的 SQLite 版本。

        另外无论走哪一档，都会额外建一张 dwg_text_lookup——这张是普通表，
        专门给"查某个文件都提取到了哪些文字"（预览用）这个场景服务，
        在 dwg_path 上建了真正的 B-tree 索引。这张表存在的原因：
        dwg_text_fts 是 FTS5 虚拟表，dwg_path 只是 UNINDEXED（意思是
        "不参与全文分词"，不等于"有索引能快速查找"）——按 dwg_path 做
        等值查询在 FTS5 虚拟表上没有走索引的路径可用，只能整表扫描，
        代价跟"这台数据库里总共有多少行"成正比，跟"这一个文件到底有
        多少文字"完全无关。数据库还小的时候扫得动、感觉不出来；
        真到几十万、上百万行的规模，这个"跟文件内容无关、只跟数据库
        总量有关"的固定扫描代价，就是预览"点哪个文件都一样慢"的真正
        原因。dwg_text_lookup 只服务"按 dwg_path 查"这一种访问模式，
        用真正的索引把这个操作变回正常的索引查找，不再是全表扫描。
        """
        try:
            with self.conn:
                self.conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS dwg_text_fts USING fts5(
                        dwg_path UNINDEXED,
                        entity_type UNINDEXED,
                        space UNINDEXED,
                        scope UNINDEXED,
                        text,
                        tokenize = 'trigram'
                    )
                """)
            self.fts_available = True
            self.trigram_available = True
        except Exception:
            self.fts_available = None  # 占位，下面还会再试一档

        if self.fts_available is None:
            try:
                with self.conn:
                    self.conn.execute("""
                        CREATE VIRTUAL TABLE IF NOT EXISTS dwg_text_fts USING fts5(
                            dwg_path UNINDEXED,
                            entity_type UNINDEXED,
                            space UNINDEXED,
                            scope UNINDEXED,
                            text
                        )
                    """)
                self.fts_available = True
                self.trigram_available = False
            except Exception:
                self.fts_available = None

        if self.fts_available is None:
            # 兜底：普通表，查询走 LIKE，没有索引加速
            with self.conn:
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS dwg_text_fts (
                        dwg_path    TEXT,
                        entity_type TEXT,
                        space       TEXT,
                        scope       TEXT,
                        text        TEXT
                    )
                """)
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_text_path ON dwg_text_fts(dwg_path)"
                )
            self.fts_available = False
            self.trigram_available = False

        # dwg_text_lookup：不管上面走了哪一档，这张预览专用表都要建。
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS dwg_text_lookup (
                    dwg_path    TEXT,
                    entity_type TEXT,
                    space       TEXT,
                    scope       TEXT,
                    text        TEXT
                )
            """)
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_text_lookup_path ON dwg_text_lookup(dwg_path)"
            )

    def get_file_mtime(self, dwg_path):
        """获取本地数据库中记录的图纸修改时间"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT modify_time FROM dwg_index WHERE dwg_path = ?", (dwg_path,))
        row = cursor.fetchone()
        return row[0] if row else None

    def get_folder_mtimes(self, folder_prefix):
        """
        批量读取某个目录下所有已索引文件的 mtime。
        返回 dict: { dwg_path: modify_time }
        一次 SQL 搞定整个目录，比逐一查询快很多。
        """
        from config import path_is_within
        cursor = self.conn.cursor()
        cursor.execute("SELECT dwg_path, modify_time FROM dwg_index")
        return {
            row[0]: row[1]
            for row in cursor.fetchall()
            if path_is_within(row[0], folder_prefix)
        }

    def update_file_index(self, dwg_path, filename, content_list, modify_time, error_msg=None, dwg_version=None):
        """
        将提取好的文字内容加入批量缓冲区。
        缓冲区满 BATCH_SIZE 条时自动刷盘，避免每张图纸单独写一次磁盘。

        error_msg：这张图纸这次提取内容失败的原因（损坏/格式不支持/超时等），
        None 表示提取成功（或者压根没有提取工具）。失败时文件名索引依然照常
        写入，只是 extract_failed 会标记成 1、error_msg 记下原因，供
        "索引管理"里的失败文件列表查看。

        dwg_version：DWG 文件头部的版本标识（比如 "AC1032（2018及以上）"），
        建索引这一步文件反正已经被访问过一次，顺手把版本读出来存进这里，
        搜索结果表格填表时直接从数据库 SELECT 出来用，不用再对每个命中
        文件单独 open() 一次现读——原来那次是搜索结果里"DWG版本"这一列
        单独耗时接近1秒的主要原因。None 表示没读到（文件损坏/不是DWG/
        提取工具不可用等），存进数据库会是 NULL，展示层按"未知"处理。
        """
        entries = _normalize_entries(content_list)
        self._batch_buffer.append((dwg_path, filename, modify_time, entries, error_msg, dwg_version))
        # 🌟 优化2：达到批量阈值时统一提交
        if len(self._batch_buffer) >= BATCH_SIZE:
            self.flush_batch()

    def flush_batch(self):
        """将缓冲区中所有待写入记录一次性提交到数据库，并更新最后索引时间"""
        if not self._batch_buffer:
            return
        now = time.time()
        paths = [row[0] for row in self._batch_buffer]
        with self.conn:
            self.conn.executemany("""
                INSERT OR REPLACE INTO dwg_index (dwg_path, filename, file_content, modify_time, extract_failed, error_msg, dwg_version)
                VALUES (?, ?, '', ?, ?, ?, ?)
            """, [
                (p, f, m, 1 if err else 0, err, ver)
                for p, f, m, _, err, ver in self._batch_buffer
            ])

            # 每个文件的文字内容整体替换：先删旧的，再插新的，
            # 比"逐条比对增删"简单可靠得多——反正每次重新索引都是全量重提取。
            self.conn.executemany(
                "DELETE FROM dwg_text_fts WHERE dwg_path = ?",
                [(p,) for p in paths]
            )
            self.conn.executemany(
                "DELETE FROM dwg_text_lookup WHERE dwg_path = ?",
                [(p,) for p in paths]
            )
            text_rows = [
                (p, entity_type, space, scope, text)
                for p, _, _, entries, _, _ in self._batch_buffer
                for entity_type, space, scope, text in entries
            ]
            if text_rows:
                self.conn.executemany(
                    "INSERT INTO dwg_text_fts (dwg_path, entity_type, space, scope, text) VALUES (?, ?, ?, ?, ?)",
                    text_rows
                )
                # dwg_text_lookup 跟 dwg_text_fts 内容完全一样，只是多存一份、
                # 换一张带真正 B-tree 索引的普通表，专供"按文件查预览"用，
                # 避免这个操作退化成全表扫描（原因见 _create_text_table 里的
                # 说明）。
                self.conn.executemany(
                    "INSERT INTO dwg_text_lookup (dwg_path, entity_type, space, scope, text) VALUES (?, ?, ?, ?, ?)",
                    text_rows
                )

            # 同步更新最后索引时间
            self.conn.execute("""
                INSERT OR REPLACE INTO meta (key, value)
                VALUES ('last_index_time', ?)
            """, (str(now),))
        self._batch_buffer.clear()

    def upsert_single_file(self, dwg_path, filename, content_list, modify_time, error_msg=None, dwg_version=None):
        """
        立即写入单条记录，不走批量缓冲区。
        用于实时监控场景——单个文件变动是偶发、稀疏的事件，不需要像
        启动全量扫描那样攒一批再统一刷盘，直接写更简单也更及时。

        error_msg：同 update_file_index，实时监控这条路径提取失败时的原因。
        dwg_version：同 update_file_index，文件变动时顺手重新读一次版本号——
        文件被修改了，理论上版本标识也可能跟着变（比如被另存为更高版本），
        不能沿用旧值。
        """
        entries = _normalize_entries(content_list)
        with self.conn:
            self.conn.execute("""
                INSERT OR REPLACE INTO dwg_index (dwg_path, filename, file_content, modify_time, extract_failed, error_msg, dwg_version)
                VALUES (?, ?, '', ?, ?, ?, ?)
            """, (dwg_path, filename, modify_time, 1 if error_msg else 0, error_msg, dwg_version))
            self.conn.execute("DELETE FROM dwg_text_fts WHERE dwg_path = ?", (dwg_path,))
            self.conn.execute("DELETE FROM dwg_text_lookup WHERE dwg_path = ?", (dwg_path,))
            if entries:
                rows = [(dwg_path, t, s, sc, txt) for t, s, sc, txt in entries]
                self.conn.executemany(
                    "INSERT INTO dwg_text_fts (dwg_path, entity_type, space, scope, text) VALUES (?, ?, ?, ?, ?)",
                    rows
                )
                self.conn.executemany(
                    "INSERT INTO dwg_text_lookup (dwg_path, entity_type, space, scope, text) VALUES (?, ?, ?, ?, ?)",
                    rows
                )

    def delete_single_file(self, dwg_path):
        """删除单条记录，用于实时监控检测到文件被删除/改名时"""
        with self.conn:
            self.conn.execute("DELETE FROM dwg_index WHERE dwg_path = ?", (dwg_path,))
            self.conn.execute("DELETE FROM dwg_text_lookup WHERE dwg_path = ?", (dwg_path,))
            self.conn.execute("DELETE FROM dwg_text_fts WHERE dwg_path = ?", (dwg_path,))

    def _select_paths_under_prefix(self, folder_prefix):
        """按目录前缀取出数据库里落在这个目录下的所有 dwg_path。

        之前这里是 `SELECT dwg_path FROM dwg_index`（不带任何 WHERE），
        也就是不管要找哪个目录的记录，都要先把整张表（所有磁盘、所有
        目录的记录）全部读进 Python 内存，再逐条用 normpath+startswith
        比对——而 _index_folder() 对扫描列表里的每一个顶层目录（C、D、
        E、F、G、H……）都会调用一次 remove_deleted_files()，等于每扫一个
        盘就要把整个数据库全表读一遍，盘越多、索引记录越多，这一步就越慢，
        表现出来就是"明明这个目录没什么变化，扫描却慢得不正常"。

        dwg_path 是这张表的主键（自带 B-tree 索引），改成先用 SQL 层面的
        `LIKE '前缀%'` 做一次粗筛，只把大概率落在这个目录下的候选行捞出来，
        再在 Python 这边用原来的 normpath+lower+startswith 做一次精确复核
        （防止 LIKE 通配符转义、大小写、盘符两种写法等边缘情况误判）——
        比对次数从"全表"降到"这个目录大致命中的这一小撮"，其余目录的
        海量记录根本不需要再经过 Python 一遍。
        """
        # 转义 LIKE 里的通配符 % 和 _，避免目录名本身含有这两个字符时
        # 被误当成通配符展开，导致粗筛范围算错。
        escaped = folder_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = escaped.rstrip("\\/") + "%"
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT dwg_path FROM dwg_index WHERE dwg_path LIKE ? ESCAPE '\\'",
            (pattern,)
        )
        from config import path_is_within
        return [
            row[0] for row in cursor.fetchall()
            if path_is_within(row[0], folder_prefix)
        ]

    def remove_deleted_files(self, existing_paths, folder_prefix):
        """
        清理账本：只清理当前目录范围内已被删除的图纸记录。
        其他目录的索引数据完全不受影响，各目录索引永久共存。
        """
        if not existing_paths:
            # 当前目录下已没有任何 DWG 文件，清空该目录的全部记录
            to_delete = self._select_paths_under_prefix(folder_prefix)
            self._delete_paths(to_delete)
            return

        # 只取当前目录下的数据库记录来做比对
        db_paths_in_folder = self._select_paths_under_prefix(folder_prefix)
        deleted_paths = set(db_paths_in_folder) - set(existing_paths)
        self._delete_paths(list(deleted_paths))

    def _delete_paths(self, paths):
        if not paths:
            return
        with self.conn:
            self.conn.executemany(
                "DELETE FROM dwg_index WHERE dwg_path = ?",
                [(p,) for p in paths]
            )
            self.conn.executemany(
                "DELETE FROM dwg_text_fts WHERE dwg_path = ?",
                [(p,) for p in paths]
            )
            self.conn.executemany(
                "DELETE FROM dwg_text_lookup WHERE dwg_path = ?",
                [(p,) for p in paths]
            )

    def search_dwg_index(self, content_keywords, filename_keywords,
                          entity_types=None, spaces=None, scopes=None,
                          filename_regex=False, content_regex=False):
        """
        核心搜索：
          1) 先在 dwg_index（一个文件一行，天生小）上按文件名 + 排除状态
             圈定候选文件集合。
          2) 内容关键词逐个在 dwg_text_fts 上查询（可选按类型/空间/范围
             过滤），每个关键词各自命中的文件集合互相取交集——这就是
             "关键词A和B都要匹配"的语义，只是现在 A、B 可能分别出现在
             同一文件的不同文字条目里，所以不能再像以前那样一条 SQL 里
             连续 LIKE 两次。

        entity_types / spaces / scopes 为 None 或空表示不筛选（等价于全选）；
        只有真正给了内容关键词时才会用上这几个筛选条件——纯文件名搜索
        不涉及"文字类型/位置/是否块定义"这些概念。

        filename_regex / content_regex：对应的关键词列表要不要按正则
        表达式解析（而不是普通子串匹配）。传 True 时，调用方应该已经把
        对应的关键词列表整理成"只有一个元素、且是完整合法正则"的形式
        （search_manager.py 在正则模式下不会按空格拆词，也会提前校验过
        语法），这里只管把 LIKE 换成 REGEXP，不做额外校验。
        返回：{ dwg_path: dwg_version, ... }——value 原来是占位的 None，
        现在顺带把建索引时存进 dwg_index.dwg_version 列的版本标识带出来，
        调用方（searcher.py -> search_thread.py -> search_manager.py）
        填搜索结果表格的"DWG版本"列时直接用这份，不用再对每个命中文件
        单独 open() 一次现读（NULL 表示还没建过索引/建索引时没读到，
        展示层按"未知"处理，行为跟以前一致）。
        """
        from config import get_exclude_folders, is_path_excluded
        cursor = self.conn.cursor()

        query = "SELECT dwg_path, dwg_version FROM dwg_index WHERE 1=1"
        params = []
        for kw in filename_keywords:
            if kw.strip():
                if filename_regex:
                    query += " AND filename REGEXP ?"
                    params.append(kw.strip())
                else:
                    query += " AND filename LIKE ?"
                    params.append(f"%{kw.strip()}%")
        cursor.execute(query, params)
        # 排除范围现在是纯内存过滤：config.json 是唯一真相来源，这里
        # 每次搜索都现读一份最新的排除列表，直接筛掉命中的路径——不再
        # 依赖数据库里一份需要额外同步的 excluded 标记。config.json 本身
        # 就是普通本地小文件，读一次的开销跟这条 SQL 查询比可以忽略，
        # 换来的是排除规则保存那一刻就已经对搜索生效，不存在"数据库
        # 标记还没追上"这种滞后状态。
        exclude_folders = get_exclude_folders()
        # 版本号单独存一份 path -> version 的映射：下面 candidate_paths
        # 还要参与多个内容关键词之间的交集运算（只能是纯路径集合），
        # 版本信息不能直接混进去一起做交集，所以拆开存，最后再按最终
        # 命中的路径集合拼回去。
        version_map = {}
        candidate_paths = set()
        for row in cursor.fetchall():
            path, version = row[0], row[1]
            if is_path_excluded(path, exclude_folders):
                continue
            candidate_paths.add(path)
            version_map[path] = version

        if not candidate_paths:
            return {}

        content_kws = [kw.strip() for kw in content_keywords if kw.strip()]
        if not content_regex:
            # 正则模式下关键词是用户精心写好的正则表达式，不能替它悄悄改字符；
            # 只有普通子串匹配模式才做直径符号归一化，让用户手打 ∅/Ø 也能
            # 搜到索引里统一存成 ⌀ 的记录。
            content_kws = [_normalize_diameter_symbol(kw) for kw in content_kws]
        if not content_kws:
            return {p: version_map.get(p) for p in candidate_paths}

        type_filter = [t for t in (entity_types or []) if t]
        space_filter = [s for s in (spaces or []) if s]
        scope_filter = [s for s in (scopes or []) if s]

        for kw in content_kws:
            if content_regex:
                fts_query = "SELECT DISTINCT dwg_path FROM dwg_text_fts WHERE text REGEXP ?"
                fts_params = [kw]
            else:
                fts_query = "SELECT DISTINCT dwg_path FROM dwg_text_fts WHERE text LIKE ?"
                fts_params = [f"%{kw}%"]
            if type_filter:
                fts_query += f" AND entity_type IN ({','.join('?' * len(type_filter))})"
                fts_params += type_filter
            if space_filter:
                fts_query += f" AND space IN ({','.join('?' * len(space_filter))})"
                fts_params += space_filter
            if scope_filter:
                fts_query += f" AND scope IN ({','.join('?' * len(scope_filter))})"
                fts_params += scope_filter
            cursor.execute(fts_query, fts_params)
            hit_paths = {row[0] for row in cursor.fetchall()}
            candidate_paths &= hit_paths
            if not candidate_paths:
                break

        return {p: version_map.get(p) for p in candidate_paths}

    def get_single_file_content(self, dwg_path, entity_types=None, spaces=None, scopes=None):
        """
        单独读取某张图纸的内容，用于右侧预览秒显（按原提取顺序返回）。
        entity_types / spaces / scopes 为 None 或空表示不筛选（显示全部）——
        跟 search_dwg_index 是同一套筛选语义，传进来的应该是当前搜索栏
        勾选的那几个筛选条件，让预览跟搜索结果保持一致，不然用户勾了
        "单行文字"去搜，点开文件却看到全部类型混在一起，容易误以为筛选
        没生效。
        """
        cursor = self.conn.cursor()

        # 查 dwg_text_lookup（普通表，dwg_path 上有真正的 B-tree 索引），
        # 不查 dwg_text_fts——FTS5 虚拟表的 UNINDEXED 列没有索引加速，
        # 按 dwg_path 查会退化成全表扫描，具体原因见 _create_text_table
        # 里的注释。
        query = "SELECT text FROM dwg_text_lookup WHERE dwg_path = ?"
        params = [dwg_path]

        type_filter = [t for t in (entity_types or []) if t]
        space_filter = [s for s in (spaces or []) if s]
        scope_filter = [s for s in (scopes or []) if s]

        if type_filter:
            query += f" AND entity_type IN ({','.join('?' * len(type_filter))})"
            params += type_filter
        if space_filter:
            query += f" AND space IN ({','.join('?' * len(space_filter))})"
            params += space_filter
        if scope_filter:
            query += f" AND scope IN ({','.join('?' * len(scope_filter))})"
            params += scope_filter

        query += " ORDER BY rowid"

        cursor.execute(query, params)
        rows = cursor.fetchall()
        cursor.close()  # 及时释放这次查询占的读快照，避免一直挡住 WAL checkpoint
        return [row[0] for row in rows if row[0]]

    # =========================================================================
    # 索引管理面板专用接口
    # =========================================================================
    def get_index_stats(self):
        """
        读取索引统计信息：已索引图纸数、数据库文件大小、最后更新时间。
        返回字典：{ total_count, db_size_mb, last_update_str }
        """
        cursor = self.conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM dwg_index")
        total_count = cursor.fetchone()[0]

        try:
            size_bytes = os.path.getsize(self.db_path)
            db_size_mb = size_bytes / (1024 * 1024)
        except Exception:
            db_size_mb = 0.0

        # 从 meta 表读取真正的最后索引时间
        try:
            cursor.execute("SELECT value FROM meta WHERE key='last_index_time'")
            row = cursor.fetchone()
            if row and row[0]:
                last_update_str = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(float(row[0]))
                )
            else:
                last_update_str = "暂无记录"
        except Exception:
            last_update_str = "暂无记录"

        cursor.execute("SELECT COUNT(*) FROM dwg_index WHERE extract_failed=1")
        failed_count = cursor.fetchone()[0]

        return {
            "total_count": total_count,
            "db_size_mb": db_size_mb,
            "last_update_str": last_update_str,
            "failed_count": failed_count
        }

    def get_failed_files(self):
        """
        取出所有"上次索引提取内容失败"的图纸清单，供"索引管理"面板里的
        失败文件列表查看。这些文件的文件名本身是索引成功的（能被文件名
        搜到），只是图纸内容解析失败，所以内容搜索找不到里面的文字。
        按路径排序，方便在长列表里定位同一个目录下的文件。
        返回：[{"dwg_path":..., "filename":..., "error_msg":...}, ...]
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT dwg_path, filename, error_msg FROM dwg_index
            WHERE extract_failed=1
            ORDER BY dwg_path
        """)
        return [
            {"dwg_path": row[0], "filename": row[1], "error_msg": row[2] or "未知原因"}
            for row in cursor.fetchall()
        ]

    def checkpoint_wal(self):
        """
        尽力把 -wal 文件截断归零，释放磁盘空间。
        用 TRUNCATE 模式；如果此刻有别的连接占着读快照没释放，
        SQLite 会自动降级为尽力而为、不完全截断，不会报错、不会卡住。
        """
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass  # 静默失败即可，不影响正常使用，下次索引完成后还会再试

    def clear_all_index(self):
        """彻底清空索引表，下次搜索时将触发全量重建"""
        with self.conn:
            self.conn.execute("DELETE FROM dwg_index")
            self.conn.execute("DELETE FROM dwg_text_fts")
            self.conn.execute("DELETE FROM dwg_text_lookup")
        # DELETE 只是把数据页标记为空闲，不会收缩数据库文件本身，
        # 需要 VACUUM 才能让磁盘上的文件大小真正变小。
        # VACUUM 不能在事务中执行，所以放在 with 块之外单独跑。
        self.conn.execute("VACUUM")

    def close(self):
        """安全关闭前先把缓冲区剩余数据刷盘，再关闭连接"""
        self.flush_batch()
        if self.conn:
            self.conn.close()