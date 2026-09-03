# helpers.py
import os
import re
import html
import subprocess
import sys
import time
from PyQt5.QtWidgets import (
    QTableWidgetItem, QMessageBox, QStyledItemDelegate, QStyle,
    QStyleOptionViewItem, QApplication, QLabel, QWidget, QHBoxLayout, QComboBox,
    QLineEdit, QTableView, QAbstractItemView, QFrame, QHeaderView, QTableWidget,
    QToolButton
)
from PyQt5.QtCore import Qt, pyqtSignal, QObject, QEvent, QTimer, QSize
from PyQt5.QtGui import (
    QTextDocument, QAbstractTextDocumentLayout, QPalette, QTextOption, QColor, QIcon,
    QFontMetrics
)
import ezdxf

_DOWN_ARROW_ICON_CACHE = {}


def _get_down_arrow_icon_path(color="#6b7280", size=10):
    """手绘一个向下箭头，缓存成本地临时 PNG 文件，返回文件路径。

    QSS 的 `image: url(...)` 没法直接指向内存里的 QPixmap/QIcon，只能
    指向一个真实存在的图片文件——跟 make_search_icon()/make_regex_icon()
    一样用 QPainter 手绘，只是多一步落盘缓存（同一个颜色+尺寸只画
    一次，存在系统临时目录，之后重复使用同一个文件，不会每次调用
    都重新画一遍）。

    这是给 QComboBox::down-arrow 用的：之前试过纯靠 QSS 描边（只设
    border，不设 image）指望 Qt 用当前系统主题原生画箭头，但 Windows
    的 vista 主题下，只要 QComboBox 本身或者 ::drop-down 任意一处被
    自定义样式表覆盖过，原生箭头绘制在不同 Qt/Windows 版本组合下
    表现很不稳定（有时候直接连箭头一起不画了）。与其继续在这上面
    赌运气，不如跟其它图标一样自己画一个，明确指定给 QSS 用，肯定
    能显示、不受原生主题影响。
    """
    key = (color, size)
    if key in _DOWN_ARROW_ICON_CACHE:
        return _DOWN_ARROW_ICON_CACHE[key]

    import tempfile
    from PyQt5.QtGui import QPixmap, QPainter, QPen
    from PyQt5.QtCore import QPointF

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.2, size * 0.16))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    # 简单的"v"形向下箭头，两条线段撇出一个尖角
    painter.drawLine(QPointF(size * 0.2, size * 0.35), QPointF(size * 0.5, size * 0.65))
    painter.drawLine(QPointF(size * 0.5, size * 0.65), QPointF(size * 0.8, size * 0.35))
    painter.end()

    tmp_dir = tempfile.gettempdir()
    file_path = os.path.join(tmp_dir, f"dwg_search_combo_arrow_{color.lstrip('#')}_{size}.png")
    pixmap.save(file_path, "PNG")
    file_path = file_path.replace("\\", "/")  # QSS url() 在 Windows 下也要用正斜杠
    _DOWN_ARROW_ICON_CACHE[key] = file_path
    return file_path


def make_search_icon(color="#6b7280", size=16, bg_color=None, border_color=None, glyph_size=None):
    """手绘一个放大镜图标（圆圈+手柄），供搜索框左侧的图标按钮使用。
    不依赖任何外部图片资源，颜色可以跟随主题调，尺寸也能跟着输入框
    高度稍微调整，避免在不同 DPI/行高下显得太大或太小。

    size：整张画布大小（应该跟按钮点击热区一致，图标才不会被拉伸模糊）。
    glyph_size：放大镜图案本身的大小，居中画在画布里，独立于 size——
    不传就默认等于 size（跟以前行为一致，画布多大图案就多大）。

    bg_color / border_color：给图标垫一块圆角方形底色（可选再加一圈描边），
    用来在"文件名搜索"和"内容搜索"两个视觉上完全一样的放大镜之间做
    色差区分——比如一个灰底白标、一个白底灰标+描边，一眼就能分清
    这是两个不同的搜索入口。不传就是纯图标、透明背景，跟以前一样。
    """
    from PyQt5.QtGui import QIcon, QPixmap, QPainter, QPen, QBrush
    from PyQt5.QtCore import QPointF, QRectF

    if glyph_size is None:
        glyph_size = size

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    if bg_color:
        # 描边要画在底色方块内侧，不然线宽的一半会被裁到画布外面，
        # 半径也留一点余量，避免锯齿边缘贴着画布边界。
        border_w = size * 0.08
        badge_rect = QRectF(border_w / 2, border_w / 2, size - border_w, size - border_w)
        if border_color:
            border_pen = QPen(QColor(border_color))
            border_pen.setWidthF(border_w)
            painter.setPen(border_pen)
        else:
            painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(bg_color)))
        painter.drawRoundedRect(badge_rect, size * 0.22, size * 0.22)

    pen = QPen(QColor(color))
    pen.setWidthF(glyph_size * 0.12)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)

    # 图案本身居中画在 glyph_size × glyph_size 的虚拟范围里，圆圈占
    # 左上大半个区域，手柄从圆圈右下方斜着伸出去，是最经典的放大镜
    # 辨识度最高的画法。
    gx = (size - glyph_size) / 2.0
    gy = (size - glyph_size) / 2.0
    circle_d = glyph_size * 0.62
    center = QPointF(gx + glyph_size * 0.42, gy + glyph_size * 0.42)
    painter.drawEllipse(center, circle_d / 2, circle_d / 2)
    handle_start = QPointF(
        center.x() + circle_d / 2 * 0.75,
        center.y() + circle_d / 2 * 0.75,
    )
    handle_end = QPointF(gx + glyph_size * 0.92, gy + glyph_size * 0.92)
    painter.drawLine(handle_start, handle_end)
    painter.end()

    return QIcon(pixmap)


def make_regex_icon(color="#6b7280", canvas_size=30, highlight_size=None, glyph_size=None, active=False):
    """手绘一个".*"正则模式开关图标。三个尺寸完全独立，互不影响：

    - canvas_size：整张图标画布大小。应该跟按钮的点击热区
      （QToolButton.setFixedSize）保持一致，这样图标贴满按钮、不会被
      Qt 自动缩放导致模糊；画布里没画到的地方都是透明的。
    - highlight_size：选中态背景高亮色块的大小，居中画在画布正中间。
      可以比 canvas_size 小（周围留出看得见的透明空隙），也可以等于
      canvas_size（贴满整个热区）。不传就默认等于 canvas_size。
    - glyph_size：".*"图案本身（句点+星号）的大小，居中画在画布正中间，
      同样可以比高亮色块小或者一样大。不传就默认是 highlight_size 的
      55%（这是视觉上比较协调的默认比例，想要更大/更小直接传具体数字
      覆盖，不用改这个百分比）。
    """
    from PyQt5.QtGui import QIcon, QPixmap, QPainter, QPen, QBrush
    from PyQt5.QtCore import QPointF, QRectF
    import math

    canvas_size = max(8, int(canvas_size))
    if highlight_size is None:
        highlight_size = canvas_size
    highlight_size = max(1, int(highlight_size))
    if glyph_size is None:
        glyph_size = round(highlight_size * 0.55)
    glyph_size = max(1, int(glyph_size))

    pixmap = QPixmap(canvas_size, canvas_size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)

    if active:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#d3e4ff"))  # 贴合参考图的浅蓝高亮色
        hl_offset = (canvas_size - highlight_size) / 2.0
        # 0.5px 边距防抗锯齿裁切，4.0px 微圆角
        painter.drawRoundedRect(
            QRectF(hl_offset + 0.5, hl_offset + 0.5, highlight_size - 1.0, highlight_size - 1.0),
            4.0, 4.0
        )
        line_color = QColor("#1a5fb4")
    else:
        line_color = QColor(color)

    # ".*" 图案画在一个 glyph_size × glyph_size 的虚拟范围里，同样居中
    gx = (canvas_size - glyph_size) / 2.0
    gy = (canvas_size - glyph_size) / 2.0
    gw = gh = glyph_size

    pen = QPen(line_color)
    pen.setWidthF(max(1.35, gh * 0.095))
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)

    # 1. 句点（左侧实心圆点）
    dot_r = max(1.1, gh * 0.08)
    dot_center = QPointF(gx + gw * 0.28, gy + gh * 0.65)
    painter.setBrush(QBrush(line_color))
    painter.drawEllipse(dot_center, dot_r, dot_r)

    # 2. 星号（右侧三条交叉线）
    painter.setBrush(Qt.NoBrush)
    star_center = QPointF(gx + gw * 0.68, gy + gh * 0.48)
    star_r = gh * 0.23

    for i in range(3):
        angle = math.radians(30 + i * 60)
        dx = math.cos(angle) * star_r
        dy = math.sin(angle) * star_r
        painter.drawLine(
            QPointF(star_center.x() - dx, star_center.y() - dy),
            QPointF(star_center.x() + dx, star_center.y() + dy),
        )

    painter.end()

    return QIcon(pixmap)


def bookmark_icon_hitbox_size(height):
    """跟"添加到书签"按钮实际用的点击热区大小保持一致的计算公式，
    单独拎出来是因为图标状态（空心/实心）需要在按钮创建完之后动态
    刷新（见 layout.py 的 _refresh_bookmark_icon_state()），刷新的
    地方拿不到 build_native_search_combo() 内部的局部变量，得用同一
    套公式重新算一遍——写成一个函数，两处保证用的是同一份逻辑，不会
    出现"按钮建好时是一个尺寸、后来刷新图标时又用另一个尺寸"这种
    不小心改错一处忘了改另一处的问题。
    """
    return height - 1


def make_bookmark_status_icon(color, height, filled=False):
    """给定按钮所在那一行的高度，画一份跟 build_native_search_combo()
    创建按钮时完全同规格的书签图标——图案大小的换算公式（0.5 倍
    热区）也统一放在这里，避免两处各写一份、以后改了一处忘了改另一处。
    layout.py 动态刷新"当前搜索条件是否已收藏"这个状态时用这个函数
    就够了，不需要关心内部的尺寸细节。
    """
    hitbox = bookmark_icon_hitbox_size(height)
    glyph = round(hitbox * 0.5)
    return make_bookmark_icon(color=color, canvas_size=hitbox, glyph_size=glyph, filled=filled)


