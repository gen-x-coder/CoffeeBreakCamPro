import os
import sys
import time
import json

# Onderdruk OpenCV terminal waarschuwingen
os.environ["OPENCV_LOG_LEVEL"] = "OFF"

import cv2
import numpy as np
import pyvirtualcam

try:
    cv2.setLogLevel(0)
except AttributeError:
    pass

from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                               QPushButton, QLabel, QComboBox, QSlider, QCheckBox,
                               QTabWidget, QGroupBox, QGridLayout, QMessageBox,
                               QSizeGrip, QFrame, QMenu, QScrollArea)
from PySide6.QtCore import Qt, QTimer, QEvent
from PySide6.QtGui import QImage, QPixmap

VERSION = "v0.1-dev2"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

try:
    from pygrabber.dshow_graph import FilterGraph
    HAS_PYGRABBER = True
except ImportError:
    HAS_PYGRABBER = False

BUTTON_SIZE = 48
RESIZE_MARGIN = 8


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
    except Exception:
        pass


def draw_spinner_frame(width, height, angle, text="Verbinden met camera..."):
    frame = np.full((height, width, 3), 20, dtype=np.uint8)
    center = (width // 2, height // 2 - 15)
    radius = min(width, height) // 12
    thickness = max(3, radius // 5)

    cv2.circle(frame, center, radius, (45, 45, 45), thickness, cv2.LINE_AA)
    cv2.ellipse(frame, center, (radius, radius), angle, 0, 270, (245, 180, 60), thickness, cv2.LINE_AA)

    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.55
    text_size, _ = cv2.getTextSize(text, font, font_scale, 1)
    text_x = (width - text_size[0]) // 2
    text_y = center[1] + radius + 40
    cv2.putText(frame, text, (text_x, text_y), font, font_scale, (200, 200, 200), 1, cv2.LINE_AA)

    return frame


def draw_no_camera_frame(width, height):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    center = (width // 2, height // 2 - 20)

    cam_w, cam_h = 110, 75
    top_left = (center[0] - cam_w // 2, center[1] - cam_h // 2)
    bottom_right = (center[0] + cam_w // 2, center[1] + cam_h // 2)
    cv2.rectangle(frame, top_left, bottom_right, (180, 180, 180), 3, cv2.LINE_AA)

    bump_tl = (center[0] - 20, top_left[1] - 14)
    bump_br = (center[0] + 20, top_left[1])
    cv2.rectangle(frame, bump_tl, bump_br, (180, 180, 180), -1)

    cv2.circle(frame, center, 24, (180, 180, 180), 3, cv2.LINE_AA)

    slash_start = (center[0] - 70, center[1] - 55)
    slash_end = (center[0] + 70, center[1] + 55)
    cv2.line(frame, slash_start, slash_end, (60, 60, 220), 6, cv2.LINE_AA)

    text = "Camera niet gevonden"
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.6
    text_size, _ = cv2.getTextSize(text, font, font_scale, 1)
    text_x = (width - text_size[0]) // 2
    text_y = center[1] + cam_h // 2 + 45
    cv2.putText(frame, text, (text_x, text_y), font, font_scale, (180, 180, 180), 1, cv2.LINE_AA)

    return frame


def detect_cameras():
    cams = []
    if HAS_PYGRABBER:
        try:
            graph = FilterGraph()
            devices = graph.get_input_devices()
            for idx, name in enumerate(devices):
                cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
                if cap.isOpened():
                    ret, _ = cap.read()
                    if ret:
                        cams.append((idx, name))
                    cap.release()
            if cams:
                return cams
        except Exception:
            pass

    for index in range(5):
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if cap.isOpened():
            ret, _ = cap.read()
            if ret:
                cams.append((index, f"Camera {index}"))
            cap.release()
    return cams


class CoffeeBreakCam(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CoffeeBreak Cam Pro")

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)

        self.camera_list = detect_cameras()
        if not self.camera_list:
            QMessageBox.critical(self, "Fout", "Geen werkende webcam(s) gevonden.")
            sys.exit()

        self.current_cam_index = self.camera_list[0][0]
        self.cap = cv2.VideoCapture(self.current_cam_index, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            QMessageBox.critical(self, "Fout", "Kon geselecteerde camera niet openen.")
            sys.exit()

        self.cam_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
        self.cam_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480
        self.fps = 30

        try:
            self.vcam = pyvirtualcam.Camera(
                width=self.cam_width, height=self.cam_height, fps=self.fps
            )
        except Exception as e:
            QMessageBox.critical(
                self,
                "Virtual Cam Fout",
                f"Kon virtuele camera niet starten.\nZorg dat OBS Virtual Cam is geïnstalleerd.\n\nDetails: {e}"
            )
            sys.exit()

        self.is_expanded = False
        self.mode = "LIVE"
        self.frozen_frame = None
        self.spinner_angle = 0

        self.is_recording_loop = False
        self.recorded_loop_frames = []
        self.loop_play_index = 0

        self.frame_counter = 0
        self.last_fps_frame = None
        self.stutter_start_time = time.time()
        self.stutter_frozen_frame = None

        self._drag_start = None
        self._drag_window_start = None
        self._dragging = False

        self._build_ui()
        self.apply_dark_theme()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_frame)
        self.timer.start(33)

        self.set_collapsed_size()
        self.restore_saved_position()

    def restore_saved_position(self):
        config = load_config()
        if "pos_x" in config and "pos_y" in config:
            self.move(config["pos_x"], config["pos_y"])
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.x() + screen.width() - BUTTON_SIZE - 20, screen.y() + 20)

    def clamp_to_screen(self):
        screen = QApplication.screenAt(self.geometry().center()) or QApplication.primaryScreen()
        geo = screen.availableGeometry()

        x = max(geo.left(), min(self.x(), geo.right() - self.width()))
        y = max(geo.top(), min(self.y(), geo.bottom() - self.height()))
        self.move(x, y)

        cfg = load_config()
        cfg["pos_x"] = self.x()
        cfg["pos_y"] = self.y()
        save_config(cfg)

    def _get_resize_edges(self, pos):
        edges = Qt.Edge(0)
        rect = self.rect()
        if pos.x() <= RESIZE_MARGIN:
            edges |= Qt.Edge.LeftEdge
        elif pos.x() >= rect.width() - RESIZE_MARGIN:
            edges |= Qt.Edge.RightEdge

        if pos.y() <= RESIZE_MARGIN:
            edges |= Qt.Edge.TopEdge
        elif pos.y() >= rect.height() - RESIZE_MARGIN:
            edges |= Qt.Edge.BottomEdge
        return edges

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.is_expanded:
            edges = self._get_resize_edges(event.position().toPoint())
            if edges != Qt.Edge(0):
                if self.windowHandle():
                    self.windowHandle().startSystemResize(edges)
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.is_expanded:
            edges = self._get_resize_edges(event.position().toPoint())
            if edges == (Qt.Edge.LeftEdge | Qt.Edge.TopEdge) or edges == (Qt.Edge.RightEdge | Qt.Edge.BottomEdge):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif edges == (Qt.Edge.RightEdge | Qt.Edge.TopEdge) or edges == (Qt.Edge.LeftEdge | Qt.Edge.BottomEdge):
                self.setCursor(Qt.CursorShape.SizeBDiagCursor)
            elif edges in (Qt.Edge.LeftEdge, Qt.Edge.RightEdge):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif edges in (Qt.Edge.TopEdge, Qt.Edge.BottomEdge):
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            else:
                self.unsetCursor()
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def _build_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        self.toggle_btn = QPushButton("☕")
        self.toggle_btn.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        self.toggle_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.toggle_btn.setToolTip("Klik: Openen/Sluiten · Slepen: Verplaatsen · Rechtsklik: Afsluiten")
        self.toggle_btn.installEventFilter(self)
        self.toggle_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.toggle_btn.customContextMenuRequested.connect(self.show_context_menu)
        self.toggle_btn.clicked.connect(self.toggle_window)
        self.main_layout.addWidget(self.toggle_btn, alignment=Qt.AlignmentFlag.AlignRight)

        self.control_container = QFrame()
        self.control_container.setObjectName("ControlPanel")
        container_layout = QVBoxLayout(self.control_container)
        container_layout.setContentsMargins(10, 10, 10, 10)

        self.status_label = QLabel("STATUS: LIVE WEBCAM")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #81c784; padding: 6px; background-color: #1e1e1e; border-radius: 6px;")
        container_layout.addWidget(self.status_label)

        self.tabs = QTabWidget()
        container_layout.addWidget(self.tabs)

        self.tab_main = self._make_scrollable_tab(self._build_tab_main)
        self.tab_settings = self._make_scrollable_tab(self._build_tab_settings)

        self.tabs.addTab(self.tab_main, " Bediening ")
        self.tabs.addTab(self.tab_settings, " Instellingen ")

        # Statusbalk onderin
        footer_layout = QHBoxLayout()
        self.version_label = QLabel(f"CoffeeBreak Cam Pro {VERSION}")
        self.version_label.setStyleSheet("color: #777777; font-size: 11px; font-weight: normal;")
        footer_layout.addWidget(self.version_label)
        footer_layout.addStretch()

        self.size_grip = QSizeGrip(self)
        footer_layout.addWidget(self.size_grip)
        container_layout.addLayout(footer_layout)

        self.control_container.hide()
        self.main_layout.addWidget(self.control_container)
        self.update_button_style()

    def _make_scrollable_tab(self, builder_func):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        content = QWidget()
        builder_func(content)
        scroll.setWidget(content)
        return scroll

    def show_context_menu(self, pos):
        menu = QMenu(self)
        exit_action = menu.addAction("❌ Afsluiten")
        action = menu.exec(self.toggle_btn.mapToGlobal(pos))
        if action == exit_action:
            QApplication.quit()

    def _build_tab_main(self, parent_widget):
        layout = QVBoxLayout(parent_widget)

        cam_layout = QHBoxLayout()
        cam_layout.addWidget(QLabel("Camera:"))
        self.cam_combobox = QComboBox()
        for idx, name in self.camera_list:
            self.cam_combobox.addItem(f"[{idx}] {name}", idx)
        self.cam_combobox.currentIndexChanged.connect(self.on_camera_change)
        cam_layout.addWidget(self.cam_combobox, stretch=1)
        layout.addLayout(cam_layout)

        self.preview_label = QLabel()
        self.preview_label.setMinimumSize(320, 180)
        self.preview_label.setStyleSheet("background-color: #000000; border-radius: 6px;")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.preview_label, alignment=Qt.AlignmentFlag.AlignCenter)

        mode_layout = QHBoxLayout()
        self.btn_live = QPushButton("LIVE")
        self.btn_live.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; padding: 10px;")
        self.btn_live.clicked.connect(self.set_mode_live)

        self.btn_freeze = QPushButton("BEVRIES")
        self.btn_freeze.setStyleSheet("background-color: #d32f2f; color: white; font-weight: bold; padding: 10px;")
        self.btn_freeze.clicked.connect(self.set_mode_freeze)

        self.btn_play_loop = QPushButton("SPEEL LOOP")
        self.btn_play_loop.setStyleSheet("background-color: #0288d1; color: white; font-weight: bold; padding: 10px;")
        self.btn_play_loop.clicked.connect(self.set_mode_loop)

        mode_layout.addWidget(self.btn_live)
        mode_layout.addWidget(self.btn_freeze)
        mode_layout.addWidget(self.btn_play_loop)
        layout.addLayout(mode_layout)

        preset_group = QGroupBox(" Snelle instellingen ")
        preset_grid = QGridLayout(preset_group)

        btn_connecting = QPushButton("Verbinden met camera")
        btn_connecting.clicked.connect(self.set_mode_connecting)

        btn_no_cam = QPushButton("Camera niet gevonden")
        btn_no_cam.clicked.connect(self.set_mode_no_camera)

        btn_bad_conn = QPushButton("Slechte verbinding")
        btn_bad_conn.clicked.connect(self.apply_preset_bad_connection)

        btn_poor_wifi = QPushButton("Matige wifi / haperend")
        btn_poor_wifi.clicked.connect(self.apply_preset_poor_wifi)

        btn_old_cam = QPushButton("Oude webcam / low-res")
        btn_old_cam.clicked.connect(self.apply_preset_old_webcam)

        btn_reset = QPushButton("Standaard instellingen")
        btn_reset.clicked.connect(self.apply_preset_reset)

        preset_grid.addWidget(btn_connecting, 0, 0)
        preset_grid.addWidget(btn_no_cam, 0, 1)
        preset_grid.addWidget(btn_bad_conn, 1, 0)
        preset_grid.addWidget(btn_poor_wifi, 1, 1)
        preset_grid.addWidget(btn_old_cam, 2, 0)
        preset_grid.addWidget(btn_reset, 2, 1)
        layout.addWidget(preset_group)

        rec_group = QGroupBox(" Video Loop Opnemen ")
        rec_layout = QVBoxLayout(rec_group)

        self.btn_rec_loop = QPushButton("● START OPNAME LOOP")
        self.btn_rec_loop.setStyleSheet("background-color: #c62828; color: white; font-weight: bold; padding: 8px;")
        self.btn_rec_loop.clicked.connect(self.toggle_record_loop)
        rec_layout.addWidget(self.btn_rec_loop)

        self.lbl_loop_info = QLabel("Geen loop opgenomen")
        self.lbl_loop_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_loop_info.setStyleSheet("color: #aaaaaa;")
        rec_layout.addWidget(self.lbl_loop_info)

        layout.addWidget(rec_group)

    def _build_tab_settings(self, parent_widget):
        layout = QVBoxLayout(parent_widget)
        layout.setSpacing(12)

        artifacts_group = QGroupBox(" Video Storingen ")
        art_layout = QVBoxLayout(artifacts_group)

        def create_slider_row(title, min_v, max_v, default_v, target_layout, step=1):
            lbl = QLabel(f"{title}: {default_v}")
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(min_v, max_v)
            slider.setValue(default_v)
            if step != 1:
                slider.setSingleStep(step)
            slider.valueChanged.connect(lambda v: lbl.setText(f"{title}: {v / 10 if step < 1 else v}"))
            target_layout.addWidget(lbl)
            target_layout.addWidget(slider)
            return slider, lbl

        self.lowres_slider, _ = create_slider_row("Resolutie Verlagen (320x240 / Low-Res)", 1, 10, 1, art_layout)
        self.block_slider, _ = create_slider_row("Slechte Verbinding (Blokjes/Pixelation)", 1, 35, 1, art_layout)
        self.noise_slider, _ = create_slider_row("Sensor Ruis Level", 0, 40, 0, art_layout)
        self.flicker_slider, _ = create_slider_row("Licht Flikkering", 0, 20, 0, art_layout)
        self.fps_slider, _ = create_slider_row("Framerate Daling (FPS Limit)", 1, 30, 30, art_layout)

        self.glitch_lbl = QLabel("Line Glitch Intensiteit: 0.0")
        self.glitch_slider = QSlider(Qt.Orientation.Horizontal)
        self.glitch_slider.setRange(0, 100)
        self.glitch_slider.setValue(0)
        self.glitch_slider.valueChanged.connect(lambda v: self.glitch_lbl.setText(f"Line Glitch Intensiteit: {v / 10.0:.1f}"))
        art_layout.addWidget(self.glitch_lbl)
        art_layout.addWidget(self.glitch_slider)

        self.auto_exp_cb = QCheckBox("Subtiele Auto-Exposure Simuleren")
        art_layout.addWidget(self.auto_exp_cb)

        self.apply_live_cb = QCheckBox("Pas effecten ook toe op Live webcam-stream")
        self.apply_live_cb.setChecked(True)
        art_layout.addWidget(self.apply_live_cb)

        layout.addWidget(artifacts_group)

        stutter_group = QGroupBox(" Periodiek Bevriezen ")
        stutter_layout = QVBoxLayout(stutter_group)

        self.stutter_enable_cb = QCheckBox("Inschakelen Periodiek Bevriezen / Haperen")
        self.stutter_enable_cb.setStyleSheet("font-size: 14px; font-weight: bold; color: #64b5f6;")
        stutter_layout.addWidget(self.stutter_enable_cb)

        self.stutter_spinner_cb = QCheckBox("Toon laadscherm (spinner) i.p.v. bevroren beeld")
        self.stutter_spinner_cb.setStyleSheet("color: #ffb74d; font-weight: bold;")
        stutter_layout.addWidget(self.stutter_spinner_cb)

        self.stutter_interval_lbl = QLabel("Interval tussen haperingen: 10 seconden")
        self.stutter_interval_slider = QSlider(Qt.Orientation.Horizontal)
        self.stutter_interval_slider.setRange(1, 60)
        self.stutter_interval_slider.setValue(10)
        self.stutter_interval_slider.valueChanged.connect(lambda v: self.stutter_interval_lbl.setText(f"Interval tussen haperingen: {v} seconden"))
        stutter_layout.addWidget(self.stutter_interval_lbl)
        stutter_layout.addWidget(self.stutter_interval_slider)

        self.stutter_duration_lbl = QLabel("Duur van hapering: 2.0 seconden")
        self.stutter_duration_slider = QSlider(Qt.Orientation.Horizontal)
        self.stutter_duration_slider.setRange(5, 100)
        self.stutter_duration_slider.setValue(20)
        self.stutter_duration_slider.valueChanged.connect(lambda v: self.stutter_duration_lbl.setText(f"Duur van hapering: {v / 10.0:.1f} seconden"))
        stutter_layout.addWidget(self.stutter_duration_lbl)
        stutter_layout.addWidget(self.stutter_duration_slider)

        layout.addWidget(stutter_group)
        layout.addStretch()

    def apply_dark_theme(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #1e1e1e;
                color: #ffffff;
                font-family: 'Segoe UI', Arial, sans-serif;
            }
            QFrame#ControlPanel {
                background-color: #252526;
                border: 1px solid #3c3c3c;
                border-radius: 12px;
            }
            QTabWidget::pane {
                border: 1px solid #3c3c3c;
                background: #252526;
                border-radius: 6px;
            }
            QTabBar::tab {
                background: #2d2d2d;
                color: #ffffff;
                padding: 8px 16px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
            }
            QTabBar::tab:selected {
                background: #3c3c3c;
                color: #64b5f6;
                font-weight: bold;
            }
            QGroupBox {
                border: 1px solid #3c3c3c;
                border-radius: 6px;
                margin-top: 10px;
                font-weight: bold;
                color: #64b5f6;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QPushButton {
                background-color: #333333;
                border: none;
                border-radius: 4px;
                padding: 6px;
                color: white;
            }
            QPushButton:hover {
                background-color: #444444;
            }
            QSlider::groove:horizontal {
                height: 6px;
                background: #3c3c3c;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #64b5f6;
                width: 14px;
                margin-top: -4px;
                margin-bottom: -4px;
                border-radius: 7px;
            }
            QScrollBar:vertical {
                border: none;
                background: #1e1e1e;
                width: 10px;
                margin: 0px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background: #4e4e50;
                min-height: 20px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical:hover {
                background: #64b5f6;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)

    def update_button_style(self):
        color = "#2e7d32"
        if self.mode in ("FROZEN", "NO_CAMERA"):
            color = "#d32f2f"
        elif self.mode == "LOOP":
            color = "#0288d1"
        elif self.mode == "CONNECTING" or self.stutter_enable_cb.isChecked() or self.block_slider.value() > 1:
            color = "#e65100"

        self.toggle_btn.setStyleSheet(f"""
            QPushButton {{
                border-radius: {BUTTON_SIZE // 2}px;
                border: 3px solid {color};
                background-color: #2b2b2b;
                font-size: 22px;
            }}
            QPushButton:hover {{
                background-color: #3d3d3d;
            }}
        """)

    def eventFilter(self, obj, event):
        if obj is self.toggle_btn:
            t = event.type()
            if t == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_start = event.globalPosition().toPoint()
                self._drag_window_start = self.pos()
                self._dragging = False
            elif t == QEvent.Type.MouseMove and self._drag_start is not None and (event.buttons() & Qt.MouseButton.LeftButton):
                delta = event.globalPosition().toPoint() - self._drag_start
                if not self._dragging and delta.manhattanLength() >= QApplication.startDragDistance():
                    self._dragging = True
                    self.toggle_btn.setDown(False)
                if self._dragging:
                    self.move(self._drag_window_start + delta)
                    return True
            elif t == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                was_dragging = self._dragging
                self._drag_start = None
                self._dragging = False
                if was_dragging:
                    self.toggle_btn.setDown(False)
                    self.clamp_to_screen()
                    return True
        return super().eventFilter(obj, event)

    def set_collapsed_size(self):
        self.control_container.hide()
        self.setMinimumSize(BUTTON_SIZE, BUTTON_SIZE)
        self.setMaximumSize(BUTTON_SIZE, BUTTON_SIZE)
        self.resize(BUTTON_SIZE, BUTTON_SIZE)

    def set_expanded_size(self):
        self.setMaximumSize(16777215, 16777215)
        self.setMinimumSize(420, 480)
        self.resize(560, 700)
        self.control_container.show()

    def toggle_window(self):
        right = self.x() + self.width()
        top = self.y()
        self.is_expanded = not self.is_expanded

        if self.is_expanded:
            self.set_expanded_size()
            self.raise_()
        else:
            self.set_collapsed_size()

        self.move(right - self.width(), top)
        self.clamp_to_screen()

    def set_mode_live(self):
        self.mode = "LIVE"
        self.status_label.setText("STATUS: LIVE WEBCAM")
        self.status_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #81c784; padding: 6px; background-color: #1e1e1e; border-radius: 6px;")
        self.update_button_style()

    def set_mode_freeze(self):
        ret, frame = self.cap.read()
        if ret:
            self.frozen_frame = cv2.resize(frame, (self.cam_width, self.cam_height)).copy()
            self.mode = "FROZEN"
            self.status_label.setText("STATUS: BEVROREN BEELD")
            self.status_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #e57373; padding: 6px; background-color: #1e1e1e; border-radius: 6px;")
            self.update_button_style()

    def set_mode_loop(self):
        if not self.recorded_loop_frames:
            QMessageBox.warning(self, "Geen Loop", "Neem eerst een video loop op via de knop onderaan.")
            return
        self.mode = "LOOP"
        self.loop_play_index = 0
        self.status_label.setText("STATUS: VIDEO LOOP AFSPELEN")
        self.status_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #64b5f6; padding: 6px; background-color: #1e1e1e; border-radius: 6px;")
        self.update_button_style()

    def set_mode_connecting(self):
        self.mode = "CONNECTING"
        self.status_label.setText("STATUS: VERBINDEN MET CAMERA...")
        self.status_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #ffb74d; padding: 6px; background-color: #1e1e1e; border-radius: 6px;")
        self.update_button_style()

    def set_mode_no_camera(self):
        self.mode = "NO_CAMERA"
        self.status_label.setText("STATUS: CAMERA NIET GEVONDEN")
        self.status_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #e57373; padding: 6px; background-color: #1e1e1e; border-radius: 6px;")
        self.update_button_style()

    def apply_preset_bad_connection(self):
        self.mode = "LIVE"
        self.status_label.setText("STATUS: SLECHTE VERBINDING")
        self.status_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #ffb74d; padding: 6px; background-color: #1e1e1e; border-radius: 6px;")

        self.lowres_slider.setValue(2)
        self.block_slider.setValue(4)
        self.noise_slider.setValue(0)
        self.flicker_slider.setValue(0)
        self.fps_slider.setValue(5)
        self.glitch_slider.setValue(0)
        self.auto_exp_cb.setChecked(False)

        self.stutter_enable_cb.setChecked(True)
        self.stutter_spinner_cb.setChecked(False)
        self.stutter_interval_slider.setValue(3)
        self.stutter_duration_slider.setValue(20)
        self.update_button_style()

    def apply_preset_poor_wifi(self):
        self.mode = "LIVE"
        self.status_label.setText("STATUS: MATIGE WIFI / HAPEREND")
        self.status_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #ffb74d; padding: 6px; background-color: #1e1e1e; border-radius: 6px;")

        self.lowres_slider.setValue(2)
        self.block_slider.setValue(2)
        self.noise_slider.setValue(0)
        self.flicker_slider.setValue(0)
        self.fps_slider.setValue(15)
        self.glitch_slider.setValue(4)
        self.auto_exp_cb.setChecked(False)

        self.stutter_enable_cb.setChecked(True)
        self.stutter_spinner_cb.setChecked(True)
        self.stutter_interval_slider.setValue(8)
        self.stutter_duration_slider.setValue(15)
        self.update_button_style()

    def apply_preset_old_webcam(self):
        self.mode = "LIVE"
        self.status_label.setText("STATUS: OUDE WEBCAM / LOW-RES")
        self.status_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #d7ccc8; padding: 6px; background-color: #1e1e1e; border-radius: 6px;")

        self.lowres_slider.setValue(3)
        self.block_slider.setValue(1)
        self.noise_slider.setValue(10)
        self.flicker_slider.setValue(0)
        self.fps_slider.setValue(18)
        self.glitch_slider.setValue(0)
        self.auto_exp_cb.setChecked(False)
        self.stutter_enable_cb.setChecked(False)
        self.update_button_style()

    def apply_preset_reset(self):
        self.lowres_slider.setValue(1)
        self.block_slider.setValue(1)
        self.noise_slider.setValue(0)
        self.flicker_slider.setValue(0)
        self.fps_slider.setValue(30)
        self.glitch_slider.setValue(0)
        self.auto_exp_cb.setChecked(False)
        self.stutter_enable_cb.setChecked(False)
        self.stutter_spinner_cb.setChecked(False)
        self.set_mode_live()

    def toggle_record_loop(self):
        if not self.is_recording_loop:
            self.is_recording_loop = True
            self.recorded_loop_frames = []
            self.btn_rec_loop.setText("■ STOP OPNAME")
            self.btn_rec_loop.setStyleSheet("background-color: #d32f2f; color: white; font-weight: bold; padding: 8px;")
            self.lbl_loop_info.setText("Bezig met opnemen...")
        else:
            self.is_recording_loop = False
            self.btn_rec_loop.setText("● START OPNAME LOOP")
            self.btn_rec_loop.setStyleSheet("background-color: #c62828; color: white; font-weight: bold; padding: 8px;")
            count = len(self.recorded_loop_frames)
            duration = count / self.fps
            if count > 0:
                self.lbl_loop_info.setText(f"Loop opgeslagen: {count} frames ({duration:.1f} sec)")
            else:
                self.lbl_loop_info.setText("Opname mislukt of te kort")

    def on_camera_change(self, index):
        new_cam_idx = self.cam_combobox.currentData()
        if new_cam_idx is not None and new_cam_idx != self.current_cam_index:
            self.cap.release()
            self.current_cam_index = new_cam_idx
            self.cap = cv2.VideoCapture(self.current_cam_index, cv2.CAP_DSHOW)
            if self.mode == "FROZEN":
                self.set_mode_live()

    def apply_effects(self, frame):
        processed = frame.copy()
        h, w = processed.shape[:2]

        lowres = self.lowres_slider.value()
        if lowres > 1:
            small_w, small_h = max(1, w // lowres), max(1, h // lowres)
            small = cv2.resize(processed, (small_w, small_h), interpolation=cv2.INTER_LINEAR)
            processed = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)

        block = self.block_slider.value()
        if block > 1:
            small_w, small_h = max(1, w // block), max(1, h // block)
            small = cv2.resize(processed, (small_w, small_h), interpolation=cv2.INTER_LINEAR)
            processed = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)

        glitch_lvl = self.glitch_slider.value() / 10.0
        if glitch_lvl > 0.0:
            chance = min(1.0, glitch_lvl * 0.15) if glitch_lvl < 1.0 else 0.4
            iterations = max(1, int(glitch_lvl))
            for _ in range(iterations):
                if np.random.rand() < chance:
                    slice_h = np.random.randint(2, max(4, int(8 * glitch_lvl)))
                    y = np.random.randint(0, max(1, h - slice_h))
                    max_shift = max(2, int(12 * glitch_lvl))
                    shift = np.random.randint(-max_shift, max_shift)
                    processed[y:y+slice_h] = np.roll(processed[y:y+slice_h], shift, axis=1)

        noise_lvl = self.noise_slider.value()
        flicker_lvl = self.flicker_slider.value()
        if noise_lvl > 0 or flicker_lvl > 0:
            noisy = processed.astype(np.float32)
            if noise_lvl > 0:
                noisy += np.random.normal(0, noise_lvl, frame.shape).astype(np.float32)
            if flicker_lvl > 0:
                noisy += np.random.uniform(-flicker_lvl, flicker_lvl)
            processed = np.clip(noisy, 0, 255).astype(np.uint8)

        if self.auto_exp_cb.isChecked() and np.random.rand() < 0.02:
            shift = np.random.choice([-12, -8, 8, 12])
            processed = np.clip(processed.astype(np.int16) + shift, 0, 255).astype(np.uint8)

        return processed

    def update_frame(self):
        self.spinner_angle = (self.spinner_angle + 12) % 360

        ret, live_frame = self.cap.read()

        if ret:
            live_frame = cv2.resize(live_frame, (self.cam_width, self.cam_height))

            if self.is_recording_loop:
                self.recorded_loop_frames.append(live_frame.copy())
                count = len(self.recorded_loop_frames)
                self.lbl_loop_info.setText(f"Opnemen... {count} frames ({count / self.fps:.1f}s)")

            if self.mode == "CONNECTING":
                source_frame = draw_spinner_frame(self.cam_width, self.cam_height, self.spinner_angle, "Verbinden met camera...")
            elif self.mode == "NO_CAMERA":
                source_frame = draw_no_camera_frame(self.cam_width, self.cam_height)
            elif self.mode == "FROZEN" and self.frozen_frame is not None:
                source_frame = self.frozen_frame
            elif self.mode == "LOOP" and self.recorded_loop_frames:
                source_frame = self.recorded_loop_frames[self.loop_play_index]
                self.loop_play_index = (self.loop_play_index + 1) % len(self.recorded_loop_frames)
            else:
                source_frame = live_frame

            if self.mode not in ("CONNECTING", "NO_CAMERA") and self.stutter_enable_cb.isChecked():
                now = time.time()
                interval = self.stutter_interval_slider.value()
                duration = self.stutter_duration_slider.value() / 10.0
                cycle_time = interval + duration

                phase = (now - self.stutter_start_time) % cycle_time
                if phase > interval:
                    if self.stutter_spinner_cb.isChecked():
                        source_frame = draw_spinner_frame(self.cam_width, self.cam_height, self.spinner_angle, "Bufferen...")
                    else:
                        if self.stutter_frozen_frame is None:
                            self.stutter_frozen_frame = source_frame.copy()
                        source_frame = self.stutter_frozen_frame
                else:
                    self.stutter_frozen_frame = None

            if self.mode not in ("CONNECTING", "NO_CAMERA") and (self.mode != "LIVE" or self.apply_live_cb.isChecked()):
                processed_frame = self.apply_effects(source_frame)
            else:
                processed_frame = source_frame

            target_fps = self.fps_slider.value()
            if target_fps < 30 and self.mode not in ("CONNECTING", "NO_CAMERA"):
                skip_interval = max(1, int(30 / target_fps))
                self.frame_counter += 1
                if self.frame_counter % skip_interval != 0 and self.last_fps_frame is not None:
                    final_frame = self.last_fps_frame
                else:
                    self.last_fps_frame = processed_frame.copy()
                    final_frame = processed_frame
            else:
                final_frame = processed_frame

            frame_rgb = cv2.cvtColor(final_frame, cv2.COLOR_BGR2RGB)
            self.vcam.send(frame_rgb)

            if self.is_expanded:
                h, w, ch = frame_rgb.shape
                bytes_per_line = ch * w
                q_img = QImage(frame_rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
                pixmap = QPixmap.fromImage(q_img)
                
                target_size = self.preview_label.size()
                if target_size.width() > 10 and target_size.height() > 10:
                    self.preview_label.setPixmap(
                        pixmap.scaled(target_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                    )

    def closeEvent(self, event):
        self.timer.stop()
        if self.cap.isOpened():
            self.cap.release()
        self.vcam.close()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = CoffeeBreakCam()
    window.show()
    sys.exit(app.exec())