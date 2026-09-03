# searcher.py
import os
import time
from config import path_is_within

class Searcher:
    def __init__(self, db_instance):
        self.db = db_instance

    def search_keywords(self, dwg_folders, content_keywords, filename_keywords=None,
                        progress_callback=None, count_callback=None, stop_flag=None, total_files=0,
                        entity_types=None, spaces=None, scopes=None,
                        filename_regex=False, content_regex=False):
        """返回 { dwg_path: dwg_version, ... }——路径命中的同时把数据库里
        缓存的 DWG 版本号一起带出来（见 database.search_dwg_index 的返回值
        说明），调用方（search_thread.py）不再需要在填搜索结果表格时
        对每个命中文件单独现读一次版本号。"""
        matched_files = {}

        content_kws  = [kw.strip().lower() for kw in content_keywords  if kw.strip()]
        filename_kws = [kw.strip().lower() for kw in filename_keywords if kw.strip()] if filename_keywords else []

        if progress_callback:
            progress_callback("⚡ 正在从 SQLite 智能文字账本中过滤匹配项...")

        try:
            db_matches = self.db.search_dwg_index(content_kws, filename_kws,
                                                    entity_types=entity_types, spaces=spaces,
                                                    scopes=scopes,
                                                    filename_regex=filename_regex,
                                                    content_regex=content_regex)

            searched_count = 0
            matched_count  = 0

            # dwg_folders 为空 = 搜全库，不限定目录；非空则是"落在其中
            # 任意一个目录下面"就算命中（多个目录之间是"或"的关系）。
            target_folders = [f for f in (dwg_folders or []) if f and f.strip()]

            for file_path in db_matches:
                if stop_flag and not stop_flag():
                    break

                norm_path = os.path.normpath(file_path)
                if not target_folders or any(path_is_within(norm_path, f) for f in target_folders):
                    matched_files[norm_path] = db_matches.get(file_path)
                    matched_count += 1
                    if progress_callback:
                        progress_callback(f"击中账本记录: {os.path.basename(norm_path)}")

                searched_count += 1
                if count_callback and total_files > 0:
                    current_searched = min(total_files, max(searched_count, total_files // 2))
                    count_callback(total_files, current_searched, matched_count)

        except Exception as e:
            if progress_callback:
                progress_callback(f"🚨 账本内容过滤时发生异常: {e}")

        return matched_files