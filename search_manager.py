# search_manager.py
"""
搜索：关键词/筛选条件解析、发起搜索线程、把结果填回表格，以及搜索框
（文件名/内容关键词、搜索目录）这三个下拉框自己的历史清空、自动搜索这些
交互逻辑。原来这些都直接挂在 MainWindow 上，现在拆成 SearchManager，
用法跟 IndexManager 一样：MainWindow 只留一个
`self.search_manager = SearchManager(self)` 的引用。

注意 get_search_parameters() / get_search_filters() 这两个方法不只是
搜索自己用——预览区（MainWindow.display_selected_file_content）也要用
它们判断"当前关键词/筛选条件是什么"来决定预览内容里要高亮哪些词，所以
这两个方法保留成公开方法，预览那边通过 window.search_manager.xxx() 调用，
不算是搜索模块内部私有的东西。
"""
import os
import re
import json
import time
from concurrent.futures import ThreadPoolExecutor
from PyQt5.QtWidgets import QMessageBox

from search_thread import SearchThread
from database import ALL_TEXT_TYPES, ALL_SPACES, ALL_SCOPES
from config import save_search_filter_options, load_config, save_config, clear_all_history
from helpers import fix_path, stat_file_safe
from log_utils import log


class SearchManager:
    def __init__(self, window):
        self.window = window

        self.search_thread = None
        # 新搜索开始时，如果旧搜索线程还没跑完，会先挪到这里占住引用，
        # 防止线程还在跑的时候对象被垃圾回收（PyQt 已知的崩溃诱因）。
        self._stale_search_threads = []
        # 搜索前记下"哪个搜索框正在被编辑、光标在哪"，搜索完再还回去，
        # 避免输入框被临时禁用/重新启用这一下把用户的焦点弄丢。
        self._focused_edit_before_search = None

    # =========================================================
    # 搜索框（文件名/内容关键词、搜索目录）下拉历史交互
    # =========================================================
    def handle_combo_clear(self, combo_box, selected_text):
        window = self.window
        if selected_text == "清空记录":
            clear_all_history(window, combo_box)
            combo_box.setEditText("")
            combo_box.clearFocus()

    def auto_search_on_dropdown_pick(self, selected_text):
        """从文件名/内容搜索框的下拉历史里选中一个关键词时，直接自动
        搜索一次，不用选完了还要再手动点一下搜索图标。"清空记录"这个
        特殊项走的是上面 handle_combo_clear 那条清空逻辑，这里要排除掉，
        不然清空之后紧接着又拿空关键词搜一次，多此一举。"""
        if selected_text == "清空记录":
            return
        self.start_search()

    def on_keyword_text_changed(self, text):
        """文件名/内容关键词框任意一个发生变化时都会调用一次（text 是
        刚发出这次变化的那个框的最新内容）。
        - 两个都被删空了：当成"用户想清空搜索"，重置表格和预览。
        - 只有这次变化的框被删空、另一个框还有内容：自动用剩下那个
          关键词重新搜一次，不用再手动按回车——相当于"删空的那边自动
          退出搜索条件，剩下那边继续生效"。
        - 单纯在还有内容的框里继续打字（text 非空）：什么都不做，跟
          其它场景一样只有回车/点搜索图标才会触发，不会每敲一个字都
          重新搜一次。
        """
        window = self.window
        content = window.keyword_edit.currentText().strip()
        filename = window.filename_keyword_edit.currentText().strip()

        if not content and not filename:
            self.reset_search_results_and_preview()
            return

        if text.strip() == "":
            # 剩下那个关键词不够2个字（搜索本身就要求的最低长度）就先
            # 别自动搜，也别弹"关键词太短"的警告框打断用户——很可能只是
            # 正在删/正在换词，还没打完。凑够字数或手动按回车再说。
            content_keywords, filename_keywords, _, _, _ = self.get_search_parameters()
            if content_keywords or filename_keywords:
                self.start_search()

    def reset_search_results_and_preview(self):
        """把左侧结果表格和右侧内容预览都还原成"还没搜索过"的初始状态。
        预览区自己的状态（文本内容、高亮、命中导航）都在 PreviewManager
        里，这里只负责结果表格，预览那部分转手交给
        window.preview_manager.reset() 去做。"""
        window = self.window
        window.results_model.clear()
        window._last_filename_keywords = []
        window._last_filename_keywords_is_regex = False
        if hasattr(window, 'filename_highlight_delegate'):
            window.filename_highlight_delegate.set_keywords([])

        window.preview_manager.reset()

    def start_new_search(self):
        """菜单栏"文件 -> 新建搜索"：清空文件名/内容两个关键词框、清空
        当前结果表格和预览区，光标定位回文件名搜索框，相当于回到刚打开
        软件时的初始状态——注意不动"搜索目录"那排勾选框，那是用户对
        "平时都搜哪些目录"的持久偏好，跟"这一次搜什么关键词"是两回事，
        不该被"新建搜索"顺带清掉。
        """
        window = self.window
        window.filename_keyword_edit.setCurrentText("")
        window.keyword_edit.setCurrentText("")
        self.reset_search_results_and_preview()
        window.status_label.setText("就绪 | 可随时搜索")
        window.filename_keyword_edit.setFocus()

    # =========================================================
    # 关键词/筛选条件解析
    # =========================================================
    def get_search_parameters(self):
        """
        返回 (content_keywords, filename_keywords, has_single_char,
              content_regex, filename_regex)。

        content_regex / filename_regex：对应搜索框的".*"开关是否打开。
        打开的那个框，keywords 列表最多只有一个元素——正则模式下整段
        输入就是一条完整的表达式，不能再按空格拆成多个词（正则语法里
        空格可能本身就有意义，比如 "\\s*"、或者字面量真的要求两个词
        中间隔一个空格），也不套用"至少2个字"这条给普通子串搜索定的
        长度门槛——正则哪怕只有1个字符（比如单独一个"."）都是合法、
        有意义的完整表达式。
        """
        window = self.window
        content_raw  = window.keyword_edit.currentText().strip()
        filename_raw = window.filename_keyword_edit.currentText().strip()

        content_regex = getattr(window, "content_regex_action", None) is not None \
            and window.content_regex_action.isChecked()
        filename_regex = getattr(window, "filename_regex_action", None) is not None \
            and window.filename_regex_action.isChecked()

        # 用正则按所有空白字符分割——只在非正则模式下才这么拆词
        def split_raw(raw):
            return [k for k in re.split(r'[\s\u3000\u00a0]+', raw) if k]

        if content_regex:
            content_keywords = [content_raw] if content_raw else []
            content_all = []  # 正则模式没有"单字被过滤掉"这回事，不参与下面的判断
        else:
            content_all = split_raw(content_raw)
            content_keywords = [k for k in content_all if len(k) >= 2]

        if filename_regex:
            filename_keywords = [filename_raw] if filename_raw else []
            filename_all = []
        else:
            filename_all = split_raw(filename_raw)
            filename_keywords = [k for k in filename_all if len(k) >= 2]

        # 判断是否有单字被过滤掉（正则模式下的框不参与这个判断，见上面）
        has_single_char = any(len(k) == 1 for k in content_all + filename_all)
        return content_keywords, filename_keywords, has_single_char, content_regex, filename_regex

    def get_search_filters(self):
        """
        读取"文字类型 / 搜索位置"筛选勾选框的状态。
        全选或全不选都等价于"不筛选"——全不选如果真的按字面意思执行会
        导致"什么都搜不到"，这不是用户想要的结果，统一按不筛选处理更安全。
        只有用户明确取消了其中一部分勾选时，才真正生效为筛选条件。
        """
        window = self.window
        checked_types = [c for c, cb in window.filter_type_checkboxes.items() if cb.isChecked()]
        checked_spaces = [c for c, cb in window.filter_space_checkboxes.items() if cb.isChecked()]
        checked_scopes = [c for c, cb in window.filter_scope_checkboxes.items() if cb.isChecked()]

        entity_types = checked_types if 0 < len(checked_types) < len(ALL_TEXT_TYPES) else []
        spaces = checked_spaces if 0 < len(checked_spaces) < len(ALL_SPACES) else []
        scopes = checked_scopes if 0 < len(checked_scopes) < len(ALL_SCOPES) else []
        return entity_types, spaces, scopes

    def save_search_filter_state(self):
        """
        把"文字类型 / 搜索位置 / 块定义范围"三组勾选框当前的原始勾选状态
        （不是 get_search_filters 归一化之后的搜索语义）存到配置文件，
        下次启动界面按这个状态还原，不用每次重开都重新勾一遍。
        """
        window = self.window
        save_search_filter_options({
            "entity_types": {c: cb.isChecked() for c, cb in window.filter_type_checkboxes.items()},
            "spaces": {c: cb.isChecked() for c, cb in window.filter_space_checkboxes.items()},
            "scopes": {c: cb.isChecked() for c, cb in window.filter_scope_checkboxes.items()},
        })

    # =========================================================
    # 搜索（纯查询，不建索引）
    # =========================================================
    def start_search(self):
        window = self.window

        # 防抖：可编辑下拉框按一次回车，会同时触发 returnPressed 和
        # activated 两个信号（选中下拉历史项时也会触发 activated），
        # 两个都连到了 start_search，短时间内的重复调用直接忽略掉，
        # 不然会被下面"旧线程还在跑"那段逻辑当成两次独立搜索来处理。
        now = time.time()
        if getattr(self, '_last_search_trigger_time', 0) and now - self._last_search_trigger_time < 0.3:
            return
        self._last_search_trigger_time = now

        for combo in [window.filename_keyword_edit, window.keyword_edit]:
            if combo.currentText() == "清空记录":
                self.handle_combo_clear(combo, "清空记录")
                return

        # 勾选的目录标签就是这次搜索的范围；一个都没勾选（或者干脆没
        # 加任何标签）= 全部目录，跟以前 path_edit 里"🖥 全部目录"是
        # 同一个语义。多个目录之间是"或"的关系——命中任意一个就算。
        search_folders = window.folder_scope_bar.get_checked_paths()

        content_keywords, filename_keywords, has_single_char, content_regex, filename_regex = \
            self.get_search_parameters()
        if not content_keywords and not filename_keywords:
            if has_single_char:
                QMessageBox.warning(window, "警告", "关键词至少需要2个字，单个字符范围太广无法搜索！")
            else:
                QMessageBox.warning(window, "警告", "请输入内容关键词或文件名关键词进行搜索！")
            return

        # 正则模式下，真正发起搜索前先校验一遍语法——括号不配对这类
        # 写法错误如果不提前拦住，会一路带到 SQLite 那边才暴露，用户
        # 只会看到"搜不到东西"，根本不知道是正则本身写错了。
        if content_regex and content_keywords:
            try:
                re.compile(content_keywords[0])
            except re.error as e:
                QMessageBox.warning(window, "正则表达式有误", f"内容搜索框里的正则表达式写法有问题：\n{e}")
                return
        if filename_regex and filename_keywords:
            try:
                re.compile(filename_keywords[0])
            except re.error as e:
                QMessageBox.warning(window, "正则表达式有误", f"文件名搜索框里的正则表达式写法有问题：\n{e}")
                return

        # 如果旧搜索线程还在（比如用户改了关键词很快又搜了一次），
        # 不再用 terminate() 硬杀——那是在线程正查着 SQLite 的时候
        # 强行砍断，线程连 db.close() 的收尾清理都没机会执行，容易
        # 导致数据库连接处于不安全的中间状态，进而引发变慢甚至崩溃。
        # 改成断开它的 finished_signal（不让它的结果再回来覆盖新搜索
        # 的结果、也不会再去帮忙恢复搜索框的可用状态），让它留在后台
        # 自然跑完自己退出，新的搜索线程立刻开始，不用等、不用硬杀。
        if self.search_thread and self.search_thread.isRunning():
            try:
                self.search_thread.finished_signal.disconnect(self.search_finished)
            except Exception:
                pass
            # 仅仅断开信号还不够：马上要把 self.search_thread 指向新线程，
            # 旧线程对象会失去唯一的 Python 引用——线程还在跑的时候被
            # 垃圾回收，本身就是 PyQt 已知的崩溃诱因之一（"QThread:
            # Destroyed while thread is still running"）。先养在这个列表
            # 里占住引用，等它用 Qt 内置的 finished 信号告诉我们真的跑完
            # 了，再放手让它被回收。
            self._stale_search_threads.append(self.search_thread)
            old_thread = self.search_thread
            old_thread.finished.connect(lambda t=old_thread: self._stale_search_threads.remove(t)
                                         if t in self._stale_search_threads else None)

        # 记下这次搜索用的文件名关键字，结果表格填充时用来高亮文件名列
        # （跟内容关键字高亮走的是不同的地方——内容那份是给预览区用的，
        # 这份是给左侧结果表格文件名列用的，两者互不影响）。连正则模式
        # 标志位也一起记下来，稍后填充表格、真正调用
        # filename_highlight_delegate.set_keywords() 的地方要用到。
        window._last_filename_keywords = filename_keywords
        window._last_filename_keywords_is_regex = filename_regex

        # 搜索框接下来要被临时禁用（防止搜索过程中重复点搜索）；Qt 里
        # 禁用一个正获得焦点的控件，焦点必然会被强制移走，禁用结束后
        # 也不会自动还回来。这里先记下当前到底是哪个输入框（文件名/
        # 内容关键词）有焦点，等 search_finished 里搜索完、重新启用
        # 输入框之后，再把焦点还给它，光标位置也一并恢复，让用户感觉
        # 不到"点了搜索/回一下车，输入框自己掉焦点"这回事。
        self._focused_edit_before_search = None
        for combo in (window.filename_keyword_edit, window.keyword_edit):
            line_edit = combo.lineEdit()
            if line_edit is not None and line_edit.hasFocus():
                self._focused_edit_before_search = (line_edit, line_edit.cursorPosition())
                break

        window.filename_keyword_edit.setEnabled(False)
        window.keyword_edit.setEnabled(False)
        window.filename_search_action.setEnabled(False)
        window.keyword_search_action.setEnabled(False)
        window.filename_search_action.setToolTip("搜索中...")
        window.keyword_search_action.setToolTip("搜索中...")
        window.results_model.clear()
        # 从点击搜索这一刻开始计时，覆盖"后台搜索 + 结果画到表格上"的
        # 完整过程，而不是只统计后台搜索线程内部的耗时——用户实际感受到
        # 的等待时间是这整段，只统计搜索线程那部分会比真实体感短一截。
        self._search_click_time = time.time()

        current_content_txt  = window.keyword_edit.currentText()
        current_filename_txt = window.filename_keyword_edit.currentText()

        entity_types, spaces, scopes = self.get_search_filters()
        self.search_thread = SearchThread(
            dwg_folders=search_folders,
            content_keywords=content_keywords,
            filename_keywords=filename_keywords,
            entity_types=entity_types,
            spaces=spaces,
            scopes=scopes,
            filename_regex=filename_regex,
            content_regex=content_regex
        )
        self.search_thread.finished_signal.connect(self.search_finished)
        self.search_thread.start()

        save_config(window)
        # 这次 load_config 只是为了让文件名/内容关键词的下拉历史把刚用
        # 过的这个词挪到最前面，跟"搜索目录"标签栏无关——它的状态没变，
        # 没必要跟着重建一遍，传 refresh_scope_bar=False 跳过。
        load_config(window, refresh_scope_bar=False)
        window.keyword_edit.setEditText(current_content_txt)
        window.filename_keyword_edit.setEditText(current_filename_txt)

        # 有勾选具体目录时才提升索引优先级；"全部目录"（没勾选任何
        # 目录）没有一个明确的"优先目录"可提，维持原来的扫描顺序。
        for folder in search_folders:
            window.index_manager.ensure_folder_indexed_first(folder)

    def search_finished(self, status_msg, result_json):
        window = self.window
        window.filename_keyword_edit.setEnabled(True)
        window.keyword_edit.setEnabled(True)
        window.filename_search_action.setEnabled(True)
        window.keyword_search_action.setEnabled(True)

        # 输入框刚被重新启用，重新把焦点、光标位置还给搜索前正在编辑
        # 的那个框（见 start_search 里的记录逻辑），不然用户点搜索/
        # 回车之后会感觉输入框自己莫名其妙失焦了。
        focus_info = getattr(self, "_focused_edit_before_search", None)
        if focus_info is not None:
            line_edit, cursor_pos = focus_info
            self._focused_edit_before_search = None
            line_edit.setFocus()
            line_edit.setCursorPosition(cursor_pos)
        window.filename_search_action.setToolTip("搜索")
        window.keyword_search_action.setToolTip("搜索")

        # ==== 临时打点：定位2秒到底花在哪一步，排查完记得删掉这几行 ====
        _t_fill_start = time.time()

        try:
            # V2.19.1：result_json 现在是 {dwg_path: dwg_version, ...}
            # （见 search_thread.py / searcher.py / database.search_dwg_index），
            # 不再是纯路径列表——版本号跟着这次查询一起从数据库带出来，
            # 不用再对每个命中文件单独现读一次文件头。
            matched_versions = json.loads(result_json)
        except Exception:
            matched_versions = {}
        matched_files = list(matched_versions.keys())
        log(f"[打点] JSON解析完成，命中 {len(matched_files)} 条，"
            f"耗时 {time.time() - _t_fill_start:.3f} 秒")

        # 把这次搜索用的文件名关键字交给委托，填表时第0列会按这份词表
        # 自动高亮命中片段；如果这次只用了内容关键字搜索（文件名关键字
        # 为空），委托会自动退回普通绘制，不会显示旧一轮搜索残留的高亮。
        window.filename_highlight_delegate.set_keywords(
            window._last_filename_keywords,
            is_regex=getattr(window, "_last_filename_keywords_is_regex", False)
        )

        # 并发预取每个文件的 stat 信息（创建/修改时间、大小）。之前是填表格时
        # 一行一行现查，几千个文件在网络共享盘上就是几千次排队等待的网络
        # 访问；这里用线程池同时发起多个访问，网络延迟可以叠在一起而不是
        # 排队等，实测对网络盘场景效果明显。线程数给个上限，避免文件数
        # 特别多时一次性开太多连接。
        #
        # V2.19.1：这里不再顺带读 DWG 版本了——版本号已经在上面
        # matched_versions 里从数据库查出来了，这一步现在只负责 os.stat()。
        stat_results = {}
        _t_prefetch_start = time.time()
        if matched_files:
            max_workers = min(32, len(matched_files))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                for file_path, stat_result in executor.map(stat_file_safe, matched_files):
                    stat_results[file_path] = stat_result
        # ==== 打点 ====
        log(f"[打点] 并发预取stat完成（版本号已从数据库随查询带出，"
            f"这一步不再读版本），耗时 {time.time() - _t_prefetch_start:.3f} 秒")

        # V2.19.2：填表格从"逐行创建 QTableWidgetItem"换成了一次
        # model.load_rows()（见 results_table_model.py）——不再需要手动
        # setRowCount(0)/setUpdatesEnabled(False)/blockSignals(True) 这一
        # 整套围绕 QTableWidget 的性能优化手法，load_rows() 内部走的是
        # beginResetModel()/endResetModel()，Qt 自己会做好这部分的批量
        # 更新优化；也不再需要 blockSignals 防止 itemChanged 误触发改名
        # ——重命名逻辑现在是 ResultsTableModel.setData() 主动调用的，
        # 不会被"程序自己往表格里灌数据"这个动作意外触发。
        _t_populate_start = time.time()
        window.results_model.load_rows(
            [(path, stat_results.get(path), matched_versions.get(path))
             for path in matched_files]
        )
        # ==== 打点：填表格这段本身，以及从解析JSON到表格填完的总耗时 ====
        log(f"[打点] 表格填充（model.load_rows，{len(matched_files)}行）"
            f"完成，耗时 {time.time() - _t_populate_start:.3f} 秒")
        log(f"[打点] 从JSON解析到表格填完，总耗时 "
            f"{time.time() - _t_fill_start:.3f} 秒")

        # 用"点击搜索"到"表格真正填完"这段真实总耗时，替换掉
        # status_msg 里原本只统计了后台搜索线程内部的那个耗时数字，
        # 这样显示出来的时间才是用户实际等待的时间。
        total_elapsed = time.time() - getattr(self, "_search_click_time", time.time())
        status_msg = re.sub(
            r"耗时\s*[\d.]+\s*秒",
            f"耗时 {total_elapsed:.2f} 秒",
            status_msg,
        )

        # 判断索引是否还在运行，给出对应提示
        if window.index_manager.index_thread and window.index_manager.index_thread.isRunning():
            window.status_label.setText(
                f"{status_msg} ⚠️ 后台索引仍在运行，结果可能不完整"
            )
        else:
            window.status_label.setText(f"{status_msg}")

        window.index_manager.refresh_stats()

        if self.search_thread:
            self.search_thread.quit()
            self.search_thread.wait()
            self.search_thread = None

    # =========================================================
    # 程序退出
    # =========================================================
    def shutdown(self):
        """程序退出前调用，把搜索线程妥善收尾。跟索引线程不同，搜索
        线程只是纯查询（SELECT），中途硬杀不会有 VACUUM 那种损坏数据库
        文件的风险，维持原来 terminate() 的做法。"""
        if self.search_thread and self.search_thread.isRunning():
            self.search_thread.terminate()
            self.search_thread.wait(1000)