# bookmark_manager.py
#
# "书签"：把当前文件名/内容两个搜索框里已经填好的关键词 + 是否开着
# 正则的组合存下来，下次不用重新敲一遍——尤其是写得比较复杂的正则
# 表达式，编一次挺费工夫，收藏起来下次点一下就能复用，不用凭记忆
# 重新敲、也不用翻聊天记录/笔记去找之前是怎么写的。
#
# 存放位置：跟数据库、日志同一个目录（get_app_data_dir()，Windows 上
# 是 %APPDATA%\DWGSearch），不跟着"当前配置的数据库路径"走——书签是
# "习惯怎么搜"这件事本身，跟"搜哪个数据库"是两回事，换了数据库存放
# 位置（config.py 的 get_db_path() 可以改）之后书签应该还在，不应该
# 跟着丢；这跟 config.json 记的窗口大小、列宽这些"跟程序本身绑定"的
# 偏好设置也是同一个道理，所以单独存一个 bookmarks.json，不跟数据库
# 文件混在一起。
import os
import json
import uuid
from datetime import datetime

from config import get_app_data_dir


def get_bookmarks_path():
    return os.path.join(get_app_data_dir(), "bookmarks.json")


def load_bookmarks():
    """读出全部书签，按创建时间从新到旧排（最近收藏的排最前面，符合
    "最近用的最容易找到"的直觉）。文件不存在/内容损坏都当成"还没有
    任何书签"处理，不能因为一个读取失败就让整个书签功能崩掉——用户
    顶多是暂时看不到列表，不会因此丢数据（原文件没被这里的读取逻辑
    动过）。
    """
    path = get_bookmarks_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        # 兼容脏数据：过滤掉不是 dict 或者缺 id 的条目，避免后面按 id
        # 查找/删除/重命名时因为一条坏数据直接抛异常。
        cleaned = [b for b in data if isinstance(b, dict) and b.get("id")]
        return sorted(cleaned, key=lambda b: b.get("created_at", ""), reverse=True)
    except Exception:
        return []


def _save_bookmarks(bookmarks):
    path = get_bookmarks_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(bookmarks, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def find_bookmark_by_content(filename_keyword, filename_regex, content_keyword, content_regex):
    """有没有一条书签的"文件名+内容"四项（关键词+是否正则）跟给定的
    完全一样，有就返回那条记录，没有返回 None。

    书签按钮要不要显示成"已收藏"的实心状态，靠的就是这个——不是记
    "这个按钮之前被点过"，而是每次都拿当前两个搜索框里实际的内容去
    跟已有书签比对，这样不管用户是刚点了按钮收藏、还是手动把搜索框
    内容改成跟某条旧书签一模一样、还是从书签管理里应用了一条书签，
    图标状态都会自动跟着对，不需要额外维护一份"是否已收藏"的标记
    到处同步。

    比较关键词用的是原始大小写（不转小写）——正则表达式大小写敏感，
    统一转小写比较会导致"大小写不同的两条正则"被误判成同一条。
    """
    keyword = (filename_keyword or "", bool(filename_regex), content_keyword or "", bool(content_regex))
    for b in load_bookmarks():
        current = (
            b.get("filename_keyword", ""), bool(b.get("filename_regex", False)),
            b.get("content_keyword", ""), bool(b.get("content_regex", False)),
        )
        if current == keyword:
            return b
    return None


def find_bookmark_by_name(name, exclude_id=None):
    """有没有一条书签叫这个名字（大小写不敏感，"报价单"和"报价单 "
    这种首尾空格差异也不算数——都去掉再比较），有就返回那条记录。
    exclude_id 用于重命名场景：允许"改成跟自己原来一样的名字"，不能
    允许"改成跟别的书签重名"。
    """
    target = (name or "").strip().lower()
    if not target:
        return None
    for b in load_bookmarks():
        if exclude_id is not None and b.get("id") == exclude_id:
            continue
        if b.get("name", "").strip().lower() == target:
            return b
    return None


def add_bookmark(name, filename_keyword, filename_regex, content_keyword, content_regex):
    """新增一条书签，返回新建的那条记录（dict）。

    这里直接读一次现有全部书签、追加、整体写回——书签数量级通常是
    几条到几十条，不是索引数据库那种成千上万条记录，没必要为了这点
    数据单独引入 sqlite，一个 json 文件足够，实现也简单很多。
    """
    bookmarks = load_bookmarks()
    record = {
        "id": uuid.uuid4().hex,
        "name": (name or "").strip() or "未命名书签",
        "filename_keyword": filename_keyword or "",
        "filename_regex": bool(filename_regex),
        "content_keyword": content_keyword or "",
        "content_regex": bool(content_regex),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    bookmarks.append(record)
    _save_bookmarks(bookmarks)
    return record


def rename_bookmark(bookmark_id, new_name):
    """改名，找不到这条 id 就什么都不做（比如用户在书签管理窗口开着
    的时候，另一个窗口正好把这条删了——静默忽略，不弹错误，重新刷新
    一下列表用户自己就看到这条已经不在了）。"""
    new_name = (new_name or "").strip()
    if not new_name:
        return False
    bookmarks = load_bookmarks()
    found = False
    for b in bookmarks:
        if b.get("id") == bookmark_id:
            b["name"] = new_name
            found = True
            break
    if not found:
        return False
    return _save_bookmarks(bookmarks)


def delete_bookmark(bookmark_id):
    bookmarks = load_bookmarks()
    remaining = [b for b in bookmarks if b.get("id") != bookmark_id]
    if len(remaining) == len(bookmarks):
        return False  # 没找到这条，可能已经被删过了
    return _save_bookmarks(remaining)