import os
import sys
import time
import json
import hashlib
import threading
import collections
import statistics
import bisect
import atexit
import traceback
import faulthandler
from datetime import datetime

# Suppress OpenCV terminal warnings
os.environ["OPENCV_LOG_LEVEL"] = "OFF"
# Media Foundation's hardware-accelerated transforms are a known cause of very slow
# camera start-up in OpenCV. Must be set before cv2 is imported; can be overridden from
# outside (set the variable to 1 to test with them enabled).
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")
APP_STARTED = __import__("time").monotonic()
STARTUP_MARKS = []          # (seconds since start, what), shown in the diagnostics


def mark_startup(what):
    STARTUP_MARKS.append((__import__("time").monotonic() - APP_STARTED, what))

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
                               QFrame, QMenu, QScrollArea, QWidgetAction, QFileDialog,
                               QButtonGroup, QDialog, QPlainTextEdit)
from PySide6.QtCore import Qt, QTimer, QEvent, QObject, Signal, QSize
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QFont, QIcon

VERSION = "v1.2.0-dev1"
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

try:
    from cv2_enumerate_cameras import enumerate_cameras
    HAS_CAMERA_ENUMERATOR = True
except ImportError:
    HAS_CAMERA_ENUMERATOR = False

try:
    # Background blur. Without onnxruntime the app still works; the option is then disabled.
    import onnxruntime as ort
    # 3 = errors only. ORT otherwise prints a harmless warning per session that it keeps
    # small shape calculations on the CPU (that is intentional and fast).
    ort.set_default_logger_severity(3)
    HAS_ONNX = True
except Exception:
    HAS_ONNX = False

# Bundled files (the model) live next to app.py, or in PyInstaller's _internal folder.
RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
SEGMENT_MODEL = os.path.join(RESOURCE_DIR, "models", "human_segmentation_pphumanseg_2023mar.onnx")
RVM_MODEL = os.path.join(RESOURCE_DIR, "models", "rvm_mobilenetv3_fp32.onnx")

try:
    import psutil
except ImportError:
    psutil = None
try:
    import pynvml
except ImportError:
    pynvml = None

# Background blur settings and their defaults (the nicest look, see README).
BLUR_AMOUNT_DEFAULT = 5                   # 1 (light) .. 10 (very strong)
BLUR_LENS_DEFAULT = True                  # round "bokeh" blur like a real lens
BLUR_DIM_DEFAULT = 10                     # % darker background
BLUR_DESAT_DEFAULT = 20                   # % less colourful background


def blur_radius(amount):
    """Slider value 1..10 → blur radius in pixels at 960 px width (5 ≈ the old 'medium')."""
    return 4.0 + 2.4 * amount

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
        "camera_backend": "Camera API:",
        "backend_msmf": "Automatic (Media Foundation, then DirectShow)",
        "backend_dshow": "DirectShow first (older cameras)",
        "backend_tip": ("Media Foundation is the modern Windows camera API and starts fastest. "
                        "If it gives no picture, the app switches to DirectShow by itself."),
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
        "btn_cam_off": "TURN OFF",
        "btn_cam_on": "TURN ON",
        "btn_cam_tip": "Releases your webcam (its light goes off). The loop and frozen image keep working.",
        "menu_cam_off": "📷  Turn camera off",
        "menu_cam_on": "📷  Turn camera on",
        "status_cam_off": "STATUS: CAMERA OFF",
        "frame_camera_off": "Camera off",
        "p_hedge_in": "Disappear into the hedge",
        "p_hedge_out": "Come out of the hedge",
        "grp_background": " Background ",
        "bg_none": "None",
        "bg_add": "Add image…",
        "bg_tip": "Replaces everything behind you with this image (takes precedence over the blur). Right-click an image to remove it.",
        "bg_remove": "Remove",
        "bg_dialog": "Choose a background image",
        "bg_filter": "Images (*.png *.jpg *.jpeg *.bmp *.webp)",
        "bg_replace_title": "Replace which background?",
        "bg_replace_text": "All 5 places are in use. Choose the image to replace:",
        "bg_cancel": "Cancel",
        "bg_error_title": "Background",
        "bg_error": "This image could not be read.",
        "grp_reactions": " Reactions ",
        "r_confetti": "Confetti",
        "r_fireworks": "Fireworks",
        "r_disco": "Disco",
        "r_hearts": "Hearts",
        "r_balloons": "Balloons",
        "r_rain": "Rain",
        "r_chase": "Chase",
        "r_pacman": "Pac-Man",
        "reactions_tip": "A short animation on top of your image. Click again for a new one.",
        "cb_blur": "Blur background",
        "cb_blur_tip": ("Blurs everything behind you. Only your camera image is blurred: loading "
                        "and 'camera not found' screens stay sharp. Turn off the blur in Teams itself."),
        "grp_blur": " Background blur ",
        "s_blur": "Strength",
        "s_blur_tip": "1 = just a little soft, 10 = completely out of focus.",
        "cb_blur_lens": "Lens blur (round highlights)",
        "cb_blur_lens_tip": "Lamps and windows become soft round discs, like a real camera lens. Slightly heavier.",
        "s_blur_dim": "Dim",
        "s_blur_dim_tip": "Makes the background a little darker, so you stand out more.",
        "s_blur_desat": "Less colour",
        "s_blur_desat_tip": "Makes the background a little greyer and calmer.",
        "blur_engine": "Runs on:",
        "diagnostics": "Performance and diagnostics…",
        "backend": "Compute provider:",
        "model_choice": "Segmentation model:",
        "blur_off": "not loaded (turns on with the blur)",
        "blur_loading": "loading…",
        "blur_missing": "not available (onnxruntime or model file missing)",
        "blur_failed": "could not start: {e}",
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
        "camera_backend": "Camera-API:",
        "backend_msmf": "Automatisch (Media Foundation, dan DirectShow)",
        "backend_dshow": "Eerst DirectShow (oudere camera's)",
        "backend_tip": ("Media Foundation is de moderne camera-API van Windows en start het snelst. "
                        "Geeft die geen beeld, dan schakelt de app zelf over naar DirectShow."),
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
        "btn_cam_off": "UITSCHAKELEN",
        "btn_cam_on": "INSCHAKELEN",
        "btn_cam_tip": "Geeft je webcam vrij (het lampje gaat uit). De loop en het bevroren beeld blijven werken.",
        "menu_cam_off": "📷  Camera uitschakelen",
        "menu_cam_on": "📷  Camera inschakelen",
        "status_cam_off": "STATUS: CAMERA UIT",
        "frame_camera_off": "Camera uit",
        "p_hedge_in": "In de heg verdwijnen",
        "p_hedge_out": "Uit de heg komen",
        "grp_background": " Achtergrond ",
        "bg_none": "Geen",
        "bg_add": "Afbeelding toevoegen…",
        "bg_tip": "Vervangt alles achter je door deze afbeelding (gaat voor de blur). Klik met rechts op een afbeelding om hem te verwijderen.",
        "bg_remove": "Verwijderen",
        "bg_dialog": "Kies een achtergrondafbeelding",
        "bg_filter": "Afbeeldingen (*.png *.jpg *.jpeg *.bmp *.webp)",
        "bg_replace_title": "Welke achtergrond vervangen?",
        "bg_replace_text": "Alle 5 plekken zijn in gebruik. Kies welke afbeelding je wilt vervangen:",
        "bg_cancel": "Annuleren",
        "bg_error_title": "Achtergrond",
        "bg_error": "Deze afbeelding kon niet worden gelezen.",
        "grp_reactions": " Reacties ",
        "r_confetti": "Confetti",
        "r_fireworks": "Vuurwerk",
        "r_disco": "Disco",
        "r_hearts": "Hartjes",
        "r_balloons": "Ballonnen",
        "r_rain": "Regen",
        "r_chase": "Achtervolging",
        "r_pacman": "Pac-Man",
        "reactions_tip": "Een korte animatie over je beeld. Klik opnieuw voor een nieuwe.",
        "cb_blur": "Achtergrond blurren",
        "cb_blur_tip": ("Maakt alles achter je wazig. Alleen je camerabeeld wordt geblurd: laad- en "
                        "'camera niet gevonden'-schermen blijven scherp. Zet de blur in Teams zelf uit."),
        "grp_blur": " Achtergrondblur ",
        "s_blur": "Sterkte",
        "s_blur_tip": "1 = net iets zachter, 10 = helemaal onscherp.",
        "cb_blur_lens": "Lens-blur (ronde lichtpunten)",
        "cb_blur_lens_tip": "Lampen en ramen worden zachte ronde vlekken, zoals bij een echte cameralens. Iets zwaarder.",
        "s_blur_dim": "Dimmen",
        "s_blur_dim_tip": "Maakt de achtergrond iets donkerder, zodat jij meer opvalt.",
        "s_blur_desat": "Minder kleur",
        "s_blur_desat_tip": "Maakt de achtergrond iets grijzer en rustiger.",
        "blur_engine": "Draait op:",
        "diagnostics": "Prestaties en diagnose…",
        "backend": "Rekenprovider:",
        "model_choice": "Segmentatiemodel:",
        "blur_off": "niet geladen (start met de blur)",
        "blur_loading": "laden…",
        "blur_missing": "niet beschikbaar (onnxruntime of modelbestand ontbreekt)",
        "blur_failed": "kon niet starten: {e}",
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


def _normalise_device_path(path):
    return str(path or "").strip().lower().replace("/", "\\")


def _camera_records(info_by_backend):
    """Merge backend entries only when the physical camera can be identified.

    An exact device path is preferred. A unique device name (and USB VID/PID when
    known) is a fallback, but duplicates remain separate to avoid wrong cameras.
    """
    records = []
    for backend in ("DSHOW", "MSMF"):
        entries = info_by_backend.get(backend, [])
        for item in entries:
            name = str(item["name"])
            if _is_ignored_camera(name):
                continue
            path = _normalise_device_path(item.get("path"))
            vid, pid = item.get("vid"), item.get("pid")
            matches = [r for r in records if backend not in r["indices"] and
                       path and path in r["paths"]]
            if not matches:
                # Only match by name when unique on BOTH backends. USB IDs must
                # agree when both sides expose them.
                same_in_new = sum(str(x["name"]).casefold() == name.casefold()
                                  for x in entries)
                same_in_old = sum(r["name"].casefold() == name.casefold()
                                  for r in records if backend not in r["indices"])
                if same_in_new == same_in_old == 1:
                    matches = [r for r in records
                               if backend not in r["indices"] and
                               r["name"].casefold() == name.casefold() and
                               (not vid or not r["vid"] or vid == r["vid"]) and
                               (not pid or not r["pid"] or pid == r["pid"])]
            if len(matches) == 1:
                rec = matches[0]
                rec["indices"][backend] = int(item["index"])
                if path:
                    rec["paths"].add(path)
            else:
                records.append({"name": name, "indices": {backend: int(item["index"])},
                                "paths": {path} if path else set(), "vid": vid, "pid": pid})
    for r in records:
        identity = (sorted(r["paths"])[0] if r["paths"] else
                    f"{r['name'].casefold()}:{sorted(r['indices'].items())}")
        r["key"] = "camera-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
        r["label"] = r["name"] + (" (DirectShow)" if "MSMF" not in r["indices"] else
                                      " (Media Foundation)" if "DSHOW" not in r["indices"] else "")
    return records


def list_camera_names():
    """Enumerate cameras independently for MSMF and DirectShow.

    Fallback to DirectShow-only discovery if the optional enumerator is missing.
    Never copy a DirectShow index into the MSMF namespace.
    """
    if HAS_CAMERA_ENUMERATOR:
        by_backend = {}
        for name in ("DSHOW", "MSMF"):
            api = capture_backend_code(name)
            if not cv2.videoio_registry.hasBackend(api):
                continue
            try:
                by_backend[name] = [{"index": c.index, "name": c.name,
                                      "path": getattr(c, "path", None),
                                      "vid": getattr(c, "vid", None),
                                      "pid": getattr(c, "pid", None)}
                                     for c in enumerate_cameras(api)]
            except Exception:
                by_backend[name] = []
        if any(by_backend.values()):
            return _camera_records(by_backend)
    if HAS_PYGRABBER:
        try:
            return _camera_records({"DSHOW": [
                {"index": i, "name": n} for i, n in enumerate(FilterGraph().get_input_devices())]})
        except Exception:
            pass
    return None


def capture_backend_code(name):
    return cv2.CAP_MSMF if name == "MSMF" else cv2.CAP_DSHOW


def set_capture_properties(cap, width, height):
    """Request a format; the driver may negotiate a different actual FOURCC or FPS.

    Every cap.set() can make the driver renegotiate the stream (on Media Foundation this
    costs seconds per call), so only values that differ are set. Returns the time spent
    per property, for the diagnostics.
    """
    timings = []
    mjpg = cv2.VideoWriter_fourcc(*"MJPG")
    wanted = ((cv2.CAP_PROP_FOURCC, mjpg, "fourcc"), (cv2.CAP_PROP_FRAME_WIDTH, width, "width"),
              (cv2.CAP_PROP_FRAME_HEIGHT, height, "height"), (cv2.CAP_PROP_FPS, OUTPUT_FPS, "fps"))
    for prop, value, label in wanted:
        try:
            current = cap.get(prop)
        except cv2.error:
            current = None
        if current is not None and (int(current) == int(value) if prop == cv2.CAP_PROP_FOURCC
                                    else abs(current - value) < 0.5):
            timings.append((label, None))           # already right: skipped
            continue
        t0 = time.monotonic()
        cap.set(prop, value)
        timings.append((label, time.monotonic() - t0))
    return timings


