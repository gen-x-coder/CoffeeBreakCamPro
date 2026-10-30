import os
import sys
import time
import json
import threading
import bisect
import atexit
import traceback
import faulthandler
from datetime import datetime

# Suppress OpenCV terminal warnings
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
                               QFrame, QMenu, QScrollArea)
from PySide6.QtCore import Qt, QTimer, QEvent, QObject, Signal
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor

VERSION = "v1.0.0"
OUTPUT_FPS = 30
TICK_TOLERANCE = 0.008                   # timer jitter allowance for frame pacing
MAX_LOOP_SECONDS = 30.0
MAX_LOOP_BYTES = 160 * 1024 * 1024      # safety net; ~20-30 MB is typical now
LOOP_RECORD_FPS = 15                     # halves memory; video calls rarely show more
LOOP_JPEG_QUALITY = 75
MAX_CAPTURE_WIDTH = 1280                 # resolutions above 720p are not offered
# Only used as fallback when Windows (pygrabber) cannot list the camera formats.
FALLBACK_RESOLUTIONS = [(1280, 720), (960, 540), (848, 480), (640, 480),
                        (640, 360), (424, 240), (320, 240)]
PREFERRED_OUTPUT = [(960, 540), (1280, 720), (640, 360), (640, 480)]
# Target heights for the "lower resolution" effect (index 0 = off) and matching JPEG quality.
LOWRES_STEPS = [0, 480, 360, 240, 144]
LOWRES_JPEG_Q = [0, 45, 35, 25, 18]
# Our own output device must never show up as an input (feedback loop).
IGNORED_CAMERA_NAMES = ("obs virtual camera", "unity video capture")

# In a PyInstaller .exe, __file__ points to a temporary folder: keep config and
# crash log next to the .exe instead, so settings survive a restart.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
CRASH_LOG = os.path.join(BASE_DIR, "crash.log")

try:
    # pygrabber generates COM wrappers on import; that can fail (e.g. in an .exe).
    # The app then falls back to the slower OpenCV camera scan.
    from pygrabber.dshow_graph import FilterGraph
    HAS_PYGRABBER = True
except Exception:
    HAS_PYGRABBER = False

BUTTON_SIZE = 48
RESIZE_MARGIN = 6          # invisible border around the expanded window used for resizing

# --------------------------------------------------------------------------- #
#  Translations
# --------------------------------------------------------------------------- #
TEXTS = {
    "en": {
        "toggle_tip": "Click: open/close · Drag: move · Right-click: quit",
        "menu_quit": "❌ Quit",
        "tab_main": " Controls ",
        "tab_effects": " Effects ",
        "tab_settings": " Settings ",
        "camera": "Camera:",
        "resolution": "Resolution:",
        "searching": "Searching for cameras…",
        "no_cameras": "No cameras found",
        "btn_live": "LIVE",
        "btn_freeze": "FREEZE",
        "btn_loop": "PLAY LOOP",
        "grp_presets": " Quick presets ",
        "p_connecting": "Connecting to camera",
        "p_no_camera": "Camera not found",
        "p_bad": "Bad connection",
        "p_wifi": "Poor wifi / stuttering",
        "p_partial": "Partial glitches",
        "p_old": "Old webcam / low-res",
        "p_flip": "Upside down",
        "p_reset": "Default settings",
        "grp_record": " Record video loop ",
        "rec_start": "● START RECORDING LOOP",
        "rec_stop": "■ STOP RECORDING",
        "loop_none": "No loop recorded",
        "loop_recording": "Recording… {t:.1f} / 30 s",
        "loop_saved": "Loop saved: {n} frames ({t:.1f} s, {mb:.1f} MB)",
        "loop_empty": "No frames recorded",
        "loop_max_hint": "Max 30 seconds; stops automatically.",
        "reason_max": "30 s reached",
        "reason_mem": "memory limit reached",
        "reason_cam": "camera switched",
        "output_error": "Video output error: {e}",
        "status_live": "STATUS: LIVE WEBCAM",
        "status_frozen": "STATUS: FROZEN IMAGE",
        "status_loop": "STATUS: PLAYING VIDEO LOOP",
        "status_connecting": "STATUS: CONNECTING TO CAMERA...",
        "status_no_camera": "STATUS: CAMERA NOT FOUND",
        "off": "off",
        "unit_s": "s",
        # Effects tab
        "grp_image": " Image ",
        "grp_connection": " Connection ",
        "grp_stutter": " Periodic freezing ",
        "s_lowres": "Resolution",
        "s_lowres_tip": "Drops the image to a lower resolution with compression artifacts, like an old webcam.",
        "s_blocks": "Blockiness",
        "s_blocks_tip": "Large square blocks, as with a bad connection.",
        "s_noise": "Sensor noise",
        "s_noise_tip": "Grainy image, as from a cheap sensor in a dark room.",
        "s_flicker": "Light flicker",
        "s_flicker_tip": "The brightness jitters slightly every frame.",
        "cb_autoexp": "Auto-exposure drift",
        "cb_autoexp_tip": "The brightness slowly drifts up and down, like a webcam adjusting itself.",
        "cb_flip": "Upside down",
        "cb_flip_tip": "Rotates the image 180°, like a driver that got confused.",
        "s_fps": "Frame rate",
        "s_fps_tip": "Limits the number of frames per second.",
        "s_glitch": "Line glitches",
        "s_glitch_tip": "Horizontal strips that shift sideways for a moment.",
        "s_partial": "Partial glitches",
        "s_partial_tip": ("Part of the image (bottom, a half, a corner or loose blocks) briefly "
                          "freezes or turns blocky, like lost video packets."),
        "cb_stutter": "Freeze the image every now and then",
        "cb_stutter_spinner": "Show a loading spinner instead of a frozen image",
        "s_stutter_interval": "Every",
        "s_stutter_duration": "For",
        # Settings tab
        "grp_general": " General ",
        "cb_apply_live": "Apply effects to the live webcam too",
        "cb_apply_live_tip": "When off, LIVE always shows your unmodified camera; effects only apply to the loop.",
        "language": "Language / Taal:",
        "grp_transition": " Switching && loop ",
        "cb_transition": "Freeze the image briefly when switching between live and loop",
        "s_transition": "Freeze for",
        "cb_pingpong": "Play the loop back and forth (hides the jump at the end)",
        # Video frames and messages
        "frame_connecting": "Connecting to camera...",
        "frame_no_camera": "Camera not found",
        "frame_buffering": "Buffering...",
        "msg_no_loop_title": "No loop",
        "msg_no_loop": "There is no recording yet.\n\nRecord a video loop first with the button at the bottom of the Controls tab.",
        "msg_vcam_title": "Virtual camera error",
        "msg_vcam": "Could not start the virtual camera. Check OBS Virtual Camera.\n\n{e}",
        "msg_res_title": "Resolution saved",
        "msg_res": ("The camera now records at {w}×{h}.\n\n"
                    "The virtual camera stays at {ow}×{oh} for now. Fully quit and "
                    "restart CoffeeBreak Cam Pro to switch the output to {w}×{h} as well."),
        "msg_crash_title": "Unexpected error",
        "msg_crash": ("CoffeeBreak Cam Pro ran into an unexpected error and has closed the "
                      "camera to be safe.\n\nDetails were written to:\n{path}"),
    },
    "nl": {
        "toggle_tip": "Klik: openen/sluiten · Slepen: verplaatsen · Rechtsklik: afsluiten",
        "menu_quit": "❌ Afsluiten",
        "tab_main": " Bediening ",
        "tab_effects": " Effecten ",
        "tab_settings": " Instellingen ",
        "camera": "Camera:",
        "resolution": "Resolutie:",
        "searching": "Camera's zoeken…",
        "no_cameras": "Geen camera's gevonden",
        "btn_live": "LIVE",
        "btn_freeze": "BEVRIES",
        "btn_loop": "SPEEL LOOP",
        "grp_presets": " Snelle instellingen ",
        "p_connecting": "Verbinden met camera",
        "p_no_camera": "Camera niet gevonden",
        "p_bad": "Slechte verbinding",
        "p_wifi": "Matige wifi / haperend",
        "p_partial": "Deels verstoord beeld",
        "p_old": "Oude webcam / low-res",
        "p_flip": "Op zijn kop",
        "p_reset": "Standaard instellingen",
        "grp_record": " Video loop opnemen ",
        "rec_start": "● START OPNAME LOOP",
        "rec_stop": "■ STOP OPNAME",
        "loop_none": "Geen loop opgenomen",
        "loop_recording": "Opnemen… {t:.1f} / 30 sec",
        "loop_saved": "Loop opgeslagen: {n} frames ({t:.1f} sec, {mb:.1f} MB)",
        "loop_empty": "Geen frames opgenomen",
        "loop_max_hint": "Maximaal 30 seconden; automatische stop.",
        "reason_max": "30 sec bereikt",
        "reason_mem": "geheugenlimiet bereikt",
        "reason_cam": "camera gewisseld",
        "output_error": "Video-uitvoerfout: {e}",
        "status_live": "STATUS: LIVE WEBCAM",
        "status_frozen": "STATUS: BEVROREN BEELD",
        "status_loop": "STATUS: VIDEO LOOP AFSPELEN",
        "status_connecting": "STATUS: VERBINDEN MET CAMERA...",
        "status_no_camera": "STATUS: CAMERA NIET GEVONDEN",
        "off": "uit",
        "unit_s": "sec",
        "grp_image": " Beeld ",
        "grp_connection": " Verbinding ",
        "grp_stutter": " Periodiek bevriezen ",
        "s_lowres": "Resolutie",
        "s_lowres_tip": "Verlaagt de resolutie met compressieblokjes, zoals bij een oude webcam.",
        "s_blocks": "Blokjes",
        "s_blocks_tip": "Grote vierkante blokken, zoals bij een slechte verbinding.",
        "s_noise": "Sensorruis",
        "s_noise_tip": "Korrelig beeld, zoals van een goedkope sensor in een donkere kamer.",
        "s_flicker": "Lichtflikkering",
        "s_flicker_tip": "De helderheid trilt elk frame een beetje.",
        "cb_autoexp": "Belichting die verloopt",
        "cb_autoexp_tip": "De helderheid gaat langzaam op en neer, alsof de webcam zichzelf bijstelt.",
        "cb_flip": "Op zijn kop",
        "cb_flip_tip": "Draait het beeld 180°, alsof de driver in de war is.",
        "s_fps": "Framerate",
        "s_fps_tip": "Beperkt het aantal beelden per seconde.",
        "s_glitch": "Lijnstoringen",
        "s_glitch_tip": "Horizontale stroken die even opzij schuiven.",
        "s_partial": "Deelstoringen",
        "s_partial_tip": ("Een deel van het beeld (onderin, een helft, een hoek of losse blokjes) "
                          "blijft even hangen of wordt blokkerig, zoals bij verloren videopakketjes."),
        "cb_stutter": "Beeld af en toe laten bevriezen",
        "cb_stutter_spinner": "Toon een laadspinner i.p.v. een bevroren beeld",
        "s_stutter_interval": "Elke",
        "s_stutter_duration": "Gedurende",
        "grp_general": " Algemeen ",
        "cb_apply_live": "Effecten ook op de live webcam toepassen",
        "cb_apply_live_tip": "Uit: LIVE toont altijd je onbewerkte camera; effecten gelden dan alleen voor de loop.",
        "language": "Language / Taal:",
        "grp_transition": " Omschakelen && loop ",
        "cb_transition": "Beeld kort bevriezen bij wisselen tussen live en loop",
        "s_transition": "Bevries",
        "cb_pingpong": "Loop heen en terug afspelen (verbergt de sprong aan het eind)",
        "frame_connecting": "Verbinden met camera...",
        "frame_no_camera": "Camera niet gevonden",
        "frame_buffering": "Bufferen...",
        "msg_no_loop_title": "Geen loop",
        "msg_no_loop": "Er is nog geen opname.\n\nNeem eerst een video loop op met de knop onderaan het tabblad Bediening.",
        "msg_vcam_title": "Virtuele camera fout",
        "msg_vcam": "Kan virtuele camera niet starten. Controleer OBS Virtual Camera.\n\n{e}",
        "msg_res_title": "Resolutie opgeslagen",
        "msg_res": ("De camera neemt nu op in {w}×{h}.\n\n"
                    "De virtuele camera blijft voorlopig op {ow}×{oh}. Sluit CoffeeBreak "
                    "Cam Pro volledig af en start opnieuw om ook de uitvoer naar {w}×{h} "
                    "te zetten."),
        "msg_crash_title": "Onverwachte fout",
        "msg_crash": ("CoffeeBreak Cam Pro liep tegen een onverwachte fout aan en heeft de "
                      "camera voor de zekerheid gesloten.\n\nDetails staan in:\n{path}"),
    },
}
LANGUAGES = [("en", "English"), ("nl", "Nederlands")]

