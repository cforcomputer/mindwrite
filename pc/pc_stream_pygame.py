#!/usr/bin/env python3
# pc_stream_pygame.py
#
# MindPalaceEmulator (Pygame) + USB-serial streaming to Pico EPD firmware.
#
# Fix in this revision:
# - FIXED crash: pygame.surfarray.blit_array() does NOT accept 4-channel RGBA arrays.
#   We now build binary (0/255) circle masks by writing ONLY the alpha plane via
#   pygame.surfarray.pixels_alpha(), which is valid for an SRCALPHA surface.
#
# Fix in this revision:
# - FIXED headset mirrored text: flip the frame horizontally ONLY for the stream pack step.
#
# Fix in this revision:
# - FIXED "thin beautiful typewriter text" vs "missing vertical strokes" tradeoff:
#   We render text with AA ON to get a nice glyph shape, then immediately convert it
#   to a *binary alpha* surface using the glyph's alpha coverage (NOT luminance).
#   The final composed frame is strictly black/white again, so the 1bpp pack is stable
#   and vertical strokes do not disappear.
#
# Other behavior preserved:
# - Very responsive: repack 1bpp only when the frame changes (needs_redraw).
# - Circle outline only appears in Settings and only when selecting “Circle radius”.
# - Rolling settings menu: selected item is centered and larger; others smaller.
# - Optional invert UI: background black, text white.
# - Per-eye IPD (left/right), circle radius, font size, center Y, tracking.
#
# Controls:
#   CTRL+R : force FULL refresh (clears ghosting / boot checkerboard)
#   CTRL+S : save (or prompt to name if needed)
#   ESC    : back / menu
#
# Run:
#   python3 pc_stream_pygame.py --port COM5
#   python3 pc_stream_pygame.py --port /dev/ttyACM0

import argparse
import binascii
import datetime
import json
import os
import re
import struct
import sys
import time

import pygame

try:
    import serial  # pyserial
except Exception:
    serial = None

try:
    import numpy as np
except Exception:
    np = None


# --- DISPLAY CONFIG ---
WINDOW_WIDTH = 792
WINDOW_HEIGHT = 272
EYE_WIDTH = WINDOW_WIDTH // 2
EYE_HEIGHT = WINDOW_HEIGHT

W, H = WINDOW_WIDTH, WINDOW_HEIGHT
BYTES_PER_ROW = (W + 7) // 8
FRAME_BYTES = BYTES_PER_ROW * H

CAPSULE_FOLDER = "capsules"
os.makedirs(CAPSULE_FOLDER, exist_ok=True)
SETTINGS_FILE = os.path.join(CAPSULE_FOLDER, "settings.json")

# Stream-side correction: mirror horizontally for the headset EPD.
# This affects ONLY the packed serial stream, NOT the on-screen Pygame preview.
STREAM_MIRROR_X = True

# Text binarization from AA glyph alpha:
# Lower = thinner (but risk holes), higher = bolder.
# Typical sweet spot for small mono fonts is ~18..40.
TEXT_ALPHA_CUTOFF = 26

# Try to get a typewriter-like mono font if installed; fallback to default.
PREFERRED_MONO_FONTS = [
    "Courier Prime",
    "Special Elite",
    "Courier New",
    "Courier",
    "Liberation Mono",
    "DejaVu Sans Mono",
    "Consolas",
    "Menlo",
    "Monaco",
    "Lucida Console",
]


def build_packet(payload: bytes) -> bytes:
    magic = b"MWF1"
    ln = struct.pack("<I", len(payload))
    crc = binascii.crc32(payload) & 0xFFFFFFFF
    return magic + ln + payload + struct.pack("<I", crc)