def make_bookmark_icon(color="#6b7280", canvas_size=30, glyph_size=None, filled=False):
    """手绘一个书签/标签形状图标（跟浏览器地址栏里的收藏按钮是同一个
    意象：竖着的丝带，底边中间剪出一个 V 字缺口），用在"把当前搜索
    条件收藏起来"这颗按钮上。

    filled=False（默认，"还没收藏"状态）：只画描边、不填色——这是个
    单纯的点击按钮（点一下=收藏一次），跟左侧放大镜 search_btn 是
    同一类，靠外层 QToolButton:hover 的背景色变化给点击反馈就够了。

    filled=True（"当前文件名+内容搜索条件已经收藏过"状态）：整个丝带
    形状实心填色，颜色跟正则开关.*选中态用的强调蓝（#1a5fb4）保持
    一致，同一个"这项设置眼下是开着的"的视觉语言在整个搜索框区域里
    统一，不用用户对着两种不同的"激活态"配色重新学一遍。
    """
    from PyQt5.QtGui import QIcon, QPixmap, QPainter, QPen, QBrush, QPolygonF
    from PyQt5.QtCore import QPointF

    canvas_size = max(8, int(canvas_size))
    if glyph_size is None:
        glyph_size = round(canvas_size * 0.5)
    glyph_size = max(1, int(glyph_size))

    pixmap = QPixmap(canvas_size, canvas_size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)

    gx = (canvas_size - glyph_size) / 2.0
    gy = (canvas_size - glyph_size) / 2.0
    gw = gh = glyph_size

    line_color = QColor("#1a5fb4") if filled else QColor(color)
    pen = QPen(line_color)
    pen.setWidthF(max(1.2, gh * 0.11))
    pen.setJoinStyle(Qt.RoundJoin)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(QBrush(line_color) if filled else Qt.NoBrush)

    # 丝带轮廓：左上 -> 右上 -> 右下 -> (底边中点往上缩一截，剪出V字
    # 缺口) -> 左下 -> 回到左上（drawPolygon 自动闭合最后一段）。
    notch_depth = gh * 0.35
    polygon = QPolygonF([
        QPointF(gx, gy),
        QPointF(gx + gw, gy),
        QPointF(gx + gw, gy + gh),
        QPointF(gx + gw / 2.0, gy + gh - notch_depth),
        QPointF(gx, gy + gh),
    ])
    painter.drawPolygon(polygon)

    painter.end()

    return QIcon(pixmap)


def refresh_bookmark_icon_everywhere(window):
    """书签数据发生了变化（新增/改名/删除）之后，把"当前搜索条件是否
    已收藏"这个图标状态刷新广播给同一进程里全部窗口，不能只刷新触发
    这次改动的那一个——书签数据是全进程共享的一份 json 文件，"在 A
    窗口删了一条书签，B 窗口的书签图标却还显示着实心"这种界面状态跟
    真实数据对不上的情况不该出现。

    用 type(window)._all_windows 拿"这个类的全部实例"这个类属性列表，
    不直接 import MainWindow——main_window.py 本来就要 import layout.py
    才能用 create_main_layout()，这里如果反过来在 layout.py 或
    bookmark_manager_dialog.py 里 import main_window.py，会绕成一个
    循环 import。每个窗口是否真的挂了书签按钮、要怎么刷新，都封装在
    各自的 _refresh_bookmark_icon_impl 闭包里（见 layout.py），这里
    只负责"挨个通知一遍"，不关心具体怎么刷新。
    """
    for w in type(window)._all_windows:
        impl = getattr(w, "_refresh_bookmark_icon_impl", None)
        if impl is not None:
            impl()


def force_window_to_foreground(win):
    """把窗口真正拉到最前面、拿到焦点，不是"调用了 show()/raise_()"
    就完事——尤其是右键菜单"用 DWG 图纸搜索工具搜索此目录"这种场景，
    光这几个 Qt 调用经常不够用。

    背景：右键菜单这个操作，实际发起请求的是资源管理器 Explorer.exe
    短暂启动的一个"传令兵"进程（main.py 里 `_try_notify_running_
    instance`）——它把目录路径通过本地 IPC 转发给真正常驻后台的那个
    进程之后就立刻退出了，新窗口是那个后台常驻进程自己创建出来的。
    Windows 有一套"前台窗口锁定"机制（防止后台进程随意抢焦点打扰
    用户），只有"刚刚收到过用户输入"的进程才有权限把自己的窗口切到
    最前面；这个后台常驻进程本身并没有直接收到那次右键点击的输入
    事件（收到点击的是 Explorer.exe），所以哪怕代码里老老实实调用了
    show()/raise_()/activateWindow()，Windows 也经常只会让新窗口的
    任务栏图标闪一下，不会真的把它切到其它窗口前面——用户看到的表现
    就是新窗口"消失"在其它窗口最底下，得手动最小化所有窗口或者去
    任务栏里找。这不是窗口没显示出来，是显示出来了但没抢到"前台"
    这个特权。

    解决办法是 Windows 上一个很经典的技巧：把当前前台窗口所在线程的
    输入状态"接"到本进程线程上（AttachThreadInput），这样在 Windows
    眼里接下来这次 SetForegroundWindow 调用就等价于"前台窗口自己
    发起的"，不再受"后台进程不能抢前台"这条限制约束；调用完之后
    立刻把两个线程的输入状态"分开"（传 False），不会有任何副作用
    残留在系统里。非 Windows 平台（比如开发机用 Linux 跑）没有这套
    API，直接跳过，退回到 Qt 原来那几个跨平台调用方式兜底。
    """
    win.show()
    if sys.platform.startswith("win"):
        try:
            import ctypes
            hwnd = int(win.winId())
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            foreground_hwnd = user32.GetForegroundWindow()
            current_thread_id = kernel32.GetCurrentThreadId()
            foreground_thread_id = user32.GetWindowThreadProcessId(foreground_hwnd, None)
            attached = False
            if foreground_thread_id and foreground_thread_id != current_thread_id:
                attached = bool(user32.AttachThreadInput(
                    foreground_thread_id, current_thread_id, True
                ))
            try:
                user32.SetForegroundWindow(hwnd)
            finally:
                # 不管上面 SetForegroundWindow 成不成功，接上的输入状态
                # 都必须解开，不然会影响系统里其它窗口后续的焦点切换。
                if attached:
                    user32.AttachThreadInput(
                        foreground_thread_id, current_thread_id, False
                    )
        except Exception:
            pass  # 拿不到 ctypes 或者调用失败，就用下面 Qt 那几行兜底
    win.raise_()
    win.activateWindow()


class _FocusBorderWatcher(QObject):
    """监听输入框的 FocusIn/FocusOut，切换外框（container）的描边颜色。
    Qt 的样式表不支持 CSS3 的 :focus-within，只能这样手动实现。"""
    def __init__(self, container, normal_qss, focus_qss):
        super().__init__(container)
        self._container = container
        self._normal_qss = normal_qss
        self._focus_qss = focus_qss

    def eventFilter(self, obj, event):
        if event.type() == QEvent.FocusIn:
            self._container.setStyleSheet(self._focus_qss)
        elif event.type() == QEvent.FocusOut:
            self._container.setStyleSheet(self._normal_qss)
        return False


class ElidedLabel(QLabel):
    """会自动省略号截断的单行 QLabel。

    背景：普通 QLabel 不会自己换行也不会自动截断——文本多长，它的
    sizeHint() 就要多宽，放在一个可自由拉伸的对话框里，一旦塞进一条
    很长的文本（比如某个报错信息、完整文件路径），QLabel 会直接把整个
    对话框撑宽甚至撑得很长，而不是老老实实待在自己的位置上把文字截断。

    这里用 QFontMetrics.elidedText() 按当前控件宽度动态算省略号该截在
    哪，宽度变化（比如对话框被用户拖动缩放）时在 resizeEvent 里重新
    算一遍；完整文本始终存在 self._full_text，同时设成 tooltip，鼠标
    悬停能看到完整内容，不会因为截断丢信息。
    """

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setText(text)

    def setText(self, text):
        self._full_text = text or ""
        self.setToolTip(self._full_text)
        self._update_elided_text()

    def text(self):
        return self._full_text

    def _update_elided_text(self):
        fm = self.fontMetrics()
        # 留几个像素余量，不然偶尔会因为四舍五入差一两个像素又触发省略
        available = max(0, self.width() - 4)
        elided = fm.elidedText(self._full_text, Qt.ElideRight, available)
        super().setText(elided)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided_text()


class ClickableIconLabel(QLabel):
    """搜索框左侧的图标区域，跟输入框合并成一个整体（图标块贴满整个
    高度、紧贴左边框和上下边框），而不是用 QLineEdit.addAction() 那种
    "文字框里嵌一个小图标"的做法——那种做法图标周围会被 Qt 自动留一圈
    间距，图标显得又小又飘，达不到参考图里图标块跟外框边线完全对齐、
    贴满一整块的效果。"""
    clicked = pyqtSignal()

    def __init__(self, icon, icon_size, parent=None):
        super().__init__(parent)
        self._icon = icon
        self._icon_size = icon_size
        self.setAlignment(Qt.AlignCenter)
        self.setPixmap(icon.pixmap(icon_size, icon_size))
        # 不强制手型光标——app 里其它按钮悬停时都是默认箭头光标 + 颜色
        # 变化，这里跟着保持一致，不用手型光标额外提示"这是个链接"。

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            self.clicked.emit()
        super().mousePressEvent(event)

    def setEnabled(self, enabled):
        super().setEnabled(enabled)
        # 禁用时图标也要跟着变灰，不然看着还是能点、体验上不一致
        mode = QIcon.Normal if enabled else QIcon.Disabled
        self.setPixmap(self._icon.pixmap(self._icon_size, self._icon_size, mode))


class _CheckableIconLabel(ClickableIconLabel):
    """跟 ClickableIconLabel 类似，但多了"按下去会保持选中状态"这个
    开关行为。目前没有地方在用（搜索框的正则开关已经改回原生
    QAction.setCheckable()，见 build_native_search_combo()），先留着
    这个类本身没坏处，万一以后哪个新按钮需要"点了变蓝高亮、再点一下
    切回去"这种效果可以直接复用，不用重新写一遍切换逻辑。
    """
    toggled = pyqtSignal(bool)

    def __init__(self, icon_off, icon_on, icon_size, parent=None):
        super().__init__(icon_off, icon_size, parent)
        self._icon_off = icon_off
        self._icon_on = icon_on
        self._checked = False
        self.setObjectName("checkableIconLabel")
        self._apply_checked_style()

    def _apply_checked_style(self):
        if self._checked:
            self.setPixmap(self._icon_on.pixmap(self._icon_size, self._icon_size))
            self.setStyleSheet(
                "#checkableIconLabel { background-color: #e3f0ff; border-radius: 3px; }"
            )
        else:
            self.setPixmap(self._icon_off.pixmap(self._icon_size, self._icon_size))
            self.setStyleSheet("#checkableIconLabel { background: transparent; }")

    def isChecked(self):
        return self._checked

    def setChecked(self, checked):
        checked = bool(checked)
        if checked == self._checked:
            return
        self._checked = checked
        self._apply_checked_style()
        self.toggled.emit(self._checked)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            self.setChecked(not self._checked)
        # 故意不调用 super().mousePressEvent()——ClickableIconLabel 的
        # 版本会额外发一次 clicked 信号，这里只需要 toggled，两个信号
        # 一起发容易在外部造成"点一下触发了两套逻辑"的误会。