STATUS_COLORS = {"LIVE": "#81c784", "FROZEN": "#e57373", "LOOP": "#64b5f6",
                 "CONNECTING": "#ffb74d", "NO_CAMERA": "#e57373"}
STATUS_KEYS = {"LIVE": "status_live", "FROZEN": "status_frozen", "LOOP": "status_loop",
               "CONNECTING": "status_connecting", "NO_CAMERA": "status_no_camera"}


# --------------------------------------------------------------------------- #
#  Config
# --------------------------------------------------------------------------- #
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
    except Exception:
        pass


def update_config(**values):
    cfg = load_config()
    cfg.update(values)
    save_config(cfg)


def parse_size(value):
    try:
        return int(value[0]), int(value[1])
    except (TypeError, ValueError, IndexError):
        return None


# --------------------------------------------------------------------------- #
#  Drawing helpers
# --------------------------------------------------------------------------- #
def draw_spinner_frame(width, height, angle, text):
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


def draw_no_camera_frame(width, height, text):
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

    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.6
    text_size, _ = cv2.getTextSize(text, font, font_scale, 1)
    text_x = (width - text_size[0]) // 2
    text_y = center[1] + cam_h // 2 + 45
    cv2.putText(frame, text, (text_x, text_y), font, font_scale, (180, 180, 180), 1, cv2.LINE_AA)
    return frame


def fit_frame(frame, width, height):
    """Scale to width×height without stretching: center-crop to the target aspect first."""
    h, w = frame.shape[:2]
    if (w, h) == (width, height):
        return frame
    target_aspect = width / height
    if abs(w / h - target_aspect) > 0.01:
        if w / h > target_aspect:                 # too wide: crop sides
            new_w = max(1, round(h * target_aspect))
            x0 = (w - new_w) // 2
            frame = frame[:, x0:x0 + new_w]
        else:                                     # too tall: crop top/bottom
            new_h = max(1, round(w / target_aspect))
            y0 = (h - new_h) // 2
            frame = frame[y0:y0 + new_h, :]
        h, w = frame.shape[:2]
    interp = cv2.INTER_AREA if w > width else cv2.INTER_LINEAR
    return cv2.resize(frame, (width, height), interpolation=interp)


def degrade_resolution(frame, target_h, jpeg_quality, saturation=0.85):
    """Old-webcam look: real downscale + JPEG artifacts + slightly washed-out colour."""
    h, w = frame.shape[:2]
    target_h = max(1, min(target_h, h))
    target_w = max(1, round(w * target_h / h))
    small = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, int(jpeg_quality)])
    if ok:
        decoded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if decoded is not None:
            small = decoded
    if saturation < 1.0:
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        hsv[..., 1] = (hsv[..., 1] * saturation).astype(np.uint8)
        small = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)


def closest_resolution(target, options):
    tw, th = target
    aspect = tw / th
    return min(options, key=lambda r: (abs(r[0] / r[1] - aspect) > 0.01, abs(r[0] * r[1] - tw * th)))


def pick_default_output(resolutions):
    for size in PREFERRED_OUTPUT:
        if size in resolutions:
            return size
    return closest_resolution((960, 540), resolutions) if resolutions else (640, 480)


# --------------------------------------------------------------------------- #
#  Camera discovery
# --------------------------------------------------------------------------- #
def _is_ignored_camera(name):
    lowered = name.lower()
    return any(part in lowered for part in IGNORED_CAMERA_NAMES)


def list_camera_names():
    """Fast: asks Windows for device names without opening any camera.

    Returns [(index, name), ...] or None when pygrabber is unavailable/failed.
    The index matches OpenCV's CAP_DSHOW index (same DirectShow enumeration order).
    """
    if not HAS_PYGRABBER:
        return None
    try:
        devices = FilterGraph().get_input_devices()
    except Exception:
        return None
    return [(idx, name) for idx, name in enumerate(devices) if not _is_ignored_camera(name)]


def scan_cameras_slow():
    """Fallback without pygrabber: open indexes one by one (slow, run in a thread)."""
    cams = []
    for index in range(5):
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        try:
            if cap.isOpened():
                ok, _ = cap.read()
                if ok:
                    cams.append((index, f"Camera {index}"))
        finally:
            cap.release()
    return cams