def wait_for_ok(ser, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    buf = bytearray()
    while time.monotonic() < deadline:
        pygame.event.pump()
        try:
            chunk = ser.read(64)
        except Exception:
            return False
        if chunk:
            buf += chunk
            if b"OK" in buf:
                return True
            if len(buf) > 256:
                buf = buf[-256:]
        time.sleep(0.001)
    return False


def pack_1bpp_fast(surface: pygame.Surface, invert: bool = False) -> bytes:
    """
    Pack a 792x272 RGB surface into 1bpp bytes (row-major, top->bottom).
    White pixel -> bit 1, Black pixel -> bit 0.
    """
    if np is not None:
        arr = pygame.surfarray.array3d(surface)  # (W,H,3)
        arr = np.transpose(arr, (1, 0, 2))  # (H,W,3)

        lum = (
            30 * arr[:, :, 0].astype(np.uint16)
            + 59 * arr[:, :, 1].astype(np.uint16)
            + 11 * arr[:, :, 2].astype(np.uint16)
        ) // 100

        white_mask = lum >= 128
        if invert:
            white_mask = ~white_mask

        packed = np.packbits(white_mask, axis=1, bitorder="big")
        return packed.tobytes()

    # Fallback without numpy (slower)
    rgb = pygame.image.tostring(surface, "RGB")
    fb = bytearray([0xFF]) * FRAME_BYTES  # start white
    for y in range(H):
        row = y * W * 3
        out_row = y * BYTES_PER_ROW
        for x in range(W):
            r = rgb[row + 3 * x + 0]
            g = rgb[row + 3 * x + 1]
            b = rgb[row + 3 * x + 2]
            lum = (30 * r + 59 * g + 11 * b) // 100
            black = lum < 128
            if invert:
                black = not black
            if black:
                i = out_row + (x // 8)
                bit = 7 - (x % 8)
                fb[i] &= ~(1 << bit)
    return bytes(fb)


def _stream_surface_from_frame(frame_rgb: pygame.Surface) -> pygame.Surface:
    """
    Returns the surface used for stream packing.
    Preview remains unmodified; only the packed bytes are corrected.
    """
    if STREAM_MIRROR_X:
        return pygame.transform.flip(frame_rgb, True, False)
    return frame_rgb


def _pick_mono_font_path() -> str | None:
    for name in PREFERRED_MONO_FONTS:
        try:
            path = pygame.font.match_font(name, bold=False, italic=False)
        except Exception:
            path = None
        if path:
            return path
    return None


class MindPalaceEmulator:
    def __init__(
        self,
        stream_enabled: bool,
        port: str,
        baud: int,
        send_fps: float,
        invert_stream: bool,
        ack_timeout: float,
    ):
        pygame.init()
        pygame.font.init()

        self.stream_enabled = stream_enabled
        self.serial_port_name = port
        self.serial_baud = baud
        self.send_fps = max(0.05, float(send_fps))
        self.invert_stream = bool(invert_stream)
        self.ack_timeout = float(ack_timeout)

        self.ser = None

        self.screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT))
        pygame.display.set_caption("Mind Palace")

        # Eye render surfaces (RGB)
        self.left_render = pygame.Surface((EYE_WIDTH, EYE_HEIGHT)).convert()
        self.right_render = pygame.Surface((EYE_WIDTH, EYE_HEIGHT)).convert()

        # Final eye outputs (RGB)
        self.left_eye_out = pygame.Surface((EYE_WIDTH, EYE_HEIGHT)).convert()
        self.right_eye_out = pygame.Surface((EYE_WIDTH, EYE_HEIGHT)).convert()

        # Full frame
        self.frame_surface = pygame.Surface((WINDOW_WIDTH, WINDOW_HEIGHT)).convert()
        self._cached_frame = pygame.Surface((WINDOW_WIDTH, WINDOW_HEIGHT)).convert()

        # Cached packed framebuffer (1bpp) for streaming; updated ONLY when redraw happens
        self._cached_fb_bytes: bytes | None = None

        # Settings (persisted)
        self.settings = {
            "typewriter_mode": False,
            # Per-eye circle offsets (moves each circle center)
            "ipd_left_px": 0,
            "ipd_right_px": 0,
            # Circle radius (lens FOV)
            "circle_radius_px": 110,
            # Vertical alignment (lens center)
            "center_y_offset_px": 0,
            # Font size inside circles
            "circle_font_size": 12,
            # Tracking between glyphs
            "tracking": 1,
            # UI invert mode (background black, text white)
            "invert_ui": False,
            # Optional
            "auto_capitalize_i": False,
            "autocap_after_period": False,
        }
        self._load_settings()

        # Limits
        self.ipd_limit = 160
        self.center_y_limit = 120
        self.radius_min = 60
        self.radius_max = min(EYE_WIDTH, EYE_HEIGHT) // 2 - 4
        self.font_min = 8
        self.font_max = 18
        self.tracking_min = 0
        self.tracking_max = 4

        self._sanitize_settings()

        # Fonts
        self.font_body = None
        self.font_focus = None
        self.font_small = None
        self.font_ui = None
        self._font_path = None
        self.line_height = 18

        # Glyph cache (for fast tracked rendering)
        self._glyph_surf = {}
        self._glyph_w = {}

        self._apply_font_settings()

        # App state
        self.state = "MENU"
        self.menu_options = ["write", "open", "settings", "exit"]
        self.selected_index = 0

        self.current_filename = None
        self.temp_filename = None
        self.auto_named = False

        self.capsule_files = []
        self.browser_index = 0

        self.notes = [""]
        self.status_msg = ""
        self.msg_timer = 0

        self.autosave_delay = 900
        self.last_edit_time = 0
        self.dirty = False

        self.scroll_lines = 0

        # Settings menu (rolling window)
        self.settings_items = [
            ("Circle radius", "circle_radius_px", "int_radius"),
            ("Center Y", "center_y_offset_px", "int_center_y"),
            ("Circle font", "circle_font_size", "int_font"),
            ("Tracking", "tracking", "int_tracking"),
            ("IPD left", "ipd_left_px", "int_ipd"),
            ("IPD right", "ipd_right_px", "int_ipd"),
            ("Invert UI", "invert_ui", "bool"),
            ('Auto capitalize "i"', "auto_capitalize_i", "bool"),
            ("Autocapitalize after a .", "autocap_after_period", "bool"),
            ("Typewriter mode", "typewriter_mode", "bool"),
            ("Back", None, "back"),
        ]
        self.settings_index = 0

        # Backspace repeat (non-typewriter)
        self.backspace_down = False
        self.backspace_hold_start = 0
        self.backspace_next_repeat = 0
        self.backspace_initial_delay = 240
        self.backspace_repeat_interval = 28
        self.backspace_fast_after = 700
        self.backspace_fast_interval = 18

        # Naming flow
        self.name_buffer = ""
        self.name_error = ""

        # Streaming control
        self.force_full_next = True
        self._next_send_time = 0.0
        self._last_sent_fb = None

        # Performance: redraw only when needed
        self.needs_redraw = True

        # Circle masks: binary (no gray edges) so no “black ring”
        self._mask_left: pygame.Surface | None = None
        self._mask_right: pygame.Surface | None = None
        self._mask_params_left = None
        self._mask_params_right = None

        if self.stream_enabled:
            self._connect_serial()

    # -----------------------
    # Colors (pure BW)
    # -----------------------
    def _bg(self):
        return (0, 0, 0) if self.settings.get("invert_ui", False) else (255, 255, 255)

    def _fg(self):
        return (255, 255, 255) if self.settings.get("invert_ui", False) else (0, 0, 0)

    # -----------------------
    # Text rendering: AA -> binary alpha (so final frame is still 1-bit clean)
    # -----------------------
    def _render_text_binary_alpha(
        self, font: pygame.font.Font, text: str
    ) -> pygame.Surface:
        """
        Render text with AA to get a nice glyph shape, then convert to *binary alpha* using
        the glyph coverage (alpha channel). This produces thin, clean text without grays,
        and avoids the "AA makes vertical lines disappear after threshold" problem.
        """
        fg = self._fg()

        # If numpy isn't available, fall back to the old non-AA path (still functional).
        if np is None:
            return font.render(text, False, fg)

        aa = font.render(text, True, fg).convert_alpha()

        # Convert AA coverage to binary coverage.
        a = pygame.surfarray.array_alpha(aa)  # shape (w,h)
        mask = a >= int(TEXT_ALPHA_CUTOFF)

        out = pygame.Surface(aa.get_size(), pygame.SRCALPHA).convert_alpha()
        out.fill((fg[0], fg[1], fg[2], 0))

        alpha_view = pygame.surfarray.pixels_alpha(out)  # (w,h)
        alpha_view[:, :] = np.where(mask, 255, 0).astype(np.uint8)
        del alpha_view

        return out

    # -----------------------
    # Settings persistence
    # -----------------------
    def _load_settings(self):
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for k in list(self.settings.keys()):
                        if k in data:
                            if isinstance(self.settings[k], bool):
                                self.settings[k] = bool(data[k])
                            elif isinstance(self.settings[k], int):
                                try:
                                    self.settings[k] = int(data[k])
                                except Exception:
                                    pass
        except Exception:
            pass

    def _save_settings(self):
        try:
            os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
            tmp = SETTINGS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, SETTINGS_FILE)
        except Exception:
            self._notify_error("settings save failed")

    def _sanitize_settings(self):
        self.settings["ipd_left_px"] = max(
            -self.ipd_limit,
            min(self.ipd_limit, int(self.settings.get("ipd_left_px", 0))),
        )
        self.settings["ipd_right_px"] = max(
            -self.ipd_limit,
            min(self.ipd_limit, int(self.settings.get("ipd_right_px", 0))),
        )
        self.settings["center_y_offset_px"] = max(
            -self.center_y_limit,
            min(self.center_y_limit, int(self.settings.get("center_y_offset_px", 0))),
        )

        r = int(self.settings.get("circle_radius_px", 110))
        r = max(self.radius_min, min(self.radius_max, r))
        self.settings["circle_radius_px"] = r

        fs = int(self.settings.get("circle_font_size", 12))
        fs = max(self.font_min, min(self.font_max, fs))
        self.settings["circle_font_size"] = fs

        tr = int(self.settings.get("tracking", 1))
        tr = max(self.tracking_min, min(self.tracking_max, tr))
        self.settings["tracking"] = tr

    # -----------------------
    # Serial
    # -----------------------
    def _connect_serial(self):
        if serial is None:
            self.stream_enabled = False
            self._notify_error("pyserial missing")
            return
        try:
            self.ser = serial.Serial(
                self.serial_port_name,
                self.serial_baud,
                timeout=0.05,
                write_timeout=5,
            )
            time.sleep(0.5)
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass
        except Exception:
            self.ser = None
            self.stream_enabled = False
            self._notify_error("serial open failed")

    def _disconnect_serial(self):
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    # -----------------------
    # Fonts + glyph cache
    # -----------------------
    def _apply_font_settings(self):
        self._sanitize_settings()
        body_size = int(self.settings["circle_font_size"])

        self._font_path = _pick_mono_font_path()

        if self._font_path:
            self.font_body = pygame.font.Font(self._font_path, body_size)
            self.font_focus = pygame.font.Font(self._font_path, body_size + 6)
            self.font_small = pygame.font.Font(self._font_path, max(10, body_size - 2))
            self.font_ui = pygame.font.Font(self._font_path, 14)
        else:
            self.font_body = pygame.font.Font(None, body_size)
            self.font_focus = pygame.font.Font(None, body_size + 6)
            self.font_small = pygame.font.Font(None, max(10, body_size - 2))
            self.font_ui = pygame.font.Font(None, 14)

        self.line_height = int(body_size * 1.25) + 6

        # Glyph cache: use AA->binary-alpha so strokes are thin but final frame remains BW.
        self._glyph_surf.clear()
        self._glyph_w.clear()
        for code in range(32, 127):
            ch = chr(code)
            s = self._render_text_binary_alpha(self.font_body, ch)
            self._glyph_surf[ch] = s
            self._glyph_w[ch] = s.get_width()

        self.needs_redraw = True

    def _glyph(self, ch: str) -> pygame.Surface:
        s = self._glyph_surf.get(ch)
        if s is None:
            s = self._render_text_binary_alpha(self.font_body, ch)
            self._glyph_surf[ch] = s
            self._glyph_w[ch] = s.get_width()
        return s

    def _glyph_width(self, ch: str) -> int:
        w = self._glyph_w.get(ch)
        if w is None:
            w = self._glyph(ch).get_width()
        return w

    def _text_width(self, s: str) -> int:
        tracking = int(self.settings.get("tracking", 0))
        if not s:
            return 0
        w = 0
        for ch in s:
            w += self._glyph_width(ch)
        w += tracking * max(0, len(s) - 1)
        return w

    def _render_tracked_line(self, surface, x, y, text):
        tracking = int(self.settings.get("tracking", 0))
        if not text:
            return
        for idx, ch in enumerate(text):
            glyph = self._glyph(ch)
            surface.blit(glyph, (x, y))
            x += glyph.get_width()
            if idx != len(text) - 1:
                x += tracking

    def _render_tracked_line_typewriter_center(self, surface, center_x, y, text):
        """
        Typewriter-center: insertion point (end of line) stays at center_x.
        """
        if text is None:
            return
        total_w = self._text_width(text)
        x = center_x - total_w
        self._render_tracked_line(surface, x, y, text)

    # -----------------------
    # Wrapping
    # -----------------------
    def _wrap_width_for_circle(self) -> int:
        r = int(self.settings["circle_radius_px"])
        wrap_px = int((2 * r) * 0.80)
        wrap_px = max(80, min(EYE_WIDTH - 20, wrap_px))
        return wrap_px

    def _wrap_lines(self, wrap_px: int):
        wrapped = []

        for p in self.notes:
            if p == "":
                wrapped.append("")
                continue

            tokens = p.split(" ")
            line = ""

            for ti, tok in enumerate(tokens):
                sep = " " if ti < len(tokens) - 1 else ""
                tok_with_sep = tok + sep
                cand = tok_with_sep if not line else (line + tok_with_sep)

                if self._text_width(cand.rstrip()) <= wrap_px:
                    line = cand
                else:
                    wrapped.append(line.rstrip())
                    line = tok_with_sep

            wrapped.append(line.rstrip())

        return wrapped

    # -----------------------
    # Messages
    # -----------------------
    def _notify_error(self, msg: str):
        self.status_msg = msg
        self.msg_timer = pygame.time.get_ticks()
        self.needs_redraw = True

    # -----------------------
    # Filenames
    # -----------------------
    def _normalize_display_name(self, s: str) -> str:
        s = s.strip()
        s = re.sub(r"\s+", " ", s)
        return s

    def _make_safe_filename_base(self, display_name: str) -> str:
        name = self._normalize_display_name(display_name)
        name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", name)
        name = name.strip().strip(".")
        if not name:
            name = "untitled"
        return name[:60].strip()

    def _unique_filename(self, base_name: str) -> str:
        base = self._make_safe_filename_base(base_name)
        candidate = f"{base}.txt"
        path = os.path.join(CAPSULE_FOLDER, candidate)
        if not os.path.exists(path):
            return candidate
        n = 2
        while True:
            candidate = f"{base} ({n}).txt"
            path = os.path.join(CAPSULE_FOLDER, candidate)
            if not os.path.exists(path):
                return candidate
            n += 1

    def _first_n_words_display(self, n=4):
        text = " ".join(self.notes).strip()
        if not text:
            return None
        raw = text.split()
        words = []
        for tok in raw:
            cleaned = tok.strip(' \t\r\n.,!?;:"()[]{}')
            if cleaned:
                words.append(cleaned)
            if len(words) >= n:
                break
        if len(words) < n:
            return None
        return self._normalize_display_name(" ".join(words))

    def _ensure_temp_filename(self):
        if self.temp_filename is None:
            ts = datetime.datetime.now().strftime("%m%d_%H%M%S")
            self.temp_filename = f"capsule_{ts}.txt"

    def _write_to_file(self, filename: str):
        path = os.path.join(CAPSULE_FOLDER, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.notes))

    def _remove_file_if_exists(self, filename: str):
        if not filename:
            return
        path = os.path.join(CAPSULE_FOLDER, filename)
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    # -----------------------
    # Save / autosave
    # -----------------------
    def save_capsule(self, autosave=False):
        try:
            if self.current_filename is None:
                if not self.auto_named:
                    display = self._first_n_words_display(4)
                    if display is not None:
                        new_name = self._unique_filename(display)
                        old_temp = self.temp_filename
                        self.current_filename = new_name
                        self.auto_named = True
                        self._write_to_file(self.current_filename)
                        self.dirty = False
                        self._remove_file_if_exists(old_temp)
                        self.temp_filename = None
                        self.needs_redraw = True
                        return

                self._ensure_temp_filename()
                self._write_to_file(self.temp_filename)
                self.dirty = False
                self.needs_redraw = True
                return

            self._write_to_file(self.current_filename)
            self.dirty = False
            self.needs_redraw = True

        except Exception:
            self._notify_error("autosave failed" if autosave else "save failed")

    def _mark_edited(self):
        self.dirty = True
        self.last_edit_time = pygame.time.get_ticks()
        self.scroll_lines = 0
        self.needs_redraw = True

    def _maybe_autosave(self):
        if self.state != "WRITING":
            return
        if not self.dirty:
            return
        now = pygame.time.get_ticks()
        if now - self.last_edit_time >= self.autosave_delay:
            self.save_capsule(autosave=True)

    # -----------------------
    # Editing
    # -----------------------
    def _delete_char(self):
        if self.notes[-1]:
            self.notes[-1] = self.notes[-1][:-1]
            self._mark_edited()
        elif len(self.notes) > 1:
            self.notes.pop()
            self._mark_edited()

    def _get_last_non_space_char(self):
        for li in range(len(self.notes) - 1, -1, -1):
            s = self.notes[li]
            for ci in range(len(s) - 1, -1, -1):
                if not s[ci].isspace():
                    return s[ci]
        return None

    def _should_autocapitalize_next_letter(self):
        if not self.settings["autocap_after_period"]:
            return False
        return self._get_last_non_space_char() == "."

    def _apply_auto_i_on_boundary(self):
        if not self.settings["auto_capitalize_i"]:
            return
        li = len(self.notes) - 1
        line = self.notes[li]
        if not line:
            return
        i = len(line) - 1
        while i >= 0 and (line[i].isspace() or line[i] in '.,!?;:"()[]{}'):
            i -= 1
        if i < 0:
            return
        end = i
        while i >= 0 and line[i].isalpha():
            i -= 1
        start = i + 1
        if start > end:
            return
        word = line[start : end + 1]
        if word != "i":
            return
        left_ok = (start == 0) or (not line[start - 1].isalpha())
        right_ok = (end == len(line) - 1) or (not line[end + 1].isalpha())
        if not (left_ok and right_ok):
            return
        self.notes[li] = line[:start] + "I" + line[end + 1 :]
        self._mark_edited()

    def _insert_char(self, ch: str):
        if ch.isalpha() and self._should_autocapitalize_next_letter():
            ch = ch.upper()
        self.notes[-1] += ch
        self._mark_edited()
        if ch.isspace() or ch in '.,!?;:"()[]{}':
            self._apply_auto_i_on_boundary()

    # -----------------------
    # Circle geometry + binary masks (no gray edges)
    # -----------------------
    def _circle_center_for_eye(self, eye: str):
        self._sanitize_settings()

        base_cx = EYE_WIDTH // 2
        base_cy = EYE_HEIGHT // 2 + int(self.settings["center_y_offset_px"])

        if eye == "left":
            cx = base_cx + int(self.settings["ipd_left_px"])
        else:
            cx = base_cx + int(self.settings["ipd_right_px"])
        cy = base_cy

        r = int(self.settings["circle_radius_px"])

        cx = max(r + 1, min(EYE_WIDTH - r - 2, cx))
        cy = max(r + 1, min(EYE_HEIGHT - r - 2, cy))

        return cx, cy, r

    def _build_binary_circle_mask(
        self, w: int, h: int, cx: int, cy: int, r: int
    ) -> pygame.Surface:
        """
        Returns an RGBA surface where alpha is strictly 0 or 255 (no AA edge).
        Mask RGB is white; alpha=255 inside circle, alpha=0 outside.
        """
        mask = pygame.Surface((w, h), pygame.SRCALPHA).convert_alpha()
        mask.fill((255, 255, 255, 0))  # white RGB, transparent alpha

        if np is not None:
            y, x = np.ogrid[:h, :w]
            dist2 = (x - cx) * (x - cx) + (y - cy) * (y - cy)
            inside = dist2 <= (r * r)

            alpha_view = pygame.surfarray.pixels_alpha(mask)  # (w,h)
            alpha = np.zeros((h, w), dtype=np.uint8)
            alpha[inside] = 255
            alpha_view[:, :] = alpha.T
            del alpha_view
            return mask

        rr = r * r
        for yy in range(max(0, cy - r), min(h, cy + r + 1)):
            dy = yy - cy
            for xx in range(max(0, cx - r), min(w, cx + r + 1)):
                dx = xx - cx
                if dx * dx + dy * dy <= rr:
                    mask.set_at((xx, yy), (255, 255, 255, 255))
        return mask

    def _ensure_masks(self):
        lcx, lcy, lr = self._circle_center_for_eye("left")
        rcx, rcy, rr = self._circle_center_for_eye("right")

        lp = (lcx, lcy, lr, self.settings.get("invert_ui", False))
        rp = (rcx, rcy, rr, self.settings.get("invert_ui", False))

        if self._mask_left is None or self._mask_params_left != lp:
            self._mask_left = self._build_binary_circle_mask(
                EYE_WIDTH, EYE_HEIGHT, lcx, lcy, lr
            )
            self._mask_params_left = lp

        if self._mask_right is None or self._mask_params_right != rp:
            self._mask_right = self._build_binary_circle_mask(
                EYE_WIDTH, EYE_HEIGHT, rcx, rcy, rr
            )
            self._mask_params_right = rp

    def _apply_circle_mask(
        self,
        src_rgb: pygame.Surface,
        dst_rgb: pygame.Surface,
        mask_rgba: pygame.Surface,
    ):
        bg = self._bg()
        dst_rgb.fill(bg)

        tmp = pygame.Surface((EYE_WIDTH, EYE_HEIGHT), pygame.SRCALPHA).convert_alpha()
        tmp.fill((0, 0, 0, 0))
        tmp.blit(src_rgb, (0, 0))
        tmp.blit(mask_rgba, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        dst_rgb.blit(tmp, (0, 0))

    # -----------------------
    # Input handling
    # -----------------------
    def handle_input(self):
        now = pygame.time.get_ticks()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self._disconnect_serial()
                pygame.quit()
                sys.exit()

            if event.type == pygame.KEYDOWN:
                mods = pygame.key.get_mods()

                if event.key == pygame.K_r and (mods & pygame.KMOD_CTRL):
                    self.force_full_next = True
                    self.needs_redraw = True
                    return

                if event.key == pygame.K_s and (mods & pygame.KMOD_CTRL):
                    if self.state == "WRITING":
                        first4 = self._first_n_words_display(4)
                        has_real_name = (
                            self.current_filename is not None
                            and not self.current_filename.startswith("capsule_")
                        )
                        if first4 is None and not has_real_name and not self.auto_named:
                            self._start_naming_prompt()
                        else:
                            self.save_capsule(autosave=False)
                    return

                if self.state == "MENU":
                    self._nav_menu(event.key)
                elif self.state == "BROWSER":
                    self._nav_browser(event.key)
                elif self.state == "SETTINGS":
                    self._nav_settings(event)
                elif self.state == "NAMING":
                    self._nav_naming(event)
                elif self.state == "WRITING":
                    if (
                        not self.settings["typewriter_mode"]
                    ) and event.key == pygame.K_BACKSPACE:
                        self.backspace_down = True
                        self.backspace_hold_start = now
                        self._delete_char()
                        self.backspace_next_repeat = now + self.backspace_initial_delay
                    else:
                        self._nav_writing(event)

            if event.type == pygame.KEYUP:
                if (
                    self.state == "WRITING"
                    and (not self.settings["typewriter_mode"])
                    and event.key == pygame.K_BACKSPACE
                ):
                    self.backspace_down = False

        if (
            self.state == "WRITING"
            and (not self.settings["typewriter_mode"])
            and self.backspace_down
        ):
            if now >= self.backspace_next_repeat:
                held_for = now - self.backspace_hold_start
                interval = (
                    self.backspace_fast_interval
                    if held_for >= self.backspace_fast_after
                    else self.backspace_repeat_interval
                )
                self._delete_char()
                self.backspace_next_repeat = now + interval

        self._maybe_autosave()

    # -----------------------
    # Menu navigation
    # -----------------------
    def _nav_menu(self, key):
        if key == pygame.K_UP:
            self.selected_index = (self.selected_index - 1) % len(self.menu_options)
            self.needs_redraw = True
        elif key == pygame.K_DOWN:
            self.selected_index = (self.selected_index + 1) % len(self.menu_options)
            self.needs_redraw = True
        elif key == pygame.K_RETURN:
            choice = self.menu_options[self.selected_index]
            if choice == "exit":
                self._disconnect_serial()
                pygame.quit()
                sys.exit()
            elif choice == "write":
                self.notes = [""]
                self.current_filename = None
                self.temp_filename = None
                self.auto_named = False
                self.dirty = False
                self.last_edit_time = 0
                self.scroll_lines = 0
                self.state = "WRITING"
                self.needs_redraw = True
            elif choice == "open":
                self._refresh_capsule_list()
                self.browser_index = 0
                self.state = "BROWSER"
                self.needs_redraw = True
            elif choice == "settings":
                self.settings_index = 0
                self.state = "SETTINGS"
                self.needs_redraw = True

    def _refresh_capsule_list(self):
        files = []
        try:
            for f in os.listdir(CAPSULE_FOLDER):
                if f.lower().endswith(".txt"):
                    files.append(f)
        except Exception:
            files = []
        files.sort(key=lambda x: x.lower())
        self.capsule_files = files

    def _display_name_from_filename(self, filename):
        return os.path.splitext(filename)[0]

    def _nav_browser(self, key):
        if key == pygame.K_ESCAPE:
            self.state = "MENU"
            self.needs_redraw = True
            return
        if not self.capsule_files:
            return

        if key == pygame.K_UP:
            self.browser_index = (self.browser_index - 1) % len(self.capsule_files)
            self.needs_redraw = True
        elif key == pygame.K_DOWN:
            self.browser_index = (self.browser_index + 1) % len(self.capsule_files)
            self.needs_redraw = True
        elif key == pygame.K_RETURN:
            f = self.capsule_files[self.browser_index]
            try:
                with open(
                    os.path.join(CAPSULE_FOLDER, f), "r", encoding="utf-8"
                ) as file:
                    self.notes = file.read().splitlines()
                if not self.notes:
                    self.notes = [""]
                self.current_filename = f
                self.temp_filename = None
                self.auto_named = True
                self.state = "WRITING"
                self.dirty = False
                self.last_edit_time = pygame.time.get_ticks()
                self.scroll_lines = 0
                self.needs_redraw = True
            except Exception:
                self._notify_error("open failed")

    def _nav_settings(self, event):
        if event.key == pygame.K_ESCAPE:
            self.state = "MENU"
            self.needs_redraw = True
            return

        if event.key == pygame.K_UP:
            self.settings_index = (self.settings_index - 1) % len(self.settings_items)
            self.needs_redraw = True
            return
        if event.key == pygame.K_DOWN:
            self.settings_index = (self.settings_index + 1) % len(self.settings_items)
            self.needs_redraw = True
            return

        if event.key in (pygame.K_RETURN, pygame.K_LEFT, pygame.K_RIGHT):
            label, setting_key, kind = self.settings_items[self.settings_index]

            if kind == "back" or setting_key is None:
                self.state = "MENU"
                self.needs_redraw = True
                return

            if kind == "bool":
                self.settings[setting_key] = not self.settings[setting_key]
                self._save_settings()

                if setting_key == "invert_ui":
                    self._apply_font_settings()
                    self._mask_left = None
                    self._mask_right = None

                self.needs_redraw = True
                return

            if kind == "int_ipd":
                v = int(self.settings.get(setting_key, 0))
                step = 2
                if event.key == pygame.K_LEFT:
                    self.settings[setting_key] = max(-self.ipd_limit, v - step)
                elif event.key == pygame.K_RIGHT:
                    self.settings[setting_key] = min(self.ipd_limit, v + step)
                elif event.key == pygame.K_RETURN:
                    self.settings[setting_key] = 0
                self._save_settings()
                self._mask_left = None
                self._mask_right = None
                self.needs_redraw = True
                return

            if kind == "int_radius":
                v = int(self.settings.get(setting_key, 110))
                step = 2
                if event.key == pygame.K_LEFT:
                    self.settings[setting_key] = max(self.radius_min, v - step)
                elif event.key == pygame.K_RIGHT:
                    self.settings[setting_key] = min(self.radius_max, v + step)
                elif event.key == pygame.K_RETURN:
                    self.settings[setting_key] = 110
                self._save_settings()
                self._mask_left = None
                self._mask_right = None
                self.needs_redraw = True
                return

            if kind == "int_center_y":
                v = int(self.settings.get(setting_key, 0))
                step = 2
                if event.key == pygame.K_LEFT:
                    self.settings[setting_key] = max(-self.center_y_limit, v - step)
                elif event.key == pygame.K_RIGHT:
                    self.settings[setting_key] = min(self.center_y_limit, v + step)
                elif event.key == pygame.K_RETURN:
                    self.settings[setting_key] = 0
                self._save_settings()
                self._mask_left = None
                self._mask_right = None
                self.needs_redraw = True
                return

            if kind == "int_font":
                v = int(self.settings.get(setting_key, 12))
                step = 1
                if event.key == pygame.K_LEFT:
                    self.settings[setting_key] = max(self.font_min, v - step)
                elif event.key == pygame.K_RIGHT:
                    self.settings[setting_key] = min(self.font_max, v + step)
                elif event.key == pygame.K_RETURN:
                    self.settings[setting_key] = 12
                self._save_settings()
                self._apply_font_settings()
                self.needs_redraw = True
                return

            if kind == "int_tracking":
                v = int(self.settings.get(setting_key, 1))
                step = 1
                if event.key == pygame.K_LEFT:
                    self.settings[setting_key] = max(self.tracking_min, v - step)
                elif event.key == pygame.K_RIGHT:
                    self.settings[setting_key] = min(self.tracking_max, v + step)
                elif event.key == pygame.K_RETURN:
                    self.settings[setting_key] = 1
                self._save_settings()
                self.needs_redraw = True
                return

    def _start_naming_prompt(self):
        self.name_buffer = ""
        self.name_error = ""
        self.state = "NAMING"
        self.needs_redraw = True

    def _commit_named_save(self):
        name = self._normalize_display_name(self.name_buffer)
        if not name:
            self.name_error = "enter a name"
            self.needs_redraw = True
            return

        filename = self._unique_filename(name)
        old_temp = self.temp_filename
        self.current_filename = filename
        self.auto_named = True
        try:
            self._write_to_file(self.current_filename)
            self.dirty = False
            self._remove_file_if_exists(old_temp)
            self.temp_filename = None
            self.state = "WRITING"
            self.name_error = ""
            self.needs_redraw = True
        except Exception:
            self._notify_error("save failed")

    def _nav_naming(self, event):
        if event.key == pygame.K_ESCAPE:
            self.state = "WRITING"
            self.name_error = ""
            self.needs_redraw = True
            return
        if event.key == pygame.K_RETURN:
            self._commit_named_save()
            return
        if event.key == pygame.K_BACKSPACE:
            if self.name_buffer:
                self.name_buffer = self.name_buffer[:-1]
                self.needs_redraw = True
            return
        if event.unicode and event.unicode.isprintable():
            if len(self.name_buffer) < 60:
                self.name_buffer += event.unicode
                self.needs_redraw = True

    def _nav_writing(self, event):
        if event.key == pygame.K_ESCAPE:
            self.state = "MENU"
            self.needs_redraw = True
            return

        if event.key == pygame.K_UP:
            wrap_px = self._wrap_width_for_circle()
            total_lines = len(self._wrap_lines(wrap_px))
            max_scroll = max(0, total_lines - 1)
            self.scroll_lines = min(max_scroll, self.scroll_lines + 1)
            self.needs_redraw = True
            return

        if event.key == pygame.K_DOWN:
            self.scroll_lines = max(0, self.scroll_lines - 1)
            self.needs_redraw = True
            return

        if event.key == pygame.K_RETURN:
            self.notes.append("")
            self._mark_edited()
            return

        if event.unicode and event.unicode.isprintable():
            self._insert_char(event.unicode)

    # -----------------------
    # Rendering helpers
    # -----------------------
    def _render_status(self, surface, cx):
        if self.status_msg and pygame.time.get_ticks() - self.msg_timer < 1400:
            msg = self._render_text_binary_alpha(self.font_ui, self.status_msg)
            surface.blit(msg, (cx - msg.get_width() // 2, 6))

    def _render_menu_centered(self, surface, cx, cy):
        surface.fill(self._bg())

        spacing = self.line_height + 6
        selected = self.selected_index

        for i, opt in enumerate(self.menu_options):
            y = cy + (i - selected) * spacing
            txt = (
                self._render_text_binary_alpha(self.font_focus, opt)
                if i == selected
                else self._render_text_binary_alpha(self.font_body, opt)
            )
            x = cx - txt.get_width() // 2
            surface.blit(txt, (x, y - txt.get_height() // 2))

        self._render_status(surface, cx)

    def _render_browser_centered(self, surface, cx, cy):
        surface.fill(self._bg())

        title = self._render_text_binary_alpha(self.font_focus, "open")
        surface.blit(title, (cx - title.get_width() // 2, 8))

        if not self.capsule_files:
            txt = self._render_text_binary_alpha(self.font_body, "no capsules")
            surface.blit(txt, (cx - txt.get_width() // 2, cy - txt.get_height() // 2))
            return

        selected = self.browser_index
        window = 7
        half = window // 2

        for offset in range(-half, half + 1):
            idx = (selected + offset) % len(self.capsule_files)
            y = cy + offset * (self.line_height + 2)

            display = self._display_name_from_filename(self.capsule_files[idx])

            if offset == 0:
                txt = self._render_text_binary_alpha(self.font_focus, display)
            elif abs(offset) == 1:
                txt = self._render_text_binary_alpha(self.font_body, display)
            else:
                txt = self._render_text_binary_alpha(self.font_small, display)

            x = cx - txt.get_width() // 2
            surface.blit(txt, (x, y - txt.get_height() // 2))

    def _settings_line_text(self, label, key, kind):
        if kind == "back":
            return "back"
        if kind == "bool":
            val = "on" if self.settings.get(key, False) else "off"
            return f"{label}: {val}"
        if kind == "int_ipd":
            v = int(self.settings.get(key, 0))
            return f"{label}: {v:+d}px"
        if kind == "int_radius":
            v = int(self.settings.get(key, 110))
            return f"{label}: {v:d}px"
        if kind == "int_center_y":
            v = int(self.settings.get(key, 0))
            return f"{label}: {v:+d}px"
        if kind == "int_font":
            v = int(self.settings.get(key, 12))
            return f"{label}: {v:d}px"
        if kind == "int_tracking":
            v = int(self.settings.get(key, 1))
            return f"{label}: {v:d}"
        return label

    def _render_settings_rolling(self, surface, cx, cy):
        surface.fill(self._bg())

        title = self._render_text_binary_alpha(self.font_focus, "settings")
        surface.blit(title, (cx - title.get_width() // 2, 8))

        selected = self.settings_index
        n = len(self.settings_items)

        window = 7
        half = window // 2

        for offset in range(-half, half + 1):
            idx = (selected + offset) % n
            label, key, kind = self.settings_items[idx]
            line = self._settings_line_text(label, key, kind)

            if offset == 0:
                font = self.font_focus
            elif abs(offset) == 1:
                font = self.font_body
            else:
                font = self.font_small

            txt = self._render_text_binary_alpha(font, line)
            x = cx - txt.get_width() // 2
            y = cy + offset * (self.line_height + 2)
            surface.blit(txt, (x, y - txt.get_height() // 2))

    def _render_naming_centered(self, surface, cx, cy):
        surface.fill(self._bg())

        title = self._render_text_binary_alpha(self.font_focus, "name")
        surface.blit(title, (cx - title.get_width() // 2, cy - 56))

        prompt = self._render_text_binary_alpha(self.font_body, "name:")
        surface.blit(prompt, (cx - 160, cy - 10))

        name_txt = self._render_text_binary_alpha(
            self.font_body, self.name_buffer or "_"
        )
        surface.blit(name_txt, (cx - 60, cy - 10))

        hint = self._render_text_binary_alpha(self.font_ui, "enter=save  esc=cancel")
        surface.blit(hint, (cx - hint.get_width() // 2, cy + 26))

        if self.name_error:
            err = self._render_text_binary_alpha(self.font_ui, self.name_error)
            surface.blit(err, (cx - err.get_width() // 2, cy + 42))

    def _render_writing_typewriter_center(self, surface, cx, cy):
        surface.fill(self._bg())

        wrap_px = self._wrap_width_for_circle()
        lines = self._wrap_lines(wrap_px)
        if not lines:
            lines = [""]

        bottom_index = max(0, len(lines) - 1 - self.scroll_lines)

        baseline_y = cy + 8

        y = baseline_y
        for idx in range(bottom_index, -1, -1):
            self._render_tracked_line_typewriter_center(surface, cx, y, lines[idx])
            y -= self.line_height
            if y < 0:
                break

    def _should_draw_circle_outline(self) -> bool:
        if self.state != "SETTINGS":
            return False
        _label, key, _kind = self.settings_items[self.settings_index]
        return key == "circle_radius_px"

    def _render_eye_scene(self, eye_surface: pygame.Surface, eye: str):
        cx, cy, r = self._circle_center_for_eye(eye)

        if self.state == "MENU":
            self._render_menu_centered(eye_surface, cx, cy)
        elif self.state == "BROWSER":
            self._render_browser_centered(eye_surface, cx, cy)
        elif self.state == "SETTINGS":
            self._render_settings_rolling(eye_surface, cx, cy)
        elif self.state == "NAMING":
            self._render_naming_centered(eye_surface, cx, cy)
        elif self.state == "WRITING":
            self._render_writing_typewriter_center(eye_surface, cx, cy)
        else:
            eye_surface.fill(self._bg())

        if self._should_draw_circle_outline():
            pygame.draw.circle(eye_surface, self._fg(), (cx, cy), r, width=1)

    def _compose_frame(self):
        self._ensure_masks()

        self._render_eye_scene(self.left_render, "left")
        self._render_eye_scene(self.right_render, "right")

        self._apply_circle_mask(self.left_render, self.left_eye_out, self._mask_left)
        self._apply_circle_mask(self.right_render, self.right_eye_out, self._mask_right)

        self.frame_surface.fill(self._bg())
        self.frame_surface.blit(self.left_eye_out, (0, 0))
        self.frame_surface.blit(self.right_eye_out, (EYE_WIDTH, 0))

    # -----------------------
    # Streaming
    # -----------------------
    def _maybe_stream_frame(self):
        if not self.stream_enabled or self.ser is None:
            return

        now = time.monotonic()
        if now < self._next_send_time:
            return

        fb = self._cached_fb_bytes
        if fb is None:
            stream_src = _stream_surface_from_frame(self._cached_frame)
            fb = pack_1bpp_fast(stream_src, invert=self.invert_stream)
            self._cached_fb_bytes = fb

        if (not self.force_full_next) and (self._last_sent_fb == fb):
            self._next_send_time = now + (1.0 / self.send_fps)
            return

        flags = 0x01 if self.force_full_next else 0x00
        payload = bytes([flags]) + fb
        pkt = build_packet(payload)

        try:
            waiting = getattr(self.ser, "in_waiting", 0)
            if waiting:
                self.ser.read(waiting)
        except Exception:
            pass

        try:
            self.ser.write(pkt)
            self.ser.flush()
        except Exception:
            self._notify_error("serial write failed")
            self._disconnect_serial()
            self.stream_enabled = False
            return

        ok = wait_for_ok(self.ser, self.ack_timeout)
        if not ok:
            self._notify_error("ACK timeout")
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass

        self._last_sent_fb = fb
        self.force_full_next = False
        self._next_send_time = now + (1.0 / self.send_fps)

    # -----------------------
    # Draw
    # -----------------------
    def draw(self):
        if self.needs_redraw:
            self._compose_frame()
            self._cached_frame.blit(self.frame_surface, (0, 0))

            stream_src = _stream_surface_from_frame(self._cached_frame)
            self._cached_fb_bytes = pack_1bpp_fast(
                stream_src, invert=self.invert_stream
            )

            self.needs_redraw = False

        self.screen.blit(self._cached_frame, (0, 0))
        pygame.display.flip()

        self._maybe_stream_frame()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--port",
        default="",
        help="Serial port to Pico (e.g. COM5 or /dev/ttyACM0). If omitted, preview only.",
    )
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument(
        "--send-fps", type=float, default=8.0, help="Max send rate to display."
    )
    ap.add_argument(
        "--invert-stream",
        action="store_true",
        help="Invert packing stream (rare; usually leave off).",
    )
    ap.add_argument("--ack-timeout", type=float, default=30.0)
    ap.add_argument(
        "--no-stream", action="store_true", help="Force preview only (no serial)."
    )
    args = ap.parse_args()

    stream_enabled = (not args.no_stream) and bool(args.port.strip())

    app = MindPalaceEmulator(
        stream_enabled=stream_enabled,
        port=args.port.strip(),
        baud=args.baud,
        send_fps=args.send_fps,
        invert_stream=args.invert_stream,
        ack_timeout=args.ack_timeout,
    )

    clock = pygame.time.Clock()
    while True:
        app.handle_input()
        app.draw()
        clock.tick(60)


if __name__ == "__main__":
    main()