def build_native_search_combo(placeholder, icon_color, height, font_size, with_bookmark_button=False):
    """搭一个用系统原生轻量样式的可编辑下拉搜索框——一个 QComboBox，
    放大镜、清空×、正则.* 三个图标都是叠加在 QComboBox 本体上的独立
    QToolButton（不挂在内部 QLineEdit 上——那个实际可用高度比整个
    下拉框矮一截，按钮会被裁切），下拉箭头是 QComboBox 自带的
    ::drop-down，用手绘图标替换掉不稳定的原生绘制（见
    _get_down_arrow_icon_path()）。

    == 尺寸怎么调 ==
    每个图标的"点击/悬停热区大小"、"选中态背景高亮色块大小"、"图案本身
    大小"这三样东西完全独立，改任意一个都不会牵动另外两个。全部集中
    写在下面"尺寸设置区"这一段，每个变量名自解释，想调哪个改哪个：

    - *_HITBOX_*：按钮实际能点/能悬停的范围（QToolButton.setFixedSize
      / QComboBox::drop-down 的 width），决定这块区域多大鼠标移上去
      会有反应，跟视觉大小没有必然关系。
    - *_HIGHLIGHT_*：选中/悬停时那块背景色块本身画多大，居中摆在热区
      正中间，可以比热区小（周围留白）也可以刚好占满热区。
    - *_GLYPH_*/*_ICON_*：图案（放大镜/.*/×/箭头）本身画多大，同样
      居中摆放，独立于前两者。

    返回 (combo, search_action, regex_action, bookmark_action)：
    - combo：QComboBox 本身，直接当成普通 QComboBox 用（取值、下拉
      历史、回车搜索等跟以前完全一样）。
    - search_action：左侧放大镜按钮，兼容 QAction 的常用接口
      （.triggered 信号、.setEnabled()、.setToolTip()），外部代码
      不用改。
    - regex_action：右侧".*"正则开关，QToolButton 本身自带
      setCheckable()/isChecked()/toggled，跟以前 QAction 提供的接口
      兼容。
    - bookmark_action：清空×左边、正则.*右边那颗"添加到书签"按钮，
      只有 with_bookmark_button=True 时才会真正创建（目前只有文件名
      搜索框传 True——内容搜索框不需要重复放一份，书签本来就是把
      "文件名+内容"两个框的搜索条件打包收藏成一条，只需要一个入口）。
      没传 True 时这一项是 None，跟以前只返回 3 个值的调用点保持兼容
      （直接 `_, _, _ = build_native_search_combo(...)[:3]` 这种写法
      还是能用；新代码建议直接按 4 个值解包）。
    """
    combo = QComboBox()
    combo.setEditable(True)
    combo.setFixedHeight(height)
    # 边框颜色跟"搜索目录"那一行的目录条、下方结果列表/表格保持统一
    # （同一个 #d5d9de，项目里其它输入类控件也是这个颜色），不用 Qt
    # 原生控件默认的深灰色描边——原生描边单独摆出来比周围其它元素
    # 明显深一截，两个主搜索框反而显得比次要元素还抢眼，观感不统一。
    # 只有输入框真正拿到焦点（开始编辑）时才切换成强调色 #4a90d9，
    # 效果类似"高亮"一下，提示当前正在往这个框里输入。
    # 注意：QComboBox 可编辑时会把内部 QLineEdit 设成 focusProxy，
    # hasFocus() 会顺着 focusProxy 判断，所以给 QComboBox 本身写
    # `:focus` 伪状态是能正常生效的，不需要单独去处理内部 lineEdit。
    #
    # 右侧下拉箭头这块来回踩了两次坑：
    #   1) 只给 QComboBox 本身写边框，::drop-down 子控件不会跟着换
    #      皮肤，Qt 还是按原生 Vista 样式单独画一个带边框的小方框把
    #      箭头框起来，跟左边自己画的细边框对不上，多出一圈黑边。
    #   2) 给 ::drop-down 加 border:none 之后，箭头本身在不同 Qt/
    #      Windows 版本组合下经常直接消失——只要 QComboBox 任意一处
    #      被自定义样式表覆盖过，原生主题箭头绘制就变得不可靠，不是
    #      靠调 QSS 参数能稳定控制住的。
    # 干脆不再依赖 Qt 原生绘制这个箭头：自己手绘一个小箭头图标，通过
    # QComboBox::down-arrow 的 image 属性显式指定，这样箭头肯定会
    # 显示、不再受原生主题渲染差异影响。

    # ========================= 尺寸设置区 =========================
    # 放大镜（左侧，点击触发搜索）
    SEARCH_HITBOX_SIZE = height - 1     # 点击热区大小（正方形）
    SEARCH_ICON_SIZE = round(SEARCH_HITBOX_SIZE * 0.55)  # 放大镜图案本身大小

    # 正则开关".*"（输入框右侧）
    REGEX_HITBOX_SIZE = height - 1      # 点击热区大小（正方形）
    REGEX_HIGHLIGHT_SIZE = height - 12   # 选中态背景高亮色块大小（居中）
    REGEX_GLYPH_SIZE = round(REGEX_HIGHLIGHT_SIZE * 0.8)  # ".*"图案本身大小（居中）

    # 清空按钮"×"（正则开关左边）
    CLEAR_HITBOX_SIZE = height - 1      # 点击/悬停热区大小（正方形）
    CLEAR_HIGHLIGHT_SIZE = height - 14   # 悬停态圆形背景大小（居中，可比热区小）
    CLEAR_GLYPH_SIZE = round(CLEAR_HITBOX_SIZE * 0.55)  # "×"字符本身大小

    # "添加到书签"（清空×左边，只有 with_bookmark_button=True 才会用到）
    BOOKMARK_HITBOX_SIZE = bookmark_icon_hitbox_size(height)  # 点击热区大小（正方形）
    BOOKMARK_HIGHLIGHT_SIZE = height - 14  # 悬停态背景高亮色块大小（居中，可比热区小）
    BOOKMARK_GLYPH_SIZE = round(BOOKMARK_HITBOX_SIZE * 0.5)  # 图案本身大小

    # 下拉箭头（最右侧，QComboBox 原生 ::drop-down 区域）
    ARROW_HITBOX_SIZE = 20              # 点击热区宽度（下面 QSS 里 width 用同一个数）
    ARROW_ICON_SIZE = 12                # 箭头图案本身大小
    # ================================================================

    arrow_icon_path = _get_down_arrow_icon_path(color=icon_color, size=ARROW_ICON_SIZE)
    DROP_DOWN_WIDTH = ARROW_HITBOX_SIZE
    combo.setStyleSheet(f"""
        QComboBox {{
            font-size: {font_size}px;
            border: 1px solid #d5d9de;
            border-radius: 0px;
            padding-left: 4px;
            background: white;
        }}
        QComboBox:hover {{
            border: 1px solid #b9c0c9;
        }}
        QComboBox:focus, QComboBox:on {{
            border: 1px solid #4a90d9;
        }}
        QComboBox::drop-down {{
            border: none;
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: {DROP_DOWN_WIDTH}px;
        }}
        QComboBox::down-arrow {{
            image: url({arrow_icon_path});
            width: {ARROW_ICON_SIZE}px;
            height: {ARROW_ICON_SIZE}px;
        }}
    """)
    combo.lineEdit().setPlaceholderText(placeholder)
    # 原生清空按钮/原生前导图标都不用了——原生这套是 Qt 自己按内部逻辑
    # 贴在固定位置画的，没法指定谁跟谁的先后顺序、也没法单独控制点击
    # 热区大小。下面三个图标（放大镜/清空×/正则.*）全部改成自己精确
    # 摆放的叠加控件，顺序、间距、尺寸全部自己说了算。
    combo.lineEdit().setClearButtonEnabled(False)
    line_edit = combo.lineEdit()

    # ---------- 左侧：放大镜（可点击触发搜索） ----------
    search_icon = make_search_icon(
        color=icon_color, size=SEARCH_HITBOX_SIZE, glyph_size=SEARCH_ICON_SIZE
    )
    search_btn = QToolButton(combo)
    search_btn.setCursor(Qt.PointingHandCursor)
    search_btn.setIcon(search_icon)
    search_btn.setIconSize(QSize(SEARCH_HITBOX_SIZE, SEARCH_HITBOX_SIZE))
    search_btn.setStyleSheet(
        "QToolButton { border: none; background: transparent; padding: 0px; }"
    )
    search_btn.setFixedSize(SEARCH_HITBOX_SIZE, SEARCH_HITBOX_SIZE)
    search_btn.setToolTip("搜索")
    # 兼容以前 QAction 提供的接口：外部代码（layout.py/search_manager.py）
    # 用的是 search_action.triggered.connect(...)，QToolButton 天生没有
    # .triggered 这个信号（那是 QAction 专属的），这里直接把 .triggered
    # 取别名指向 .clicked——两者是同一个信号对象，外部代码原样连接、
    # 原样触发，不用改一行。setEnabled()/setToolTip() 本来就是 QWidget
    # 通用方法，QToolButton 天然就有，同样不用额外处理。
    search_btn.triggered = search_btn.clicked
    search_action = search_btn

    # ---------- 右侧：正则开关".*" ----------
    regex_icon_off = make_regex_icon(
        color=icon_color, canvas_size=REGEX_HITBOX_SIZE,
        highlight_size=REGEX_HIGHLIGHT_SIZE, glyph_size=REGEX_GLYPH_SIZE, active=False
    )
    regex_icon_on = make_regex_icon(
        color=icon_color, canvas_size=REGEX_HITBOX_SIZE,
        highlight_size=REGEX_HIGHLIGHT_SIZE, glyph_size=REGEX_GLYPH_SIZE, active=True
    )
    regex_btn = QToolButton(combo)
    regex_btn.setCheckable(True)
    regex_btn.setCursor(Qt.PointingHandCursor)
    regex_btn.setIcon(regex_icon_off)
    regex_btn.setIconSize(QSize(REGEX_HITBOX_SIZE, REGEX_HITBOX_SIZE))
    regex_btn.setToolTip("按正则表达式解析（Python re 语法）")
    # 高亮底色已经画进 make_regex_icon() 生成的图标本身了（激活态背景
    # 是图标像素的一部分，大小由 REGEX_HIGHLIGHT_SIZE 单独控制），按钮
    # 自己不需要再叠一层背景色，保持透明。
    regex_btn.setStyleSheet(
        "QToolButton { border: none; background: transparent; padding: 0px; }"
    )
    regex_btn.setFixedSize(REGEX_HITBOX_SIZE, REGEX_HITBOX_SIZE)

    def _on_regex_toggled(checked):
        regex_btn.setIcon(regex_icon_on if checked else regex_icon_off)
        regex_btn.setToolTip(
            "正则模式已开启：整段输入按一条正则表达式解析，不再按空格拆成多个关键词。"
            "再点一下关闭"
            if checked else
            "按正则表达式解析（Python re 语法）"
        )
    regex_btn.toggled.connect(_on_regex_toggled)
    # QToolButton 本身就是货真价实的 QObject/QAbstractButton，天生自带
    # blockSignals()/setChecked()/isChecked()/toggled，跟以前 QAction
    # 提供的接口完全兼容，外部代码不用改一行。
    regex_action = regex_btn

    # ---------- 右侧：清空按钮"×"（正则开关左边） ----------
    # 悬停态圆形背景用 QSS 的 margin 技巧跟点击热区解耦：margin 越大，
    # Qt 实际画 border/background 的范围就越往内缩，但鼠标响应范围
    # 还是 setFixedSize() 给的整个按钮大小，不受 margin 影响——这样
    # CLEAR_HIGHLIGHT_SIZE 才能做到比 CLEAR_HITBOX_SIZE 小（留出看得见
    # 的空隙）也没问题，两者互不牵连。
    clear_margin = max(0, (CLEAR_HITBOX_SIZE - CLEAR_HIGHLIGHT_SIZE) // 2)
    clear_btn = QToolButton(combo)
    clear_btn.setCursor(Qt.PointingHandCursor)
    clear_btn.setText("×")
    clear_btn.setStyleSheet(f"""
        QToolButton {{
            border: none;
            background: transparent;
            color: {icon_color};
            font-size: {CLEAR_GLYPH_SIZE}px;
            font-weight: bold;
            margin: {clear_margin}px;
            border-radius: {max(0, CLEAR_HIGHLIGHT_SIZE // 2 - clear_margin)}px;
            padding: 0px;
        }}
        QToolButton:hover {{
            color: #c0392b;
            background-color: #f3d9d5;
        }}
    """)
    clear_btn.setFixedSize(CLEAR_HITBOX_SIZE, CLEAR_HITBOX_SIZE)
    clear_btn.setVisible(False)  # 一开始没有文字，先隐藏，不占视觉空间

    def _update_clear_btn_visibility(text):
        clear_btn.setVisible(bool(text))

    clear_btn.clicked.connect(line_edit.clear)
    line_edit.textChanged.connect(_update_clear_btn_visibility)
    _update_clear_btn_visibility(line_edit.text())

    # ---------- （可选）清空×左边：添加到书签 ----------
    bookmark_action = None
    if with_bookmark_button:
        bookmark_icon = make_bookmark_icon(
            color=icon_color, canvas_size=BOOKMARK_HITBOX_SIZE, glyph_size=BOOKMARK_GLYPH_SIZE
        )
        bookmark_btn = QToolButton(combo)
        bookmark_btn.setCursor(Qt.PointingHandCursor)
        bookmark_btn.setIcon(bookmark_icon)
        bookmark_btn.setIconSize(QSize(BOOKMARK_HITBOX_SIZE, BOOKMARK_HITBOX_SIZE))
        bookmark_btn.setToolTip("把当前的文件名/内容搜索条件收藏为书签")
        # 悬停态背景高亮色块大小跟点击热区解耦，用跟清空按钮 clear_btn
        # 一样的 margin 技巧：margin 越大，Qt 实际画 border/background 的
        # 范围就越往内缩，但鼠标响应范围还是 setFixedSize() 给的整个
        # 按钮大小，不受 margin 影响——这样 BOOKMARK_HIGHLIGHT_SIZE 才能
        # 做到比 BOOKMARK_HITBOX_SIZE 小（留出看得见的空隙）也没问题。
        bookmark_margin = max(0, (BOOKMARK_HITBOX_SIZE - BOOKMARK_HIGHLIGHT_SIZE) // 2)
        bookmark_btn.setStyleSheet(f"""
            QToolButton {{
                border: none;
                background: transparent;
                padding: 0px;
                margin: {bookmark_margin}px;
                border-radius: {max(0, BOOKMARK_HIGHLIGHT_SIZE // 2 - bookmark_margin)}px;
            }}
            QToolButton:hover {{
                background-color: #eef2f6;
            }}
        """)
        bookmark_btn.setFixedSize(BOOKMARK_HITBOX_SIZE, BOOKMARK_HITBOX_SIZE)
        # 跟左侧放大镜 search_btn 一样，补一个 .triggered 别名，外部代码
        # 统一用 .triggered.connect(...) 就行，不用区分这是 QToolButton
        # 还是以前的 QAction。
        bookmark_btn.triggered = bookmark_btn.clicked
        bookmark_action = bookmark_btn

    # ---------- 统一摆放位置 ----------
    # 从左到右固定顺序：[放大镜] [输入文字] [清空×] [书签，可选]
    # [正则.*] [下拉箭头]，全部按 combo 自身当前宽度换算，位置固定、
    # 不随输入内容动态挪动——之前 V6.4 版本让图标位置跟着"清空按钮
    # 出不出现"动态计算，结果输入文字一变化图标就跟着跳，这里直接
    # 吸取教训，位置从头到尾焊死，清空按钮不需要时是真正隐藏（不占
    # 视觉空间），不是"位置空出来"。
    gap = 3
    left_margin = gap + SEARCH_HITBOX_SIZE + gap
    right_edge_reserved = DROP_DOWN_WIDTH + gap
    bookmark_reserved = (gap + BOOKMARK_HITBOX_SIZE) if with_bookmark_button else 0
    right_margin = (
        DROP_DOWN_WIDTH + gap + REGEX_HITBOX_SIZE + gap
        + bookmark_reserved + CLEAR_HITBOX_SIZE + gap
    )
    line_edit.setTextMargins(left_margin, 0, right_margin, 0)

    def _reposition_overlay_btns():
        w = combo.width()
        search_btn.move(gap, (height - SEARCH_HITBOX_SIZE) // 2)
        regex_x = w - right_edge_reserved - REGEX_HITBOX_SIZE
        regex_btn.move(max(0, regex_x), (height - REGEX_HITBOX_SIZE) // 2)
        next_x = regex_x
        if with_bookmark_button:
            bookmark_x = next_x - gap - BOOKMARK_HITBOX_SIZE
            bookmark_btn.move(max(0, bookmark_x), (height - BOOKMARK_HITBOX_SIZE) // 2)
            next_x = bookmark_x
        clear_x = next_x - gap - CLEAR_HITBOX_SIZE
        clear_btn.move(max(0, clear_x), (height - CLEAR_HITBOX_SIZE) // 2)

    class _OverlayBtnResizeFilter(QObject):
        """combo 尺寸变化时（比如中间那条分隔线被拖动、窗口被拉伸）
        重新摆一次几个按钮的位置，保持贴在各自的固定槽位里。"""
        def eventFilter(self_, obj, event):
            if event.type() == QEvent.Resize:
                _reposition_overlay_btns()
            return False

    resize_filter = _OverlayBtnResizeFilter(combo)
    combo.installEventFilter(resize_filter)
    combo._overlay_btn_resize_filter = resize_filter  # 防止被垃圾回收
    _reposition_overlay_btns()
    search_btn.raise_()
    regex_btn.raise_()
    clear_btn.raise_()
    if with_bookmark_button:
        bookmark_btn.raise_()

    return combo, search_action, regex_action, bookmark_action


def build_icon_search_combo(placeholder, icon_color, icon_bg, border_color,
                             height=34, icon_size=16, font_size=13, radius=0):
    """搭一个"左边图标块 + 右边可编辑下拉框"合并成一个整体外框的搜索框，
    图标块贴满输入框整个高度、紧贴左边框，中间一条竖分隔线，风格对齐
    参考设计图里那种搜索框。

    注意 icon_size 跟 height 是两回事：icon_size 只控制放大镜图案本身
    画多大（默认 16px，居中放在图标块里，不会因为框变高就跟着变大），
    height 才是图标块贴满的那个整框高度。

    下拉箭头用的是 Qt 原生的 QComboBox::drop-down/down-arrow，没有另外
    自己画。QComboBox 本身设了 border:none/background:transparent（是
    为了跟图标块拼成一整条无缝外框，不是这两条属性本身有问题），但完全
    不去碰 QComboBox::drop-down / ::down-arrow 这两个子控件——只要不单独
    覆盖这两个子控件，原生下拉箭头和原生的开合行为就不受影响。

    返回 (container, combo, icon_label)：
    - container：要放进布局里的最外层控件（带描边、白底，圆角默认 0）
    - combo：真正的 QComboBox，其余逻辑（取值、下拉历史、回车搜索等）
      跟以前完全一样，直接当成普通 QComboBox 用即可
    - icon_label：左侧图标区域，点击会发出 clicked 信号；也可以直接
      setEnabled()/setToolTip() 来配合"搜索中..."这种状态切换
    """

    container = QWidget()
    container.setObjectName("iconSearchBox")
    container.setAttribute(Qt.WA_StyledBackground, True)
    container.setFixedHeight(height)

    normal_qss = f"""
        #iconSearchBox {{
            background: white;
            border: 1px solid {border_color};
            border-radius: {radius}px;
        }}
    """
    focus_qss = f"""
        #iconSearchBox {{
            background: white;
            border: 1px solid #9aa0a6;
            border-radius: {radius}px;
        }}
    """
    container.setStyleSheet(normal_qss)

    row = QHBoxLayout(container)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)

    icon = make_search_icon(color=icon_color, size=icon_size)
    def _hover_shade(color_hex):
        """根据底色自动算一个悬停时用的深浅色：浅色底（比如白色）悬停
        时略微加深，深色底（比如灰色徽章）悬停时略微变浅——跟其它按钮
        划过去会变色的反馈保持一致，不能只有鼠标手型没有颜色变化。"""
        c = QColor(color_hex)
        if c.lightness() > 200:
            return c.darker(106).name()
        return c.lighter(115).name()

    icon_label = ClickableIconLabel(icon, icon_size)
    icon_label.setFixedSize(height, height)
    icon_label.setAttribute(Qt.WA_StyledBackground, True)
    icon_label.setObjectName("iconSearchLeftIcon")
    icon_hover_bg = _hover_shade(icon_bg)
    icon_label.setStyleSheet(f"""
        #iconSearchLeftIcon {{
            background: {icon_bg};
            border-top-left-radius: {radius}px;
            border-bottom-left-radius: {radius}px;
            border-right: 1px solid {border_color};
        }}
        #iconSearchLeftIcon:hover {{
            background: {icon_hover_bg};
        }}
    """)

    combo = QComboBox()
    combo.setEditable(True)
    combo.setFixedHeight(height)  # 撑满整条外框的高度，不然默认只有自然高度（比如19px），
                                   # 下拉箭头的可点击区域会比看起来的输入框矮一截
    # 只给最外层的 QComboBox 设边框/背景/字号，完全不碰
    # QComboBox::drop-down / ::down-arrow 这两个子控件——只要不写这两条
    # 规则，原生下拉箭头就不受影响，照常显示、照常有正确的点一下开、
    # 再点一下收的开合行为，不需要任何额外代码。
    combo.setStyleSheet(f"""
        QComboBox {{
            border: none;
            background: transparent;
            font-size: {font_size}px;
            padding-left: 6px;
        }}
    """)
    combo.lineEdit().setPlaceholderText(placeholder)
    combo.lineEdit().setClearButtonEnabled(True)
    combo.lineEdit().setStyleSheet("border: none; background: transparent;")

    focus_watcher = _FocusBorderWatcher(container, normal_qss, focus_qss)
    combo.lineEdit().installEventFilter(focus_watcher)
    container._focus_watcher = focus_watcher  # 防止事件过滤器对象被提前垃圾回收，导致过滤器失效

    row.addWidget(icon_label)
    row.addWidget(combo, 1)

    return container, combo, icon_label


class NumericSortItem(QTableWidgetItem):
    def __lt__(self, other):
        try:
            return int(self.text()) < int(other.text())
        except ValueError:
            return super().__lt__(other)

class CustomSortItem(QTableWidgetItem):
    def __lt__(self, other):
        my_data = self.data(Qt.UserRole)
        other_data = other.data(Qt.UserRole)
        if my_data is not None and other_data is not None:
            return my_data < other_data
        return super().__lt__(other)

class FrozenColumnTableWidget(QTableWidget):
    """搜索结果表格要用这个类，不能用普通 QTableWidget——就多重写了一个
    scrollTo()。

    原因：create_frozen_first_column() 会让第0列（文件名）继续在这个
    表格自己身上保留真实宽度（不再隐藏），这样拖宽"文件名"表头才能像
    Excel/资源管理器那样把右边的列推挤过去，而不是盖住它们。但只要
    第0列在表格自己身上还"存在"，Qt 就有一个内置行为：当前选中的格子
    变了、如果这个格子在当前横向滚动位置下不可见，会自动把表格横向
    滚回能看到这个格子的位置。用户在冻结列（叠加视图）上点选某一行时，
    这个格子在表格自己看来就是"第0列"，如果表格之前被手动横向拖动过、
    第0列已经滚出可见范围，Qt 就会不由分说地把横向滚动条拽回最左边，
    把用户手动拖动过的位置全部复位掉。

    这里只需要把这一个自动滚动行为，对"目标格子在第0列"这一种情况
    专门拦掉，其余列该怎么自动滚动还是怎么滚动，不受影响。"""

    def scrollTo(self, index, hint=QAbstractItemView.EnsureVisible):
        if index.isValid() and index.column() == 0:
            return
        super().scrollTo(index, hint)


class _FrozenColumnSync(QObject):
    """给 create_frozen_first_column() 用的辅助对象：把主表格第0列的
    列宽和叠加视图（frozen_view）自己第0列的列宽双向同步起来——不管是
    拖主表格底下那个（现在被 frozen_view 盖住摸不到，理论上不会发生）
    还是拖 frozen_view 自己的表头，两边宽度都要保持一致，并且同步维护
    frozen_view 的位置和尺寸，让它严丝合缝地贴在主表格第0列的位置上，
    随主表格垂直滚动，但不随横向滚动移动。这是 Qt 官方"Frozen Column
    Example"的标准做法，不是真的把第0列从主表格拆出来，而是另外叠
    一份只显示第0列的视图，跟主表格共享同一份数据模型和选中状态。

    重要：这次没有再把第0列在主表格自己身上隐藏掉（之前那版隐藏过，
    副作用是拖宽"文件名"表头时只会让叠加视图自己变宽、盖住旁边的列，
    主表格并不知道第0列变宽了，右边的列不会被推挤——现在改成两边宽度
    双向同步，主表格的第0列始终跟叠加视图一样宽，右边的列自然会跟着
    被正常的列布局机制推挤过去，效果就是"贴合、推着走"而不是"盖上去"）。
    "点击后横向滚动被复位"这个问题现在改成靠 FrozenColumnTableWidget
    重写 scrollTo() 来解决，不再依赖隐藏列这个手段。"""

    def __init__(self, table, frozen_view):
        super().__init__(table)
        self.table = table
        self.frozen_view = frozen_view
        self._syncing_width = False  # 双向同步的重入保护
        table.installEventFilter(self)
        table.horizontalHeader().sectionResized.connect(self._on_table_col0_resized)
        frozen_view.horizontalHeader().sectionResized.connect(self._on_frozen_col0_resized)
        # frozen_view 自己的滚动条整个隐藏掉（ScrollBarAlwaysOff），
        # 纯靠跟主表格双向同步 value 来滚动内容——这样视觉上第0列的
        # 内容照样会跟着上下滚动，只是没有独立的横向滚动条，也不会
        # 跟着主表格的横向滚动条一起动。
        table.verticalScrollBar().valueChanged.connect(frozen_view.verticalScrollBar().setValue)
        frozen_view.verticalScrollBar().valueChanged.connect(table.verticalScrollBar().setValue)
        self.update_geometry()

    def eventFilter(self, obj, event):
        if obj is self.table and event.type() in (QEvent.Resize, QEvent.Show):
            self.update_geometry()
        return False

    def _on_table_col0_resized(self, logical_index, old_size, new_size):
        if logical_index != 0 or self._syncing_width:
            return
        self._syncing_width = True
        self.frozen_view.setColumnWidth(0, new_size)
        self._syncing_width = False
        self.update_geometry()

    def _on_frozen_col0_resized(self, logical_index, old_size, new_size):
        if logical_index != 0 or self._syncing_width:
            return
        self._syncing_width = True
        # 这一步会让主表格的第0列也跟着变宽——主表格自己身上第1列往后
        # 都是 Interactive 缩放模式（不是 Stretch），列宽一变会自然把
        # 右边的列整体往右推，不用额外手写"推挤"逻辑，这是 QHeaderView
        # 本来就有的行为，只是之前第0列被隐藏掉了，没机会触发它。
        self.table.setColumnWidth(0, new_size)
        self._syncing_width = False
        self.update_geometry()

    def update_geometry(self):
        table = self.table
        frame = table.frameWidth()
        self.frozen_view.setGeometry(
            frame,
            frame,
            self.frozen_view.columnWidth(0),
            table.viewport().height() + table.horizontalHeader().height()
        )


def create_frozen_first_column(table):
    """给 table（必须是 FrozenColumnTableWidget 实例）的第0列（文件名）
    做"冻结列"效果：横向拖动底部滚动条时，第0列固定在最左边不动，其余
    列该怎么滚怎么滚；拖宽这一列的表头，右边的列会被推挤，不是被盖住。

    做法：另外叠一个只显示第0列的 QTableView，盖在主表格第0列的位置上，
    跟主表格共享同一份数据模型（model()）和同一份选中状态
    （selectionModel()）——这样不管用户是点了叠加视图还是主表格，
    选中的都是同一行，行选中高亮、itemSelectionChanged 这些信号也
    照常能用，不用额外同步。

    调用方需要注意：第0列的鼠标事件（双击打开、右键菜单、点击进入
    编辑、点表头排序等）现在实际上被这个叠加视图接住了，主表格本身
    收不到——原来接在 table.cellDoubleClicked / table.customContextMenuRequested
    上的交互，要另外再给这个返回的视图接一份（它是 QTableView，双击
    信号是 doubleClicked(index)，不是 QTableWidget 的
    cellDoubleClicked(row, col)；右键菜单同理，位置坐标是相对于
    frozen_view 自己的 viewport，传给主表格原来的右键菜单处理函数
    之前要换算坐标系）。
    """
    frozen_view = QTableView(table)
    frozen_view.setModel(table.model())
    frozen_view.setSelectionModel(table.selectionModel())
    frozen_view.setFocusPolicy(Qt.NoFocus)
    # 不画自己的外框——主表格本身已经有一圈边框了，frozen_view 叠在
    # 上面如果还带一圈默认的 QFrame 边框，会在文件名列和文件路径列
    # 交界的地方多出一条突兀的深色竖线，跟其余列的分隔线粗细/颜色都
    # 对不上。
    frozen_view.setFrameShape(QFrame.NoFrame)

    for col in range(1, table.columnCount()):
        frozen_view.setColumnHidden(col, True)

    frozen_view.verticalHeader().setVisible(False)
    frozen_view.horizontalHeader().setFixedHeight(table.horizontalHeader().height())
    frozen_view.horizontalHeader().setHighlightSections(False)
    frozen_view.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
    # 表头允许点击——用来支持"点文件名表头排序"，见下面 sectionClicked
    # 那一段；表头文字（"文件名"）本身是从共享的模型里自动读出来的，
    # 不用重复设置。
    frozen_view.horizontalHeader().setSectionsClickable(True)
    # 初始不显示箭头——只有真正点过"文件名"表头排序之后才显示，
    # 逻辑见下面 _on_frozen_header_clicked / _on_other_column_sorted。
    frozen_view.horizontalHeader().setSortIndicatorShown(False)
    frozen_view.setShowGrid(table.showGrid())
    frozen_view.setAlternatingRowColors(table.alternatingRowColors())
    # 单独给一份 QTableView 专用的样式表，不能直接照抄主表格的——主
    # 表格那份样式表选择器写的是 `QTableWidget`/`QTableWidget::item`，
    # frozen_view 是 QTableView 类型，选择器按类型精确匹配，套主表格
    # 那份根本不会命中，之前就是这里漏了，导致 frozen_view 用的是系统
    # 默认选中色（刺眼的深蓝），跟主表格柔和浅蓝对不上。
    frozen_view.setStyleSheet(
        "QTableView {"
        "    border: none;"
        "    alternate-background-color: #f4f6f9;"
        "    background-color: #ffffff;"
        "    gridline-color: #e3e6ea;"
        "}"
        "QTableView::item:selected {"
        "    background-color: #cfe4ff;"
        "    color: black;"
        "}"
    )
    frozen_view.setSelectionBehavior(table.selectionBehavior())
    frozen_view.setSelectionMode(table.selectionMode())
    frozen_view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    frozen_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    frozen_view.setColumnWidth(0, table.columnWidth(0))
    # 单击选中的行再点一下（不是双击）才进入这一列的编辑态——Qt 原生
    # 就支持这种"点已选中项才编辑"的触发方式，不用自己再手搓一套
    # 点击时序判断。真正的双击（Qt 自己能识别出来的那种）不会触发这个，
    # 交给调用方另外接的 doubleClicked 信号去处理"打开文件"。
    frozen_view.setEditTriggers(QAbstractItemView.SelectedClicked)
    frozen_view.show()
    # 明确提到最上层——frozen_view 是在主表格自己的 viewport 创建好之后
    # 才追加的子控件，正常情况下后创建的子控件本来就会叠在上层，这里
    # 显式调用一次纯粹是为了不依赖"创建顺序决定层叠顺序"这个隐含假设，
    # 万一以后有什么改动导致假设不成立，这一行能兜底。
    frozen_view.raise_()

    # 升/降序切换靠这个存在 table 身上的显式标记来判断"上一次点成了
    # 升序还是降序"，不再反过来读 Qt 表头自己的 sortIndicatorOrder()。
    # 原因：table 身上还挂着 setSortingEnabled(True)，每次搜索完成后
    # search_manager 都会先关再开一次 sortingEnabled——Qt 在重新打开
    # 时会用表头当前记的方向自动把新数据重排一遍，这个"表头记的方向"
    # 是 Qt 内部维护的状态，不受这里单独控制，容易跟这段代码以为的
    # 状态对不上，表现出来就是点了几次表头之后卡在降序切不回升序。
    # 改成自己存一份就没有这个问题，Qt 表头的 sortIndicator 只用来
    # 显示箭头图标，不再参与判断。
    table._filename_sort_order = None  # None：还没手动排过序；之后是 Qt.AscendingOrder / Qt.DescendingOrder

    def _on_frozen_header_clicked(logical_index):
        # 只处理第0列（文件名）；理论上 frozen_view 只剩这一列没隐藏，
        # 点别的地方点不到表头，这个判断是防御性的。
        if logical_index != 0:
            return
        # 再点一下同一列表头，升序/降序切换；第一次点（还没记录过方向）
        # 默认从升序开始。
        if table._filename_sort_order == Qt.AscendingOrder:
            new_order = Qt.DescendingOrder
        else:
            new_order = Qt.AscendingOrder
        table._filename_sort_order = new_order
        # table.sortItems() 是 QTableWidget 原生的排序 API，两边视图
        # 共享同一份数据模型，排完序 frozen_view 这边的行序自然跟着变，
        # 不用额外手动同步。
        table.sortItems(0, new_order)
        # 这两行只是让箭头图标显示对，不参与下次点击时的方向判断。
        # 点了文件名表头，箭头才显示出来；点别的列表头排序时会被下面
        # _on_other_column_sorted 收回去。
        frozen_view.horizontalHeader().setSortIndicatorShown(True)
        frozen_view.horizontalHeader().setSortIndicator(0, new_order)
        table.horizontalHeader().setSortIndicator(0, new_order)

    frozen_view.horizontalHeader().sectionClicked.connect(_on_frozen_header_clicked)

    def _on_other_column_sorted(logical_index, order):
        # 主表格真实表头（"文件路径"/"创建日期"/"修改日期"/"大小"这些
        # 列）被点了排序时会触发这个信号；只要排序的不是第0列（文件名），
        # 就把 frozen_view 自己独立那份箭头收起来——否则它是单独的表头
        # 对象，Qt 不会替我们自动同步，箭头会一直挂在"文件名"上不消失，
        # 看着像文件名和别的列同时在排序。同时清掉文件名这边记的排序
        # 方向，下次再点文件名表头时从升序重新开始，跟点其它列排序时
        # "默认先升序"的体验保持一致。
        if logical_index == 0:
            return
        frozen_view.horizontalHeader().setSortIndicatorShown(False)
        table._filename_sort_order = None

    table.horizontalHeader().sortIndicatorChanged.connect(_on_other_column_sorted)

    _FrozenColumnSync(table, frozen_view)
    return frozen_view


class FilenameHighlightDelegate(QStyledItemDelegate):
    """
    文件名列专用委托：把当前"文件名关键字"在文件名文字里高亮标出来，
    让用户一眼看出这一行是不是因为文件名命中才出现在结果里
    （跟右侧内容预览区的关键字高亮呼应，风格保持一致）。

    没有关键字要高亮时（比如这次只用了内容关键字搜索），直接退回父类
    默认绘制，不额外产生任何开销——只有真正命中时才走 QTextDocument
    这条稍重一点的富文本渲染路径。
    """
    HIGHLIGHT_BG = "#fff2a8"  # 跟内容预览区高亮色保持一致的暖黄色

    def __init__(self, parent=None):
        super().__init__(parent)
        self._keywords = []
        self._is_regex = False

    def set_keywords(self, keywords, is_regex=False):
        """更新当前要高亮的关键字列表。
        is_regex=True 时，keywords 应该只有一个元素（一条完整的正则
        表达式，search_manager.py 在正则模式下不会拆词），高亮时直接
        按这条正则本身去匹配，不再 re.escape 成字面量——escape 会把
        正则的元字符（比如 \\d、括号分组）当成普通字符对待，那样高亮
        永远匹配不上任何东西。
        非正则模式跟原来一样：按长度从长到短排序，多个关键字互相包含时
        （比如"图纸"和"图纸1"），优先整体匹配更长的那个，避免长词被
        短词提前切碎，导致高亮片段断裂、显示不完整。"""
        if is_regex:
            self._keywords = [k.strip() for k in keywords if k and k.strip()]
        else:
            self._keywords = sorted({k.strip() for k in keywords if k and k.strip()},
                                     key=len, reverse=True)
        self._is_regex = is_regex

    def _build_html(self, text):
        """把命中的关键字片段包一层高亮 <span>，其余部分原样转义后拼回去。
        大小写不敏感匹配；用一个合并正则一次性分割整段文字，不逐个关键字
        单独 replace——避免多个关键字互相重叠/相邻时，重复替换出嵌套标签
        或者把已经高亮过的片段又匹配一次这类问题。"""
        if not self._keywords or not text:
            return None
        if self._is_regex:
            # 正则模式下 self._keywords 只有一个元素，就是完整的正则
            # 表达式本身。
            pattern = self._keywords[0]
        else:
            pattern = "|".join(re.escape(k) for k in self._keywords)
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return None
        # 用 finditer 而不是 split 来重建高亮片段：split 依赖"外层包一层
        # 捕获组、按奇偶下标区分普通文字和命中片段"这个技巧，但正则模式
        # 下用户自己写的表达式很可能带括号分组（比如 "(abc|def)"），会
        # 把内部分组也一起拆出来，打乱奇偶下标的假设。finditer 只关心
        # 每次匹配的起止位置，不管表达式内部有没有分组，两种模式都稳妥。
        html_parts = []
        last_end = 0
        matched_any = False
        for m in regex.finditer(text):
            if m.end() == m.start():
                continue  # 零宽匹配（比如全是零宽断言的表达式）跳过，没有实际文字可以高亮
            if m.start() < last_end:
                continue  # 跟上一个命中重叠，跳过，避免高亮片段互相交叉错位
            matched_any = True
            html_parts.append(html.escape(text[last_end:m.start()]))
            html_parts.append(
                f'<span style="background-color:{self.HIGHLIGHT_BG};">'
                f'{html.escape(text[m.start():m.end()])}</span>'
            )
            last_end = m.end()
        if not matched_any:
            return None  # 没有命中任何关键字，没必要走富文本渲染这条路
        html_parts.append(html.escape(text[last_end:]))
        return "".join(html_parts)

    def paint(self, painter, option, index):
        text = index.data(Qt.DisplayRole)
        if not text:
            super().paint(painter, option, index)
            return

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget else QApplication.style()
        text_rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, opt.widget)

        # 文字太长、超出格子宽度时用省略号截断——这里改成"先按可用宽度
        # 截断成带省略号的短文本，再对这段短文本做高亮"，而不是像之前
        # 那样先高亮全文、超宽部分再硬裁剪掉（那样虽然避免了"高亮片段被
        # 从中间切断"的问题，但代价是压根没有省略号，跟其余走原生渲染
        # 的列比起来不一致——这正是"内容搜索出来的结果有省略号、文件名
        # 搜索出来的结果没有"这个奇怪现象的根源：内容搜索命中的行，
        # 文件名本身通常没有关键字可高亮，直接落到下面 super().paint()
        # 那条原生渲染路径，天然带省略号；文件名搜索命中的行，文件名里
        # 有关键字要高亮，走的是这段自定义富文本渲染，之前这条路径根本
        # 没做省略号处理）。现在先截断再高亮，顺序反过来之后，高亮永远
        # 作用在已经截断完成的文本上，不会有跨越省略号的怪异片段，两条
        # 路径的省略号效果也就统一了。
        fm = QFontMetrics(opt.font)
        elided_text = fm.elidedText(text, Qt.ElideRight, text_rect.width())

        html_text = self._build_html(elided_text)
        if not html_text:
            # 这一段截断后的文本里没有命中任何关键字（比如这一行是靠
            # 别的搜索条件命中的，文件名本身没有关键字）——退回原生
            # 渲染就够了，原生本来就会正确处理省略号。
            super().paint(painter, option, index)
            return

        painter.save()

        # 先按正常样式画完选中态/斑马纹底色等基础外观，只是不画文字部分——
        # 文字接下来单独用 QTextDocument 画一份富文本上去，两者叠在一起，
        # 视觉上跟其余没有高亮的普通列完全一致。
        opt.text = ""
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        doc.setDocumentMargin(0)
        # 关掉自动换行：默认 QTextDocument 一旦 setTextWidth 小于内容宽度
        # 就会自动折成多行，普通 QTableWidgetItem 是单行+右侧省略号截断，
        # 这里强制单行，跟其余没走富文本渲染的列观感保持一致。
        no_wrap_option = doc.defaultTextOption()
        no_wrap_option.setWrapMode(QTextOption.NoWrap)
        doc.setDefaultTextOption(no_wrap_option)
        doc.setHtml(html_text)
        # 文字已经在上面按可用宽度截断过了，这里让文档按内容实际宽度
        # 自然排版成一行即可，不会再超出格子宽度，理论上不需要裁剪，
        # 但还是留一道裁剪兜底（比如极端窄的列宽下取整误差之类）。
        doc.setTextWidth(doc.idealWidth())
        painter.setClipRect(text_rect)

        # 选中行时文字颜色要跟主表格 QSS 里 item:selected 定的黑字保持一致；
        # 不能用系统调色板的 HighlightedText——那通常是白色，跟这里手动画的
        # 高亮关键字黄色底叠在一起，会又刺眼又看不清，跟其余没走富文本
        # 渲染的列（走 QSS，选中后是黑字）也对不上。
        ctx = QAbstractTextDocumentLayout.PaintContext()
        if opt.state & QStyle.State_Selected:
            ctx.palette.setColor(QPalette.Text, QColor("black"))
        else:
            ctx.palette.setColor(QPalette.Text, opt.palette.color(QPalette.Text))

        # 单行文字垂直居中：文档实际渲染高度和格子高度之间的差值均分到上下
        painter.translate(
            text_rect.left(),
            text_rect.top() + (text_rect.height() - doc.size().height()) / 2
        )
        doc.documentLayout().draw(painter, ctx)

        painter.restore()

    def createEditor(self, parent, option, index):
        # 只创建标准的 QLineEdit 编辑器，不在这里设置选区——这时候
        # editor 里还是空的，Qt 要等 createEditor 返回之后才会调用
        # setEditorData() 真正把文字填进去，这里设选区会被那一步覆盖掉，
        # 等于白设。选区逻辑放到下面的 setEditorData() 里做。
        return super().createEditor(parent, option, index)

    def setEditorData(self, editor, index):
        """双击/单击已选中项进入编辑态时，Qt 默认会把整个文件名（含扩展名）
        全部选中——但正常改名极少会去动扩展名，全选中扩展名反而容易手滑
        把它删了或者改错。这里把编辑框的初始选区收窄到"主文件名"部分
        （最后一个"."之前），扩展名留着但不选中，跟 Windows 资源管理器
        改文件名的选取范围保持一致。

        用 QTimer.singleShot(0, ...) 把设置选区这一步推迟到下一个事件
        循环再执行，不是在这里直接调用——Qt 自己在把编辑器真正装进表格、
        交给用户操作之前，内部还会有一步"默认全选编辑框文字"的收尾动作，
        发生在 setEditorData() 返回之后。如果直接在这里设置选区，会被
        Qt 那一步收尾动作重新覆盖成"全选"，等于白设——这正是"編輯時仍然
        整个文件名被选中"这个问题的真正原因。推迟到下一轮事件循环，
        确保我们的设置是最后生效的那一个。"""
        super().setEditorData(editor, index)
        if isinstance(editor, QLineEdit):
            def _narrow_selection():
                full_name = editor.text()
                dot_pos = full_name.rfind(".")
                # 没有扩展名，或者"."出现在开头（比如".gitignore"这种隐藏
                # 文件风格的名字，虽然 DWG 场景基本不会遇到）——这两种
                # 情况没有"扩展名"可言，就全选，跟原生行为一致。
                if dot_pos > 0:
                    editor.setSelection(0, dot_pos)
                else:
                    editor.selectAll()
            QTimer.singleShot(0, _narrow_selection)


def fix_path(path_str):
    if not isinstance(path_str, str):
        raise TypeError("path_str must be a string")
    fixed = path_str.replace('/', os.sep).replace('\\\\', os.sep)
    if len(fixed) > 1 and fixed[1] == ':':
        if len(fixed) == 2 or (len(fixed) > 2 and fixed[2] != os.sep):
            fixed = fixed[:2] + os.sep + fixed[2:]
    return os.path.normpath(fixed)

def human_readable_size(size_in_bytes):
    try:
        size = float(size_in_bytes)
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024.0:
                return f"{size:.2f} {unit}"
            size /= 1024.0
        return f"{size:.2f} TB"
    except Exception:
        return "未知"

def stat_file_safe(file_path):
    """对单个文件做一次 os.stat()，失败时返回 None 而不抛异常。设计成
    (path, stat_result) 这种独立、无副作用的纯函数，方便丢进线程池并发
    跑——多个文件的磁盘/网络访问可以同时进行，不用排队一个个等。

    V2.19.1：DWG 版本标识不再在这里读。原来的做法是"填搜索结果表格
    这一刻，对每个命中文件单独 open() 一次现读文件头"，21357 个文件
    实测要额外付出接近0.9秒（见 search_manager.py 填表那段的耗时打点
    分析），而且这个信息在建索引阶段本来就已经顺手读过一次、存进了
    dwg_index.dwg_version 列——现在版本号跟着搜索结果的 SQL 查询一起
    从数据库里 SELECT 出来（见 database.search_dwg_index），search_
    manager.py 直接用查询结果里的版本号，不再调用这个函数去现读文件。
    """
    try:
        stat_result = os.stat(file_path)
    except Exception:
        stat_result = None
    return file_path, stat_result


# DWG 文件格式规定死的：文件最前面 6 个字节是版本标识（ASCII 文本，
# 比如 "AC1032"），是文件头的一部分，不需要真正解析图纸内容——读这
# 6 个字节的开销小到可以忽略，跟 os.stat() 一次访问的量级差不多。
#
# 这份年份对照表来自 DWG 格式的公开版本标识规律，后面几个是目前
# （2026年）市面上还在用的主流版本；再往后 AutoCAD 如果引入新的版本
# 标识（比如某年之后又换了新代号），这里没收录到的话，
# read_dwg_version_tag() 会退化成直接显示原始标识（比如 "AC1040"），
# 不会报错、也不会显示空白，只是暂时没有对应的年份说明文字，不影响
# 这一列正常显示和以后按需要补充这张表。
_DWG_VERSION_LABELS = {
    "AC1006": "R10",
    "AC1009": "R11/R12",
    "AC1012": "R13",
    "AC1014": "R14",
    "AC1015": "2000/2000i/2002",
    "AC1018": "2004-2006",
    "AC1021": "2007-2009",
    "AC1024": "2010-2012",
    "AC1027": "2013-2017",
    "AC1032": "2018及以上",
}


def read_dwg_version_tag(file_path):
    """读 DWG 文件头部的版本标识，换算成人能看懂的 AutoCAD 年份范围
    （比如 "AC1032（2018及以上）"）。

    读不到（文件不存在、正被占用、压根不是 DWG 格式等）时返回空
    字符串，调用方按"未知"处理，不抛异常、不影响其余文件继续正常
    显示——这一列纯粹是锦上添花的展示信息，不应该因为某一张图纸
    读取失败就拖累整个搜索结果列表填不出来。"""
    try:
        with open(file_path, "rb") as f:
            head = f.read(6)
        tag = head.decode("ascii", errors="ignore")
        if not tag.startswith("AC"):
            return ""
        label = _DWG_VERSION_LABELS.get(tag)
        return f"{tag}（{label}）" if label else tag
    except Exception:
        return ""


def update_table_row(table, row_idx, data_dict, stat_result=None, dwg_version=None):
    file_path = data_dict.get("path", "")
    filename = os.path.basename(file_path)

    item_name = QTableWidgetItem(filename)
    item_name.setToolTip(filename)
    # 文件名这一列要能编辑（配合 create_frozen_first_column() 里开的
    # SelectedClicked 编辑触发，用来做"资源管理器式"改名）——
    # QTableWidgetItem 默认本来就带 Qt.ItemIsEditable，这里显式声明
    # 一下，表明这是刻意保留的，不是漏改。
    item_name.setFlags(item_name.flags() | Qt.ItemIsEditable)
    table.setItem(row_idx, 0, item_name)

    path_item = QTableWidgetItem(file_path)
    path_item.setToolTip(file_path)
    # 其余列都不允许编辑——路径、日期、版本、大小这些是从磁盘读出来的
    # 客观信息，不该被用户在表格里手滑改动看起来像是能改一样。
    path_item.setFlags(path_item.flags() & ~Qt.ItemIsEditable)
    table.setItem(row_idx, 1, path_item)

    # DWG 版本这一列单独处理，不放进下面 os.stat() 那个 try/except
    # 里——版本标识是另外单独一次文件读取，跟 os.stat() 是否成功没有
    # 关系，就算下面网络访问超时/失败，版本号这一列依然应该能正常
    # 显示（反之亦然）。
    item_version = QTableWidgetItem(dwg_version if dwg_version else "未知")
    item_version.setToolTip(dwg_version if dwg_version else "未能识别到 DWG 版本信息")
    item_version.setFlags(item_version.flags() & ~Qt.ItemIsEditable)
    table.setItem(row_idx, 4, item_version)

    try:
        # 如果外部已经并发预取过 stat 结果，直接用，不用再单独访问一次磁盘；
        # 没有预取时（比如单独调用这个函数的场景）才退回到自己现查一次。
        if stat_result is None:
            stat_result = os.stat(file_path)
        mtime = stat_result.st_mtime
        ctime = stat_result.st_ctime
        size = stat_result.st_size

        from PyQt5.QtCore import QDateTime
        str_ctime = QDateTime.fromTime_t(int(ctime)).toString("yyyy-MM-dd hh:mm:ss")
        str_mtime = QDateTime.fromTime_t(int(mtime)).toString("yyyy-MM-dd hh:mm:ss")

        item_ctime = CustomSortItem(str_ctime)
        item_ctime.setData(Qt.UserRole, ctime)
        item_ctime.setToolTip(str_ctime)
        item_ctime.setFlags(item_ctime.flags() & ~Qt.ItemIsEditable)
        table.setItem(row_idx, 2, item_ctime)

        item_mtime = CustomSortItem(str_mtime)
        item_mtime.setData(Qt.UserRole, mtime)
        item_mtime.setToolTip(str_mtime)
        item_mtime.setFlags(item_mtime.flags() & ~Qt.ItemIsEditable)
        table.setItem(row_idx, 3, item_mtime)

        size_str = human_readable_size(size)
        item_size = CustomSortItem(size_str)
        item_size.setData(Qt.UserRole, size)
        item_size.setToolTip(size_str)
        item_size.setFlags(item_size.flags() & ~Qt.ItemIsEditable)
        table.setItem(row_idx, 5, item_size)
    except Exception:
        table.setItem(row_idx, 2, CustomSortItem("未知"))
        table.setItem(row_idx, 3, CustomSortItem("未知"))
        table.setItem(row_idx, 5, CustomSortItem("未知"))

def open_file_with_default_app(file_path):
    if not os.path.exists(file_path):
        QMessageBox.warning(None, "错误", "文件不存在或路径错误！")
        return
    try:
        if os.name == 'nt':
            norm_path = os.path.abspath(file_path)
            # 用 PowerShell 的 Start-Process 打开文件，而不是 os.startfile()
            # 或者 explorer.exe，是因为这两种都各自有坑：
            # 1）explorer.exe 是单实例进程，subprocess 拉起来的只是个"信使"
            #    进程，把路径转发给已经在跑的那个真正 explorer.exe——这层
            #    转发在路径带特殊字符/网络路径/路径较长时经常悄悄失败，
            #    失败后 explorer.exe 不报错，直接静默跳到用户主目录（"文档"）。
            # 2）os.startfile() 是在当前 Python 进程内部直接调用
            #    ShellExecuteEx，会把这个文件类型关联的 shell 扩展（图标
            #    处理器、右键菜单处理器、跟目标程序握手用的 DDE 会话等）
            #    加载进当前调用它的进程——也就是加载进这个 PyQt 进程。
            #    如果 dwg 关联程序注册的这些 shell 扩展跟 PyQt 进程有
            #    线程模型/位数之类的冲突，会直接把宿主进程崩掉，而且是
            #    原生层面的崩溃，try/except 完全抓不住，表现就是"闪退"。
            # Start-Process 是在一个完全独立的外部进程（PowerShell）里
            # 触发打开动作，不经过 explorer.exe 的单实例转发，也不在
            # 当前进程内加载任何 shell 扩展——就算目标文件关联的 shell
            # 扩展本身有问题，炸的也只是那个独立的 PowerShell 子进程，
            # 不会连累主程序。
            # -NoProfile / -NonInteractive：跳过用户 PowerShell 配置文件的
            # 加载和交互式提示，减少无关启动开销，打开文件的反应能快一点。
            # -WindowStyle Hidden + CREATE_NO_WINDOW：双保险，确保连一闪
            # 而过的黑框都不会出现。
            # 路径里如果带单引号（比如文件夹名里有撇号），PowerShell 单
            # 引号字符串里字面单引号要写成两个单引号来转义，不然拼出来的
            # 命令会在这个引号处提前截断、直接语法错误。
            escaped_path = norm_path.replace("'", "''")
            ps_command = f"Start-Process -FilePath '{escaped_path}'"
            subprocess.Popen(
                ['powershell', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-Command', ps_command],
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        else:
            subprocess.Popen(['xdg-open', file_path])
    except Exception as e:
        QMessageBox.warning(None, "错误", f"无法打开文件:\n{e}")


def open_containing_folder(file_path, parent=None):
    """打开文件所在目录，并且让 Explorer 里那个文件本身是选中（高亮）
    状态——而不是只把目录打开、一堆文件里还得自己再找一遍。

    这里统一成一个公共函数，是因为之前"查看失败文件"弹窗里的
    "打开所在目录"按钮和搜索结果列表右键"打开文件所在位置"各自单独
    写了一遍逻辑，两边一个用 explorer 的 /select 参数（真的会选中文件）、
    一个只是把 os.path.dirname() 算出来的目录传给 explorer（只是打开
    目录，不会选中文件）——两处看着功能一样，用户体验却不一致。统一
    成这一个函数之后，以后不管在哪里加"打开所在位置"这个操作，都调用
    这一个，不会再出现两边行为不一致的问题。
    """
    if not os.path.exists(file_path):
        QMessageBox.warning(parent, "错误", "该文件所在目录已被移出或不存在！")
        return
    try:
        if os.name == 'nt':
            subprocess.Popen(f'explorer /select,"{file_path}"')
        else:
            subprocess.Popen(['xdg-open', os.path.dirname(file_path)])
    except Exception as e:
        QMessageBox.warning(parent, "错误", f"无法打开指定目录:\n{e}")


# =========================================================================
# 🌟 V4.8：改用 ACadSharp 编译出的独立 exe，彻底脱离 AutoCAD COM/ObjectDBX。
# 不再需要隐藏实例、不再有实例合并/卡顿风险、不再受 AutoCAD 版本对应的
# .NET 版本锁定——用户机器上有没有装 AutoCAD 都不影响索引扫描。
# =========================================================================

_EXTRACT_ERROR_PREFIX = "[ACadSharp解析异常]:"

# 直径符号在 DWG 里可能以好几种不同的 Unicode 字符出现（比如 ∅ 空集符号、
# Ø/ø 带删除线的字母O），渲染出来跟标准直径符号 ⌀ 几乎无法用肉眼区分，
# 但字符串比较时是完全不同的字符，会导致搜索只能命中其中一种写法。
# 这份映射跟 DwgTextExtractor/Program.cs 里的 NormalizeDiameterVariants
# 保持同一套对应关系，这里再做一遍是为了兜底两种情况：
#   1) exe 还没重新编译发布、用户先更新了 Python 端；
#   2) extract_dxf_text()（走 ezdxf，不经过 exe）这条独立提取路径。
_DIAMETER_VARIANTS = {
    "\u2205": "\u2300",  # ∅ EMPTY SET → ⌀
    "\u00d8": "\u2300",  # Ø LATIN CAPITAL LETTER O WITH STROKE → ⌀
    "\u00f8": "\u2300",  # ø LATIN SMALL LETTER O WITH STROKE → ⌀
}


def normalize_diameter_symbol(text):
    """把 ∅ / Ø / ø 这几种跟直径符号视觉上分不清、但编码不同的字符，
    统一替换成标准直径符号 ⌀（U+2300）。"""
    if not text:
        return text
    for variant, canonical in _DIAMETER_VARIANTS.items():
        if variant in text:
            text = text.replace(variant, canonical)
    return text


# "查找替换"这条链路读的是 AutoCAD 原生 TextString/Contents 属性（不管是
# replace_worker.py 走 COM，还是 AccoreconsolePlugin/Commands.cs 走
# accoreconsole），拿到的都是实体里的原始存储值——不像索引/预览那样，
# 提取阶段（本文件 _parse_extracted_line）已经统一解码归一化过。同一个
# 视觉上一样的直径符号，原始存储可能是好几种样子：已经解码的字符
# （⌀/∅/Ø/ø）、老式单行文字尚未解码的控制码（%%c/%%C），或者 MTEXT
# 尚未解码的转义序列（\U+2205 这 7 个字符的字面文本，不是真正的 U+2205
# 字符）。用户在替换框里填的"旧文字"是索引/预览里看到的、已经解码归一化
# 过的样子（比如"⌀20"），只按字面比较的话，遇到还是原始编码形态的实体
# 就会比对不上、静默跳过不替换——两份图纸各用一种编码存直径符号时，
# 就是"只替换了一个"的直接原因。
_DIAMETER_CANONICAL = "\u2300"  # ⌀
_DIAMETER_RAW_FORMS = [
    "\u2300",    # ⌀ 标准直径符号（原样保留，扩展逻辑统一处理更简单）
    "\u2205",    # ∅ 空集符号
    "\u00d8",    # Ø
    "\u00f8",    # ø
    "%%c",       # 老式单行文字控制码
    "%%C",
    "\\U+2205",  # MTEXT 未解码的转义序列，字面 7 个字符，不是真正的 ∅
]


def expand_pairs_with_diameter_variants(pairs):
    """给每一组包含直径符号的 (旧文字, 新文字)，按上面列出的等价形式各
    追加一组变体，替换引擎本身逻辑不用改——扩展出来的每组变体依次按
    字面比较尝试，不管命中的实体原始存的是哪种编码形态都能替换到。

    pairs: [(old_text, new_text), ...]
    返回：扩展后的 [(old_text, new_text), ...]，原始组保留在前面，
    不影响没有直径符号的组的既有行为。
    """
    expanded = []
    for old_text, new_text in pairs:
        expanded.append((old_text, new_text))
        normalized_old = normalize_diameter_symbol(old_text)
        if _DIAMETER_CANONICAL not in normalized_old:
            continue
        seen = {old_text}
        for form in _DIAMETER_RAW_FORMS:
            variant_old = normalized_old.replace(_DIAMETER_CANONICAL, form)
            if variant_old not in seen:
                seen.add(variant_old)
                expanded.append((variant_old, new_text))
    return expanded


def extract_dwg_text_via_exe(dwg_path, exe_path, timeout=30,
                              register_process=None, unregister_process=None,
                              is_cancelled=None):
    """
    调用编译好的 DwgTextExtractor.exe（基于 ACadSharp）离线解析 DWG 文字内容。

    子进程级隔离：单张图纸的解析崩溃/超时只会让这次调用抛异常，
    不会拖垮整个索引线程或影响用户正在操作的任何东西——因为压根不涉及
    AutoCAD 进程，也就不存在"抢占/卡顿"这个问题的前提条件。

    register_process / unregister_process：可选回调，调用方（IndexThread）
    用它们把当前正在跑的子进程句柄登记到自己维护的"存活进程"集合里。
    这样调用方在用户点"退出"时能直接 kill() 掉这些还没跑完的子进程，
    而不必等 subprocess 自己跑完——是让"点退出"能在秒级内响应、
    不用每次都等满强制超时时间的关键（改用 Popen 而不是一次性的
    subprocess.run，就是为了能拿到这个句柄）。

    is_cancelled：可选回调，返回 True 表示调用方已经主动要求停止
    （比如用户点了退出，进程是被 register_process 拿到的句柄从外部
    kill 掉的）。用来把"被我们自己杀掉"和"图纸本身解析失败/超时"
    区分开，避免前者被当成真实错误打印出来。
    """
    if not exe_path or not os.path.exists(exe_path):
        raise RuntimeError(f"未找到 DWG 提取工具: {exe_path}")

    abs_path = os.path.abspath(dwg_path)
    kwargs = {}
    if os.name == 'nt':
        # 防止每次调用子进程时在任务栏一闪而过的黑框窗口
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(
        [exe_path, abs_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )
    if register_process:
        register_process(proc)
    try:
        try:
            stdout, _stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise RuntimeError(f"提取超时(可能是损坏图纸，超过{timeout}秒): {dwg_path}")
    finally:
        if unregister_process:
            unregister_process(proc)

    if proc.returncode != 0 and is_cancelled and is_cancelled():
        # 进程是被外部（用户点退出）主动杀掉的，不是图纸本身解析失败，
        # 静默返回空结果，不当真实错误抛出去
        return []

    lines = [line.strip("\r\n") for line in stdout.splitlines() if line.strip("\r\n")]
    err_lines = [l for l in lines if l.startswith(_EXTRACT_ERROR_PREFIX)]
    raw_lines = [l for l in lines if not l.startswith(_EXTRACT_ERROR_PREFIX)]

    if err_lines and not raw_lines:
        # exe 明确报告了解析失败，把原因抛给调用方，由索引循环决定怎么记录
        raise RuntimeError(err_lines[0][len(_EXTRACT_ERROR_PREFIX):].strip())

    if proc.returncode != 0 and not raw_lines:
        # exe 是异常崩溃退出的（比如缺依赖 DLL 抛的未处理异常、native 崩溃
        # 等），这类情况不会走 CleanLegacyCodes/Emit 那条正常输出路径，
        # stdout 通常是空的，报错信息只会出现在 stderr 里。以前这里没检查
        # returncode，空 stdout + 非0退出码会被当成"成功但没提取到文字"，
        # 索引照样标记完成、不出现在失败文件列表里——表现就是这个文件
        # 悄悄变成"没内容"，而不是"提取失败"，非常容易被忽略掉。
        # 这里把 stderr 内容（截断到合理长度）当成真实错误抛给调用方。
        err_detail = (_stderr or "").strip()
        if len(err_detail) > 500:
            err_detail = err_detail[:500] + "...(截断)"
        raise RuntimeError(
            f"DwgTextExtractor.exe 异常退出(退出码 {proc.returncode})，"
            f"没有产生任何输出。可能是运行目录下缺少依赖 DLL"
            f"（比如 ACadSharp.dll），或者图纸本身导致解析器崩溃。"
            + (f" 错误详情: {err_detail}" if err_detail else "")
        )

    return [_parse_extracted_line(l) for l in raw_lines if l.strip()]


# 输出格式约定：TYPE\tSPACE\tSCOPE\t文字内容（跟 DwgTextExtractor.exe 的 Emit() 对应）。
# 兜底分支处理的是老版本 exe 输出（3 字段甚至纯文字行）——exe 还没重新编译
# 发布、或者极端意外情况，不能让整条索引直接炸掉，按最保守的默认值补齐。
_DEFAULT_TYPE = "TEXT"
_DEFAULT_SPACE = "MODEL"
_DEFAULT_SCOPE = "PLACED"


def _parse_extracted_line(line):
    """把一行 'TYPE\\tSPACE\\tSCOPE\\t文字' 解析成 (entity_type, space, scope, text) 四元组。"""
    parts = line.split("\t", 3)
    if len(parts) == 4:
        entity_type, space, scope, text = parts
        entity_type = entity_type.strip() or _DEFAULT_TYPE
        space = space.strip() or _DEFAULT_SPACE
        scope = scope.strip() or _DEFAULT_SCOPE
        return (entity_type, space, scope, normalize_diameter_symbol(text))
    if len(parts) == 3:
        # 老版本 exe（还没加 SCOPE 字段那一版）的输出，按"摆放实体"兜底
        entity_type, space, text = parts
        entity_type = entity_type.strip() or _DEFAULT_TYPE
        space = space.strip() or _DEFAULT_SPACE
        return (entity_type, space, _DEFAULT_SCOPE, normalize_diameter_symbol(text))
    # 格式完全不认识（更老版本 exe 输出、或者意外情况），整行当纯文字兜底
    return (_DEFAULT_TYPE, _DEFAULT_SPACE, _DEFAULT_SCOPE, normalize_diameter_symbol(line))


def extract_dxf_text(dxf_file_path):

    all_text_content = []
    if not os.path.exists(dxf_file_path):
        return [f"错误: DXF 文件不存在: {dxf_file_path}"]
    try:
        doc = ezdxf.readfile(dxf_file_path)
        for text_entity in doc.modelspace().query("TEXT MTEXT"):
            if getattr(text_entity.dxf, "text", None):
                all_text_content.append(normalize_diameter_symbol(text_entity.dxf.text))
        for pspace in doc.paperspaces():
            for text_entity in pspace.query("TEXT MTEXT"):
                if getattr(text_entity.dxf, "text", None):
                    all_text_content.append(normalize_diameter_symbol(text_entity.dxf.text))
    except Exception as e:
        all_text_content.append(f"解析内容失败: {e}")
    return all_text_content