def list_formats(camera_index):
    """Ask Windows (DirectShow) which resolutions the camera offers. Does not stream."""
    if not HAS_PYGRABBER:
        return []
    graph = None
    try:
        graph = FilterGraph()
        graph.add_video_input_device(camera_index)
        formats = graph.get_input_device().get_formats()
    except Exception:
        return []
    finally:
        if graph is not None:
            try:
                graph.remove_filters()
            except Exception:
                pass
    sizes = set()
    for fmt in formats:
        try:
            w, h = int(fmt["width"]), abs(int(fmt["height"]))
            if 160 <= w <= MAX_CAPTURE_WIDTH and h > 0 and fmt.get("max_framerate", 30) >= 14:
                sizes.add((w, h))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(sizes, key=lambda s: s[0] * s[1], reverse=True)


def probe_resolutions(camera_index, candidates=FALLBACK_RESOLUTIONS, capture_factory=None):
    """Slow fallback when Windows cannot list formats: try each size and check the real frame size."""
    factory = capture_factory or (lambda index: cv2.VideoCapture(index, cv2.CAP_DSHOW))
    cap = None
    supported = []
    try:
        cap = factory(camera_index)
        if not cap.isOpened():
            return []
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        except Exception:
            pass
        for width, height in candidates:
            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                cap.set(cv2.CAP_PROP_FPS, 30)
                ok, frame = cap.read()
                if ok and frame is not None and frame.shape[:2] == (height, width):
                    supported.append((width, height))
            except (cv2.error, ValueError, OSError):
                continue
    except (cv2.error, OSError):
        pass
    finally:
        if cap is not None:
            cap.release()
    return supported


# --------------------------------------------------------------------------- #
#  Camera capture thread
# --------------------------------------------------------------------------- #
class CameraReader:
    """The capture device is exclusively owned by the worker thread."""
    def __init__(self, index, width, height):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._requested = (index, width, height)
        self._frame = None
        self._timestamp = 0.0
        self._status = "connecting"
        self._generation = 0
        self._thread = threading.Thread(target=self._run, name="CameraCapture", daemon=True)
        self._thread.start()

    def configure(self, index, width, height):
        with self._lock:
            self._requested = (index, width, height)
            self._generation += 1
            self._frame = None
            self._status = "connecting"

    def snapshot(self):
        with self._lock:
            return self._frame, self._timestamp, self._status

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self):
        cap = None
        active_generation = -1
        failures = 0
        next_retry = 0.0
        try:
            while not self._stop.is_set():
                with self._lock:
                    requested, generation = self._requested, self._generation
                if generation != active_generation or cap is None:
                    if cap is not None:
                        cap.release()
                    cap = None
                    if time.monotonic() < next_retry and generation == active_generation:
                        self._stop.wait(.1)
                        continue
                    active_generation = generation
                    index, width, height = requested
                    candidate = cv2.VideoCapture(index, cv2.CAP_DSHOW)
                    if candidate.isOpened():
                        candidate.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                        candidate.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                        candidate.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                        candidate.set(cv2.CAP_PROP_FPS, 30)
                        cap = candidate
                        failures = 0
                    else:
                        candidate.release()
                        with self._lock:
                            if generation == self._generation:
                                self._status = "missing"
                        next_retry = time.monotonic() + 2.0
                        self._stop.wait(.15)
                        continue
                ok, frame = cap.read()
                if ok and frame is not None and frame.size:
                    failures = 0
                    with self._lock:
                        if generation == self._generation:
                            self._frame = frame
                            self._timestamp = time.monotonic()
                            self._status = "ok"
                else:
                    failures += 1
                    if failures >= 5:
                        with self._lock:
                            if generation == self._generation:
                                self._status = "missing"
                                self._frame = None
                        cap.release()
                        cap = None
                        next_retry = time.monotonic() + 2.0
                    self._stop.wait(.04)
        finally:
            if cap is not None:
                cap.release()


class EdgeGrip(QWidget):
    """Invisible strip on the window border that resizes the frameless window.

    Each grip has its own cursor, so the resize cursor can never "stick" to the
    rest of the interface (the old approach set it on the whole window).
    """
    def __init__(self, parent, edges, cursor):
        super().__init__(parent)
        self.edges = edges
        self.setCursor(cursor)

    def paintEvent(self, event):
        # Alpha 1 instead of 0: fully transparent pixels are click-through on Windows.
        QPainter(self).fillRect(self.rect(), QColor(0, 0, 0, 1))

    def mousePressEvent(self, event):
        handle = self.window().windowHandle()
        if event.button() == Qt.MouseButton.LeftButton and handle is not None:
            handle.startSystemResize(self.edges)
            event.accept()


def write_crash_log(exc_type, exc, tb, where="main"):
    try:
        with open(CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} · {VERSION} · {where} ===\n")
            f.write("".join(traceback.format_exception(exc_type, exc, tb)))
    except Exception:
        pass


def install_crash_handlers(get_window):
    """Make sure an unexpected error never leaves the camera (or virtual camera) open.

    A hard crash or a killed process is cleaned up by Windows itself. The risky case is
    an error that leaves the process running (e.g. the frame timer failing every tick):
    then we log it, close both cameras and quit.
    """
    try:
        # Native crashes (inside OpenCV / Qt) get at least a traceback in the log.
        install_crash_handlers.fault_file = open(CRASH_LOG, "a", encoding="utf-8")
        faulthandler.enable(install_crash_handlers.fault_file)
    except Exception:
        pass

    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            window = get_window()
            if window is not None:
                window.quit_app()
            return
        write_crash_log(exc_type, exc, tb)
        window = get_window()
        if window is None or window.crashed:
            return
        window.crashed = True
        window.shutdown()
        try:
            window.show_message(QMessageBox.Icon.Critical, "msg_crash_title",
                                window.t("msg_crash", path=CRASH_LOG))
        finally:
            QApplication.quit()

    def thread_excepthook(args):
        write_crash_log(args.exc_type, args.exc_value, args.exc_traceback,
                        where=getattr(args.thread, "name", "thread"))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    atexit.register(lambda: get_window() is not None and get_window().shutdown())


class _ThreadBridge(QObject):
    """Delivers results from worker threads back on the Qt main thread."""
    done = Signal(object, object)


