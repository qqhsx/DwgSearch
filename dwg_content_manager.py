# dwg_content_manager.py
import os
from database import DWGDatabase
from helpers import normalize_diameter_symbol

class DxfContentManager:
    """
    内容管理器（完美重构版）：
    彻底移除了 QObject 继承，解决跨线程 parent 指针死锁报错。
    """
    def __init__(self, parent_window=None):
        self.window = parent_window
        # 建立独立的只读账本连接
        self.db = DWGDatabase()

    def request_content(self, dwg_path, keywords, entity_types=None, spaces=None, scopes=None):
        """
        不再发射内部跨线程信号，直接通过主窗体显式回调，实现真正的 0 延迟秒显。
        entity_types / spaces / scopes：跟当前搜索栏勾选的筛选条件保持一致，
        让预览框只显示"这次搜索实际会命中"的那部分内容，而不是不管筛选、
        始终把这个文件提取到的全部文字都倒出来——不然用户勾了"单行文字"
        去搜，点开文件却看到各种类型混在一起，会以为筛选根本没生效。
        """
        if not os.path.exists(dwg_path):
            if self.window:
                self.window.on_content_error(f"错误: 图纸物理文件不存在: {dwg_path}")
            return

        try:
            # 直接从本地数据库读取高能文本串
            extracted_texts = self.db.get_single_file_content(
                dwg_path, entity_types=entity_types, spaces=spaces, scopes=scopes
            )
            keywords_for_highlight = [normalize_diameter_symbol(kw.lower()) for kw in keywords if kw.strip()]

            if not extracted_texts:
                extracted_texts = ["[提示] 当前筛选条件下，该图纸没有匹配的文字内容（或该图纸暂未建立文字账本索引，请重新点击“搜索”激活增量扫描）。"]

            # 直接安全路由通知主视窗进行高亮填充
            if self.window:
                self.window.on_content_ready(extracted_texts, keywords_for_highlight)

        except Exception as e:
            if self.window:
                self.window.on_content_error(f"读取账本预览缓存失败: {e}")

    def clear_cache(self):
        pass