def scan_cameras_slow():
    """Last resort: enumerate DirectShow devices without assigning MSMF indices."""
    cams = []
    for index in range(5):
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        try:
            if cap.isOpened():
                ok, _ = cap.read()
                if ok:
                    cams.append({"index": index, "name": f"Camera {index}"})
        finally:
            cap.release()
    return _camera_records({"DSHOW": cams})


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
    """Capture frames in a dedicated thread. Prefer MSMF; fall back to DirectShow.

    OpenCV exposes camera *indices*, not stable Windows device IDs. Camera names
    come from DirectShow enumeration; Media Foundation can use a different order.
    The selected capture backend is therefore exposed in the diagnostics and UI.
    """

    def __init__(self, index, width, height, backend="MSMF"):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._requested = (dict(index) if isinstance(index, dict) else {"DSHOW": index, "MSMF": index}, width, height, backend)
        self._frame = None
        self._timestamp = 0.0
        self._status = "connecting"
        self._backend = None
        self._actual_format = None
        self._frame_times = collections.deque(maxlen=240)
        self._frames_received = 0
        self._generation = 0
        self._events = collections.deque(maxlen=12)   # start-up timeline, newest last
        self._thread = threading.Thread(target=self._run, name="CameraCapture", daemon=True)
        self._thread.start()

    def configure(self, index, width, height, backend=None):
        with self._lock:
            selected_backend = backend or self._requested[3]
            self._requested = (dict(index) if isinstance(index, dict) else {"DSHOW": index, "MSMF": index}, width, height, selected_backend)
            self._generation += 1
            self._frame = None
            self._timestamp = 0.0
            self._status = "connecting"
            self._backend = None
            self._actual_format = None
            self._frame_times.clear()
            self._frames_received = 0

    def snapshot(self):
        with self._lock:
            return self._frame, self._timestamp, self._status

    def diagnostics(self):
        """Read actual capture cadence, independent from the UI polling frequency."""
        with self._lock:
            times = list(self._frame_times)
            age = time.monotonic() - self._timestamp if self._timestamp else None
            backend = self._backend
            details = self._actual_format
            count = self._frames_received
        now = time.monotonic()
        recent = [t for t in times if t >= now - 5.0]
        fps = ((len(recent) - 1) / (recent[-1] - recent[0])
               if len(recent) >= 2 and recent[-1] > recent[0] else None)
        with self._lock:
            requested_indices = dict(self._requested[0])
            events = list(self._events)
        return {"backend": backend, "format": details, "fps": fps,
                "frames": count, "age": age, "device_indices": requested_indices, "events": events}

    def _log(self, text):
        with self._lock:
            self._events.append(f"{time.monotonic() - APP_STARTED:7.1f}s  {text}")

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self):
        cap = None
        active_generation = -1
        failures = 0
        next_retry = 0.0
        backend_number = 0
        backend_opened_at = 0.0
        had_good_frame = False
        try:
            while not self._stop.is_set():
                with self._lock:
                    requested, generation = self._requested, self._generation
                if generation != active_generation:
                    active_generation = generation
                    backend_number = 0
                    next_retry = 0.0
                    failures = 0
                    had_good_frame = False
                    if cap is not None:
                        cap.release()
                        cap = None
                index, width, height, preferred = requested
                backends = [preferred, "DSHOW" if preferred == "MSMF" else "MSMF"]
                # A fallback is safe only with the index for this exact device.
                backends = [b for b in backends if b in index]
                if not backends:
                    with self._lock:
                        if generation == self._generation:
                            self._status = "missing"
                            self._backend = None
                    self._stop.wait(.25)
                    continue
                if cap is None:
                    if time.monotonic() < next_retry:
                        self._stop.wait(.1)
                        continue
                    backend = backends[min(backend_number, len(backends) - 1)]
                    t_open = time.monotonic()
                    try:
                        candidate = cv2.VideoCapture(index[backend], capture_backend_code(backend))
                    except (cv2.error, OSError):
                        candidate = None
                    open_s = time.monotonic() - t_open
                    if candidate is not None and candidate.isOpened():
                        try:
                            timings = set_capture_properties(candidate, width, height)
                            done = ", ".join(f"{name} {t:.1f}s" for name, t in timings if t is not None)
                            skipped = [name for name, t in timings if t is None]
                            self._log(f"{backend}[{index[backend]}] opened in {open_s:.1f}s; set "
                                      f"{done or 'nothing'}" + (f" (already ok: {', '.join(skipped)})" if skipped else ""))
                            cap = candidate
                            actual = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                                      int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                                      float(cap.get(cv2.CAP_PROP_FPS)))
                        except Exception:
                            candidate.release()
                            cap = None
                        if cap is not None:
                            failures = 0
                            backend_opened_at = time.monotonic()
                            had_good_frame = False
                            with self._lock:
                                if generation == self._generation:
                                    self._backend = backend
                                    self._actual_format = actual
                                    self._status = "connecting"
                    elif candidate is not None:
                        candidate.release()
                        self._log(f"{backend}[{index[backend]}] could not be opened ({open_s:.1f}s)")
                    else:
                        self._log(f"{backend}[{index[backend]}] open failed ({open_s:.1f}s)")
                    if cap is None:
                        if backend_number + 1 < len(backends):
                            backend_number += 1
                            continue
                        with self._lock:
                            if generation == self._generation:
                                self._status = "missing"
                        next_retry = time.monotonic() + 2.0
                        self._stop.wait(.15)
                        continue
                try:
                    ok, frame = cap.read()
                except (cv2.error, OSError):
                    ok, frame = False, None
                if ok and frame is not None and frame.size:
                    if not had_good_frame:
                        self._log(f"{backend}: first frame {frame.shape[1]}×{frame.shape[0]} after "
                                  f"{time.monotonic() - backend_opened_at:.1f}s")
                    had_good_frame = True
                    failures = 0
                    stamp = time.monotonic()
                    with self._lock:
                        if generation == self._generation:
                            self._frame = frame
                            self._timestamp = stamp
                            self._frame_times.append(stamp)
                            self._frames_received += 1
                            self._status = "ok"
                else:
                    failures += 1
                    # MSMF often needs a warm-up period before its first frame.
                    # Five immediate failures previously caused premature DSHOW fallback.
                    grace = (backend == "MSMF" and not had_good_frame
                             and time.monotonic() - backend_opened_at < 4.0)
                    if failures >= 5 and not grace:
                        self._log(f"{backend}: no frames ({failures} failed reads) — "
                                  + ("trying next API" if backend_number + 1 < len(backends) else "retrying in 2 s"))
                        cap.release()
                        cap = None
                        if backend_number + 1 < len(backends):
                            backend_number += 1
                            next_retry = 0.0
                        else:
                            with self._lock:
                                if generation == self._generation:
                                    self._status = "missing"
                                    self._frame = None
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


class BackgroundSegmenter:
    """Runs the selected ONNX model on a worker thread, retaining only the newest frame."""
    SIZE = 192

    PROVIDERS = {"DirectML": "DmlExecutionProvider", "CPU": "CPUExecutionProvider"}

    def __init__(self, model="PP-HumanSeg", provider="Auto"):
        self.model = model
        self.requested_provider = provider
        self.engine = "loading"
        self.provider_name = ""
        self.error = ""
        self.events = collections.deque(maxlen=40)
        self.inference_times = collections.deque(maxlen=180)
        self.processed_frames = collections.deque(maxlen=180)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pending = None
        self._mask = None
        self._generation = 0
        self._thread = threading.Thread(target=self._run, name="BackgroundSegmenter", daemon=True)
        self._thread.start()

    def submit(self, frame):
        with self._lock:
            self._pending = frame
        self._wake.set()

    def mask(self):
        with self._lock:
            return self._mask

    def snapshot(self):
        with self._lock:
            return (self.engine, self.provider_name, self.error,
                    list(self.inference_times), list(self.processed_frames), list(self.events))

    def reset(self):
        with self._lock:
            self._mask = None
            self._pending = None
            self._generation += 1
        self._wake.set()

    def close(self):
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=2.0)

    def _create_session(self):
        available = ort.get_available_providers()
        if self.requested_provider == "Auto":
            choices = ["DirectML", "CPU"]
        else:
            choices = [self.requested_provider]
        path = RVM_MODEL if self.model == "RVM MobileNetV3" else SEGMENT_MODEL
        failures = []
        for choice in choices:
            provider = self.PROVIDERS[choice]
            if provider not in available:
                failures.append(f"{choice}: not installed")
                continue
            if choice == "CPU" and self.model == "RVM MobileNetV3" and self.requested_provider == "Auto":
                # RVM works at full resolution: far too slow for live video on a CPU
                # (100+ ms per frame). Without a usable GPU, use the light model instead.
                self.model, path = "PP-HumanSeg", SEGMENT_MODEL
                failures.append("RVM needs a GPU; using PP-HumanSeg on the CPU")
            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            opts.intra_op_num_threads = 2
            # By default ORT's worker threads keep spinning (busy-waiting) between runs,
            # which shows up as high CPU use even when the model runs on the GPU.
            opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
            opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
            if choice == "DirectML":
                opts.enable_mem_pattern = False
                opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            try:
                session = ort.InferenceSession(path, sess_options=opts, providers=[provider])
                # ORT otherwise can silently retry with the CPU on run() failure.
                session.disable_fallback()
                if session.get_providers()[0] != provider:
                    raise RuntimeError(f"requested {provider}, got {session.get_providers()}")
                self.events.append(f"Active: {provider} ({self.model})")
                if failures:
                    self.events.extend(failures)
                return session, choice
            except Exception as exc:
                failures.append(f"{choice}: {str(exc).splitlines()[0][:220]}")
        raise RuntimeError(" | ".join(failures) or "No execution provider")

    def _run(self):
        try:
            session, engine = self._create_session()
            inputs = session.get_inputs()
            input_names = [i.name for i in inputs]
            if self.model == "RVM MobileNetV3":
                if len(inputs) != 6:
                    raise RuntimeError(f"RVM needs 6 inputs, got {len(inputs)}")
                rec = [np.zeros((1, 1, 1, 1), np.float32) for _ in range(4)]
                # Recurrent state requires streaming frame continuity.
                # Only alpha + recurrent states: the foreground output (fgr) is a full-size
                # 3-channel float image the app never uses; copying it back costs time.
                out_names = [o.name for o in session.get_outputs()]
                rvm_outputs = [out_names[1]] + out_names[2:6]
            else:
                # Warm up GPU without contaminating measured inference statistics.
                session.run(None, {input_names[0]: np.zeros((1, 3, self.SIZE, self.SIZE), np.float32)})
            self.provider_name = session.get_providers()[0]
            self.engine = engine.lower() if engine != "DirectML" else "gpu"
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"[:350]
            self.events.append(self.error)
            self.engine = "failed"
            return
        smoothed = None
        last_frame_size = None
        last_generation = 0
        while not self._stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            with self._lock:
                frame, self._pending = self._pending, None
                generation = self._generation
            if generation != last_generation:
                smoothed = None
                last_frame_size = None
                last_generation = generation
            if frame is None:
                continue
            try:
                t0 = time.perf_counter()
                if self.model == "RVM MobileNetV3":
                    if last_frame_size != frame.shape[:2]:
                        rec = [np.zeros((1, 1, 1, 1), np.float32) for _ in range(4)]
                        last_frame_size = frame.shape[:2]
                    # BGR→RGB, HWC→NCHW and scaling to 0..1 in one pass (~1 ms faster at 540p).
                    src = cv2.dnn.blobFromImage(frame, 1 / 255.0, swapRB=True)
                    # RVM's recommended ratios keep the internal width around 480 px
                    # (1280×720 → 0.375, 1920×1080 → 0.25); 0.5 at 720p did more work.
                    ratio = min(1.0, 480.0 / frame.shape[1])
                    feed = dict(zip(input_names, [src, *rec, np.array([ratio], np.float32)]))
                    outputs = session.run(rvm_outputs, feed)
                    rec = [np.ascontiguousarray(x) for x in outputs[1:5]]
                    person = np.squeeze(outputs[0]).astype(np.float32)
                    person = np.clip(person, 0, 1)
                else:
                    rgb = cv2.cvtColor(cv2.resize(frame, (self.SIZE, self.SIZE), interpolation=cv2.INTER_AREA),
                                       cv2.COLOR_BGR2RGB)
                    blob = (rgb.astype(np.float32) / 127.5 - 1).transpose(2, 0, 1)[np.newaxis]
                    person = session.run(None, {input_names[0]: blob})[0][0, 1]
                    person = person if smoothed is None else cv2.addWeighted(person, 0.6, smoothed, 0.4, 0)
                    smoothed = person
                ms = (time.perf_counter() - t0) * 1000
                with self._lock:
                    self._mask = person
                    self.inference_times.append(ms)
                    self.processed_frames.append(time.monotonic())
            except Exception as exc:
                with self._lock:
                    self.error = f"Runtime error: {type(exc).__name__}: {exc}"[:350]
                    self.events.append(self.error)
                self.engine = "failed"
                return



_DISK_KERNELS = {}


def _disk_kernel(radius):
    """Round, normalised kernel: highlights become soft discs like real lens bokeh."""
    if radius not in _DISK_KERNELS:
        k = np.zeros((2 * radius + 1, 2 * radius + 1), np.float32)
        cv2.circle(k, (radius, radius), radius, 1.0, -1, cv2.LINE_AA)
        _DISK_KERNELS[radius] = k / k.sum()
    return _DISK_KERNELS[radius]


