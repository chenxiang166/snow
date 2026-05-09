"""PC 音频补偿 — 主程序 (PyQt6 GUI)

使用方法:
  python main.py                # 正常启动
  python main.py --list         # 列出音频设备
  python main.py --preset mild  # 使用预设听力图
"""

import sys
import os
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('main')

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QGroupBox, QButtonGroup, QRadioButton, QComboBox, QStatusBar,
    QMessageBox, QTabWidget, QSpinBox, QSlider, QFrame, QFileDialog,
    QDialog, QSplitter, QCheckBox,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread, QPointF, QRectF
from PyQt6.QtGui import (
    QFont, QPalette, QColor, QIcon, QPixmap, QImage,
    QPainter, QPen, QBrush, QMouseEvent, QWheelEvent,
)

import matplotlib
matplotlib.use('QtAgg')
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

import numpy as np
import sounddevice as sd

from nal_nl2 import calculate_gains, calculate_gains_simple, constants as C
from nal_nl2.prescription import get_gain_array, get_cr_array
from audio.stream import AudioStream, register_exit_handlers, check_and_recover
from audio import loopback as lb


# ── 样式表 (深色主题) ──

DARK_STYLE = """
QMainWindow, QWidget {
    background-color: #0d1117;
    color: #e6edf3;
    font-family: 'Segoe UI', system-ui, sans-serif;
}
QGroupBox {
    border: 1px solid #30363d;
    border-radius: 8px;
    margin-top: 16px;
    padding-top: 20px;
    font-weight: bold;
    color: #8b949e;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
}
QPushButton {
    background-color: #21262d;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 6px 16px;
    color: #e6edf3;
    font-size: 13px;
}
QPushButton:hover {
    background-color: #30363d;
}
QPushButton:pressed {
    background-color: #484f58;
}
QPushButton#btnStart {
    background-color: #238636;
    border-color: #238636;
    font-weight: bold;
}
QPushButton#btnStart:hover {
    background-color: #2ea043;
}
QPushButton#btnStop {
    background-color: #da3633;
    border-color: #da3633;
    font-weight: bold;
}
QPushButton#btnStop:hover {
    background-color: #f85149;
}
QTableWidget {
    background-color: #161b22;
    border: 1px solid #30363d;
    border-radius: 4px;
    gridline-color: #21262d;
}
QTableWidget::item {
    padding: 4px;
}
QHeaderView::section {
    background-color: #161b22;
    border: none;
    border-bottom: 1px solid #30363d;
    padding: 6px;
    color: #8b949e;
    font-weight: normal;
}
QSpinBox {
    background-color: #0d1117;
    border: 1px solid #30363d;
    border-radius: 4px;
    padding: 3px 6px;
    color: #e6edf3;
}
QSpinBox:focus {
    border-color: #58a6ff;
}
QComboBox {
    background-color: #0d1117;
    border: 1px solid #30363d;
    border-radius: 4px;
    padding: 4px 8px;
    color: #e6edf3;
    min-width: 120px;
}
QRadioButton {
    color: #8b949e;
    spacing: 4px;
}
QRadioButton::indicator {
    width: 14px;
    height: 14px;
}
QStatusBar {
    background-color: #161b22;
    border-top: 1px solid #30363d;
    color: #8b949e;
    font-size: 12px;
}
QLabel#statusLabel {
    font-size: 12px;
    padding: 2px 8px;
}
QSlider::groove:horizontal {
    height: 4px;
    background: #21262d;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 14px;
    height: 14px;
    background: #58a6ff;
    border-radius: 7px;
    margin: -5px 0;
}
QTabWidget::pane {
    border: 1px solid #30363d;
    border-radius: 4px;
    background: #161b22;
}
QTabBar::tab {
    background: #21262d;
    border: 1px solid #30363d;
    padding: 6px 14px;
    border-radius: 4px;
    margin-right: 2px;
    color: #8b949e;
}
QTabBar::tab:selected {
    background: #58a6ff;
    color: #fff;
}
"""


# ── 照片识别对话框 ──

