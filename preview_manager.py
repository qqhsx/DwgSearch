# preview_manager.py
"""
内容预览：点击左侧表格某一行，后台加载这张图纸的文字内容显示到右侧预览区，
并把当前关键词在预览文字里高亮、在原生滚动条上画命中位置标记，支持
"上一个/下一个命中"跳转导航。原来这些都直接挂在 MainWindow 上，现在
拆成 PreviewManager，用法跟 IndexManager/SearchManager 一样：MainWindow
只留一个 `self.preview_manager = PreviewManager(self)` 的引用。

跟 IndexManager/SearchManager 不同的一点：PreviewManager 继承自 QObject，
不是普通 Python 对象——因为 `eventFilter` 这个方法要被直接装到预览区
原生滚动条上（`verticalScrollBar().installEventFilter(...)`），Qt 要求
被装的对象本身必须是 QObject，普通 Python 对象没法承担这个角色。
"""
from PyQt5.QtWidgets import QWidget, QTextEdit, QStyleOptionSlider, QStyle
from PyQt5.QtCore import Qt, QObject, QEvent, QTimer, QRegularExpression
from PyQt5.QtGui import QTextCharFormat, QTextCursor, QColor, QPainter

from search_thread import PreviewLoadThread
from database import DWGDatabase


class PreviewManager(QObject):
    def __init__(self, window):
        super().__init__(window)  # window 当 QObject 的 parent，窗口销毁时自动跟着清理
        self.window = window

        # 预览内容改成后台线程加载（见 PreviewLoadThread），这里只保留一个
        # 线程引用槽位；_preview_request_path 记的是"最近一次请求预览的
        # 文件路径"，线程跑完回来时拿它跟当前选中行比对，选中行已经换了
        # 就直接丢弃这次结果，不会把旧结果误显示成新选中文件的内容。
        self._preview_request_path = None
        # 预览专用的常驻数据库连接，程序启动时开一次，之后每次选中文件
        # 复用这一个连接——不再每次选中都新建 DWGDatabase()（那样每次
        # 都会重新跑一遍建表检查，是白白的固定开销，参见 PreviewLoadThread
        # 里的说明）。
        self._preview_db = DWGDatabase()
        # 存成列表而不是单个可覆盖的属性：如果用户连续快速切换选中行，
        # 新请求会覆盖旧请求，但旧的那个后台线程可能还没跑完——如果它的
        # Python 引用被直接覆盖丢掉，线程还在跑的时候 Python 端就没有
        # 引用指着它了，有被提前垃圾回收的风险。用列表存着，线程真正
        # 跑完（finished 信号）之后再从列表里摘除，那时候删掉才安全。
        self._preview_threads = []

        # 关键词命中位置标记：不再用独立控件去猜坐标/猜透明度，改成给
        # 原生滚动条本身安装事件过滤器，在它自己的绘制事件里"加一笔"。
        # 这样天然贴合、坐标绝对准确，也不需要处理透明合成、鼠标穿透
        # 这些容易在不同 Qt/系统环境下出问题的细节。
        self._preview_marks = []  # 0.0~1.0 的相对位置列表
        self._match_spans = []          # 预览区关键词命中区间 [(起,止), ...]，已按位置排序
        self._match_extra_selections = []  # 与 _match_spans 一一对应的高亮叠加层
        self._current_match_index = -1  # 当前"上一个/下一个"导航到第几个命中

        window.text_display_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        window.text_display_area.verticalScrollBar().installEventFilter(self)

    # =========================================================
    # 触发预览加载
    # =========================================================
    def display_selected_file_content(self):
        window = self.window
        selected_rows = window.table.selectionModel().selectedRows()
        if not selected_rows:
            return
        row = selected_rows[0].row()
        file_path = window.results_model.path_at(row)
        if not file_path:
            return
        window.status_label.setText(file_path)
        content_keywords, _, _, content_regex, _ = window.search_manager.get_search_parameters()
        entity_types, spaces, scopes = window.search_manager.get_search_filters()

        self._preview_request_path = file_path
        # 上一个还没跑完的预览线程不用特意去打断——它查完之后结果会被
        # 下面的"路径是否还匹配当前选中"这一层过滤掉，静默丢弃，不会
        # 影响这次新选中文件的显示。
        thread = PreviewLoadThread(
            self._preview_db, file_path, content_keywords,
            entity_types=entity_types, spaces=spaces, scopes=scopes,
            content_regex=content_regex
        )
        thread.content_ready.connect(self._on_preview_ready)
        thread.content_error.connect(self._on_preview_error)
        thread.finished.connect(lambda t=thread: self._cleanup_preview_thread(t))
        self._preview_threads.append(thread)
        thread.start()

    def _cleanup_preview_thread(self, thread):
        if thread in self._preview_threads:
            self._preview_threads.remove(thread)
        thread.deleteLater()

    def _on_preview_ready(self, dwg_path, texts, keywords_for_highlight, is_regex):
        # 只有这条结果对应的还是"当前最新一次选中的文件"才真正显示——
        # 用户可能在这次查询跑完之前就已经点了别的文件，那种情况下这条
        # 结果已经过期，直接丢弃即可，不做任何界面更新。
        if dwg_path != self._preview_request_path:
            return
        self.on_content_ready(texts, keywords_for_highlight, is_regex)

    def _on_preview_error(self, dwg_path, error_message):
        if dwg_path != self._preview_request_path:
            return
        self.on_content_error(error_message)

    # =========================================================
    # 滚动条命中标记（事件过滤器）
    # =========================================================
    def eventFilter(self, watched, event):
        window = self.window
        # 在预览区原生垂直滚动条自己的绘制事件上"加一笔"：先让滚动条
        # 按正常样式完整画完自己（滑块、轨道、箭头等），再在它自己的
        # 画布上叠加画黄色标记。因为是直接画在滚动条本身上，坐标天然
        # 就是准的，不需要另外猜控件位置、猜透明度这些容易出问题的细节。
        if (watched is window.text_display_area.verticalScrollBar()
                and event.type() == QEvent.Paint):
            # 手动调用一次原生的绘制逻辑，让滚动条先正常画完自己
            QWidget.event(watched, event)
            if self._preview_marks:
                painter = QPainter(watched)
                painter.setRenderHint(QPainter.Antialiasing)

                # 不能直接拿整个控件的高度来算位置——滚动条控件本身包含
                # 顶部和底部两个箭头按钮，真正能滑动的"滑道"范围是去掉
                # 这两个箭头之后剩下的一段，不是从控件最顶端到最底端。
                # 用 QStyleOptionSlider 问一下当前样式下滑道的真实坐标，
                # 而不是自己拿整个控件尺寸去猜。
                opt = QStyleOptionSlider()
                watched.initStyleOption(opt)
                groove_rect = watched.style().subControlRect(
                    QStyle.CC_ScrollBar, opt, QStyle.SC_ScrollBarGroove, watched
                )
                top = groove_rect.top()
                h = groove_rect.height()
                # 横向也用滑道自己的矩形来算，不用整个控件的宽度——
                # watched.width() 包含控件的外边框、阴影这些额外像素，
                # 在高DPI缩放或者不同系统主题下，跟滑道真实的左右边界
                # 可能对不上，导致标记横向偏心或者超出滑道边界。
                left = groove_rect.left()
                gw = groove_rect.width()
                pad = max(1, gw // 8)  # 左右留一点内边距，不贴死滑道边缘
                mark_h = max(3, min(8, h // 100))
                # 分母用"滑道高度 - 标记自身高度"，而不是直接用滑道
                # 高度——标记本身是有高度的，画的时候 y 代表的是标记
                # 顶端而不是中心点，如果直接拿 pos * h 去算、算完了再
                # 夹回边界内，会导致越靠近100%的几个标记全被夹到同一个
                # 上限值，实际挤在一起看着像重叠了，不是连续的比例分布。
                # 从一开始就把标记自身的高度让出来，才能让 0%~100% 全程
                # 平滑连续，不在边界处失真。
                usable_h = max(1, h - mark_h)
                for pos in self._preview_marks:
                    y = top + int(pos * usable_h)
                    y = max(top, min(top + h - mark_h, y))
                    painter.fillRect(left + pad, y, max(1, gw - pad * 2), mark_h, QColor("#f0c000"))
                painter.end()
            return True  # 事件已经处理完，不用再往下传
        return super().eventFilter(watched, event)

    # =========================================================
    # 内容显示 + 关键词高亮
    # =========================================================
    def on_content_ready(self, text_lines, keywords, is_regex=False):
        window = self.window
        full_text = "\n".join(text_lines)
        window.text_display_area.setPlainText(full_text)
        # 清掉上一个文件残留的高亮叠加层（不是文档格式，只是画面上的叠加，清空很快）
        window.text_display_area.setExtraSelections([])

        self._preview_marks = []  # 清空旧标记
        window.text_display_area.verticalScrollBar().update()
        self._match_spans = []
        self._match_extra_selections = []
        self._current_match_index = -1
        self._update_match_nav_label()

        char_count = len(full_text)
        window.preview_overlay_toolbar.setVisible(True)
        if not keywords:
            window.preview_stats_label.setText(f"字数：{char_count}")
            window.text_display_area.moveCursor(QTextCursor.Start)
            return

        # 关键词命中数要等高亮算完才知道，这里先把字数显示出来，
        # 命中数那部分等 _apply_content_highlight 算完之后再补上。
        window.preview_stats_label.setText(f"字数：{char_count} ｜ 命中：统计中…")

        # 到这里文字已经完整显示出来了。关键字高亮（尤其是大文档逐个找匹配位置、
        # 算滚动条标记坐标）单独放到下一个事件循环 tick 里做，让 Qt 先把上面
        # setPlainText 的内容画出来，用户点下去能立刻看到文字，高亮再紧跟着
        # 填上，而不是卡着黑屏等全部算完一起冒出来。
        QTimer.singleShot(0, lambda: self._apply_content_highlight(keywords, char_count, is_regex))

    def _apply_content_highlight(self, keywords, char_count, is_regex=False):
        window = self.window
        doc = window.text_display_area.document()
        # 调试日志证实：这个环境里 documentLayout().documentSize() /
        # blockBoundingRect() 拿到的高度和"块数"完全相等（比如 178 块，
        # 高度就是 178.0），也就是每块都按未初始化的占位值 1 在算，根本
        # 不是真实排版出来的像素高度，不能用。改回用 fontMetrics 自己估算
        # 行高——这个 API 只依赖字体本身，不依赖那个有问题的内部排版对象，
        # 可靠。
        block_count = max(1, doc.blockCount())
        viewport_height = max(1, window.text_display_area.viewport().height())
        line_height = max(1, window.text_display_area.fontMetrics().lineSpacing())
        visible_lines = max(1, viewport_height // line_height)

        if block_count <= visible_lines:
            # 内容一屏就能显示完，不需要滚动：用"一屏能显示的行数"当分母，
            # 标记位置对应实际显示的地方，而不是被拉伸铺满整条标记条。
            denom = visible_lines
        else:
            # 需要滚动：原生滚动条本身就是按"行/块"滚动的，用命中所在的
            # 行号 / 总行数，才能跟原生滚动条实际滚动的位置对应上。
            denom = block_count - 1 if block_count > 1 else 1

        mark_positions = []  # 命中位置换算出的 0.0~1.0 比例
        match_spans = []  # 每个命中的 (起始位置, 结束位置)，之后统一按位置排序

        # 单个关键词最多处理这么多次命中。命中超过这个数之后就不再继续找，
        # 避免遇到那种全文到处都是的常见字/型号时无限增加叠加层数量。
        # 这样不管文件多大、命中多离谱，这一步的耗时都有一个明确上限。
        MAX_MATCHES_PER_KEYWORD = 3000

        # 记录每个关键词各命中多少次，用于统计栏显示；
        # 如果某个词命中数刚好顶到上限，说明实际数量可能更多，标个"+"提示一下。
        keyword_counts = {}

        cursor = QTextCursor(doc)
        for kw in keywords:
            if not kw.strip():
                continue

            regex = None
            if is_regex:
                # 正则模式：编译成 QRegularExpression 交给 QTextDocument.find()
                # 原生支持的重载版本去找，而不是当字面量子串找——大小写
                # 不敏感靠 CaseInsensitiveOption，跟非正则模式的体感一致。
                regex = QRegularExpression(kw)
                regex.setPatternOptions(QRegularExpression.CaseInsensitiveOption)
                if not regex.isValid():
                    # search_manager.py 发起搜索前已经校验过语法，这里
                    # 理论上不会走到——留着只是防御性兜底，不让一个
                    # 编译失败的正则把整个预览高亮搞崩。
                    keyword_counts[kw] = 0
                    continue

            cursor.movePosition(QTextCursor.Start)
            match_count = 0
            while match_count < MAX_MATCHES_PER_KEYWORD:
                cursor = doc.find(regex, cursor) if is_regex else doc.find(kw, cursor)
                if cursor.isNull():
                    break
                match_spans.append((cursor.selectionStart(), cursor.selectionEnd()))
                mark_positions.append(cursor.blockNumber() / denom)
                match_count += 1
                if cursor.selectionStart() == cursor.selectionEnd():
                    # 零宽匹配（正则里全是零宽断言，比如单独一个 "^" 或
                    # 前瞻/后顾断言）：光标原地不动，doc.find() 会在同一
                    # 个位置反复"命中"，陷入死循环。手动把光标往后挪一位
                    # 再继续找，挪到文档末尾就直接结束这一轮。
                    cursor.movePosition(QTextCursor.NextCharacter)
                    if cursor.atEnd():
                        break
            keyword_counts[kw] = match_count

        # 上面是按关键词逐个搜的（先搜完关键词A所有命中，再搜关键词B），
        # 不是按文档先后顺序，这里统一按起始位置排序一次，"上一个/下一个"
        # 导航、以及下面重建 ExtraSelection 列表都依赖这个顺序。
        match_spans.sort(key=lambda span: span[0])
        self._match_spans = match_spans
        self._current_match_index = -1

        # 普通命中和"当前选中的这一处"用两种颜色区分，不然命中一多，
        # 点"上一个/下一个"根本看不出跳到了哪一处。
        # 这里先把每一处都建成"普通"样式存起来，go_to_match 里再单独把
        # 当前这一处的样式换成高亮色，其余复位，重新整体应用一次。
        self._match_extra_selections = []
        for start, end in match_spans:
            sel = QTextEdit.ExtraSelection()
            sel_cursor = QTextCursor(doc)
            sel_cursor.setPosition(start)
            sel_cursor.setPosition(end, QTextCursor.KeepAnchor)
            sel.cursor = sel_cursor
            sel.format = self._match_format(is_current=False)
            self._match_extra_selections.append(sel)

        # 一次性把所有高亮叠加层交给 Qt，纯粹是"画的时候多盖一层颜色"，
        # 不修改文档本身的字符格式、不产生撤销记录、不触发文档级别的
        # 修改信号，即使有几千个命中也只是一次列表赋值 + 一次重绘。
        window.text_display_area.setExtraSelections(self._match_extra_selections)

        # 用字符位置比例直接当作滚动条标记的相对位置。
        # 之前这里用 blockBoundingRect() 去查每个命中位置的真实像素坐标，
        # 精度更高，但每查一次都会触发一次文档排版计算，命中次数一多
        # （大文件、常见关键词）就会明显卡顿。滚动条标记本来就只是个
        # 大致的位置提示，不需要像素级精确，改成按字符比例算，
        # 命中再多也是纯数值计算，不会再触发排版。
        self._preview_marks = mark_positions
        window.text_display_area.verticalScrollBar().update()

        # 更新统计栏：总命中数 + 每个关键词各命中多少次
        total_hits = sum(keyword_counts.values())
        per_kw_parts = []
        for kw, cnt in keyword_counts.items():
            suffix = "+" if cnt >= MAX_MATCHES_PER_KEYWORD else ""
            per_kw_parts.append(f"{kw}:{cnt}{suffix}")
        detail = "，".join(per_kw_parts)
        window.preview_stats_label.setText(
            f"字数：{char_count} ｜ 命中：{total_hits} 处（{detail}）" if detail
            else f"字数：{char_count} ｜ 命中：0 处"
        )

        if self._match_spans:
            self.go_to_match(0)
        else:
            self._update_match_nav_label()
            window.text_display_area.moveCursor(QTextCursor.Start)

    @staticmethod
    def _match_format(is_current):
        fmt = QTextCharFormat()
        if is_current:
            # 当前导航到的这一处：橙色，跟其它普通命中的黄色明显区分开
            fmt.setBackground(QColor(255, 140, 0))
        else:
            fmt.setBackground(QColor(Qt.yellow))
        fmt.setForeground(QColor(Qt.black))
        return fmt

    def on_content_error(self, err_msg):
        window = self.window
        window.text_display_area.setPlainText(err_msg)
        window.preview_stats_label.setText("字数：—")
        # 错误信息本身就是文本框里显示的正文，这里没有真实的字数/命中
        # 可统计，浮层没必要露出来挡一块地方在那儿显示"字数：—"。
        window.preview_overlay_toolbar.setVisible(False)
        self._match_spans = []
        self._match_extra_selections = []
        self._current_match_index = -1
        self._update_match_nav_label()

    # =========================================================
    # 命中导航
    # =========================================================
    def go_to_match(self, index):
        window = self.window
        if not self._match_spans:
            return
        # 循环导航：最后一个"下一个"回到第一个，第一个"上一个"回到最后一个
        index = index % len(self._match_spans)

        # 把上一次的"当前命中"改回普通颜色，避免同时有两处显示成高亮色
        if 0 <= self._current_match_index < len(self._match_extra_selections):
            self._match_extra_selections[self._current_match_index].format = self._match_format(is_current=False)

        self._current_match_index = index
        self._match_extra_selections[index].format = self._match_format(is_current=True)
        window.text_display_area.setExtraSelections(self._match_extra_selections)

        start, _ = self._match_spans[index]
        cursor = QTextCursor(window.text_display_area.document())
        cursor.setPosition(start)
        window.text_display_area.setTextCursor(cursor)
        window.text_display_area.ensureCursorVisible()
        self._update_match_nav_label()

    def go_to_next_match(self):
        if not self._match_spans:
            return
        self.go_to_match(self._current_match_index + 1)

    def go_to_prev_match(self):
        if not self._match_spans:
            return
        self.go_to_match(self._current_match_index - 1)

    def _update_match_nav_label(self):
        window = self.window
        if self._match_spans:
            window.match_nav_label.setText(
                f"{self._current_match_index + 1}/{len(self._match_spans)}"
            )
        else:
            window.match_nav_label.setText("")

    # =========================================================
    # 供搜索流程调用：清空重置成"还没搜索过"的初始状态
    # =========================================================
    def reset(self):
        window = self.window
        window.text_display_area.clear()  # 清空后占位提示文字会自动露出来
        window.text_display_area.setExtraSelections([])
        window.preview_stats_label.setText("字数：—")
        window.preview_overlay_toolbar.setVisible(False)
        self._preview_marks = []
        self._match_spans = []
        self._match_extra_selections = []
        self._current_match_index = -1
        self._update_match_nav_label()

    # =========================================================
    # 程序退出
    # =========================================================
    def shutdown(self):
        try:
            self._preview_db.close()
        except Exception:
            pass