def blur_background(frame, mask, radius, lens=BLUR_LENS_DEFAULT, dim=0.0, desaturate=0.0):
    """Person sharp, background blurred.

    Halo-free: only the background is blurred (the person is weighted out and the result
    renormalised), so your hair and shoulders don't smear into a dark or coloured rim.
    All work happens on a quarter-size copy and is scaled up once: much faster, same look.
    dim / desaturate (0..1) make the background a little calmer.
    """
    h, w = frame.shape[:2]
    alpha = mask_to_alpha(mask, w, h)
    q = (max(1, w // 4), max(1, h // 4))
    small = cv2.resize(frame, q, interpolation=cv2.INTER_AREA).astype(np.float32)
    background = cv2.resize(1.0 - alpha, q, interpolation=cv2.INTER_AREA)
    sigma = max(1.0, radius * (w / 960) / 4)
    if lens:
        kernel = _disk_kernel(max(1, int(round(sigma * 1.6))))
        blur = lambda x: cv2.filter2D(x, -1, kernel, borderType=cv2.BORDER_REFLECT)
    else:
        blur = lambda x: cv2.GaussianBlur(x, (0, 0), sigma)
    weight = blur(background)[..., None]
    summed = blur(small * background[..., None])
    # Renormalise. The small epsilon only matters deep inside the person, where there is
    # no background nearby: it falls back to the original pixel, which the person covers anyway.
    eps = 1e-2
    soft = (summed + small * eps) / (weight + eps)
    if desaturate > 0:
        grey = cv2.cvtColor(soft, cv2.COLOR_BGR2GRAY)[..., None]
        soft = soft * (1.0 - desaturate) + grey * desaturate
    if dim > 0:
        soft = soft * (1.0 - dim)
    blurred = cv2.resize(np.clip(soft, 0, 255).astype(np.uint8), (w, h), interpolation=cv2.INTER_LINEAR)
    return cv2.blendLinear(frame, blurred, alpha, 1.0 - alpha)


def mask_to_alpha(mask, w, h):
    """Person mask → alpha at frame size.

    A low-resolution segmentation (PP-HumanSeg, 192×192) is softened and firmed up before
    scaling. A full-resolution matte (RVM) already has proper soft edges and hair: firming
    it would make hair hard, and blurring it at 720p costs CPU, so it is used directly.
    """
    if mask.shape[:2] == (h, w):
        return mask
    alpha = cv2.GaussianBlur(mask, (0, 0), 1.2)
    alpha = np.clip((alpha - 0.3) * 2.5, 0.0, 1.0)
    return cv2.resize(alpha, (w, h), interpolation=cv2.INTER_LINEAR)


EMOJI_FONTS = ["Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji"]
REACTION_EMOJIS = ["👍", "👎", "😂", "😮", "😢", "👏", "🙈"]
REACTION_EFFECTS = [("confetti", "🎊"), ("fireworks", "🎆"), ("disco", "🪩"), ("chase", "☕"),
                    ("hearts", "❤️"), ("balloons", "🎈"), ("rain", "🌧️"), ("pacman", "👻")]


def render_emoji(char, size):
    """Draw one emoji with the system's colour-emoji font. Returns premultiplied BGRA uint8."""
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    font = QFont()
    font.setFamilies(EMOJI_FONTS)
    font.setPixelSize(int(size * 0.8))
    painter.setFont(font)
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, char)
    painter.end()
    ptr = image.constBits()
    data = np.frombuffer(ptr, np.uint8, count=image.sizeInBytes()).reshape(size, image.bytesPerLine() // 4, 4)
    return data[:, :size].copy()          # little-endian ARGB32 = B, G, R, A in memory


def paste_sprite(dst, sprite, cx, cy, opacity=1.0):
    """Alpha-blend a premultiplied BGRA sprite centred at (cx, cy), clipped to the frame."""
    sh, sw = sprite.shape[:2]
    h, w = dst.shape[:2]
    x0, y0 = int(cx - sw / 2), int(cy - sh / 2)
    x1, y1 = max(0, x0), max(0, y0)
    x2, y2 = min(w, x0 + sw), min(h, y0 + sh)
    if x1 >= x2 or y1 >= y2 or opacity <= 0:
        return
    part = sprite[y1 - y0:y2 - y0, x1 - x0:x2 - x0].astype(np.float32) * (opacity / 255.0)
    alpha = part[..., 3:4]
    region = dst[y1:y2, x1:x2]
    region[:] = np.clip(part[..., :3] * 255.0 + region * (1.0 - alpha), 0, 255).astype(np.uint8)


class ReactionPlayer:
    """Short animated overlays (confetti, fireworks, emoji…) drawn on top of the image.

    Started with a button; one reaction at a time (a new one replaces the current one).
    Everything is generated here, nothing is detected in the camera image.
    """
    DURATION = {"confetti": 4.5, "fireworks": 5.0, "disco": 5.5, "chase": 4.5, "pacman": 4.5,
                "hearts": 4.0, "balloons": 5.0, "rain": 5.0, "emoji": 3.2}

    def __init__(self):
        self.kind = None
        self.start = 0.0
        self.state = None
        self._sprites = {}
        self._rng = np.random.default_rng()

    @property
    def active(self):
        return self.kind is not None

    def stop(self):
        self.kind = None
        self.state = None

    def sprite(self, char, size):
        key = (char, size)
        if key not in self._sprites:
            self._sprites[key] = render_emoji(char, size)
        return self._sprites[key]

    # ------------------------------------------------------------------ start --
    def play(self, kind, width, height, char=None):
        self.kind = kind
        self.start = time.monotonic()
        self.size = (width, height)
        scale = width / 960
        rng = self._rng
        if kind == "confetti":
            n = 170
            self.state = {
                "x": rng.uniform(0, width, n), "y": rng.uniform(-height * 1.2, -10, n),
                "vy": rng.uniform(90, 170, n) * scale, "sway": rng.uniform(15, 45, n) * scale,
                "phase": rng.uniform(0, 6.3, n), "freq": rng.uniform(1.5, 3.5, n),
                "angle": rng.uniform(0, 6.3, n), "spin": rng.uniform(-6, 6, n),
                "w": rng.uniform(6, 11, n) * scale, "h": rng.uniform(3, 6, n) * scale,
                "color": rng.choice(np.array([(60, 60, 235), (60, 200, 255), (80, 210, 90), (235, 150, 50),
                                              (200, 80, 220), (50, 240, 240), (240, 240, 240)]), n),
            }
        elif kind == "fireworks":
            bursts = []
            for i in range(6):
                bursts.append({
                    "t": 0.15 + i * 0.7 + rng.uniform(-0.15, 0.15),
                    "x": rng.uniform(0.15, 0.85) * width, "y": rng.uniform(0.15, 0.45) * height,
                    "color": np.array(rng.choice([(80, 80, 255), (80, 220, 255), (255, 160, 60),
                                                  (120, 255, 120), (255, 120, 255), (255, 255, 255)])),
                    "angles": rng.uniform(0, 2 * np.pi, 110), "speeds": rng.uniform(80, 240, 110) * scale,
                })
            self.state = {"bursts": bursts}
        elif kind == "disco":
            self.state = {"beams": [(x, rng.uniform(0, 6.3), rng.uniform(0.6, 1.4), c) for x, c in
                                    zip((0.05, 0.35, 0.65, 0.95),
                                        ((255, 60, 200), (60, 255, 120), (255, 200, 40), (60, 140, 255)))]}
        elif kind in ("chase", "pacman"):
            self.state = {}                       # everything follows from the time
        elif kind == "rain":
            n = 260
            self.state = {"x": rng.uniform(0, width, n), "y": rng.uniform(0, height, n),
                          "v": rng.uniform(500, 800, n) * scale, "len": rng.uniform(12, 26, n) * scale}
        else:
            # Floating sprites: hearts, balloons or one emoji.
            char = {"hearts": "❤️", "balloons": "🎈"}.get(kind, char or "👍")
            count, sizes, rise = {"hearts": (22, (34, 64), (1.8, 2.8)),
                                  "balloons": (14, (70, 110), (2.8, 4.0)),
                                  "emoji": (12, (48, 90), (2.0, 3.0))}[kind]
            items = []
            for i in range(count):
                size = int(rng.uniform(*sizes) * scale) | 1
                items.append({"sprite": self.sprite(char, max(16, size)),
                              "x": rng.uniform(0.08, 0.92) * width, "delay": rng.uniform(0, 1.3),
                              "rise": rng.uniform(*rise), "sway": rng.uniform(10, 30) * scale,
                              "phase": rng.uniform(0, 6.3)})
            if kind == "emoji":
                # Just one emoji in the middle: calmer than a stream of them.
                items = [{"sprite": self.sprite(char, max(32, int(180 * scale))), "big": True}]
            self.state = {"items": items}

    # ----------------------------------------------------------------- render --
    def render(self, frame, now):
        """Return a new frame with the reaction drawn on it (the input is never modified)."""
        if self.kind is None:
            return frame
        t = now - self.start
        duration = self.DURATION[self.kind if self.kind in self.DURATION else "emoji"]
        if t >= duration:
            self.stop()
            return frame
        h, w = frame.shape[:2]
        if (w, h) != self.size:          # output resolution changed: restart cleanly
            self.stop()
            return frame
        envelope = min(1.0, t / 0.35, (duration - t) / 0.7)
        out = frame.copy()
        getattr(self, "_draw_" + (self.kind if self.kind in ("confetti", "fireworks", "disco", "rain",
                                                             "chase", "pacman")
                                  else "floating"))(out, t, envelope)
        return out

    def _draw_confetti(self, out, t, envelope):
        s = self.state
        h = out.shape[0]
        ys = s["y"] + s["vy"] * t
        xs = s["x"] + np.sin(s["phase"] + s["freq"] * t) * s["sway"]
        angles = s["angle"] + s["spin"] * t
        flip = np.abs(np.cos(angles * 1.7))        # pieces "tumble": width shrinks and grows
        for i in np.nonzero((ys > -20) & (ys < h + 20))[0]:
            c, sn = np.cos(angles[i]), np.sin(angles[i])
            hw, hh = s["w"][i] * (0.3 + 0.7 * flip[i]) / 2, s["h"][i] / 2
            corners = np.array([[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]])
            pts = corners @ np.array([[c, sn], [-sn, c]]) + (xs[i], ys[i])
            cv2.fillConvexPoly(out, pts.astype(np.int32), tuple(int(v) for v in s["color"][i]), cv2.LINE_AA)

    def _draw_fireworks(self, out, t, envelope):
        h, w = out.shape[:2]
        # Darken a little so the sparks stand out, like an evening sky.
        cv2.convertScaleAbs(out, out, alpha=1.0 - 0.35 * envelope)
        glow = np.zeros((h // 2, w // 2, 3), np.uint8)
        gravity = 90 * (w / 960)
        for b in self.state["bursts"]:
            age = t - b["t"]
            if age < -0.45 or age > 1.6:
                continue
            if age < 0:                                       # rocket going up
                ry = b["y"] + (-age) * h * 0.9
                cv2.circle(out, (int(b["x"]), int(ry)), max(2, int(w / 400)), (200, 230, 255), -1, cv2.LINE_AA)
                cv2.circle(glow, (int(b["x"] / 2), int(ry / 2)), 3, (120, 160, 220), -1)
                continue
            fade = max(0.0, 1.0 - age / 1.6) ** 1.3
            dist = b["speeds"] * (1 - np.exp(-age * 2.2)) / 2.2 * 2.2
            xs = b["x"] + np.cos(b["angles"]) * dist
            ys = b["y"] + np.sin(b["angles"]) * dist + 0.5 * gravity * age ** 2
            color = tuple(int(min(255, v * 1.2) * fade) for v in b["color"])
            core = tuple(int(min(255, v * 0.6 + 150) * fade) for v in b["color"])
            radius = max(1, int(w / 300 * (1.4 - age / 1.6)))
            for x, y in zip(xs.astype(int), ys.astype(int)):
                cv2.circle(glow, (x // 2, y // 2), radius + 1, color, -1)
                cv2.circle(out, (x, y), radius, core, -1, cv2.LINE_AA)
        glow = cv2.add(glow, cv2.GaussianBlur(glow, (0, 0), 5))
        cv2.add(out, cv2.resize(glow, (w, h)), out)

    def _draw_disco(self, out, t, envelope):
        h, w = out.shape[:2]
        cv2.convertScaleAbs(out, out, alpha=1.0 - 0.45 * envelope)
        # Colour wash that cycles through the rainbow.
        hue = int((t * 70) % 180)
        wash = cv2.cvtColor(np.uint8([[[hue, 255, 255]]]), cv2.COLOR_HSV2BGR)[0, 0]
        cv2.addWeighted(out, 1.0, np.full_like(out, wash), 0.18 * envelope, 0, out)
        glow = np.zeros((h // 2, w // 2, 3), np.uint8)
        length = int(1.4 * max(w, h) / 2)
        for i, (x, phase, speed, color) in enumerate(self.state["beams"]):
            origin = (int(x * w / 2), -4)
            for k in (-1, 1):
                angle = np.pi / 2 + k * 0.25 + np.sin(phase + t * speed * 2.2) * 0.75
                end = (int(origin[0] + np.cos(angle) * length), int(origin[1] + np.sin(angle) * length))
                c = tuple(int(v * envelope) for v in color)
                cv2.line(glow, origin, end, c, max(2, w // 320), cv2.LINE_AA)
        # A few light spots that drift over the room.
        for i in range(6):
            sx = int((0.5 + 0.45 * np.sin(t * (0.9 + i * 0.23) + i)) * w / 2)
            sy = int((0.5 + 0.4 * np.cos(t * (0.7 + i * 0.31) + i * 2)) * h / 2)
            sh = int((hue + i * 30) % 180)
            sc = cv2.cvtColor(np.uint8([[[sh, 200, 255]]]), cv2.COLOR_HSV2BGR)[0, 0] * (0.6 * envelope)
            cv2.circle(glow, (sx, sy), max(4, w // 70), tuple(int(v) for v in sc), -1, cv2.LINE_AA)
        soft = cv2.GaussianBlur(glow, (0, 0), 3)
        cv2.add(out, cv2.resize(cv2.add(glow, soft), (w, h)), out)

    def _draw_rain(self, out, t, envelope):
        h, w = out.shape[:2]
        gloom = cv2.addWeighted(out, 0.6, np.full_like(out, (120, 90, 70)), 0.25, 0)
        cv2.addWeighted(gloom, envelope, out, 1.0 - envelope, 0, out)
        s = self.state
        ys = (s["y"] + s["v"] * t) % (h + 40) - 20
        xs = s["x"] - ys * 0.08
        color = tuple(int(v * (0.5 + 0.5 * envelope)) for v in (235, 215, 190))
        for x, y, length in zip(xs.astype(int), ys.astype(int), s["len"].astype(int)):
            cv2.line(out, (x, y), (x - length // 12, y + length), color, 1, cv2.LINE_AA)

    def _draw_chase(self, out, t, envelope):
        """A running coffee cup, chased across the screen by a sugar cube."""
        h, w = out.shape[:2]
        size = h * 0.2
        # Shoe soles rest on the bottom edge of the frame; the bounce lifts them off it.
        # The extra fraction sinks the sole into the edge, so a mid-stride foot still
        # reads as standing on the border instead of hovering just above it.
        ground = h + 1 + size * 0.02
        duration = self.DURATION["chase"]
        lead_x = -0.15 * w + (1.45 * w) * (t / duration)
        self._draw_cup(out, lead_x, ground, size, t)
        # The cube's legs start slightly higher on its body, so its floor is shifted
        # until those shoes land on the same edge.
        self._draw_sugar(out, lead_x - 0.32 * w, ground + 0.05 * size * 0.8, size * 0.8, t)

    @staticmethod
    def _legs(out, x, y_hip, size, t, speed, color):
        swing = np.sin(t * speed) * 0.6
        for side, phase in ((-1, 0.0), (1, np.pi)):
            a = swing * np.cos(phase)
            hip = (int(x + side * size * 0.12), int(y_hip))
            foot = (int(hip[0] + np.sin(a) * size * 0.35), int(y_hip + np.cos(a) * size * 0.35))
            thick = max(2, int(size * 0.06))
            cv2.line(out, hip, foot, color, thick, cv2.LINE_AA)
            cv2.ellipse(out, (foot[0] + int(size * 0.05), foot[1]), (int(size * 0.09), int(size * 0.045)),
                        0, 0, 360, color, -1, cv2.LINE_AA)

    @staticmethod
    def _eyes(out, cx, cy, size, look, worried):
        for dx in (-0.14, 0.14):
            c = (int(cx + dx * size), int(cy))
            cv2.circle(out, c, max(2, int(size * 0.09)), (255, 255, 255), -1, cv2.LINE_AA)
            cv2.circle(out, c, max(2, int(size * 0.09)), (40, 40, 40), max(1, int(size * 0.015)), cv2.LINE_AA)
            cv2.circle(out, (c[0] + int(look * size * 0.04), c[1]), max(1, int(size * 0.04)), (20, 20, 20), -1,
                       cv2.LINE_AA)
            # Eyebrows: worried (up in the middle) or determined (down in the middle).
            y_in = cy - size * 0.15 - (size * 0.05 if worried else -size * 0.04)
            y_out = cy - size * 0.15
            p_in = (int(c[0] - dx / abs(dx) * size * 0.09), int(y_in))
            p_out = (int(c[0] + dx / abs(dx) * size * 0.09), int(y_out))
            cv2.line(out, p_in, p_out, (40, 40, 40), max(2, int(size * 0.03)), cv2.LINE_AA)

    def _draw_cup(self, out, x, ground, size, t):
        bounce = abs(np.sin(t * 11)) * size * 0.08
        bottom = ground - size * 0.35 - bounce
        top = bottom - size
        left, right = x - size * 0.4, x + size * 0.4
        ink = (50, 50, 50)
        self._legs(out, x, bottom - 2, size, t, 22, ink)
        # Handle on the back (left, it runs to the right).
        cv2.ellipse(out, (int(left), int(top + size * 0.5)), (int(size * 0.2), int(size * 0.24)),
                    0, 90, 270, (245, 245, 245), max(3, int(size * 0.09)), cv2.LINE_AA)
        cv2.ellipse(out, (int(left), int(top + size * 0.5)), (int(size * 0.2), int(size * 0.24)),
                    0, 90, 270, ink, max(1, int(size * 0.02)), cv2.LINE_AA)
        body = np.array([[left, top], [right, top], [right - size * 0.06, bottom], [left + size * 0.06, bottom]],
                        np.int32)
        cv2.fillConvexPoly(out, body, (245, 245, 245), cv2.LINE_AA)
        cv2.polylines(out, [body], True, ink, max(2, int(size * 0.025)), cv2.LINE_AA)
        cv2.ellipse(out, (int(x), int(top)), (int(size * 0.4), int(size * 0.09)), 0, 0, 360, (30, 60, 110), -1,
                    cv2.LINE_AA)
        cv2.ellipse(out, (int(x), int(top)), (int(size * 0.4), int(size * 0.09)), 0, 0, 360, ink,
                    max(2, int(size * 0.025)), cv2.LINE_AA)
        # Steam, blown backwards by the speed.
        for i in range(3):
            pts = []
            for k in range(8):
                yy = top - size * 0.12 - k * size * 0.07
                xx = x - size * 0.15 + i * size * 0.15 - k * size * 0.05 + np.sin(t * 6 + k + i) * size * 0.03
                pts.append((int(xx), int(yy)))
            cv2.polylines(out, [np.array(pts, np.int32)], False, (225, 225, 225), max(1, int(size * 0.025)),
                          cv2.LINE_AA)
        # Scared face, looking back at the sugar cube.
        self._eyes(out, x + size * 0.03, top + size * 0.38, size, -1, worried=True)
        cv2.ellipse(out, (int(x + size * 0.03), int(top + size * 0.68)), (int(size * 0.07), int(size * 0.09)),
                    0, 0, 360, (60, 40, 40), -1, cv2.LINE_AA)
        # A few motion lines behind it.
        for i in range(3):
            y = int(top + size * (0.25 + i * 0.25))
            cv2.line(out, (int(left - size * 0.35 - i * 6), y), (int(left - size * 0.75 - i * 6), y),
                     (200, 200, 200), max(1, int(size * 0.02)), cv2.LINE_AA)

    def _draw_sugar(self, out, x, ground, size, t):
        bounce = abs(np.sin(t * 11 + 1.3)) * size * 0.1
        bottom = ground - size * 0.4 - bounce
        ink = (60, 60, 60)
        self._legs(out, x, bottom - 2, size, t + 0.4, 24, ink)
        angle = np.radians(6 * np.sin(t * 11))
        c, s = np.cos(angle), np.sin(angle)
        half = size * 0.5
        center = np.array([x, bottom - half])
        corners = np.array([[-half, -half], [half, -half], [half, half], [-half, half]]) @ np.array([[c, s], [-s, c]])
        cube = (corners + center).astype(np.int32)
        cv2.fillConvexPoly(out, cube, (250, 250, 248), cv2.LINE_AA)
        # Sugar crystals: little sparkles on the surface.
        rng = np.random.default_rng(3)
        for px, py in rng.uniform(-0.38, 0.38, (14, 2)):
            pt = (center + np.array([px, py]) * size @ np.array([[c, s], [-s, c]])).astype(int)
            cv2.circle(out, tuple(int(v) for v in pt), max(1, int(size * 0.02)), (215, 215, 210), -1, cv2.LINE_AA)
        cv2.polylines(out, [cube], True, ink, max(2, int(size * 0.03)), cv2.LINE_AA)
        # Determined face, eyes on the cup.
        self._eyes(out, center[0] + size * 0.05, center[1] - size * 0.08, size, 1, worried=False)
        mouth = (int(center[0] + size * 0.05), int(center[1] + size * 0.22))
        cv2.ellipse(out, mouth, (int(size * 0.14), int(size * 0.06)), 0, 200, 340, (60, 40, 40),
                    max(2, int(size * 0.035)), cv2.LINE_AA)


    def _draw_pacman(self, out, t, envelope):
        """Pac-Man chomps across the screen, a ghost right behind him. Same path as the coffee chase."""
        h, w = out.shape[:2]
        size = h * 0.22
        floor = h - 1                      # bottom edge of the frame
        duration = self.DURATION["pacman"]
        chomp_hz = 4.5
        start_x = -0.08 * w
        speed = 1.55 * w / duration
        lead_x = start_x + speed * t
        self._draw_pac_pellets(out, start_x, speed, chomp_hz, lead_x, floor, size, w, t)
        self._draw_ghost(out, lead_x - 0.33 * w, floor, size * 0.96, t, chomp_hz)
        self._draw_pac(out, lead_x, floor, size, t, chomp_hz)

    def _draw_pac_pellets(self, out, origin, speed, chomp_hz, lead_x, ground, size, width, t):
        """Dots lined up so the mouth closes on each one. Every fifth dot is a power pellet."""
        radius = size * 0.48
        spacing = speed / chomp_hz
        if spacing <= 1:
            return
        thick = max(2, int(radius * 0.055))
        y = int(round(ground - thick / 2.0 - radius))   # mouth height, body standing on the edge
        yellow, ink = (20, 220, 255), (30, 40, 50)
        k0 = int(np.floor((-spacing - origin) / spacing)) - 1
        k1 = int(np.ceil((width + spacing - origin) / spacing)) + 1
        for k in range(k0, k1 + 1):
            x = origin + k * spacing
            if x < lead_x - radius * 0.02 or x < -spacing or x > width + spacing:
                continue
            power = (k % 5 == 4)
            if power and (t * 6.0) % 1.0 > 0.78:
                continue
            pr = max(2, int(round(radius * (0.20 if power else 0.085))))
            center = (int(round(x)), y)
            cv2.circle(out, center, pr, yellow, -1, cv2.LINE_AA)
            if power:
                cv2.circle(out, center, pr, ink, max(1, int(pr * 0.18)), cv2.LINE_AA)

    def _draw_pac(self, out, x, ground, size, t, chomp_hz):
        radius = size * 0.48
        bounce = abs(np.sin(t * chomp_hz * np.pi)) * size * 0.055
        thick = max(2, int(radius * 0.055))
        # Bottom of the outline sits on the frame edge; the bounce lifts him off it.
        cx, cy = x, ground - thick / 2.0 - radius - bounce
        phase = (t * chomp_hz) % 1.0
        openness = 1.0 - abs(phase * 2.0 - 1.0)          # shut on each pellet, wide in between
        half = 4.0 + 42.0 * openness
        center = (int(round(cx)), int(round(cy)))
        r = max(2, int(round(radius)))
        yellow, ink = (20, 220, 255), (25, 35, 45)
        # OpenCV angles run clockwise from the right, so this leaves the mouth pointing forward.
        cv2.ellipse(out, center, (r, r), 0, half, 360 - half, yellow, -1, cv2.LINE_AA)
        cv2.ellipse(out, center, (r, r), 0, half, 360 - half, ink, thick, cv2.LINE_AA)
        for deg in (half, -half):
            a = np.radians(deg)
            tip = (int(round(cx + np.cos(a) * radius)), int(round(cy + np.sin(a) * radius)))
            cv2.line(out, center, tip, ink, thick, cv2.LINE_AA)
        eye_r = max(2, int(radius * 0.115))
        eye = (int(round(cx + radius * 0.10)), int(round(cy - radius * 0.46)))
        cv2.ellipse(out, eye, (int(eye_r * 0.85), int(eye_r * 1.15)), 0, 0, 360, ink, -1, cv2.LINE_AA)
        cv2.circle(out, (eye[0] - eye_r // 3, eye[1] - eye_r // 3), max(1, eye_r // 3), (255, 255, 255), -1,
                   cv2.LINE_AA)
        for i in range(3):
            y = int(cy - radius * 0.35 + i * radius * 0.38)
            x0 = int(cx - radius * (1.25 + i * 0.18))
            cv2.line(out, (x0, y), (x0 - int(radius * 0.55), y), (190, 190, 190), max(1, thick // 2), cv2.LINE_AA)

    def _draw_ghost(self, out, x, ground, size, t, chomp_hz):
        """Classic red ghost: round head, three-part skirt, eyes fixed on Pac-Man."""
        bounce = abs(np.sin(t * chomp_hz * np.pi + 1.1)) * size * 0.07
        body_w, body_h = size * 0.92, size * 1.12
        outline = max(2, int(body_w * 0.04))
        # Deepest point of the skirt rests on the same bottom edge.
        bottom = ground - outline / 2.0 - bounce
        head_r = body_w / 2
        wave_h = body_h * 0.18
        top = bottom - body_h
        head_cy = top + head_r
        side_bottom = bottom - wave_h
        pts = []
        for a in np.linspace(np.pi, 0, 24):
            pts.append([x + np.cos(a) * head_r, head_cy - np.sin(a) * head_r])
        pts.append([x + head_r, side_bottom])
        waves = 3
        for i in range(waves):
            x_right = x + head_r - i * body_w / waves
            x_left = x + head_r - (i + 1) * body_w / waves
            depth = wave_h * (0.92 + 0.08 * np.sin(t * 13 + i * 2.2))
            steps = 6
            for k in range(1, steps + 1):
                u = k / steps
                pts.append([x_right + (x_left - x_right) * u, side_bottom + np.sin(np.pi * u) * depth])
        red, ink = (40, 40, 225), (25, 25, 70)
        self._paint_polygon(out, pts, red, ink, outline)
        eye_y = head_cy + head_r * 0.12
        eye_rx, eye_ry = body_w * 0.155, body_h * 0.115
        pupil_r = max(2, int(eye_rx * 0.48))
        for side in (-1, 1):
            ex, ey = x + side * body_w * 0.20, eye_y
            cv2.ellipse(out, (int(ex), int(ey)), (max(2, int(eye_rx)), max(2, int(eye_ry))),
                        0, 0, 360, (255, 255, 255), -1, cv2.LINE_AA)
            px = int(ex + eye_rx * 0.42)
            py = int(ey + eye_ry * 0.08)
            cv2.circle(out, (px, py), pupil_r, (210, 70, 30), -1, cv2.LINE_AA)
            cv2.circle(out, (px - pupil_r // 3, py - pupil_r // 3), max(1, pupil_r // 3),
                       (255, 255, 255), -1, cv2.LINE_AA)
            outer = (int(ex + side * eye_rx * 1.15), int(ey - eye_ry * 1.55))
            inner = (int(ex - side * eye_rx * 0.05), int(ey - eye_ry * 0.72))
            cv2.line(out, outer, inner, ink, max(2, int(body_w * 0.045)), cv2.LINE_AA)
        for i in range(2):
            y = int(head_cy + i * body_h * 0.22)
            x0 = int(x - head_r - body_w * (0.22 + i * 0.16))
            cv2.line(out, (x0, y), (x0 - int(body_w * 0.28), y), (190, 190, 190),
                     max(1, int(body_w * 0.025)), cv2.LINE_AA)

    @staticmethod
    def _paint_polygon(out, points, color, outline, thickness):
        """Fill a (possibly concave) polygon with a soft edge, then a crisp outline."""
        pts = np.asarray(points, np.float64)
        h, w = out.shape[:2]
        if pts[:, 0].max() < -2 or pts[:, 0].min() > w + 2 or pts[:, 1].max() < -2 or pts[:, 1].min() > h + 2:
            return
        pad = max(2, int(thickness) + 2)
        x1 = max(0, int(np.floor(pts[:, 0].min())) - pad)
        y1 = max(0, int(np.floor(pts[:, 1].min())) - pad)
        x2 = min(w, int(np.ceil(pts[:, 0].max())) + pad + 1)
        y2 = min(h, int(np.ceil(pts[:, 1].max())) + pad + 1)
        if x1 >= x2 or y1 >= y2:
            return
        roi = out[y1:y2, x1:x2]
        mask = np.zeros(roi.shape[:2], np.uint8)
        shifted = np.round(pts - (x1, y1)).astype(np.int32)
        cv2.fillPoly(mask, [shifted], 255)
        soft = cv2.GaussianBlur(mask, (0, 0), 0.7).astype(np.float32) * (1.0 / 255.0)
        painted = roi.astype(np.float32) * (1.0 - soft[..., None]) + np.array(color, np.float32) * soft[..., None]
        roi[:] = np.clip(painted, 0, 255).astype(np.uint8)
        if outline is not None and thickness > 0:
            cv2.polylines(out, [np.round(pts).astype(np.int32)], True, outline, thickness, cv2.LINE_AA)

    def _draw_floating(self, out, t, envelope):
        h, w = out.shape[:2]
        for item in self.state["items"]:
            sprite = item["sprite"]
            if item.get("big"):
                # Pops in, then beats like a heart and tilts gently from side to side.
                grow = min(1.0, t / 0.25)
                beat = (t * 1.25) % 1.0                    # ~75 beats per minute
                pulse = 1.0 + 0.09 * np.exp(-((beat - 0.12) / 0.06) ** 2) \
                    + 0.05 * np.exp(-((beat - 0.32) / 0.06) ** 2)
                size = max(8, int(sprite.shape[0] * grow * pulse))
                angle = 7.0 * np.sin(t * 2.4)
                pad = int(size * 0.25)
                canvas = np.zeros((size + 2 * pad, size + 2 * pad, 4), np.uint8)
                canvas[pad:pad + size, pad:pad + size] = cv2.resize(sprite, (size, size),
                                                                    interpolation=cv2.INTER_LINEAR)
                c = canvas.shape[0] / 2
                turned = cv2.warpAffine(canvas, cv2.getRotationMatrix2D((c, c), angle, 1.0),
                                        canvas.shape[1::-1], flags=cv2.INTER_LINEAR,
                                        borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
                paste_sprite(out, turned, w * 0.5, h * 0.45, envelope)
                continue
            age = t - item["delay"]
            if age < 0:
                continue
            progress = age / item["rise"]
            if progress > 1:
                continue
            y = h + sprite.shape[0] - progress * (h + 2 * sprite.shape[0])
            x = item["x"] + np.sin(item["phase"] + age * 2.5) * item["sway"]
            opacity = min(1.0, progress * 6, (1 - progress) * 4) * envelope
            paste_sprite(out, sprite, x, y, opacity)


BG_DIR = os.path.join(BASE_DIR, "backgrounds")
BG_SLOTS = 5


def bg_path(slot):
    return os.path.join(BG_DIR, f"background{slot}.jpg")


def read_image(path):
    """cv2.imread cannot open paths with non-ASCII characters on Windows; this can."""
    try:
        return cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None


def write_image(path, image):
    ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if ok:
        data.tofile(path)
    return ok


def replace_background(frame, mask, background):
    """Composite: person from the camera, everything else from the chosen image."""
    h, w = frame.shape[:2]
    alpha = mask_to_alpha(mask, w, h)
    return cv2.blendLinear(frame, background, alpha, 1.0 - alpha)


class HedgeEffect:
    """You disappear into a hedge.

    1. The hedge fades in as your background (like the blur, around you).
    2. It then takes you over from the left and right, closing towards the middle with a
       leafy edge, until only hedge is left. It stays until switched off.
    3. Switched off, it opens again from the middle outwards and fades away.

    Needs the person mask from the background segmenter; without it (no model) the hedge
    simply closes over the whole image from both sides.
    """
    FADE = 0.6           # seconds: hedge appears as background
    CLOSE = 1.8          # seconds: hedge closes over you from both sides
    OPEN = 1.4
    UNFADE = 0.6

    def __init__(self):
        self.state = "off"          # off | waiting | entering | hidden | leaving
        self.since = 0.0
        self._from = 0.0
        self._size = None
        self._center = None         # smoothed horizontal position and half-width of you

    @property
    def active(self):
        return self.state != "off"

    @property
    def covering(self):
        return self.state in ("waiting", "entering", "hidden")

    def enter(self):
        if self.covering:
            return
        if self.state == "leaving":
            self._from = self._progress(time.monotonic())
            self.state, self.since = "entering", time.monotonic()
        else:
            self._from = 0.0
            self.state = "waiting"          # starts as soon as the person mask is available

    def leave(self):
        if self.state in ("off", "leaving"):
            return
        if self.state == "waiting":
            self.state = "off"
            return
        self._from = self._progress(time.monotonic())
        self.state, self.since = "leaving", time.monotonic()

    def _progress(self, now):
        """0 = no hedge, 1 = background is hedge, 2 = you are completely gone."""
        t = now - self.since
        enter_total = self.FADE + self.CLOSE
        if self.state == "entering":
            # Map elapsed time onto the 0..2 scale, starting from where we were.
            start_time = self._from * self.FADE if self._from <= 1 else self.FADE + (self._from - 1) * self.CLOSE
            t += start_time
            if t >= enter_total:
                self.state = "hidden"
                return 2.0
            return t / self.FADE if t < self.FADE else 1.0 + (t - self.FADE) / self.CLOSE
        if self.state == "hidden":
            return 2.0
        if self.state == "leaving":
            start_time = (2.0 - self._from) * self.OPEN if self._from >= 1 else self.OPEN + (1 - self._from) * self.UNFADE
            t += start_time
            if t >= self.OPEN + self.UNFADE:
                self.state = "off"
                return 0.0
            return 2.0 - t / self.OPEN if t < self.OPEN else 1.0 - (t - self.OPEN) / self.UNFADE
        return 0.0

    def _build(self, w, h):
        """Pre-render a full-frame hedge, an irregular 'leafy' noise field for the closing
        edge and a displacement grid for rustling (all at half resolution)."""
        rng = np.random.default_rng(7)
        hw, hh = max(2, w // 2), max(2, h // 2)
        tex = np.full((hh, hw, 3), (22, 52, 20), np.uint8)
        n = int(4200 * (hw * hh) / (480 * 270))
        scale = hw / 480
        for _ in range(n):
            ax = (max(2, int(rng.uniform(5, 11) * scale)), max(1, int(rng.uniform(2.5, 5) * scale)))
            shade = rng.uniform(0, 1)
            color = (int(25 + 35 * shade), int(85 + 85 * shade), int(30 + 40 * shade))
            angle = rng.uniform(0, 180)
            center = (int(rng.uniform(-0.02, 1.02) * hw), int(rng.uniform(-0.02, 1.02) * hh))
            cv2.ellipse(tex, (center[0] + 1, center[1] + 2), ax, angle, 0, 360, (14, 36, 14), -1, cv2.LINE_AA)
            cv2.ellipse(tex, center, ax, angle, 0, 360, color, -1, cv2.LINE_AA)
        self._tex = tex
        # Leafy edge: smooth noise, so the closing line is ragged like a hedge, not straight.
        fine = cv2.GaussianBlur(rng.uniform(-1, 1, (hh, hw)).astype(np.float32), (0, 0), 1.5 * scale + 0.5)
        coarse = cv2.GaussianBlur(rng.uniform(-1, 1, (hh, hw)).astype(np.float32), (0, 0), 6 * scale + 1)
        noise = fine / (np.abs(fine).max() + 1e-6) + 1.5 * coarse / (np.abs(coarse).max() + 1e-6)
        self._edge_noise = noise / np.abs(noise).max()
        self._grid = np.meshgrid(np.arange(hw, dtype=np.float32), np.arange(hh, dtype=np.float32))
        self._size = (w, h)

    def _person_span(self, mask, hw):
        """Horizontal centre and half-width of the person (half-res pixels), smoothed."""
        if mask is not None:
            cols = cv2.resize(mask, (hw, mask.shape[0])).max(axis=0) > 0.5
            if cols.any():
                xs = np.nonzero(cols)[0]
                target = ((xs[0] + xs[-1]) / 2, (xs[-1] - xs[0]) / 2 + hw * 0.04)
            else:
                target = (hw / 2, hw * 0.25)
        else:
            target = (hw / 2, hw / 2 + 2)       # no mask: close over the whole width
        if self._center is None:
            self._center = target
        else:
            self._center = tuple(0.85 * c + 0.15 * t for c, t in zip(self._center, target))
        return self._center

    def render(self, frame, mask, now, mask_expected=True):
        if self.state == "waiting":
            if mask is None and mask_expected:
                return frame                    # model still loading: start when it is ready
            self.state, self.since, self._from = "entering", now, 0.0
        p = self._progress(now)
        if p <= 0.0:
            self._center = None
            return frame
        h, w = frame.shape[:2]
        if self._size != (w, h):
            self._build(w, h)
        hh, hw = self._tex.shape[:2]
        gx, gy = self._grid
        # Rustle: the leaves sway a little.
        sway = (np.sin(gy / 9.0 + now * 2.1) * 1.4 + np.sin(gy / 23.0 - now * 1.3) * 1.0) * (w / 960)
        tex = cv2.remap(self._tex, gx + sway, gy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)

        # Where is the hedge? 1) everything that is not you, faded in.
        if mask is not None:
            person = cv2.resize(mask, (hw, hh), interpolation=cv2.INTER_LINEAR)
            person = np.clip((cv2.GaussianBlur(person, (0, 0), 1.0) - 0.3) * 2.5, 0, 1)
        else:
            person = np.ones((hh, hw), np.float32)
        background = (1.0 - person) * min(1.0, p)
        # 2) closing in on you from both sides towards your middle.
        cx, half = self._person_span(mask, hw)
        closing = max(0.0, p - 1.0)                       # 0..1
        opening = (1.0 - closing) * half                  # distance from the middle still free
        edge = np.abs(gx - cx) - opening + self._edge_noise * (14 * hw / 480) * min(1.0, 0.4 + closing)
        side = np.clip(edge / (2.0 * hw / 480) + 0.5, 0, 1) if closing > 0 else 0.0
        if closing >= 1.0:
            side = 1.0
        alpha = np.maximum(background, side)
        tex = cv2.resize(tex, (w, h), interpolation=cv2.INTER_LINEAR)
        alpha = cv2.resize(np.asarray(alpha, np.float32) * np.ones((hh, hw), np.float32), (w, h),
                           interpolation=cv2.INTER_LINEAR)
        return cv2.blendLinear(tex, frame, alpha, 1.0 - alpha)


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
        self.camera_devices = {}
        self.current_cam_index = None
        self.current_cam_name = None
        self.camera_backend = cfg.get("camera_backend", "MSMF")
        if self.camera_backend not in ("MSMF", "DSHOW"):
            self.camera_backend = "MSMF"
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
        self.reactions = ReactionPlayer()
        self.hedge = HedgeEffect()
        self.camera_enabled = True
        self.bg_slot = 0                    # 0 = no background image, 1..5 = chosen image
        self._bg_cache = (None, None)       # ((slot, size), image fitted to the output size)
        self._size_timer = QTimer(self)
        self._size_timer.setSingleShot(True)
        self._size_timer.timeout.connect(self._save_panel_size)
        self.segmenter = None
        self._retiring_segmenter = None
        self._retire_thread = None
        self._diag_window = None
        self._enumerate_seconds = None
        QTimer.singleShot(0, lambda: mark_startup("window shown"))
        self._stats_output_times = collections.deque(maxlen=180)
        self._stats_camera_times = collections.deque(maxlen=180)
        self._last_captured_timestamp = None
        self._stats_render_ms = collections.deque(maxlen=180)
        self._last_segmentation_frame_key = None
        self._blur_cache = (None, None, None, None)   # (frame, mask, strength, result)
        self._blur_status = None
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
        t0 = time.monotonic()
        cams = list_camera_names()
        self._enumerate_seconds = time.monotonic() - t0
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
        mark_startup("virtual camera started")
        return True

    def _on_cameras_found(self, cams):
        if self._shut_down:
            return
        devices = cams or []
        self.camera_devices = {d["key"]: d for d in devices}
        self.camera_list = [(d["key"], d["label"]) for d in devices]
        if not self.camera_list:
            self._fill_camera_combo()
            if self.vcam is None:
                self._start_vcam(640, 480)
            self.set_mode_no_camera()
            return
        cfg = load_config()
        saved_name = cfg.get("camera_name")
        saved_key = cfg.get("camera_device_key")
        saved_index = cfg.get("camera_index")
        matched = next(((key, name) for key, name in self.camera_list
                        if key == saved_key and self.camera_devices[key]["name"] == saved_name), None)
        if matched is None and saved_name:
            same_name = [(key, name) for key, name in self.camera_list
                         if self.camera_devices[key]["name"] == saved_name]
            if len(same_name) == 1:
                matched = same_name[0]
            elif len(same_name) > 1:
                matched = next(((key, name) for key, name in same_name
                                if self.camera_devices[key]["indices"].get("DSHOW") == saved_index), None)
        if matched is None:
            matched = self.camera_list[0]
        self.current_cam_index, self.current_cam_name = matched
        self._fill_camera_combo()
        self._select_camera(*matched)

    def _selected_camera_indices(self):
        device = self.camera_devices.get(self.current_cam_index)
        return dict(device["indices"]) if device else {}

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
                self.cam_combobox.addItem(name, idx)
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
        self._stats_camera_times.clear()
        self._last_captured_timestamp = None
        device = self.camera_devices.get(index, {})
        dshow_index = device.get("indices", {}).get("DSHOW")
        update_config(camera_name=device.get("name", name), camera_index=dshow_index,
                      camera_device_key=index)

        cached = load_config().get("camera_resolutions", {}).get(device.get("name", name))
        if cached:
            sizes = [s for s in (parse_size(r) for r in cached) if s]
            if sizes:
                self._start_camera(token, sizes)
                return
        sizes = list_formats(dshow_index) if dshow_index is not None else []
        if sizes:
            self._cache_resolutions(device.get("name", name), sizes)
            self._start_camera(token, sizes)
            return
        # Windows could not tell us: probe in the background (opens the camera).
        self.resolution_combo.setEnabled(False)
        # Probe the chosen backend and its own index, never a foreign index.
        indices = self._selected_camera_indices()
        backend = self.camera_backend if self.camera_backend in indices else next(iter(indices), None)
        if backend is None:
            self._on_probed(token, name, [])
        else:
            device_index = indices[backend]
            self.run_in_thread(probe_resolutions,
                               lambda result: self._on_probed(token, name, result),
                               device_index, FALLBACK_RESOLUTIONS,
                               lambda i: cv2.VideoCapture(i, capture_backend_code(backend)))

    def _on_probed(self, token, name, sizes):
        if self._shut_down or token != self._cam_token:
            return
        if sizes:
            device = self.camera_devices.get(self.current_cam_index, {})
            self._cache_resolutions(device.get("name", name), sizes)
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
        if self.camera_enabled:
            self.reader = CameraReader(self._selected_camera_indices(), *self.capture_res, self.camera_backend)

        self.resolution_combo.blockSignals(True)
        self.resolution_combo.clear()
        for width, height in self.available_resolutions:
            self.resolution_combo.addItem(f"{width}×{height}", (width, height))
        self.resolution_combo.setCurrentIndex(self.available_resolutions.index(self.capture_res))
        self.resolution_combo.setEnabled(True)
        self.resolution_combo.blockSignals(False)

    # ------------------------------------------------------- camera on/off --
    def toggle_camera(self):
        self.set_camera_enabled(not self.camera_enabled)

    def set_camera_enabled(self, enabled):
        if enabled == self.camera_enabled:
            return
        self.camera_enabled = enabled
        if not enabled:
            if self.is_recording_loop:
                self._finish_recording()
            self._cam_token += 1                    # cancel a camera start that is still pending
            reader, self.reader = self.reader, None
            if reader is not None:
                threading.Thread(target=reader.close, daemon=True).start()
        elif self.current_cam_index is not None and self.camera_list:
            self._select_camera(self.current_cam_index, self.current_cam_name)
        self.btn_camera.setText(self.t("btn_cam_on" if not enabled else "btn_cam_off"))
        self._refresh_status()
        self.update_button_style()

    # ----------------------------------------------------------------- blur --
    def blur_available(self):
        chosen = self.model_combo.currentData() if hasattr(self, "model_combo") else "PP-HumanSeg"
        path = RVM_MODEL if chosen == "RVM MobileNetV3" else SEGMENT_MODEL
        return HAS_ONNX and os.path.isfile(path)

    def background_wanted(self):
        return self.blur_cb.isChecked() or self.bg_slot > 0

    def segmenter_wanted(self):
        return self.background_wanted() or self.hedge.active

    def _person_mask(self, frame, frame_key=None):
        """Newest person mask for this frame (submits the frame if it is new)."""
        seg = self.segmenter
        if seg is None or seg.engine == "failed":
            return None
        key = frame_key if frame_key is not None else id(frame)
        if key != self._last_segmentation_frame_key:
            seg.submit(frame)
            self._last_segmentation_frame_key = key
        return seg.mask()

    def on_blur_toggled(self, checked, save=True):
        if save:
            update_config(blur=bool(checked))
        self._update_segmenter()
        for slider in (self.blur_strength_slider, self.blur_dim_slider, self.blur_desat_slider):
            self._set_row_enabled(slider, checked)
        self.blur_lens_cb.setEnabled(checked)

    def _on_segmentation_option_changed(self):
        update_config(compute_provider=self.provider_combo.currentData(),
                      segmentation_model=self.model_combo.currentData())
        # Never create two DirectML sessions concurrently. A worker still inside
        # session.run() must finish and release its session before replacement.
        if self._retiring_segmenter is not None:
            return  # Latest combo selection is read when retirement finishes.
        old, self.segmenter = self.segmenter, None
        self._last_segmentation_frame_key = None
        self._blur_cache = (None, None, None, None)
        if old is None:
            self._update_segmenter()
            return
        self._retiring_segmenter = old
        self._retire_thread = threading.Thread(target=old.close,
                                               name="SegmenterRetire", daemon=True)
        self._retire_thread.start()
        QTimer.singleShot(50, self._finish_segmenter_switch)

    def _finish_segmenter_switch(self):
        if self._shut_down:
            return
        old = self._retiring_segmenter
        if old is None:
            return
        if self._retire_thread.is_alive() or old._thread.is_alive():
            QTimer.singleShot(50, self._finish_segmenter_switch)
            return
        self._retiring_segmenter = None
        self._retire_thread = None
        self._update_segmenter()

    def show_diagnostics(self):
        if self._diag_window is None:
            self._diag_window = PerformanceDialog(self)
        self._diag_window.show()
        self._diag_window.raise_()
        self._diag_window.activateWindow()

    def _update_segmenter(self):
        if self._retiring_segmenter is not None:
            return
        wanted = self.segmenter_wanted()
        if wanted and self.segmenter is None and self.blur_available():
            chosen = self.model_combo.currentData() if hasattr(self, "model_combo") else "PP-HumanSeg"
            provider = self.provider_combo.currentData() if hasattr(self, "provider_combo") else "Auto"
            self.segmenter = BackgroundSegmenter(chosen, provider)
        elif not wanted and self.segmenter is not None:
            # Free GPU/CPU memory while the blur is off.
            segmenter, self.segmenter = self.segmenter, None
            threading.Thread(target=segmenter.close, daemon=True).start()
        self._last_segmentation_frame_key = None
        self._blur_cache = (None, None, None, None)
        self._refresh_blur_status()

    def _refresh_blur_status(self):
        if not self.blur_available():
            key, kwargs = "blur_missing", {}
        elif self.segmenter is None:
            key, kwargs = "blur_off", {}
        elif self.segmenter.engine == "failed":
            key, kwargs = "blur_failed", {"e": self.segmenter.error}
        elif self.segmenter.provider_name:
            state = ("active", self.segmenter.provider_name, self.lang)
            if state != self._blur_status:
                self._blur_status = state
                self.blur_engine_label.setText(self.segmenter.provider_name)
            return
        else:
            key, kwargs = "blur_loading", {}
        state = (key, self.lang, tuple(kwargs.items()))
        if state != self._blur_status:
            self._blur_status = state
            self.blur_engine_label.setText(self.t(key, **kwargs))

    def _blurred(self, frame, frame_key=None):
        seg = self.segmenter
        if seg is None or seg.engine == "failed":
            return frame
        mask = self._person_mask(frame, frame_key)
        background = self._background_image(frame.shape[1], frame.shape[0])
        setting = (("image", self.bg_slot) if background is not None else
                   ("blur", self.blur_strength_slider.value(), self.blur_lens_cb.isChecked(),
                    self.blur_dim_slider.value(), self.blur_desat_slider.value()))
        cached_frame, cached_mask, cached_setting, cached = self._blur_cache
        if frame is cached_frame and mask is cached_mask and setting == cached_setting:
            return cached                                 # frozen image / same loop frame
        # No mask yet (model still loading): hide the whole room rather than show it.
        person = mask if mask is not None else np.zeros((BackgroundSegmenter.SIZE,) * 2, np.float32)
        if background is not None:
            result = replace_background(frame, person, background)
        else:
            _, amount, lens, dim, desat = setting
            result = blur_background(frame, person, blur_radius(amount), lens, dim / 100, desat / 100)
        self._blur_cache = (frame, mask, setting, result)
        return result

    # ---------------------------------------------------- background image --
    def _background_image(self, w, h):
        if self.bg_slot <= 0:
            return None
        key, image = self._bg_cache
        if key != (self.bg_slot, (w, h)):
            source = read_image(bg_path(self.bg_slot))
            image = fit_frame(source, w, h).copy() if source is not None else None
            self._bg_cache = ((self.bg_slot, (w, h)), image)
        return image

    def _refresh_bg_buttons(self):
        for slot, btn in self.bg_buttons.items():
            if slot == 0:
                continue
            image = read_image(bg_path(slot))
            btn.setVisible(image is not None)
            if image is not None:
                thumb = fit_frame(image, 128, 72)
                rgb = np.ascontiguousarray(cv2.cvtColor(thumb, cv2.COLOR_BGR2RGB))
                pix = QPixmap.fromImage(QImage(rgb.data, 128, 72, 3 * 128, QImage.Format.Format_RGB888).copy())
                btn.setIcon(QIcon(pix))
        if self.bg_slot > 0 and not os.path.isfile(bg_path(self.bg_slot)):
            self.bg_slot = 0
        self.bg_buttons[self.bg_slot].setChecked(True)

    def select_background(self, slot):
        self.bg_slot = slot
        self._bg_cache = (None, None)
        self._blur_cache = (None, None, None, None)
        update_config(background_slot=slot)
        self.bg_buttons[slot].setChecked(True)
        self._update_segmenter()

    def add_background(self):
        path, _ = QFileDialog.getOpenFileName(self, self.t("bg_dialog"), "", self.t("bg_filter"))
        if not path:
            return
        image = read_image(path)
        if image is None:
            self.show_message(QMessageBox.Icon.Warning, "bg_error_title", self.t("bg_error"))
            return
        free = [slot for slot in range(1, BG_SLOTS + 1) if not os.path.isfile(bg_path(slot))]
        slot = free[0] if free else self._ask_replace_slot()
        if not slot:
            return
        os.makedirs(BG_DIR, exist_ok=True)
        # Stored cropped to 16:9 at 1280×720: plenty for a video call, small in memory.
        write_image(bg_path(slot), fit_frame(image, 1280, 720))
        self._refresh_bg_buttons()
        self.select_background(slot)

    def _ask_replace_slot(self):
        box = QMessageBox(QMessageBox.Icon.Question, self.t("bg_replace_title"),
                          self.t("bg_replace_text"), QMessageBox.StandardButton.NoButton, self)
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        choices = {}
        for slot in range(1, BG_SLOTS + 1):
            btn = QPushButton(str(slot))
            btn.setIcon(self.bg_buttons[slot].icon())
            btn.setIconSize(self.bg_buttons[slot].iconSize())
            box.addButton(btn, QMessageBox.ButtonRole.AcceptRole)
            choices[btn] = slot
        box.addButton(self.t("bg_cancel"), QMessageBox.ButtonRole.RejectRole)
        QTimer.singleShot(0, lambda: (box.raise_(), box.activateWindow()))
        box.exec()
        return choices.get(box.clickedButton())

    def remove_background(self, slot):
        try:
            os.remove(bg_path(slot))
        except OSError:
            pass
        if self.bg_slot == slot:
            self.select_background(0)
        self._refresh_bg_buttons()

    def _bg_context_menu(self, slot, pos):
        menu = QMenu(self)
        remove = menu.addAction(self.t("bg_remove"))
        if menu.exec(self.bg_buttons[slot].mapToGlobal(pos)) == remove:
            self.remove_background(slot)

    # ---------------------------------------------------------------- hedge --
    def toggle_hedge(self):
        if self.hedge.covering:
            self.hedge.leave()
        else:
            self.hedge.enter()
            self._update_segmenter()             # the hedge needs to know where you are
            if self.mode not in ("LIVE", "LOOP", "FROZEN"):
                self._go_live()
        self._refresh_hedge_button()

    def _refresh_hedge_button(self):
        self.btn_hedge.setText(self.t("p_hedge_out" if self.hedge.covering else "p_hedge_in"))

    # ------------------------------------------------------------- shutdown --
    def shutdown(self):
        if self._shut_down:
            return
        self._shut_down = True
        self.timer.stop()
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        if self.segmenter is not None:
            self.segmenter.close()
            self.segmenter = None
        if self._retiring_segmenter is not None:
            self._retiring_segmenter.close()
            self._retiring_segmenter = None
        self._size_timer.stop()
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
        if self.is_expanded:
            width = max(320, min(720, self.width() - 2 * RESIZE_MARGIN - 70))
            self.preview_label.setFixedSize(width, width * 9 // 16)
            self._size_timer.start(400)          # save once the user stops dragging

    def _save_panel_size(self):
        if self.is_expanded:
            update_config(panel_size=[self.width(), self.height()])

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

    def _emoji_row(self, on_click=None):
        row = QHBoxLayout()
        row.setSpacing(4)
        for char in REACTION_EMOJIS:
            btn = QPushButton(char)
            btn.setFixedSize(38, 34)
            btn.setStyleSheet("font-size: 18px; padding: 0;")
            btn.setFont(self._emoji_font())
            btn.clicked.connect(lambda _=False, char=char: (self.play_reaction("emoji", char),
                                                            on_click() if on_click else None))
            row.addWidget(btn)
        row.addStretch()
        return row

    @staticmethod
    def _emoji_font():
        font = QFont()
        font.setFamilies(["Segoe UI", "Segoe UI Emoji"] + EMOJI_FONTS)
        return font

    def play_reaction(self, kind, char=None):
        self.reactions.play(kind, self.cam_width, self.cam_height, char)

    def show_context_menu(self, pos):
        # Quick actions for when the panel is closed: emoji, camera on/off, quit.
        menu = QMenu(self)
        menu.setFont(self._emoji_font())
        emoji_widget = QWidget()
        emoji_widget.setLayout(self._emoji_row(on_click=menu.close))
        emoji_widget.layout().setContentsMargins(8, 4, 8, 4)
        emoji_action = QWidgetAction(menu)
        emoji_action.setDefaultWidget(emoji_widget)
        menu.addAction(emoji_action)
        menu.addSeparator()
        menu.addAction(self.t("menu_cam_on" if not self.camera_enabled else "menu_cam_off"), self.toggle_camera)
        menu.addSeparator()
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

        self.blur_cb = self._checkbox("cb_blur")
        self._tr(lambda: self.blur_cb.setToolTip(self.t("cb_blur_tip")))
        layout.addWidget(self.blur_cb)

        self.preview_label = QLabel()
        self.preview_label.setFixedSize(400, 225)        # follows the window width (resizeEvent)
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
        self.btn_camera = QPushButton()
        self.btn_camera.setStyleSheet("background-color: #545454; color: white; font-weight: bold; padding: 10px;")
        self._tr(lambda: (self.btn_camera.setText(self.t("btn_cam_off" if self.camera_enabled else "btn_cam_on")),
                          self.btn_camera.setToolTip(self.t("btn_cam_tip"))))
        self.btn_camera.clicked.connect(self.toggle_camera)
        mode_layout.addWidget(self.btn_live)
        mode_layout.addWidget(self.btn_freeze)
        mode_layout.addWidget(self.btn_play_loop)
        mode_layout.addWidget(self.btn_camera)
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
        # "Default settings" last, across the full width: it undoes everything above it.
        preset_grid.addWidget(self._button("p_reset", self.apply_preset_reset), 4, 0, 1, 2)
        self.btn_hedge = QPushButton()
        self.btn_hedge.clicked.connect(self.toggle_hedge)
        self._tr(self._refresh_hedge_button)
        preset_grid.addWidget(self.btn_hedge, 3, 1)
        layout.addWidget(preset_group)

        reactions_group = self._group("grp_reactions")
        self._tr(lambda: reactions_group.setToolTip(self.t("reactions_tip")))
        reactions_grid = QGridLayout(reactions_group)
        for i, (kind, icon) in enumerate(REACTION_EFFECTS):
            btn = QPushButton()
            self._tr(lambda btn=btn, kind=kind, icon=icon: btn.setText(f"{icon}  {self.t('r_' + kind)}"))
            btn.clicked.connect(lambda _=False, kind=kind: self.play_reaction(kind))
            reactions_grid.addWidget(btn, i // 4, i % 4)
        reactions_grid.addLayout(self._emoji_row(), (len(REACTION_EFFECTS) + 3) // 4, 0, 1, 4)
        layout.addWidget(reactions_group)

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
        layout.addStretch()          # extra window height goes below, nothing gets pulled apart

    def _build_tab_effects(self, parent_widget):
        layout = QVBoxLayout(parent_widget)
        layout.setSpacing(12)
        off = self.t
        sec = lambda v: f"{v / 10:.1f} {self.t('unit_s')}"
        off_or = lambda fmt: (lambda v: off("off") if v == 0 else fmt(v))

        background, grid = self._card("grp_background")
        self._tr(lambda: background.setToolTip(self.t("bg_tip")))
        row = QHBoxLayout()
        row.setSpacing(6)
        self.bg_group = QButtonGroup(self)
        self.bg_group.setExclusive(True)
        self.bg_buttons = {}
        for slot in range(BG_SLOTS + 1):
            btn = QPushButton()
            btn.setCheckable(True)
            btn.setFixedSize(66, 42)
            btn.setStyleSheet("QPushButton { padding: 2px; } QPushButton:checked { border: 2px solid #64b5f6; }")
            if slot == 0:
                self._tr(lambda btn=btn: btn.setText(self.t("bg_none")))
            else:
                btn.setIconSize(QSize(58, 33))
                btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                btn.customContextMenuRequested.connect(lambda pos, slot=slot: self._bg_context_menu(slot, pos))
            btn.clicked.connect(lambda _=False, slot=slot: self.select_background(slot))
            self.bg_group.addButton(btn)
            self.bg_buttons[slot] = btn
            row.addWidget(btn)
        row.addStretch()
        grid.addLayout(row, 0, 0, 1, 3)
        add_btn = self._button("bg_add", self.add_background)
        grid.addWidget(add_btn, 1, 0, 1, 1)
        layout.addWidget(background)
        self._background_card = background

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
        row = self._next_row(grid)
        grid.addWidget(self._label("camera_backend"), row, 0)
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("", "MSMF")
        self.backend_combo.addItem("", "DSHOW")
        self._tr(lambda: (self.backend_combo.setItemText(0, self.t("backend_msmf")),
                          self.backend_combo.setItemText(1, self.t("backend_dshow")),
                          self.backend_combo.setToolTip(self.t("backend_tip"))))
        self.backend_combo.setCurrentIndex(0 if self.camera_backend == "MSMF" else 1)
        self.backend_combo.currentIndexChanged.connect(self.on_backend_change)
        grid.addWidget(self.backend_combo, row, 1, 1, 2)
        layout.addWidget(general)

        blur, grid = self._card("grp_blur")
        self.blur_strength_slider = self._form_slider(
            grid, "s_blur", 1, 10, BLUR_AMOUNT_DEFAULT, str, "s_blur_tip")
        self.blur_lens_cb = self._form_checkbox(grid, "cb_blur_lens", BLUR_LENS_DEFAULT, "cb_blur_lens_tip")
        self.blur_dim_slider = self._form_slider(
            grid, "s_blur_dim", 0, 50, BLUR_DIM_DEFAULT, lambda v: self.t("off") if v == 0 else f"{v}%", "s_blur_dim_tip")
        self.blur_desat_slider = self._form_slider(
            grid, "s_blur_desat", 0, 100, BLUR_DESAT_DEFAULT, lambda v: self.t("off") if v == 0 else f"{v}%",
            "s_blur_desat_tip")
        row = self._next_row(grid)
        grid.addWidget(self._label("blur_engine"), row, 0)
        self.blur_engine_label = QLabel()
        self.blur_engine_label.setStyleSheet("color: #9e9e9e;")
        grid.addWidget(self.blur_engine_label, row, 1, 1, 2)
        row = self._next_row(grid)
        grid.addWidget(self._label("backend"), row, 0)
        self.provider_combo = QComboBox()
        for label, value in [("Auto (DirectML → CPU)", "Auto"),
                             ("DirectML", "DirectML"), ("CPU", "CPU")]:
            self.provider_combo.addItem(label, value)
        saved_provider = load_config().get("compute_provider", "Auto")
        if saved_provider not in ("Auto", "DirectML", "CPU"):
            saved_provider = "Auto"  # migrate configurations saved with the removed CUDA option
        self.provider_combo.setCurrentIndex(max(0, self.provider_combo.findData(saved_provider)))
        grid.addWidget(self.provider_combo, row, 1, 1, 2)
        row = self._next_row(grid)
        grid.addWidget(self._label("model_choice"), row, 0)
        self.model_combo = QComboBox()
        self.model_combo.addItem("PP-HumanSeg (2023)", "PP-HumanSeg")
        if os.path.isfile(RVM_MODEL):
            self.model_combo.addItem("RVM MobileNetV3", "RVM MobileNetV3")
        saved_model = load_config().get("segmentation_model", "PP-HumanSeg")
        self.model_combo.setCurrentIndex(max(0, self.model_combo.findData(saved_model)))
        grid.addWidget(self.model_combo, row, 1, 1, 2)
        self.diag_button = QPushButton()
        self._tr(lambda: self.diag_button.setText(self.t("diagnostics")))
        self.diag_button.clicked.connect(self.show_diagnostics)
        row = self._next_row(grid)
        grid.addWidget(self.diag_button, row, 0, 1, 3)
        self.provider_combo.currentIndexChanged.connect(self._on_segmentation_option_changed)
        self.model_combo.currentIndexChanged.connect(self._on_segmentation_option_changed)
        layout.addWidget(blur)

        switching, grid = self._card("grp_transition")
        self.transition_enable = self._form_checkbox(grid, "cb_transition", True)
        self.transition_slider = self._form_slider(
            grid, "s_transition", 10, 30, 20, lambda v: f"{v / 10:.1f} {self.t('unit_s')}")
        self.pingpong_cb = self._form_checkbox(grid, "cb_pingpong", True)
        self.transition_enable.toggled.connect(lambda on: self._set_row_enabled(self.transition_slider, on))
        layout.addWidget(switching)
        layout.addStretch()

        # Blur preference survives restarts (it is a setting, not a glitch effect).
        cfg = load_config()
        # Older versions saved 1..3 as "blur_strength"; map that onto the new 1..10 scale.
        amount = cfg.get("blur_amount")
        if not isinstance(amount, int):
            amount = {1: 3, 2: 5, 3: 8}.get(cfg.get("blur_strength"), BLUR_AMOUNT_DEFAULT)
        self.blur_strength_slider.setValue(max(1, min(10, amount)))
        self.blur_lens_cb.setChecked(bool(cfg.get("blur_lens", BLUR_LENS_DEFAULT)))
        for slider, key, default in ((self.blur_dim_slider, "blur_dim", BLUR_DIM_DEFAULT),
                                     (self.blur_desat_slider, "blur_desaturate", BLUR_DESAT_DEFAULT)):
            value = cfg.get(key, default)
            slider.setValue(value if isinstance(value, int) else default)
        self.blur_strength_slider.valueChanged.connect(lambda v: update_config(blur_amount=v))
        self.blur_lens_cb.toggled.connect(lambda v: update_config(blur_lens=bool(v)))
        self.blur_dim_slider.valueChanged.connect(lambda v: update_config(blur_dim=v))
        self.blur_desat_slider.valueChanged.connect(lambda v: update_config(blur_desaturate=v))
        self.blur_cb.setChecked(bool(cfg.get("blur", False)) and self.blur_available())
        self.blur_cb.setEnabled(self.blur_available())
        self.blur_cb.toggled.connect(self.on_blur_toggled)
        slot = cfg.get("background_slot", 0)
        self.bg_slot = slot if isinstance(slot, int) and 0 <= slot <= BG_SLOTS and self.blur_available() else 0
        self._refresh_bg_buttons()
        self._background_card.setEnabled(self.blur_available())
        self.on_blur_toggled(self.blur_cb.isChecked(), save=False)

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
        if self.mode == "LIVE" and not getattr(self, "camera_enabled", True):
            color = "#757575"
        elif self.mode in ("FROZEN", "NO_CAMERA"):
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
        size = parse_size(load_config().get("panel_size")) or (560, 720)
        screen = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        self.resize(max(420, min(size[0], screen.width())), max(480, min(size[1], screen.height())))
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
        camera_off = self.mode == "LIVE" and not self.camera_enabled
        text = self.t("status_cam_off" if camera_off else STATUS_KEYS[self.mode])
        if self.preset_key and self.mode in ("LIVE", "LOOP") and not camera_off:
            text += f" · {self.t(self.preset_key)}"
        self.status_label.setText(text)
        color = "#9e9e9e" if camera_off else STATUS_COLORS[self.mode]
        self.status_label.setStyleSheet(
            f"font-weight: bold; font-size: 14px; color: {color}; "
            "padding: 6px; background-color: #1e1e1e; border-radius: 6px;")

    def _set_mode(self, mode):
        if mode != self.mode and self.segmenter is not None:
            # Live, loop and frozen image are different "videos": a recurrent model (RVM)
            # carrying memory from one into the other gives ghosting at the edges.
            self.segmenter.reset()
            self._last_segmentation_frame_key = None
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

    def _go_live(self):
        self._begin_transition("LIVE")
        self._set_mode("LIVE")

    def set_mode_live(self):
        """The LIVE button: your plain camera again. Clears every quick setting and the hedge
        (blur and background image are settings and stay as they are)."""
        self._apply_preset(None, switch_mode=False)
        self.hedge.leave()
        self._refresh_hedge_button()
        self.set_camera_enabled(True)
        self._go_live()

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
                      stutter=False, spinner=False, interval=10, duration=20, switch_mode=True):
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
        if self.mode in ("LIVE", "LOOP") or not switch_mode:
            self._refresh_status()
            self.update_button_style()
        else:
            self._go_live()

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

    def on_backend_change(self, _index):
        backend = self.backend_combo.currentData()
        if backend not in ("MSMF", "DSHOW") or backend == self.camera_backend:
            return
        self.camera_backend = backend
        self._stats_camera_times.clear()
        self._last_captured_timestamp = None
        update_config(camera_backend=backend)
        if self.current_cam_index is not None and self.capture_res:
            if self.reader is not None:
                self.reader.configure(self._selected_camera_indices(), *self.capture_res, backend)
            # The selected device may have different supported sizes on this backend.
            # Actual negotiated capture size is always shown in diagnostics.

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
        self.reader.configure(self._selected_camera_indices(), *size)
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
        render_start = time.perf_counter()
        now = time.monotonic()
        self.spinner_angle = (self.spinner_angle + 12) % 360
        w, h = self.cam_width, self.cam_height

        if self.reader is not None:
            live, capture_stamp, status = self.reader.snapshot()
        else:
            live, capture_stamp, status = None, 0.0, "connecting"
        if live is not None:
            if capture_stamp != self._last_captured_timestamp:
                self._last_captured_timestamp = capture_stamp
                self._stats_camera_times.append(now)
            live = fit_frame(live, w, h)

        if self.is_recording_loop:
            self._record_frame(now, live, capture_stamp)

        source = None
        camera_image = self.mode in ("FROZEN", "LOOP")    # refined below for live
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
            camera_image = False
            if live is not None and now - capture_stamp < 3.0:
                source = cv2.flip(live, -1) if effects_on and self.flip_cb.isChecked() else live
                camera_image = True
            elif not self.camera_enabled:
                source = draw_no_camera_frame(w, h, self.t("frame_camera_off"))
            elif self.camera_list is None or (self.reader is not None and status == "connecting") \
                    or (self.reader is None and self.camera_list):
                source = draw_spinner_frame(w, h, self.spinner_angle, self.t("frame_connecting"))
            else:
                source = draw_no_camera_frame(w, h, self.t("frame_no_camera"))

        # Background blur: only on images of you. Loading / "camera not found" screens
        # stay sharp; glitch effects are applied afterwards, like a real bad connection.
        self._refresh_blur_status()
        raw_source = source
        flipped = effects_on and self.flip_cb.isChecked()
        if not camera_image:
            frame_key = None
        elif self.mode == "LIVE":
            frame_key = ("live", capture_stamp, flipped)
        elif self.mode == "LOOP":
            frame_key = ("loop", self._loop_decoded_index, flipped)
        else:
            frame_key = ("frozen", id(self.frozen_frame), flipped)
        if camera_image and self.background_wanted():
            source = self._blurred(source, frame_key)
        if camera_image and self.hedge.active:
            mask = self._person_mask(raw_source, frame_key)
            usable = self.segmenter is not None and self.segmenter.engine != "failed"
            source = self.hedge.render(source, mask, now, mask_expected=usable)
            if not self.hedge.active:
                self._update_segmenter()          # free the model again if nothing else needs it
                self._refresh_hedge_button()

        if self.transition_frame is not None:
            if now < self.transition_until:
                source = self.transition_frame
            else:
                self.transition_frame = None
                self.transition_until = 0.0
                self.last_fps_frame = None

        in_transition = self.transition_frame is not None
        if self.reactions.active and (camera_image or in_transition):
            source = self.reactions.render(source, now)
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
                self._stats_output_times.append(time.monotonic())
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
        finally:
            self._stats_render_ms.append((time.perf_counter() - render_start) * 1000)



class PerformanceDialog(QDialog):
    """Read-only live diagnostics. GPU metrics are explicitly labeled when system-wide."""
    def __init__(self, owner):
        super().__init__(owner)
        self.setWindowTitle("Prestaties en diagnose / Performance diagnostics")
        self.resize(660, 530)
        layout = QVBoxLayout(self)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setStyleSheet("font-family: Consolas, monospace; font-size: 11pt")
        layout.addWidget(self.report)
        buttons = QHBoxLayout()
        copy_button = QPushButton("Kopieer rapport / Copy report")
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self.report.toPlainText()))
        buttons.addWidget(copy_button)
        refresh = QPushButton("Vernieuwen / Refresh")
        refresh.clicked.connect(self.refresh)
        buttons.addWidget(refresh)
        layout.addLayout(buttons)
        self._process = psutil.Process(os.getpid()) if psutil else None
        if self._process:
            self._process.cpu_percent(None)
        self._nvml = False
        if pynvml:
            try:
                pynvml.nvmlInit()
                self._nvml = True
            except Exception:
                pass
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    @staticmethod
    def _rate(times):
        if len(times) < 2:
            return "—"
        recent = [t for t in times if t >= time.monotonic() - 5]
        return f"{(len(recent)-1) / (recent[-1]-recent[0]):.1f}" if len(recent) >= 2 and recent[-1] > recent[0] else "—"

    def refresh(self):
        owner = self.parent()
        lines = [f"CoffeeBreak Cam Pro {VERSION}", "", "VIDEO / SEGMENTATIE"]
        seg = owner.segmenter
        lines.append(f"Camera: {owner.current_cam_name or '—'}  |  {owner.cam_width}×{owner.cam_height}")
        lines.append(f"Mode: {owner.mode}  |  Blur: {'on' if owner.background_wanted() else 'off'}")
        camera_diag = owner.reader.diagnostics() if owner.reader else None
        if camera_diag:
            lines.append(f"Camera backend (active): {camera_diag['backend'] or 'connecting'}"
                         f"  |  requested: {owner.camera_backend}")
            indices = camera_diag.get("device_indices", {})
            lines.append("Camera device mapping: " + (", ".join(
                f"{backend}={idx}" for backend, idx in sorted(indices.items())) or "unknown"))
            if owner.camera_backend not in indices:
                lines.append("  Selected backend has no verified index for this camera")
            if camera_diag["backend"] and camera_diag["backend"] != owner.camera_backend:
                lines.append("  Fallback in use: preferred camera backend could not provide frames")
            actual = camera_diag["format"]
            if actual:
                lines.append(f"Capture format reported: {actual[0]}×{actual[1]} @ {actual[2]:.1f} FPS")
            fps = camera_diag["fps"]
            lines.append(f"Camera capture FPS (cap.read): {fps:.1f}" if fps is not None
                         else "Camera capture FPS (cap.read): —")
            lines.append(f"Captured frames since start: {camera_diag['frames']}")
            lines += ["", "CAMERA START-UP (seconds since app start)"]
            if owner._enumerate_seconds is not None:
                lines.append(f"Camera enumeration: {owner._enumerate_seconds:.1f}s")
            lines += [f"  {t:7.1f}s  {what}" for t, what in STARTUP_MARKS]
            lines.append("MSMF hardware transforms: "
                         + ("off" if os.environ.get("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS") == "0" else "on"))
            lines += ["  " + e for e in camera_diag.get("events", [])] or ["  (no events yet)"]
        else:
            lines.append("Camera capture FPS (cap.read): — (camera off)")
        lines.append(f"Camera FPS (seen by UI): {self._rate(owner._stats_camera_times)}")
        lines.append(f"Output FPS (send):       {self._rate(owner._stats_output_times)}")
        if owner._stats_render_ms:
            lines.append(f"Frame processing:        {statistics.mean(owner._stats_render_ms):.1f} ms avg (last {len(owner._stats_render_ms)})")
        lines += ["", "ONNX RUNTIME"]
        lines.append(f"Requested provider: {owner.provider_combo.currentData()}")
        lines.append(f"Model: {owner.model_combo.currentData()}")
        if HAS_ONNX:
            lines.append(f"ORT version: {ort.__version__}")
            lines.append(f"Available providers: {', '.join(ort.get_available_providers())}")
        else:
            lines.append("onnxruntime not installed")
        if seg:
            engine, provider, error, timings, stamps, events = seg.snapshot()
            lines.append(f"Selected session provider: {provider or engine}")
            lines.append(f"Segmentations/s: {self._rate(stamps)}")
            if timings:
                sorted_times = sorted(timings)
                lines.append(f"Inference avg: {statistics.mean(timings):.1f} ms | p95: {sorted_times[int(.95*(len(sorted_times)-1))]:.1f} ms")
            if error:
                lines.append(f"ERROR: {error}")
            if events:
                lines.extend(["Recent events:"] + ["  " + e for e in events[-5:]])
        else:
            lines.append("Segmenter off (enable background blur to measure inference)")
        lines += ["", "PROCESS"]
        if self._process:
            try:
                mi = self._process.memory_info()
                lines.append(f"Process RAM RSS: {mi.rss / 1048576:.0f} MiB")
                lines.append(f"CPU: {self._process.cpu_percent(None):.0f}% (100%=one logical CPU)")
            except Exception as exc:
                lines.append(f"psutil: {exc}")
        else:
            lines.append("Install psutil for process RAM/CPU statistics")
        lines += ["", "NVIDIA GPU"]
        if self._nvml:
            try:
                count = pynvml.nvmlDeviceGetCount()
                for i in range(count):
                    dev = pynvml.nvmlDeviceGetHandleByIndex(i)
                    name = pynvml.nvmlDeviceGetName(dev)
                    if isinstance(name, bytes):
                        name = name.decode(errors='replace')
                    mem = pynvml.nvmlDeviceGetMemoryInfo(dev)
                    util = pynvml.nvmlDeviceGetUtilizationRates(dev)
                    lines.append(f"GPU {i}: {name}")
                    lines.append(f"  Total GPU memory: {mem.used / 1048576:.0f} / {mem.total / 1048576:.0f} MiB (system-wide)")
                    lines.append(f"  GPU compute/util: {util.gpu}% (system-wide)")
                    try:
                        apps = pynvml.nvmlDeviceGetComputeRunningProcesses(dev)
                        current = [x for x in apps if x.pid == os.getpid()]
                        if current:
                            lines.append(f"  This process VRAM (compute): {sum(x.usedGpuMemory for x in current) / 1048576:.0f} MiB")
                        else:
                            lines.append("  Per-process VRAM: unavailable/not listed")
                    except Exception:
                        lines.append("  Per-process VRAM: unavailable")
            except Exception as exc:
                lines.append(f"GPU monitoring error: {exc}")
        else:
            lines.append("Install nvidia-ml-py for NVIDIA monitoring (nvidia-smi backend)")
        lines += ["", "NOTES", "Session provider ≠ proof every ONNX node runs on GPU.",
                  "DirectML adapter identity cannot be verified from this readout.",
                  "For DirectML GPU selection, inspect Windows per-process GPU Engine.",
                  "The project uses onnxruntime-directml; no CUDA runtime is needed."]
        self.report.setPlainText("\n".join(lines))

    def closeEvent(self, event):
        self.timer.stop()
        self.parent()._diag_window = None
        event.accept()


if __name__ == "__main__":
    mark_startup("modules loaded")
    app = QApplication(sys.argv)
    # The floating button is a "tool" window, which Qt does not count as a main window:
    # without this, closing any message box made Qt think the last window was gone and
    # quietly ended the app. Quitting is done explicitly (menu / closeEvent).
    app.setQuitOnLastWindowClosed(False)
    _window = None
    install_crash_handlers(lambda: _window)
    window = _window = CoffeeBreakCam()
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    sys.exit(app.exec())