# --------------------------------------------------------------------------- #
#  Main window
# --------------------------------------------------------------------------- #
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

        cfg = load_config()
        self.lang = cfg.get("language", "en") if cfg.get("language") in TEXTS else "en"
        self._saved_output = parse_size(cfg.get("resolution"))

        # Camera / output state. Nothing is opened here: the UI must appear first.
        self.camera_list = None               # None = still searching
        self.current_cam_index = None
        self.current_cam_name = None
        self.available_resolutions = []
        self.capture_res = None
        self.reader = None
        self.vcam = None
        self.cam_width, self.cam_height = self._saved_output or (640, 480)
        self._cam_token = 0
        self._shut_down = False
        self.crashed = False
        self._bridge = _ThreadBridge()
        self._bridge.done.connect(lambda callback, result: callback(result))

        self.is_expanded = False
        self.mode = "LIVE"
        self.preset_key = None
        self._partial_event = None
        self.frozen_frame = None
        self.spinner_angle = 0

        # Loop recording: new frames go into a separate buffer, so the loop that is
        # playing stays intact until the new recording is finished.
        self.is_recording_loop = False
        self._rec_frames = []
        self._rec_bytes = 0
        self._rec_next = 0.0
        self.record_start = None
        self.last_capture_stamp = -1.0
        self.recorded_loop_frames = []        # [(relative_timestamp, jpeg_bytes), ...]
        self.loop_timestamps = []
        self.loop_duration = 0.0
        self.loop_bytes = 0
        self.loop_start = None
        self._loop_decoded_index = -1
        self._loop_decoded_frame = None
        self._loop_info = ("loop_none", {}, None)

        self.transition_until = 0.0
        self.transition_frame = None
        self.last_output_frame = None
        self.last_output_warning = 0.0

        self.last_fps_frame = None
        self.next_fps_emit = 0.0
        self.stutter_start_time = time.monotonic()
        self.stutter_frozen_frame = None
        self._noise_buf = None
        self._exposure = 0.0
        self._exposure_target = 0.0

        self._drag_start = None
        self._drag_window_start = None
        self._dragging = False

        self._i18n = []
        self._build_ui()
        self._grips = [
            EdgeGrip(self, Qt.Edge.LeftEdge, Qt.CursorShape.SizeHorCursor),
            EdgeGrip(self, Qt.Edge.RightEdge, Qt.CursorShape.SizeHorCursor),
            EdgeGrip(self, Qt.Edge.TopEdge, Qt.CursorShape.SizeVerCursor),
            EdgeGrip(self, Qt.Edge.BottomEdge, Qt.CursorShape.SizeVerCursor),
            EdgeGrip(self, Qt.Edge.LeftEdge | Qt.Edge.TopEdge, Qt.CursorShape.SizeFDiagCursor),
            EdgeGrip(self, Qt.Edge.RightEdge | Qt.Edge.BottomEdge, Qt.CursorShape.SizeFDiagCursor),
            EdgeGrip(self, Qt.Edge.RightEdge | Qt.Edge.TopEdge, Qt.CursorShape.SizeBDiagCursor),
            EdgeGrip(self, Qt.Edge.LeftEdge | Qt.Edge.BottomEdge, Qt.CursorShape.SizeBDiagCursor),
        ]
        self.apply_dark_theme()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_frame)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.start(33)

        self.set_collapsed_size()
        self.restore_saved_position()

        # Start cameras only after the window has been painted.
        QTimer.singleShot(50, self._startup)

    # ----------------------------------------------------------------- i18n --
    def t(self, key, **kwargs):
        text = TEXTS.get(self.lang, TEXTS["en"]).get(key) or TEXTS["en"].get(key, key)
        return text.format(**kwargs) if kwargs else text

    def _tr(self, fn):
        """Register a function that (re)applies translated text, and run it once."""
        self._i18n.append(fn)
        fn()

    def retranslate(self):
        for fn in self._i18n:
            fn()
        self._refresh_status()
        self._render_loop_info()
        self._fill_camera_combo()

    def on_language_change(self, _index):
        lang = self.lang_combo.currentData()
        if lang and lang != self.lang:
            self.lang = lang
            update_config(language=lang)
            self.retranslate()

    def show_message(self, icon, title_key, text):
        """Message box that is always visible.

        Our own window is "always on top"; a normal dialog could open behind it,
        blocking the interface with only a system beep. Force this one on top.
        """
        box = QMessageBox(icon, self.t(title_key), text, QMessageBox.StandardButton.Ok, self)
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        box.setStyleSheet("QLabel { min-width: 260px; }")
        QTimer.singleShot(0, lambda: (box.raise_(), box.activateWindow()))
        box.exec()

    # -------------------------------------------------------------- threads --
    def run_in_thread(self, func, callback, *args):
        def work():
            try:
                result = func(*args)
            except Exception:
                result = None
            if not self._shut_down:
                self._bridge.done.emit(callback, result)
        threading.Thread(target=work, daemon=True).start()

    # -------------------------------------------------------------- startup --
    def _startup(self):
        if self._shut_down:
            return
        # Known output resolution: start the virtual camera right away. Viewers then
        # see the "connecting" spinner while the real camera is still starting up.
        if self._saved_output and not self._start_vcam(*self._saved_output):
            return
        cams = list_camera_names()
        if cams is None:
            self.run_in_thread(scan_cameras_slow, self._on_cameras_found)
        else:
            self._on_cameras_found(cams)

    def _start_vcam(self, width, height):
        try:
            self.vcam = pyvirtualcam.Camera(width=width, height=height, fps=OUTPUT_FPS,
                                            fmt=pyvirtualcam.PixelFormat.BGR)
        except Exception as e:
            self.show_message(QMessageBox.Icon.Critical, "msg_vcam_title", self.t("msg_vcam", e=e))
            self.quit_app()
            return False
        self.cam_width, self.cam_height = width, height
        return True

    def _on_cameras_found(self, cams):
        if self._shut_down:
            return
        self.camera_list = cams or []
        if not self.camera_list:
            self._fill_camera_combo()
            if self.vcam is None:
                self._start_vcam(640, 480)
            self.set_mode_no_camera()
            return
        cfg = load_config()
        saved_name = cfg.get("camera_name")
        saved_index = cfg.get("camera_index")
        matched = None
        if saved_name:
            same_name = [(i, n) for i, n in self.camera_list if n == saved_name]
            matched = next(((i, n) for i, n in same_name if i == saved_index), None) or \
                (same_name[0] if same_name else None)
        if matched is None:
            matched = self.camera_list[0]
        self.current_cam_index, self.current_cam_name = matched
        self._fill_camera_combo()
        self._select_camera(*matched)

    def _fill_camera_combo(self):
        self.cam_combobox.blockSignals(True)
        self.cam_combobox.clear()
        if self.camera_list is None:
            self.cam_combobox.addItem(self.t("searching"), None)
            self.cam_combobox.setEnabled(False)
        elif not self.camera_list:
            self.cam_combobox.addItem(self.t("no_cameras"), None)
            self.cam_combobox.setEnabled(False)
        else:
            for idx, name in self.camera_list:
                self.cam_combobox.addItem(f"[{idx}] {name}", idx)
            pos = self.cam_combobox.findData(self.current_cam_index)
            self.cam_combobox.setCurrentIndex(max(0, pos))
            self.cam_combobox.setEnabled(True)
        self.cam_combobox.blockSignals(False)

    def _select_camera(self, index, name):
        """Switch to a camera. Saved camera → trust its cached resolutions, no checks."""
        self._cam_token += 1
        token = self._cam_token
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        self.current_cam_index, self.current_cam_name = index, name
        update_config(camera_name=name, camera_index=index)

        cached = load_config().get("camera_resolutions", {}).get(name)
        if cached:
            sizes = [s for s in (parse_size(r) for r in cached) if s]
            if sizes:
                self._start_camera(token, sizes)
                return
        sizes = list_formats(index)
        if sizes:
            self._cache_resolutions(name, sizes)
            self._start_camera(token, sizes)
            return
        # Windows could not tell us: probe in the background (opens the camera).
        self.resolution_combo.setEnabled(False)
        self.run_in_thread(probe_resolutions, lambda result: self._on_probed(token, name, result), index)

    def _on_probed(self, token, name, sizes):
        if self._shut_down or token != self._cam_token:
            return
        if sizes:
            self._cache_resolutions(name, sizes)
        self._start_camera(token, sizes or [(640, 480)])

    @staticmethod
    def _cache_resolutions(name, sizes):
        cfg = load_config()
        cache = cfg.get("camera_resolutions")
        if not isinstance(cache, dict):
            cache = {}
        cache[name] = [list(s) for s in sizes]
        cfg["camera_resolutions"] = cache
        save_config(cfg)

    def _start_camera(self, token, sizes):
        if self._shut_down or token != self._cam_token:
            return
        self.available_resolutions = list(sizes)
        if self.vcam is None:
            # First run: no saved output size yet, pick a sensible one from the camera.
            output = pick_default_output(self.available_resolutions)
            update_config(resolution=list(output))
            if not self._start_vcam(*output):
                return
        output = (self.cam_width, self.cam_height)
        self.capture_res = (output if output in self.available_resolutions
                            else closest_resolution(output, self.available_resolutions))
        self.reader = CameraReader(self.current_cam_index, *self.capture_res)

        self.resolution_combo.blockSignals(True)
        self.resolution_combo.clear()
        for width, height in self.available_resolutions:
            self.resolution_combo.addItem(f"{width}×{height}", (width, height))
        self.resolution_combo.setCurrentIndex(self.available_resolutions.index(self.capture_res))
        self.resolution_combo.setEnabled(True)
        self.resolution_combo.blockSignals(False)

    # ------------------------------------------------------------- shutdown --
    def shutdown(self):
        if self._shut_down:
            return
        self._shut_down = True
        self.timer.stop()
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        if self.vcam is not None:
            try:
                self.vcam.close()
            except Exception:
                pass
            self.vcam = None

    def quit_app(self):
        self.shutdown()
        self.close()
        QApplication.quit()

    def closeEvent(self, event):
        # A Qt "Tool" window does not end the application when closed: without
        # quit() the process would keep running invisibly.
        self.shutdown()
        super().closeEvent(event)
        QApplication.quit()

    # ------------------------------------------------------------- position --
    def restore_saved_position(self):
        config = load_config()
        if "pos_x" in config and "pos_y" in config:
            self.move(config["pos_x"], config["pos_y"])
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.x() + screen.width() - BUTTON_SIZE - 20, screen.y() + 20)
        # Saved position may be on a monitor that is no longer connected.
        self.clamp_to_screen()

    def clamp_to_screen(self):
        screen = QApplication.screenAt(self.geometry().center()) or QApplication.primaryScreen()
        geo = screen.availableGeometry()
        x = max(geo.x(), min(self.x(), geo.x() + geo.width() - self.width()))
        y = max(geo.y(), min(self.y(), geo.y() + geo.height() - self.height()))
        self.move(x, y)
        update_config(pos_x=self.x(), pos_y=self.y())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_grips()

    def _position_grips(self):
        m, c = RESIZE_MARGIN, RESIZE_MARGIN * 2
        w, h = self.width(), self.height()
        geometries = [(0, c, m, h - 2 * c), (w - m, c, m, h - 2 * c),
                      (c, 0, w - 2 * c, m), (c, h - m, w - 2 * c, m),
                      (0, 0, c, c), (w - c, h - c, c, c), (w - c, 0, c, c), (0, h - c, c, c)]
        for grip, geo in zip(self._grips, geometries):
            grip.setGeometry(*geo)
            grip.setVisible(self.is_expanded)
            grip.raise_()

    # ------------------------------------------------------------------- UI --
    def _build_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        self.toggle_btn = QPushButton("☕")
        self.toggle_btn.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        self.toggle_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._tr(lambda: self.toggle_btn.setToolTip(self.t("toggle_tip")))
        self.toggle_btn.installEventFilter(self)
        self.toggle_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.toggle_btn.customContextMenuRequested.connect(self.show_context_menu)
        self.toggle_btn.clicked.connect(self.toggle_window)
        self.main_layout.addWidget(self.toggle_btn, alignment=Qt.AlignmentFlag.AlignRight)

        self.control_container = QFrame()
        self.control_container.setObjectName("ControlPanel")
        container_layout = QVBoxLayout(self.control_container)
        container_layout.setContentsMargins(10, 10, 10, 10)

        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        container_layout.addWidget(self.status_label)

        self.tabs = QTabWidget()
        container_layout.addWidget(self.tabs)
        for i, (key, builder) in enumerate([("tab_main", self._build_tab_main),
                                            ("tab_effects", self._build_tab_effects),
                                            ("tab_settings", self._build_tab_settings)]):
            self.tabs.addTab(self._make_scrollable_tab(builder), "")
            self._tr(lambda i=i, key=key: self.tabs.setTabText(i, self.t(key)))

        footer_layout = QHBoxLayout()
        self.version_label = QLabel(f"CoffeeBreak Cam Pro {VERSION}")
        self.version_label.setStyleSheet("color: #777777; font-size: 11px; font-weight: normal;")
        footer_layout.addWidget(self.version_label)
        footer_layout.addStretch()
        container_layout.addLayout(footer_layout)

        self.control_container.hide()
        self.main_layout.addWidget(self.control_container)

        self._refresh_status()
        self._render_loop_info()
        self._fill_camera_combo()
        self.update_button_style()

    def _make_scrollable_tab(self, builder_func):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        builder_func(content)
        scroll.setWidget(content)
        return scroll

    def show_context_menu(self, pos):
        menu = QMenu(self)
        exit_action = menu.addAction(self.t("menu_quit"))
        action = menu.exec(self.toggle_btn.mapToGlobal(pos))
        if action == exit_action:
            self.quit_app()

    def _button(self, key, slot, style=None):
        btn = QPushButton()
        self._tr(lambda: btn.setText(self.t(key)))
        btn.clicked.connect(slot)
        if style:
            btn.setStyleSheet(style)
        return btn

    def _group(self, key):
        group = QGroupBox()
        self._tr(lambda: group.setTitle(self.t(key)))
        return group

    def _label(self, key):
        label = QLabel()
        self._tr(lambda: label.setText(self.t(key)))
        return label

    def _checkbox(self, key, checked=False):
        cb = QCheckBox()
        self._tr(lambda: cb.setText(self.t(key)))
        cb.setChecked(checked)
        return cb

    def _card(self, key):
        """Titled group with a 3-column form: label | control | value."""
        group = self._group(key)
        grid = QGridLayout(group)
        grid.setContentsMargins(12, 18, 12, 12)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        grid.setColumnMinimumWidth(0, 115)     # same slider start in every card
        return group, grid

    @staticmethod
    def _next_row(grid):
        return 0 if grid.count() == 0 else grid.rowCount()

    def _form_slider(self, grid, key, lo, hi, value, fmt=str, tip_key=None):
        row = self._next_row(grid)
        label, value_label = QLabel(), QLabel()
        value_label.setMinimumWidth(56)
        value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        value_label.setStyleSheet("color: #9e9e9e;")
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(value)
        self._tr(lambda: label.setText(self.t(key)))
        if tip_key:
            self._tr(lambda: (label.setToolTip(self.t(tip_key)), slider.setToolTip(self.t(tip_key))))

        def refresh(*_):
            value_label.setText(fmt(slider.value()))
        slider.valueChanged.connect(refresh)
        self._tr(refresh)
        slider.valueChanged.connect(lambda *_: self.update_button_style())
        grid.addWidget(label, row, 0)
        grid.addWidget(slider, row, 1)
        grid.addWidget(value_label, row, 2)
        slider.form_widgets = (label, value_label)     # enabled/disabled together
        return slider

    def _form_checkbox(self, grid, key, checked=False, tip_key=None):
        cb = self._checkbox(key, checked)
        if tip_key:
            self._tr(lambda: cb.setToolTip(self.t(tip_key)))
        cb.toggled.connect(lambda *_: self.update_button_style())
        grid.addWidget(cb, self._next_row(grid), 0, 1, 3)
        return cb

    @staticmethod
    def _set_row_enabled(slider, enabled):
        slider.setEnabled(enabled)
        for widget in slider.form_widgets:
            widget.setEnabled(enabled)

    def _build_tab_main(self, parent_widget):
        layout = QVBoxLayout(parent_widget)

        cam_layout = QHBoxLayout()
        cam_layout.addWidget(self._label("camera"))
        self.cam_combobox = QComboBox()
        self.cam_combobox.currentIndexChanged.connect(self.on_camera_change)
        cam_layout.addWidget(self.cam_combobox, stretch=1)
        layout.addLayout(cam_layout)

        resolution_layout = QHBoxLayout()
        resolution_layout.addWidget(self._label("resolution"))
        self.resolution_combo = QComboBox()
        self.resolution_combo.setEnabled(False)
        self.resolution_combo.currentIndexChanged.connect(self.on_resolution_change)
        resolution_layout.addWidget(self.resolution_combo, stretch=1)
        layout.addLayout(resolution_layout)

        self.preview_label = QLabel()
        self.preview_label.setMinimumSize(320, 180)
        self.preview_label.setStyleSheet("background-color: #000000; border-radius: 6px;")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.preview_label, alignment=Qt.AlignmentFlag.AlignCenter)

        mode_layout = QHBoxLayout()
        self.btn_live = self._button("btn_live", self.set_mode_live,
                                     "background-color: #2e7d32; color: white; font-weight: bold; padding: 10px;")
        self.btn_freeze = self._button("btn_freeze", self.set_mode_freeze,
                                       "background-color: #d32f2f; color: white; font-weight: bold; padding: 10px;")
        self.btn_play_loop = self._button("btn_loop", self.set_mode_loop,
                                          "background-color: #0288d1; color: white; font-weight: bold; padding: 10px;")
        mode_layout.addWidget(self.btn_live)
        mode_layout.addWidget(self.btn_freeze)
        mode_layout.addWidget(self.btn_play_loop)
        layout.addLayout(mode_layout)

        preset_group = self._group("grp_presets")
        preset_grid = QGridLayout(preset_group)
        preset_grid.addWidget(self._button("p_connecting", self.set_mode_connecting), 0, 0)
        preset_grid.addWidget(self._button("p_no_camera", self.set_mode_no_camera), 0, 1)
        preset_grid.addWidget(self._button("p_bad", self.apply_preset_bad_connection), 1, 0)
        preset_grid.addWidget(self._button("p_wifi", self.apply_preset_poor_wifi), 1, 1)
        preset_grid.addWidget(self._button("p_partial", self.apply_preset_partial), 2, 0)
        preset_grid.addWidget(self._button("p_old", self.apply_preset_old_webcam), 2, 1)
        preset_grid.addWidget(self._button("p_flip", self.apply_preset_flip), 3, 0)
        preset_grid.addWidget(self._button("p_reset", self.apply_preset_reset), 3, 1)
        layout.addWidget(preset_group)

        rec_group = self._group("grp_record")
        rec_layout = QVBoxLayout(rec_group)
        self.btn_rec_loop = QPushButton()
        self._tr(lambda: self.btn_rec_loop.setText(
            self.t("rec_stop" if self.is_recording_loop else "rec_start")))
        self.btn_rec_loop.setStyleSheet("background-color: #c62828; color: white; font-weight: bold; padding: 8px;")
        self.btn_rec_loop.clicked.connect(self.toggle_record_loop)
        rec_layout.addWidget(self.btn_rec_loop)

        self.lbl_loop_info = QLabel()
        self.lbl_loop_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_loop_info.setStyleSheet("color: #aaaaaa;")
        rec_layout.addWidget(self.lbl_loop_info)
        rec_layout.addWidget(self._label("loop_max_hint"))
        layout.addWidget(rec_group)

    def _build_tab_effects(self, parent_widget):
        layout = QVBoxLayout(parent_widget)
        layout.setSpacing(12)
        off = self.t
        sec = lambda v: f"{v / 10:.1f} {self.t('unit_s')}"
        off_or = lambda fmt: (lambda v: off("off") if v == 0 else fmt(v))

        image, grid = self._card("grp_image")
        self.lowres_slider = self._form_slider(
            grid, "s_lowres", 0, len(LOWRES_STEPS) - 1, 0,
            off_or(lambda v: f"{LOWRES_STEPS[v]}p"), "s_lowres_tip")
        self.block_slider = self._form_slider(
            grid, "s_blocks", 1, 35, 1, lambda v: off("off") if v <= 1 else str(v), "s_blocks_tip")
        self.noise_slider = self._form_slider(grid, "s_noise", 0, 40, 0, off_or(str), "s_noise_tip")
        self.flicker_slider = self._form_slider(grid, "s_flicker", 0, 20, 0, off_or(str), "s_flicker_tip")
        self.auto_exp_cb = self._form_checkbox(grid, "cb_autoexp", tip_key="cb_autoexp_tip")
        self.flip_cb = self._form_checkbox(grid, "cb_flip", tip_key="cb_flip_tip")
        layout.addWidget(image)

        connection, grid = self._card("grp_connection")
        self.fps_slider = self._form_slider(grid, "s_fps", 1, 30, 30, lambda v: f"{v} fps", "s_fps_tip")
        self.glitch_slider = self._form_slider(
            grid, "s_glitch", 0, 100, 0, off_or(lambda v: f"{v / 10:.1f}"), "s_glitch_tip")
        self.partial_slider = self._form_slider(grid, "s_partial", 0, 10, 0, off_or(str), "s_partial_tip")
        layout.addWidget(connection)

        stutter, grid = self._card("grp_stutter")
        self.stutter_enable_cb = self._form_checkbox(grid, "cb_stutter")
        self.stutter_interval_slider = self._form_slider(
            grid, "s_stutter_interval", 1, 60, 10, lambda v: f"{v} {self.t('unit_s')}")
        self.stutter_duration_slider = self._form_slider(grid, "s_stutter_duration", 5, 100, 20, sec)
        self.stutter_spinner_cb = self._form_checkbox(grid, "cb_stutter_spinner")
        layout.addWidget(stutter)
        layout.addStretch()

        self.stutter_enable_cb.toggled.connect(self._on_stutter_toggled)
        self._on_stutter_toggled(False)

    def _build_tab_settings(self, parent_widget):
        layout = QVBoxLayout(parent_widget)
        layout.setSpacing(12)

        general, grid = self._card("grp_general")
        self.apply_live_cb = self._form_checkbox(grid, "cb_apply_live", True, "cb_apply_live_tip")
        row = self._next_row(grid)
        grid.addWidget(self._label("language"), row, 0)
        self.lang_combo = QComboBox()
        for code, name in LANGUAGES:
            self.lang_combo.addItem(name, code)
        self.lang_combo.setCurrentIndex(max(0, self.lang_combo.findData(self.lang)))
        self.lang_combo.currentIndexChanged.connect(self.on_language_change)
        grid.addWidget(self.lang_combo, row, 1, 1, 2)
        layout.addWidget(general)

        switching, grid = self._card("grp_transition")
        self.transition_enable = self._form_checkbox(grid, "cb_transition", True)
        self.transition_slider = self._form_slider(
            grid, "s_transition", 10, 30, 20, lambda v: f"{v / 10:.1f} {self.t('unit_s')}")
        self.pingpong_cb = self._form_checkbox(grid, "cb_pingpong", True)
        self.transition_enable.toggled.connect(lambda on: self._set_row_enabled(self.transition_slider, on))
        layout.addWidget(switching)
        layout.addStretch()

    def _on_stutter_toggled(self, checked):
        if checked:
            # Start with a full "normal" interval instead of possibly freezing right away.
            self.stutter_start_time = time.monotonic()
        self._set_row_enabled(self.stutter_interval_slider, checked)
        self._set_row_enabled(self.stutter_duration_slider, checked)
        self.stutter_spinner_cb.setEnabled(checked)

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
            QSlider::handle:horizontal:disabled {
                background: #555555;
            }
            QCheckBox::indicator {
                width: 14px;
                height: 14px;
                border: 1px solid #8a8a8a;
                border-radius: 3px;
                background: #2d2d2d;
            }
            QCheckBox::indicator:checked {
                background: #64b5f6;
                border-color: #64b5f6;
            }
            QCheckBox::indicator:disabled {
                border-color: #555555;
                background: #262626;
            }
            QLabel:disabled, QCheckBox:disabled {
                color: #666666;
            }
            QToolTip {
                background-color: #2d2d2d;
                color: #ffffff;
                border: 1px solid #3c3c3c;
                padding: 4px;
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

    def effects_active(self):
        """True when the outgoing image differs from the plain camera image."""
        if not hasattr(self, "partial_slider"):
            return False
        if self.stutter_enable_cb.isChecked():
            return True          # stuttering also applies to an otherwise plain live image
        if self.mode == "LIVE" and not self.apply_live_cb.isChecked():
            return False
        return (self.lowres_slider.value() > 0 or self.block_slider.value() > 1
                or self.noise_slider.value() > 0 or self.flicker_slider.value() > 0
                or self.fps_slider.value() < OUTPUT_FPS or self.glitch_slider.value() > 0
                or self.partial_slider.value() > 0 or self.auto_exp_cb.isChecked()
                or self.flip_cb.isChecked())

    def update_button_style(self):
        # Green: plain live camera · orange: live with effects or "connecting" ·
        # red: frozen / no camera · blue: loop.
        if self.mode in ("FROZEN", "NO_CAMERA"):
            color = "#d32f2f"
        elif self.mode == "LOOP":
            color = "#0288d1"
        elif self.mode == "CONNECTING" or self.effects_active():
            color = "#e65100"
        else:
            color = "#2e7d32"

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
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.control_container.hide()
        self.setMinimumSize(BUTTON_SIZE, BUTTON_SIZE)
        self.setMaximumSize(BUTTON_SIZE, BUTTON_SIZE)
        self.resize(BUTTON_SIZE, BUTTON_SIZE)

    def set_expanded_size(self):
        self.setMaximumSize(16777215, 16777215)
        self.setMinimumSize(420, 480)
        self.main_layout.setContentsMargins(RESIZE_MARGIN, RESIZE_MARGIN, RESIZE_MARGIN, RESIZE_MARGIN)
        self.resize(560, 720)
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
        self._position_grips()
        self.clamp_to_screen()

    # ---------------------------------------------------------------- modes --
    def _refresh_status(self):
        text = self.t(STATUS_KEYS[self.mode])
        if self.preset_key and self.mode in ("LIVE", "LOOP"):
            text += f" · {self.t(self.preset_key)}"
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"font-weight: bold; font-size: 14px; color: {STATUS_COLORS[self.mode]}; "
            "padding: 6px; background-color: #1e1e1e; border-radius: 6px;")

    def _set_mode(self, mode):
        self.mode = mode
        self._refresh_status()
        self.update_button_style()

    def _begin_transition(self, target):
        if self.mode != target and self.mode in ("LIVE", "LOOP") and self.transition_enable.isChecked() and self.last_output_frame is not None:
            self.transition_frame = self.last_output_frame.copy()
            self.transition_until = time.monotonic() + self.transition_slider.value() / 10.0
        else:
            self.transition_frame = None
            self.transition_until = 0.0
        self.last_fps_frame = None

    def set_mode_live(self):
        self._begin_transition("LIVE")
        self._set_mode("LIVE")

    def set_mode_freeze(self):
        frame = self.reader.snapshot()[0] if self.reader is not None else None
        if frame is not None:
            self.frozen_frame = fit_frame(frame, self.cam_width, self.cam_height).copy()
            self._set_mode("FROZEN")

    def set_mode_loop(self):
        if not self.recorded_loop_frames:
            self.show_message(QMessageBox.Icon.Warning, "msg_no_loop_title", self.t("msg_no_loop"))
            return
        self._begin_transition("LOOP")
        self.loop_start = time.monotonic() + max(0.0, self.transition_until - time.monotonic())
        self._set_mode("LOOP")

    def set_mode_connecting(self):
        self._set_mode("CONNECTING")

    def set_mode_no_camera(self):
        self._set_mode("NO_CAMERA")

    # -------------------------------------------------------------- presets --
    def _apply_preset(self, key, lowres=0, block=1, noise=0, flicker=0, fps=OUTPUT_FPS, glitch=0,
                      auto_exp=False, partial=0, flip=False,
                      stutter=False, spinner=False, interval=10, duration=20):
        self.partial_slider.setValue(partial)
        self.flip_cb.setChecked(flip)
        self.lowres_slider.setValue(lowres)
        self.block_slider.setValue(block)
        self.noise_slider.setValue(noise)
        self.flicker_slider.setValue(flicker)
        self.fps_slider.setValue(fps)
        self.glitch_slider.setValue(glitch)
        self.auto_exp_cb.setChecked(auto_exp)
        self.stutter_spinner_cb.setChecked(spinner)
        self.stutter_interval_slider.setValue(interval)
        self.stutter_duration_slider.setValue(duration)
        self.stutter_enable_cb.setChecked(stutter)
        self.preset_key = key
        # A playing loop keeps playing (now with the preset's effects). Static
        # screens (frozen/connecting/no camera) go back to live.
        if self.mode in ("LIVE", "LOOP"):
            self._refresh_status()
            self.update_button_style()
        else:
            self.set_mode_live()

    def apply_preset_bad_connection(self):
        self._apply_preset("p_bad", lowres=2, block=4, fps=5,
                           stutter=True, interval=3, duration=20)

    def apply_preset_poor_wifi(self):
        self._apply_preset("p_wifi", lowres=1, block=2, fps=15, glitch=4,
                           stutter=True, spinner=True, interval=8, duration=15)

    def apply_preset_partial(self):
        self._apply_preset("p_partial", lowres=1, fps=24, partial=6)

    def apply_preset_old_webcam(self):
        self._apply_preset("p_old", lowres=3, noise=8, flicker=2, fps=15, auto_exp=True)

    def apply_preset_flip(self):
        self._apply_preset("p_flip", flip=True)

    def apply_preset_reset(self):
        self._apply_preset(None)

    # ------------------------------------------------------------ recording --
    def _set_loop_info(self, key, reason=None, **kwargs):
        self._loop_info = (key, kwargs, reason)
        self._render_loop_info()

    def _render_loop_info(self):
        key, kwargs, reason = self._loop_info
        text = self.t(key, **kwargs)
        if reason:
            text += f" — {self.t(reason)}"
        self.lbl_loop_info.setText(text)

    def toggle_record_loop(self):
        if not self.is_recording_loop:
            self.is_recording_loop = True
            self._rec_frames = []
            self._rec_bytes = 0
            self.record_start = time.monotonic()
            self._rec_next = 0.0
            self.last_capture_stamp = -1.0
            self.btn_rec_loop.setText(self.t("rec_stop"))
            self.btn_rec_loop.setStyleSheet("background-color: #d32f2f; color: white; font-weight: bold; padding: 8px;")
            self._set_loop_info("loop_recording", t=0.0)
        else:
            self._finish_recording()

    def _finish_recording(self, reason=None):
        self.is_recording_loop = False
        self.btn_rec_loop.setText(self.t("rec_start"))
        self.btn_rec_loop.setStyleSheet("background-color: #c62828; color: white; font-weight: bold; padding: 8px;")
        frames, self._rec_frames = self._rec_frames, []
        if not frames:
            # Keep the previous loop (if any) instead of throwing it away.
            self._set_loop_info("loop_empty", reason)
            return
        first = frames[0][0]
        self.recorded_loop_frames = [(stamp - first, packet) for stamp, packet in frames]
        self.loop_timestamps = [stamp for stamp, _ in self.recorded_loop_frames]
        self.loop_duration = self.loop_timestamps[-1]
        self.loop_bytes = self._rec_bytes
        self._loop_decoded_index = -1
        self._loop_decoded_frame = None
        self._set_loop_info("loop_saved", reason, n=len(frames), t=self.loop_duration,
                            mb=self.loop_bytes / (1024 * 1024))
        if self.mode == "LOOP":
            self.loop_start = time.monotonic()

    def _record_frame(self, now, live, capture_stamp):
        elapsed = now - self.record_start
        if elapsed >= MAX_LOOP_SECONDS:
            self._finish_recording("reason_max")
            return
        if live is not None and capture_stamp != self.last_capture_stamp \
                and elapsed >= self._rec_next - TICK_TOLERANCE:
            self.last_capture_stamp = capture_stamp
            period = 1.0 / LOOP_RECORD_FPS
            self._rec_next = max(self._rec_next + period, elapsed - period)
            ok, encoded = cv2.imencode(".jpg", live, [cv2.IMWRITE_JPEG_QUALITY, LOOP_JPEG_QUALITY])
            if ok:
                packet = encoded.tobytes()
                if self._rec_bytes + len(packet) > MAX_LOOP_BYTES:
                    self._finish_recording("reason_mem")
                    return
                self._rec_frames.append((elapsed, packet))
                self._rec_bytes += len(packet)
        self._set_loop_info("loop_recording", t=min(elapsed, MAX_LOOP_SECONDS))

    def _loop_frame(self, now):
        position = 0.0
        if self.loop_duration > 0:
            elapsed = max(0.0, now - self.loop_start)
            if self.pingpong_cb.isChecked():
                position = elapsed % (2 * self.loop_duration)
                if position > self.loop_duration:
                    position = 2 * self.loop_duration - position
            else:
                position = elapsed % self.loop_duration
        index = max(0, bisect.bisect_right(self.loop_timestamps, position) - 1)
        if index != self._loop_decoded_index:
            decoded = cv2.imdecode(np.frombuffer(self.recorded_loop_frames[index][1], np.uint8), cv2.IMREAD_COLOR)
            if decoded is not None and decoded.shape[:2] != (self.cam_height, self.cam_width):
                decoded = fit_frame(decoded, self.cam_width, self.cam_height)
            self._loop_decoded_index = index
            self._loop_decoded_frame = decoded
        return self._loop_decoded_frame

    # ------------------------------------------------- camera / resolution --
    def on_camera_change(self, _index):
        new_idx = self.cam_combobox.currentData()
        if new_idx is None or new_idx == self.current_cam_index:
            return
        if self.is_recording_loop:
            self._finish_recording("reason_cam")
        name = next((n for i, n in (self.camera_list or []) if i == new_idx), f"Camera {new_idx}")
        if self.mode == "FROZEN":
            self.set_mode_live()
        self._select_camera(new_idx, name)

    def on_resolution_change(self, _index):
        size = self.resolution_combo.currentData()
        if not size or self.reader is None:
            return
        size = tuple(size)
        if size == self.capture_res:
            return
        # The physical camera can switch right away; the virtual camera cannot be
        # recreated safely while it is in use, so the output follows on next start.
        self.capture_res = size
        self.reader.configure(self.current_cam_index, *size)
        update_config(resolution=list(size))
        if size != (self.cam_width, self.cam_height):
            self.show_message(QMessageBox.Icon.Information, "msg_res_title",
                              self.t("msg_res", w=size[0], h=size[1], ow=self.cam_width, oh=self.cam_height))

    # -------------------------------------------------------------- effects --
    def apply_effects(self, frame):
        processed = frame
        h, w = processed.shape[:2]

        step = self.lowres_slider.value()
        if 0 < step < len(LOWRES_STEPS) and LOWRES_STEPS[step] < h:
            processed = degrade_resolution(processed, LOWRES_STEPS[step], LOWRES_JPEG_Q[step])

        block = self.block_slider.value()
        if block > 1:
            small_w, small_h = max(1, w // block), max(1, h // block)
            small = cv2.resize(processed, (small_w, small_h), interpolation=cv2.INTER_LINEAR)
            processed = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)

        glitch_lvl = self.glitch_slider.value() / 10.0
        if glitch_lvl > 0.0:
            if processed is frame:
                processed = frame.copy()
            chance = min(1.0, glitch_lvl * 0.15) if glitch_lvl < 1.0 else 0.4
            for _ in range(max(1, int(glitch_lvl))):
                if np.random.rand() < chance:
                    slice_h = np.random.randint(2, max(4, int(8 * glitch_lvl)))
                    y = np.random.randint(0, max(1, h - slice_h))
                    max_shift = max(2, int(12 * glitch_lvl))
                    shift = np.random.randint(-max_shift, max_shift)
                    processed[y:y + slice_h] = np.roll(processed[y:y + slice_h], shift, axis=1)

        processed = self._apply_partial_glitch(processed)

        # Auto-exposure: drift slowly towards an occasionally changing target.
        if self.auto_exp_cb.isChecked():
            if np.random.rand() < 0.01:
                self._exposure_target = float(np.random.choice([-14, -9, -5, 0, 5, 9, 14]))
            self._exposure += max(-0.6, min(0.6, self._exposure_target - self._exposure))
        else:
            self._exposure = self._exposure_target = 0.0

        noise_lvl = self.noise_slider.value()
        flicker_lvl = self.flicker_slider.value()
        offset = self._exposure
        if flicker_lvl > 0:
            offset += np.random.uniform(-flicker_lvl, flicker_lvl)
        offset = int(round(offset))
        if noise_lvl > 0 or offset:
            buf = processed.astype(np.int16)
            if noise_lvl > 0:
                if self._noise_buf is None or self._noise_buf.shape != buf.shape:
                    self._noise_buf = np.empty(buf.shape, dtype=np.int16)
                cv2.randn(self._noise_buf, (0, 0, 0), (noise_lvl, noise_lvl, noise_lvl))
                buf += self._noise_buf
            if offset:
                buf += offset
            processed = np.clip(buf, 0, 255).astype(np.uint8)

        return processed

    def _new_partial_event(self, frame, level):
        """Pick a region and a failure type, like a lost packet in a video stream.

        Video codecs send the image in slices and 16×16 macroblocks; when data goes
        missing, the receiver keeps showing old content there (it "hangs") or shows
        a coarse, blocky version until the next full frame arrives.
        """
        h, w = frame.shape[:2]
        mask = np.zeros((h, w), dtype=bool)
        kind = np.random.choice(["bottom", "half", "corner", "macroblocks"], p=[0.35, 0.2, 0.25, 0.2])
        if kind == "bottom":
            mask[int(h * np.random.uniform(0.35, 0.8)):, :] = True
        elif kind == "half":
            side = np.random.randint(4)
            if side == 0:
                mask[h // 2:, :] = True
            elif side == 1:
                mask[:h // 2, :] = True
            elif side == 2:
                mask[:, w // 2:] = True
            else:
                mask[:, :w // 2] = True
        elif kind == "corner":
            ch, cw = int(h * np.random.uniform(0.3, 0.55)), int(w * np.random.uniform(0.3, 0.55))
            ys = slice(0, ch) if np.random.rand() < 0.5 else slice(h - ch, h)
            xs = slice(0, cw) if np.random.rand() < 0.5 else slice(w - cw, w)
            mask[ys, xs] = True
        else:
            cell = 16
            grid = np.random.rand(-(-h // cell), -(-w // cell)) < 0.4
            tiles = np.repeat(np.repeat(grid, cell, axis=0), cell, axis=1)[:h, :w]
            band_h = int(h * np.random.uniform(0.2, 0.45))
            y0 = np.random.randint(0, max(1, h - band_h))
            mask[y0:y0 + band_h] = tiles[y0:y0 + band_h]
        style = np.random.rand()
        freeze = style < 0.75                     # 40% hang, 35% hang + blocky, 25% blocky
        blocky = style >= 0.4
        return {
            "mask": mask,
            "stale": frame.copy() if freeze else None,
            "block": int(np.random.uniform(12, 28)) if blocky else 0,
            "end": time.monotonic() + np.random.uniform(0.4, 0.8 + level * 0.15),
        }

    def _apply_partial_glitch(self, frame):
        level = self.partial_slider.value()
        if level <= 0:
            self._partial_event = None
            return frame
        event = self._partial_event
        if event is not None and (time.monotonic() >= event["end"]
                                  or event["mask"].shape != frame.shape[:2]):
            event = self._partial_event = None
        if event is None:
            # ~0.1 events/s at level 1, ~1.2 events/s at level 10 (30 ticks per second)
            if np.random.rand() >= level * 0.004:
                return frame
            event = self._partial_event = self._new_partial_event(frame, level)
        out = frame.copy()
        mask = event["mask"]
        if event["stale"] is not None:
            out[mask] = event["stale"][mask]
        if event["block"]:
            h, w = out.shape[:2]
            b = event["block"]
            small = cv2.resize(out, (max(1, w // b), max(1, h // b)), interpolation=cv2.INTER_AREA)
            pixelated = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
            out[mask] = pixelated[mask]
        return out

    # ----------------------------------------------------------- main loop --
    def update_frame(self):
        now = time.monotonic()
        self.spinner_angle = (self.spinner_angle + 12) % 360
        w, h = self.cam_width, self.cam_height

        if self.reader is not None:
            live, capture_stamp, status = self.reader.snapshot()
        else:
            live, capture_stamp, status = None, 0.0, "connecting"
        if live is not None:
            live = fit_frame(live, w, h)

        if self.is_recording_loop:
            self._record_frame(now, live, capture_stamp)

        source = None
        if self.mode == "CONNECTING":
            source = draw_spinner_frame(w, h, self.spinner_angle, self.t("frame_connecting"))
        elif self.mode == "NO_CAMERA":
            source = draw_no_camera_frame(w, h, self.t("frame_no_camera"))
        elif self.mode == "FROZEN" and self.frozen_frame is not None:
            source = self.frozen_frame
        elif self.mode == "LOOP" and self.recorded_loop_frames:
            source = self._loop_frame(now)
        effects_on = self.mode != "LIVE" or self.apply_live_cb.isChecked()
        if source is not None and self.mode in ("FROZEN", "LOOP") and effects_on and self.flip_cb.isChecked():
            source = cv2.flip(source, -1)
        if source is None:
            if live is not None and now - capture_stamp < 3.0:
                source = cv2.flip(live, -1) if effects_on and self.flip_cb.isChecked() else live
            elif self.camera_list is None or (self.reader is not None and status == "connecting") \
                    or (self.reader is None and self.camera_list):
                source = draw_spinner_frame(w, h, self.spinner_angle, self.t("frame_connecting"))
            else:
                source = draw_no_camera_frame(w, h, self.t("frame_no_camera"))

        if self.transition_frame is not None:
            if now < self.transition_until:
                source = self.transition_frame
            else:
                self.transition_frame = None
                self.transition_until = 0.0
                self.last_fps_frame = None

        in_transition = self.transition_frame is not None
        if not in_transition and self.mode not in ("CONNECTING", "NO_CAMERA") and self.stutter_enable_cb.isChecked():
            interval = self.stutter_interval_slider.value()
            duration = self.stutter_duration_slider.value() / 10.0
            phase = (now - self.stutter_start_time) % (interval + duration)
            if phase > interval:
                if self.stutter_spinner_cb.isChecked():
                    source = draw_spinner_frame(w, h, self.spinner_angle, self.t("frame_buffering"))
                else:
                    if self.stutter_frozen_frame is None:
                        self.stutter_frozen_frame = source.copy()
                    source = self.stutter_frozen_frame
            else:
                self.stutter_frozen_frame = None

        if in_transition or self.mode in ("FROZEN", "CONNECTING", "NO_CAMERA"):
            processed = source  # true static image, without randomly changing effects
        elif effects_on:
            processed = self.apply_effects(source)
        else:
            processed = source

        # Time-based FPS limit: averages out to the requested rate on a 30 Hz timer.
        target_fps = self.fps_slider.value()
        if not in_transition and target_fps < OUTPUT_FPS and self.mode not in ("CONNECTING", "NO_CAMERA"):
            period = 1.0 / target_fps
            if self.last_fps_frame is None or now >= self.next_fps_emit - TICK_TOLERANCE:
                self.last_fps_frame = processed
                self.next_fps_emit = max(self.next_fps_emit + period, now - period) \
                    if self.next_fps_emit else now + period
            else:
                processed = self.last_fps_frame
        else:
            self.last_fps_frame = None
            self.next_fps_emit = 0.0

        try:
            if self.vcam is not None:
                if processed.shape[:2] != (self.vcam.height, self.vcam.width):
                    processed = fit_frame(processed, self.vcam.width, self.vcam.height)
                self.vcam.send(np.ascontiguousarray(processed))
            self.last_output_frame = processed
            if self.is_expanded:
                height, width = processed.shape[:2]
                frame = np.ascontiguousarray(processed)
                q_img = QImage(frame.data, width, height, 3 * width, QImage.Format.Format_BGR888)
                scaled = QPixmap.fromImage(q_img).scaled(self.preview_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                self.preview_label.setPixmap(scaled)
        except (cv2.error, RuntimeError, ValueError) as e:
            if now - self.last_output_warning > 10:
                self.last_output_warning = now
                self._set_loop_info("output_error", e=e)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    _window = None
    install_crash_handlers(lambda: _window)
    window = _window = CoffeeBreakCam()
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    sys.exit(app.exec())