class PhotoDialog(QDialog):
    """听力图照片识别 — 点击标记点提取听阈数据"""

    FREQS = C.FREQS

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("📷 听力图照片识别")
        self.setMinimumSize(900, 600)

        self._pixmap: QPixmap | None = None
        self._image_rect = QRectF()
        self._current_ear = 'right'  # 'right' or 'left'
        self._right_points: list[QPointF] = []  # 图像坐标
        self._left_points: list[QPointF] = []

        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 说明
        hint = QLabel(
            "🔵 先在图上<b>点击右耳标记点</b>（红色○），沿曲线从低频→高频依次点击  |  "
            "🔵 切换<b>左耳</b>（蓝色×），同样点击  |  "
            "🔵 点击「应用数据」"
        )
        hint.setStyleSheet(
            "padding: 8px 12px; background: rgba(88,166,255,0.08); border: 1px solid rgba(88,166,255,0.2); "
            "border-radius: 6px; font-size: 12px; color: #58a6ff;"
        )
        layout.addWidget(hint)

        # 主区域: 图片 + 侧栏
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 图片显示区域
        self._image_label = QLabel("点击上传或拖放听力图照片\n\n支持 JPG / PNG / WEBP")
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet(
            "border: 2px dashed #30363d; border-radius: 8px; "
            "background: #0d1117; color: #8b949e; font-size: 14px;"
        )
        self._image_label.setMinimumSize(500, 400)
        self._image_label.setMouseTracking(True)
        self._image_label.mousePressEvent = self._on_image_click
        self._image_label.mouseMoveEvent = self._on_image_move
        splitter.addWidget(self._image_label)

        # 侧栏
        side = QWidget()
        side_layout = QVBoxLayout(side)

        # 耳朵切换
        ear_label = QLabel("当前标记: <span style='color:#f85149'>🔴 右耳</span>")
        ear_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        self._ear_label = ear_label
        side_layout.addWidget(ear_label)

        ear_btns = QHBoxLayout()
        self._btn_right = QPushButton("🔴 右耳")
        self._btn_right.setStyleSheet(
            "background: rgba(248,81,73,0.2); border: 1px solid #f85149; border-radius: 6px; padding: 8px;")
        self._btn_right.clicked.connect(lambda: self._switch_ear('right'))
        ear_btns.addWidget(self._btn_right)

        self._btn_left = QPushButton("🔵 左耳")
        self._btn_left.setStyleSheet(
            "background: #21262d; border: 1px solid #30363d; border-radius: 6px; padding: 8px;")
        self._btn_left.clicked.connect(lambda: self._switch_ear('left'))
        ear_btns.addWidget(self._btn_left)
        side_layout.addLayout(ear_btns)

        # 点列表
        side_layout.addWidget(QLabel("已标记点:"))
        self._point_list = QLabel("在图片上点击来标记听阈点")
        self._point_list.setStyleSheet(
            "background: #0d1117; border: 1px solid #30363d; border-radius: 4px; "
            "padding: 8px; font-size: 11px; min-height: 150px;"
        )
        self._point_list.setAlignment(Qt.AlignmentFlag.AlignTop)
        side_layout.addWidget(self._point_list)

        # 清除按钮
        clear_btn = QPushButton("🗑 清除当前耳标记")
        clear_btn.clicked.connect(self._clear_current)
        side_layout.addWidget(clear_btn)

        side_layout.addStretch()
        splitter.addWidget(side)
        layout.addWidget(splitter)

        # 底部按钮
        footer = QHBoxLayout()
        load_btn = QPushButton("📂 加载图片")
        load_btn.clicked.connect(self._load_image)
        footer.addWidget(load_btn)

        footer.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        footer.addWidget(cancel_btn)

        apply_btn = QPushButton("✅ 应用数据")
        apply_btn.setStyleSheet(
            "background: #238636; border-color: #238636; font-weight: bold; padding: 8px 20px;")
        apply_btn.clicked.connect(self._apply)
        footer.addWidget(apply_btn)
        layout.addLayout(footer)

    def _load_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择听力图照片", "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp);;All Files (*)"
        )
        if not path:
            return

        self._pixmap = QPixmap(path)
        self._right_points.clear()
        self._left_points.clear()
        self._redraw()

    def _switch_ear(self, ear: str):
        self._current_ear = ear
        if ear == 'right':
            self._btn_right.setStyleSheet(
                "background: rgba(248,81,73,0.2); border: 1px solid #f85149; border-radius: 6px; padding: 8px;")
            self._btn_left.setStyleSheet(
                "background: #21262d; border: 1px solid #30363d; border-radius: 6px; padding: 8px;")
            self._ear_label.setText("当前标记: <span style='color:#f85149'>🔴 右耳</span>")
        else:
            self._btn_left.setStyleSheet(
                "background: rgba(88,166,255,0.2); border: 1px solid #58a6ff; border-radius: 6px; padding: 8px;")
            self._btn_right.setStyleSheet(
                "background: #21262d; border: 1px solid #30363d; border-radius: 6px; padding: 8px;")
            self._ear_label.setText("当前标记: <span style='color:#58a6ff'>🔵 左耳</span>")
        self._update_point_list()

    def _on_image_click(self, event: QMouseEvent):
        if self._pixmap is None:
            self._load_image()
            return

        pos = self._to_image_coords(event.position())
        if pos is None:
            return

        pts = self._right_points if self._current_ear == 'right' else self._left_points
        pts.append(pos)
        self._update_point_list()
        self._redraw()

    def _on_image_move(self, event: QMouseEvent):
        # Crosshair cursor when over image
        pass

    def _to_image_coords(self, widget_pos: QPointF) -> QPointF | None:
        """将控件坐标转换为图像坐标"""
        if self._pixmap is None:
            return None

        # 计算图像在 label 中的显示区域 (保持比例居中)
        label_size = self._image_label.size()
        pixmap_size = self._pixmap.size()
        pixmap_size.scale(label_size, Qt.AspectRatioMode.KeepAspectRatio)

        offset_x = (label_size.width() - pixmap_size.width()) / 2
        offset_y = (label_size.height() - pixmap_size.height()) / 2
        self._image_rect = QRectF(offset_x, offset_y,
                                  pixmap_size.width(), pixmap_size.height())

        if not self._image_rect.contains(widget_pos):
            return None

        # 映射到原始图像坐标
        scale_x = self._pixmap.width() / pixmap_size.width()
        scale_y = self._pixmap.height() / pixmap_size.height()
        ix = (widget_pos.x() - offset_x) * scale_x
        iy = (widget_pos.y() - offset_y) * scale_y
        return QPointF(ix, iy)

    def _clear_current(self):
        if self._current_ear == 'right':
            self._right_points.clear()
        else:
            self._left_points.clear()
        self._update_point_list()
        self._redraw()

    def _update_point_list(self):
        lines = []
        for ear, color, pts in [('右耳', '#f85149', self._right_points),
                                ('左耳', '#58a6ff', self._left_points)]:
            if pts:
                lines.append(f"<b style='color:{color}'>{ear} ({len(pts)}点)</b>")
                for i, p in enumerate(pts):
                    lines.append(
                        f"  <span style='color:{color}'>●</span> "
                        f"#{i+1} ({p.x():.0f}, {p.y():.0f})"
                    )
        self._point_list.setText(
            "<br>".join(lines) if lines else "在图片上点击来标记听阈点"
        )

    def _redraw(self):
        if self._pixmap is None:
            return

        # 创建画布
        label_size = self._image_label.size()
        canvas = QPixmap(label_size)
        canvas.fill(QColor("#0d1117"))
        painter = QPainter(canvas)

        # 绘制缩放后的图像
        pixmap_size = self._pixmap.size()
        pixmap_size.scale(label_size, Qt.AspectRatioMode.KeepAspectRatio)
        offset_x = (label_size.width() - pixmap_size.width()) / 2
        offset_y = (label_size.height() - pixmap_size.height()) / 2
        scaled = self._pixmap.scaled(pixmap_size, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap(int(offset_x), int(offset_y), scaled)

        self._image_rect = QRectF(offset_x, offset_y,
                                  pixmap_size.width(), pixmap_size.height())

        # 网格
        painter.setPen(QPen(QColor(255, 255, 255, 30), 1))
        for i in range(1, 10):
            x = offset_x + pixmap_size.width() * i / 10
            y = offset_y + pixmap_size.height() * i / 10
            painter.drawLine(QPointF(x, offset_y),
                             QPointF(x, offset_y + pixmap_size.height()))
            painter.drawLine(QPointF(offset_x, y),
                             QPointF(offset_x + pixmap_size.width(), y))

        # 轴标签
        painter.setPen(QPen(QColor(255, 255, 255, 50)))
        font = QFont('system-ui', 7)
        painter.setFont(font)
        freqs = ['125', '250', '500', '1k', '2k', '4k', '8k']
        for i, f in enumerate(freqs):
            fx = offset_x + pixmap_size.width() * 0.05 + pixmap_size.width() * 0.9 * i / (len(freqs) - 1)
            painter.drawText(QPointF(fx, offset_y + pixmap_size.height() - 3), f + 'Hz')

        # 绘制标记点
        scale_x = pixmap_size.width() / self._pixmap.width()
        scale_y = pixmap_size.height() / self._pixmap.height()

        def draw_points(pts, color, symbol):
            painter.setPen(QPen(QColor(color), 2))
            for i, p in enumerate(pts):
                sx = offset_x + p.x() * scale_x
                sy = offset_y + p.y() * scale_y
                # 外圈
                painter.setBrush(QBrush(QColor(0, 0, 0, 120)))
                painter.drawEllipse(QPointF(sx, sy), 7, 7)
                # 内点
                painter.setBrush(QBrush(QColor(color)))
                painter.drawEllipse(QPointF(sx, sy), 3, 3)
                # 标签
                painter.setPen(QPen(QColor('#fff')))
                painter.setFont(QFont('system-ui', 9, QFont.Weight.Bold))
                painter.drawText(QPointF(sx - 12, sy - 10), str(i + 1))
                # 连线
                if i > 0:
                    prev = pts[i - 1]
                    px = offset_x + prev.x() * scale_x
                    py = offset_y + prev.y() * scale_y
                    pen = QPen(QColor(color), 1.5, Qt.PenStyle.DashLine)
                    painter.setPen(pen)
                    painter.drawLine(QPointF(px, py), QPointF(sx, sy))

        draw_points(self._right_points, '#f85149', '○')
        draw_points(self._left_points, '#58a6ff', '×')

        painter.end()
        self._image_label.setPixmap(canvas)

    def _apply(self):
        """将标记点映射为听力图数据"""
        right_pts = self._right_points
        left_pts = self._left_points

        if len(right_pts) < 2 and len(left_pts) < 2:
            QMessageBox.warning(self, "数据不足", "请至少为一只耳朵标记 2 个以上的点")
            return

        def pts_to_htl(pts):
            if len(pts) < 2:
                return None
            # X → log 频率, Y → dB (反转)
            xs = [p.x() for p in pts]
            ys = [p.y() for p in pts]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            if max_x == min_x or max_y == min_y:
                return None

            import math
            log_min = math.log10(125)
            log_max = math.log10(8000)

            values = {}
            for x, y in zip(xs, ys):
                t = (x - min_x) / (max_x - min_x)
                log_f = log_min + t * (log_max - log_min)
                freq = 10 ** log_f
                db_t = (y - min_y) / (max_y - min_y)
                db_hl = db_t * 120
                # 吸附到最近标准频率
                best = min(self.FREQS, key=lambda f: abs(freq - f))
                if best not in values:
                    values[best] = max(0, min(120, round(db_hl)))
                else:
                    values[best] = max(0, min(120, round((values[best] + db_hl) / 2)))

            # 填充 11 个频率
            known = sorted(values.items())
            result = []
            for f in self.FREQS:
                if f in values:
                    result.append(values[f])
                else:
                    # 插值
                    lo = [k for k in known if k[0] <= f]
                    hi = [k for k in known if k[0] >= f]
                    if lo and hi:
                        f0, v0 = lo[-1]
                        f1, v1 = hi[0]
                        t = (f - f0) / (f1 - f0) if f1 != f0 else 0
                        result.append(round(v0 + t * (v1 - v0)))
                    elif lo:
                        result.append(lo[-1][1])
                    elif hi:
                        result.append(hi[0][1])
                    else:
                        result.append(0)
            return result

        self._result_left = pts_to_htl(left_pts)
        self._result_right = pts_to_htl(right_pts)

        if not self._result_left and not self._result_right:
            QMessageBox.warning(self, "识别失败", "无法从标记点推算出听力图数据")
            return

        self.accept()

    def get_result(self) -> tuple[list | None, list | None]:
        return getattr(self, '_result_left', None), getattr(self, '_result_right', None)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PC 音频补偿 — NAL-NL2")
        self.setMinimumSize(800, 650)

        # 音频流
        self._stream = AudioStream()
        register_exit_handlers(self._stream)

        # 听力图数据
        self._left_htl = list(C.PRESETS['custom']['left'])
        self._right_htl = list(C.PRESETS['custom']['right'])

        # 配置
        self._config = {
            'num_aids': 2,
            'gender': 'M',
            'experience': 'new',
            'age': 'adult',
        }

        # 构建 UI
        self._build_ui()

        # 初始计算
        self._update_gains()

        # 状态更新定时器
        self._timer = QTimer()
        self._timer.timeout.connect(self._update_status)
        self._timer.start(500)

        # Layer 3: 启动时检查并恢复
        check_and_recover()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # ── 标题 ──
        header_layout = QHBoxLayout()
        title_label = QLabel("🎧  PC 音频补偿")
        title_label.setFont(QFont('Segoe UI', 18, QFont.Weight.Bold))
        subtitle_label = QLabel("基于 NAL-NL2 处方公式 · 个性化听力补偿")
        subtitle_label.setStyleSheet("color: #8b949e; font-size: 12px;")
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        header_layout.addWidget(subtitle_label)
        layout.addLayout(header_layout)

        # ── 听力图输入 ──
        audiogram_group = QGroupBox("听力图输入")
        audiogram_layout = QVBoxLayout(audiogram_group)

        # 表格
        self._table = QTableWidget(2, 11)
        self._table.setHorizontalHeaderLabels(
            [f'{f}' if f < 1000 else (f'{f/1000:.0f}k' if f % 1000 == 0 else f'{f/1000:.1f}k')
             for f in C.FREQS])
        self._table.setVerticalHeaderLabels(['🔵 左耳', '🔴 右耳'])
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)

        # 填充初始值
        for col in range(11):
            left_item = QTableWidgetItem(str(self._left_htl[col]))
            left_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(0, col, left_item)

            right_item = QTableWidgetItem(str(self._right_htl[col]))
            right_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(1, col, right_item)

        self._table.cellChanged.connect(self._on_table_changed)

        audiogram_layout.addWidget(self._table)

        # 预设按钮
        preset_layout = QHBoxLayout()
        preset_layout.addWidget(QLabel("快速预设:"))
        for name, label in [('custom', '我的'), ('normal', '正常'), ('mild', '轻度'),
                            ('moderate', '中度'), ('severe', '重度')]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _, n=name: self._apply_preset(n))
            preset_layout.addWidget(btn)
        preset_layout.addSpacing(12)
        photo_btn = QPushButton("📷 识别照片")
        photo_btn.setStyleSheet(
            "border: 1px solid #d2991d; color: #d2991d;")
        photo_btn.clicked.connect(self._open_photo_dialog)
        preset_layout.addWidget(photo_btn)
        preset_layout.addStretch()
        audiogram_layout.addLayout(preset_layout)

        layout.addWidget(audiogram_group)

        # ── 验配参数 ──
        config_group = QGroupBox("验配参数")
        config_layout = QHBoxLayout(config_group)

        # 助听模式
        aids_layout = QVBoxLayout()
        aids_layout.addWidget(QLabel("助听模式"))
        self._aids_group = QButtonGroup(self)
        for val, text in [(2, '🦻 双耳'), (1, '👂 单耳')]:
            rb = QRadioButton(text)
            rb.setChecked(val == 2)
            self._aids_group.addButton(rb, val)
            aids_layout.addWidget(rb)
        self._aids_group.idClicked.connect(self._on_config_changed)
        config_layout.addLayout(aids_layout)

        # 性别
        gender_layout = QVBoxLayout()
        gender_layout.addWidget(QLabel("性别"))
        self._gender_group = QButtonGroup(self)
        for val, text in [('M', '♂ 男'), ('F', '♀ 女')]:
            rb = QRadioButton(text)
            rb.setChecked(val == 'M')
            self._gender_group.addButton(rb)
            gender_layout.addWidget(rb)
        self._gender_group.buttonClicked.connect(self._on_config_changed)
        config_layout.addLayout(gender_layout)

        # 经验
        exp_layout = QVBoxLayout()
        exp_layout.addWidget(QLabel("使用经验"))
        self._exp_group = QButtonGroup(self)
        for val, text in [('new', '🆕 新用户'), ('experienced', '✅ 老用户')]:
            rb = QRadioButton(text)
            rb.setChecked(val == 'new')
            self._exp_group.addButton(rb)
            exp_layout.addWidget(rb)
        self._exp_group.buttonClicked.connect(self._on_config_changed)
        config_layout.addLayout(exp_layout)

        # 年龄
        age_layout = QVBoxLayout()
        age_layout.addWidget(QLabel("年龄组"))
        self._age_group = QButtonGroup(self)
        for val, text in [('adult', '🧑 成人'), ('child', '👶 儿童')]:
            rb = QRadioButton(text)
            rb.setChecked(val == 'adult')
            self._age_group.addButton(rb)
            age_layout.addWidget(rb)
        self._age_group.buttonClicked.connect(self._on_config_changed)
        config_layout.addLayout(age_layout)

        config_layout.addStretch()
        layout.addWidget(config_group)

        # ── 增益曲线图 ──
        self._figure = Figure(figsize=(8, 1.8), dpi=100)
        self._figure.patch.set_facecolor('#0d1117')
        self._ax = self._figure.add_subplot(111)
        self._ax.set_facecolor('#0d1117')
        self._ax.tick_params(colors='#8b949e', labelsize=8)
        self._ax.spines['bottom'].set_color('#30363d')
        self._ax.spines['left'].set_color('#30363d')
        self._ax.spines['top'].set_visible(False)
        self._ax.spines['right'].set_visible(False)
        self._ax.set_xlim(0, 10)
        self._ax.set_ylim(0, 50)
        self._ax.set_xticks(range(11))
        self._ax.set_xticklabels(C.FREQ_LABELS)
        self._ax.set_ylabel('Gain (dB)', color='#8b949e', fontsize=8)
        self._ax.grid(True, color='#21262d', linewidth=0.5)
        self._canvas = FigureCanvas(self._figure)
        self._canvas.setMaximumHeight(160)
        layout.addWidget(self._canvas)

        # ── 增益信息 ──
        self._gain_info_label = QLabel("增益信息: 计算中...")
        self._gain_info_label.setStyleSheet(
            "padding: 8px; background: #161b22; border-radius: 6px; font-size: 12px;")
        layout.addWidget(self._gain_info_label)

        # ── 控制栏 ──
        control_layout = QHBoxLayout()

        self._btn_start = QPushButton("▶ 开始处理")
        self._btn_start.setObjectName("btnStart")
        self._btn_start.clicked.connect(self._start_processing)
        control_layout.addWidget(self._btn_start)

        self._btn_stop = QPushButton("■ 停止")
        self._btn_stop.setObjectName("btnStop")
        self._btn_stop.clicked.connect(self._stop_processing)
        self._btn_stop.setVisible(False)
        control_layout.addWidget(self._btn_stop)

        control_layout.addStretch()

        # 设备选择
        control_layout.addWidget(QLabel("输出设备:"))
        self._device_combo = QComboBox()
        self._device_combo.setMinimumWidth(250)
        self._refresh_devices()
        control_layout.addWidget(self._device_combo)
        refresh_btn = QPushButton("🔄")
        refresh_btn.setFixedWidth(36)
        refresh_btn.setToolTip("刷新设备列表")
        refresh_btn.clicked.connect(self._refresh_devices)
        control_layout.addWidget(refresh_btn)

        layout.addLayout(control_layout)

        # ── 状态栏 ──
        self._status_bar = QStatusBar()
        self._status_label = QLabel("● 已停止")
        self._status_label.setObjectName("statusLabel")
        self._latency_label = QLabel("延迟: —")
        self._cpu_label = QLabel("CPU: —")
        self._peak_label = QLabel("峰值: —")
        self._status_bar.addWidget(self._status_label)
        self._status_bar.addWidget(self._latency_label)
        self._status_bar.addWidget(self._cpu_label)
        self._status_bar.addWidget(self._peak_label)
        self.setStatusBar(self._status_bar)

    # ── 事件处理 ──

    def _on_table_changed(self, row, col):
        try:
            item = self._table.item(row, col)
            val = int(item.text())
            val = max(0, min(120, val))
            item.setText(str(val))

            if row == 0:
                self._left_htl[col] = val
            else:
                self._right_htl[col] = val

            self._update_gains()
        except (ValueError, AttributeError):
            pass

    def _on_config_changed(self, _=None):
        aid_id = self._aids_group.checkedId()
        if aid_id > 0:
            self._config['num_aids'] = aid_id

        gender_btn = self._gender_group.checkedButton()
        if gender_btn:
            for val, text in [('M', '♂ 男'), ('F', '♀ 女')]:
                if gender_btn.text() == text:
                    self._config['gender'] = val

        exp_btn = self._exp_group.checkedButton()
        if exp_btn:
            for val, text in [('new', '🆕 新用户'), ('experienced', '✅ 老用户')]:
                if exp_btn.text() == text:
                    self._config['experience'] = val

        age_btn = self._age_group.checkedButton()
        if age_btn:
            for val, text in [('adult', '🧑 成人'), ('child', '👶 儿童')]:
                if age_btn.text() == text:
                    self._config['age'] = val

        self._update_gains()

    def _apply_preset(self, name: str):
        preset = C.PRESETS.get(name)
        if not preset:
            return

        self._left_htl = list(preset['left'])
        self._right_htl = list(preset['right'])

        # 更新表格
        self._table.blockSignals(True)
        for col in range(11):
            self._table.item(0, col).setText(str(self._left_htl[col]))
            self._table.item(1, col).setText(str(self._right_htl[col]))
        self._table.blockSignals(False)

        self._update_gains()
        logger.info(f"应用预设: {name}")

    def _open_photo_dialog(self):
        """打开听力图照片识别对话框"""
        dlg = PhotoDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            left, right = dlg.get_result()
            changed = False
            if left:
                self._left_htl = left
                changed = True
            if right:
                self._right_htl = right
                changed = True

            if changed:
                self._table.blockSignals(True)
                for col in range(11):
                    self._table.item(0, col).setText(str(self._left_htl[col]))
                    self._table.item(1, col).setText(str(self._right_htl[col]))
                self._table.blockSignals(False)
                self._update_gains()
                logger.info("照片识别数据已应用")

    def _update_gains(self):
        """调用 NAL-NL2 计算并更新 DSP"""
        try:
            result = calculate_gains(
                self._left_htl, self._right_htl,
                num_aids=self._config['num_aids'],
                gender=self._config['gender'],
                experience=self._config['experience'],
                age=self._config['age'],
            )

            left_gains = get_gain_array(result, 'left')
            right_gains = get_gain_array(result, 'right')
            left_crs = get_cr_array(result, 'left')
            right_crs = get_cr_array(result, 'right')

            self._stream.set_audiogram(self._left_htl, self._right_htl,
                                       self._config)

            # 显示增益信息
            max_gain = max(max(left_gains), max(right_gains))
            min_cr = min(min(left_crs), min(right_crs))
            max_cr = max(max(left_crs), max(right_crs))

            info_text = (
                f"左耳增益: {', '.join(f'{g:.0f}' for g in left_gains)} dB  |  "
                f"右耳增益: {', '.join(f'{g:.0f}' for g in right_gains)} dB  |  "
                f"压缩比范围: {min_cr:.1f}–{max_cr:.1f}:1"
            )
            self._gain_info_label.setText(info_text)

            # 绘制增益曲线
            self._draw_gain_curve(left_gains, right_gains)

        except Exception as e:
            logger.error(f"增益计算失败: {e}")
            QMessageBox.warning(self, "计算错误", str(e))

    def _draw_gain_curve(self, left_gains: list, right_gains: list):
        """在 matplotlib canvas 上绘制增益曲线"""
        self._ax.clear()
        self._ax.set_facecolor('#0d1117')
        self._ax.tick_params(colors='#8b949e', labelsize=8)
        self._ax.spines['bottom'].set_color('#30363d')
        self._ax.spines['left'].set_color('#30363d')
        self._ax.spines['top'].set_visible(False)
        self._ax.spines['right'].set_visible(False)
        self._ax.set_xticks(range(11))
        self._ax.set_xticklabels(C.FREQ_LABELS)
        self._ax.set_ylabel('Gain (dB)', color='#8b949e', fontsize=8)
        self._ax.grid(True, color='#21262d', linewidth=0.5)

        max_gain = max(max(left_gains), max(right_gains), 5)
        self._ax.set_ylim(0, max_gain + 10)

        x = range(11)
        self._ax.plot(x, left_gains, 'o-', color='#58a6ff', linewidth=2,
                      markersize=6, label='Left Ear', zorder=5)
        self._ax.plot(x, right_gains, 's--', color='#f85149', linewidth=2,
                      markersize=6, label='Right Ear', zorder=5)
        self._ax.legend(loc='upper left', fontsize=8,
                        facecolor='#161b22', edgecolor='#30363d',
                        labelcolor='#e6edf3')
        self._canvas.draw()

    def _start_processing(self):
        self._update_gains()

        # 使用用户选择的输出设备
        selected_id = self._device_combo.currentData()
        if selected_id is not None and selected_id >= 0:
            self._stream._output_id = selected_id
            logger.info(f"使用输出设备: [{selected_id}]")

        if self._stream.start():
            self._btn_start.setVisible(False)
            self._btn_stop.setVisible(True)
            self._status_label.setText("● 运行中")
            self._status_label.setStyleSheet("color: #3fb950;")
        else:
            QMessageBox.warning(
                self, "启动失败",
                "无法启动音频处理。\n请检查音频设备连接。"
            )

    def _stop_processing(self):
        self._stream.stop()
        self._btn_start.setVisible(True)
        self._btn_stop.setVisible(False)
        self._status_label.setText("● 已停止")
        self._status_label.setStyleSheet("color: #8b949e;")
        self._latency_label.setText("延迟: —")
        self._cpu_label.setText("CPU: —")
        self._peak_label.setText("峰值: —")

    def _update_status(self):
        if self._stream.is_active:
            stats = self._stream.stats
            lat = stats['latency_ms']
            if isinstance(lat, (tuple, list)):
                lat = sum(lat) / len(lat)
            self._latency_label.setText(f"延迟: {lat:.0f}ms")
            self._peak_label.setText(f"峰值: {stats['peak_input_db']:.1f}dB")
            self._cpu_label.setText(f"速率: ~{stats['frame_count']}fps")

    def _refresh_devices(self):
        """刷新输出设备下拉列表 — 仅显示与输入同 API 的物理设备"""
        self._device_combo.clear()
        try:
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
            default_output = sd.default.device[1] if sd.default.device[1] >= 0 else None

            # 确定输入设备的 hostapi (只显示兼容的设备)
            from audio.loopback import find_input_device
            try:
                input_id, _ = find_input_device()
                target_api = devices[input_id]['hostapi']
            except:
                target_api = None

            added_names = set()
            for i, d in enumerate(devices):
                if d['max_output_channels'] < 2:
                    continue
                if target_api is not None and d['hostapi'] != target_api:
                    continue  # 跳过不兼容的 API
                name = d['name'].lower()
                if any(w in name for w in ('cable', 'vb-audio', 'loopback', 'sonar', 'vad', 'digital', 'spdif')):
                    continue
                if name in added_names:
                    continue
                added_names.add(name)
                label = f"[{i}] {d['name'][:45]}"
                self._device_combo.addItem(label, i)
                if i == default_output:
                    self._device_combo.setCurrentIndex(self._device_combo.count() - 1)

            if self._device_combo.count() == 0:
                self._device_combo.addItem("无兼容设备", -1)
        except Exception as e:
            logger.warning(f"刷新设备列表失败: {e}")
            self._device_combo.addItem("默认设备", -1)

    def closeEvent(self, event):
        """窗口关闭时停止处理"""
        if self._stream.is_active:
            reply = QMessageBox.question(
                self, "确认退出",
                "音频处理正在运行，确定要退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return

        self._stop_processing()
        self._timer.stop()
        event.accept()


# ── 命令行入口 ──

def main():
    import sounddevice as sd

    if '--list' in sys.argv:
        lb.list_devices()
        return

    if '--preset' in sys.argv:
        try:
            idx = sys.argv.index('--preset')
            preset_name = sys.argv[idx + 1]
        except (ValueError, IndexError):
            preset_name = 'custom'
    else:
        preset_name = 'custom'

    uniform_test = '--uniform-test' in sys.argv

    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setStyleSheet(DARK_STYLE)

    window = MainWindow()
    if preset_name in C.PRESETS:
        window._apply_preset(preset_name)
    if uniform_test:
        window._stream.uniform_test = True
        logger.info("均匀增益测试模式: 所有频率应用相同增益")
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
