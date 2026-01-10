import argparse
import binascii
import datetime
import json
import math
import os
import re
import struct
import sys
import time

import pygame
import pygame.gfxdraw
import serial

# ============================================================
# DISPLAY / PROTOCOL CONSTANTS
# ============================================================
WINDOW_WIDTH = 792
WINDOW_HEIGHT = 272
EYE_WIDTH = WINDOW_WIDTH // 2
EYE_HEIGHT = WINDOW_HEIGHT

BYTES_PER_ROW = (WINDOW_WIDTH + 7) // 8
FRAME_BYTES = BYTES_PER_ROW * WINDOW_HEIGHT

CAPSULE_FOLDER = "capsules"
if not os.path.exists(CAPSULE_FOLDER):
    os.makedirs(CAPSULE_FOLDER)

SETTINGS_FILE = os.path.join(CAPSULE_FOLDER, "settings.json")

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
GRAY = (220, 220, 220)


# ============================================================
# SERIAL FRAME PACKING (PC -> PICO)
# ============================================================
def pack_1bpp(
    surface: pygame.Surface, invert: bool = False, mirror_x: bool = True
) -> bytes:
    """
    Packs a 792x272 surface into 1bpp row-major bytes, MSB = leftmost pixel.
    mirror_x=True fixes the "mirrored on display but not in pygame" issue by flipping X.
    """
    rgb = pygame.image.tostring(surface, "RGB")
    fb = bytearray([0xFF]) * FRAME_BYTES  # start white

    w, h = WINDOW_WIDTH, WINDOW_HEIGHT

    for y in range(h):
        row = y * w * 3
        out_row = y * BYTES_PER_ROW
        for x in range(w):
            sx = (
                (w - 1 - x) if mirror_x else x
            )  # <-- mirror here to fix e-ink mirroring
            r = rgb[row + 3 * sx + 0]
            g = rgb[row + 3 * sx + 1]
            b = rgb[row + 3 * sx + 2]
            lum = (30 * r + 59 * g + 11 * b) // 100
            black = lum < 128
            if invert:
                black = not black
            if black:
                i = out_row + (x // 8)
                bit = 7 - (x % 8)
                fb[i] &= ~(1 << bit)

    return bytes(fb)


def build_packet(payload: bytes) -> bytes:
    magic = b"MWF1"
    ln = struct.pack("<I", len(payload))
    crc = binascii.crc32(payload) & 0xFFFFFFFF
    return magic + ln + payload + struct.pack("<I", crc)


def wait_for_ok(ser: serial.Serial, timeout_s: float) -> bool:
    """
    Read bytes until we see b'OK' (in-stream), while not blocking pygame.
    """
    deadline = time.monotonic() + timeout_s
    last = bytearray()

    while time.monotonic() < deadline:
        pygame.event.pump()

        chunk = ser.read(64)  # non-blocking-ish due to ser.timeout
        if chunk:
            last += chunk
            if b"OK" in last:
                return True
            if len(last) > 256:
                last = last[-256:]
        time.sleep(0.001)

    return False


class EInkStreamer:
    """
    Sends frames to Pico using the MWF1 protocol.
    Payload can be:
      - legacy: FRAME_BYTES
      - new:    1 + FRAME_BYTES where payload[0] flags bit0 = force_full
    """

    def __init__(self, port: str, baud: int = 115200, ack_timeout: float = 30.0):
        self.ser = serial.Serial(port, baud, timeout=0.05, write_timeout=5)
        time.sleep(0.5)
        self.ser.reset_input_buffer()
        self.ack_timeout = ack_timeout

        self.last_sent_frame: bytes | None = None

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def send_frame(self, frame_1bpp: bytes, force_full: bool):
        if len(frame_1bpp) != FRAME_BYTES:
            raise ValueError("frame size mismatch")

        flags = 0x01 if force_full else 0x00
        payload = bytes([flags]) + frame_1bpp
        pkt = build_packet(payload)

        waiting = self.ser.in_waiting
        if waiting:
            self.ser.read(waiting)

        self.ser.write(pkt)
        self.ser.flush()

        ok = wait_for_ok(self.ser, self.ack_timeout)
        if not ok:
            print("ACK timeout (no OK). Resyncing input buffer.", file=sys.stderr)
            self.ser.reset_input_buffer()

    def maybe_send(self, frame_1bpp: bytes, force_full: bool):
        """
        Only send on changes, unless force_full is requested.
        """
        if force_full:
            self.send_frame(frame_1bpp, force_full=True)
            self.last_sent_frame = frame_1bpp
            return

        if self.last_sent_frame is None or self.last_sent_frame != frame_1bpp:
            self.send_frame(frame_1bpp, force_full=False)
            self.last_sent_frame = frame_1bpp


# ============================================================
# APP (YOUR ORIGINAL EMULATOR, WITH WHITE BACKGROUND + STREAM)
# ============================================================
class MindPalaceEmulator:
    def __init__(self, streamer: EInkStreamer | None = None, invert: bool = False):
        pygame.init()

        self.screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT))
        pygame.display.set_caption("Mind Palace")

        self.eye_surface = pygame.Surface((EYE_WIDTH, EYE_HEIGHT), flags=0)

        self.font_adjust_min = 10
        self.font_adjust_max = 24

        self.settings = {
            "typewriter_mode": False,
            "room_mode": False,  # still exists but background is forced white now
            "font_size": 14,
            "tracking": 1,
            "auto_capitalize_i": False,
            "autocap_after_period": False,
        }
        self._load_settings()

        self.font_body = None
        self.font_header = None
        self.font_ui = None
        self.line_height = 20

        self._apply_font_settings()

        # --- Room look members kept (but we won't draw room bg anymore) ---
        self.mouse_look_enabled = True
        self.mouse_look_quant_step = 0.010
        self.room_yaw = 0.0
        self.room_pitch = 0.0
        self.room_yaw_limit = 0.75
        self.room_pitch_limit = 0.35
        self.room_yaw_step = 0.045
        self.room_pitch_step = 0.035

        self.room_half_w = 3.2
        self.room_half_h = 2.1
        self.room_half_d = 5.2
        self.cam_height = 1.10
        self.cam_back_bias = 0.35
        self.cam_x = 0.0
        self.cam_y = -self.room_half_h + self.cam_height
        self.cam_z = -self.room_half_d * self.cam_back_bias

        self.room_bg = pygame.Surface((EYE_WIDTH, EYE_HEIGHT), flags=0)

        panel_w = int(EYE_WIDTH * 0.74)
        panel_h = int(EYE_HEIGHT * 0.58)
        panel_x = (EYE_WIDTH - panel_w) // 2
        panel_y = (EYE_HEIGHT - panel_h) // 2 + 8
        self.hud_panel_rect = pygame.Rect(panel_x, panel_y, panel_w, panel_h)

        self.panel_shadow_surf = pygame.Surface(
            (EYE_WIDTH, EYE_HEIGHT), pygame.SRCALPHA
        )
        self.typewriter_offset_y = 18

        self.state = "MENU"
        self.menu_options = ["write", "open", "settings", "exit"]
        self.selected_index = 0

        self.current_filename = None
        self.temp_filename = None
        self.auto_named = False

        self.capsule_files = []
        self.browser_index = 0
        self.browser_scroll = 0
        self.browser_row_h = 25
        self.browser_top_y = 50
        self.browser_bottom_pad = 18

        self.notes = [""]
        self.status_msg = ""
        self.msg_timer = 0

        self.autosave_delay = 900
        self.last_edit_time = 0
        self.dirty = False

        self.scroll_lines = 0

        self.tw_sel_line = None
        self.tw_sel_char = None

        self.settings_items = [
            ("Typewriter mode", "typewriter_mode"),
            ("Room", "room_mode"),
            ('Auto capitalize "i"', "auto_capitalize_i"),
            ("Autocapitalize after a .", "autocap_after_period"),
            ("Back", None),
        ]
        self.settings_index = 0

        self.backspace_down = False
        self.backspace_hold_start = 0
        self.backspace_next_repeat = 0
        self.backspace_initial_delay = 260
        self.backspace_repeat_interval = 32
        self.backspace_fast_after = 900
        self.backspace_fast_interval = 22

        self.name_buffer = ""
        self.name_error = ""

        self.font_adjust_mode = False

        self._glyph_surf = {}
        self._glyph_w = {}
        self._rebuild_glyph_cache()

        # streaming
        self.streamer = streamer
        self.stream_invert = invert
        self.force_full_next = False  # set by hotkey; consumed after a send

    # -----------------------
    # Settings persistence
    # -----------------------
    def _load_settings(self):
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for k in self.settings.keys():
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
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2)
        except Exception:
            self._notify_error("settings save failed.")

    # -----------------------
    # Fonts
    # -----------------------
    def _pick_mono_font(self):
        return (
            pygame.font.match_font("couriernew")
            or pygame.font.match_font("courier")
            or pygame.font.match_font("consolas")
            or pygame.font.match_font("menlo")
            or pygame.font.match_font("monospace")
            or pygame.font.match_font("arial")
        )

    def _apply_font_settings(self):
        mono = self._pick_mono_font()

        body_size = int(self.settings.get("font_size", 14))
        body_size = max(self.font_adjust_min, min(self.font_adjust_max, body_size))
        self.settings["font_size"] = body_size

        tracking = int(self.settings.get("tracking", 1))
        tracking = max(0, min(4, tracking))
        self.settings["tracking"] = tracking

        self.font_body = pygame.font.Font(mono, body_size)
        self.font_header = pygame.font.Font(mono, body_size + 4)
        self.font_ui = pygame.font.Font(mono, 12)

        self.line_height = int(body_size * 1.25) + 6

        if hasattr(self, "_glyph_surf"):
            self._rebuild_glyph_cache()

    # -----------------------
    # Overlay (unmasked): errors, CAPS, font-adjust hint only
    # -----------------------
    def _notify_error(self, msg):
        self.status_msg = msg
        self.msg_timer = pygame.time.get_ticks()

    def _render_overlay_unmasked(self, surface):
        if self.status_msg and pygame.time.get_ticks() - self.msg_timer < 1400:
            msg = self.font_ui.render(self.status_msg, True, GRAY)
            surface.blit(msg, (surface.get_width() // 2 - msg.get_width() // 2, 8))

        if pygame.key.get_mods() & pygame.KMOD_CAPS:
            caps = self.font_ui.render("CAPS", True, GRAY)
            surface.blit(caps, (surface.get_width() - caps.get_width() - 10, 8))

        if self.font_adjust_mode and self.state == "WRITING":
            hint = self.font_ui.render(
                f"FONT {self.settings['font_size']}  (←/→)  CTRL+S save",
                True,
                GRAY,
            )
            surface.blit(
                hint,
                (
                    surface.get_width() // 2 - hint.get_width() // 2,
                    surface.get_height() - 18,
                ),
            )

        # NOTE: intentionally NO "ctrl-r for full refresh" text here.

    # -----------------------
    # Glyph cache
    # -----------------------
    def _rebuild_glyph_cache(self):
        self._glyph_surf.clear()
        self._glyph_w.clear()
        for code in range(32, 127):
            ch = chr(code)
            s = self.font_body.render(ch, True, BLACK)
            self._glyph_surf[ch] = s
            self._glyph_w[ch] = s.get_width()

        for ch in ["’", "“", "”", "—", "–"]:
            try:
                s = self.font_body.render(ch, True, BLACK)
                self._glyph_surf[ch] = s
                self._glyph_w[ch] = s.get_width()
            except Exception:
                pass

    def _glyph(self, ch):
        s = self._glyph_surf.get(ch)
        if s is None:
            s = self.font_body.render(ch, True, BLACK)
            self._glyph_surf[ch] = s
            self._glyph_w[ch] = s.get_width()
        return s

    def _glyph_width(self, ch):
        w = self._glyph_w.get(ch)
        if w is None:
            w = self._glyph(ch).get_width()
        return w

    # -----------------------
    # Filename helpers
    # -----------------------
    def _normalize_display_name(self, s):
        s = s.strip()
        s = re.sub(r"\s+", " ", s)
        return s

    def _make_safe_filename_base(self, display_name):
        name = self._normalize_display_name(display_name)
        name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", name)
        name = name.strip().strip(".")
        if not name:
            name = "untitled"
        return name[:60].strip()

    def _unique_filename(self, base_name):
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

    # -----------------------
    # Saving / autosave
    # -----------------------
    def _write_to_file(self, filename):
        path = os.path.join(CAPSULE_FOLDER, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.notes))

    def _remove_file_if_exists(self, filename):
        if not filename:
            return
        path = os.path.join(CAPSULE_FOLDER, filename)
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

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
                        return

                self._ensure_temp_filename()
                self._write_to_file(self.temp_filename)
                self.dirty = False
                return

            self._write_to_file(self.current_filename)
            self.dirty = False

        except Exception:
            self._notify_error("autosave failed." if autosave else "save failed.")

    def _mark_edited(self):
        self.dirty = True
        self.last_edit_time = pygame.time.get_ticks()
        self.scroll_lines = 0

    def _maybe_autosave(self):
        if self.state != "WRITING":
            return
        if not self.dirty:
            return
        now = pygame.time.get_ticks()
        if now - self.last_edit_time >= self.autosave_delay:
            self.save_capsule(autosave=True)

    # -----------------------
    # Editing (backspace)
    # -----------------------
    def _delete_char(self):
        if self.notes[-1]:
            self.notes[-1] = self.notes[-1][:-1]
            self._mark_edited()
        elif len(self.notes) > 1:
            self.notes.pop()
            self._mark_edited()

    # -----------------------
    # Typewriter selection / overwrite
    # -----------------------
    def _tw_clear_selection(self):
        self.tw_sel_line = None
        self.tw_sel_char = None

    def _tw_has_selection(self):
        return self.tw_sel_line is not None and self.tw_sel_char is not None

    def _tw_move_left(self):
        if not self._tw_has_selection():
            li = len(self.notes) - 1
            while li >= 0:
                if self.notes[li]:
                    self.tw_sel_line = li
                    self.tw_sel_char = len(self.notes[li]) - 1
                    return
                li -= 1
            return

        li = self.tw_sel_line
        ci = self.tw_sel_char
        if li is None or ci is None:
            return

        if ci > 0:
            self.tw_sel_char = ci - 1
            return

        li -= 1
        while li >= 0:
            if self.notes[li]:
                self.tw_sel_line = li
                self.tw_sel_char = len(self.notes[li]) - 1
                return
            li -= 1

        self.tw_sel_line = 0
        self.tw_sel_char = 0

    def _tw_move_right(self):
        if not self._tw_has_selection():
            return

        li = self.tw_sel_line
        ci = self.tw_sel_char
        if li is None or ci is None:
            return

        if ci < len(self.notes[li]) - 1:
            self.tw_sel_char = ci + 1
            return

        li += 1
        while li < len(self.notes):
            if self.notes[li]:
                self.tw_sel_line = li
                self.tw_sel_char = 0
                return
            li += 1

        self._tw_clear_selection()

    # -----------------------
    # Text helpers / settings logic
    # -----------------------
    def _get_last_non_space_char(self):
        for li in range(len(self.notes) - 1, -1, -1):
            s = self.notes[li]
            for ci in range(len(s) - 1, -1, -1):
                if not s[ci].isspace():
                    return s[ci]
        return None

    def _get_prev_non_space_char_before(self, line_index, char_index):
        li = line_index
        if li is None:
            return None
        li = max(0, min(li, len(self.notes) - 1))

        ci = char_index - 1
        while li >= 0:
            s = self.notes[li]
            if ci >= len(s):
                ci = len(s) - 1
            while ci >= 0:
                if not s[ci].isspace():
                    return s[ci]
                ci -= 1
            li -= 1
            if li >= 0:
                ci = len(self.notes[li]) - 1
        return None

    def _should_autocapitalize_next_letter(self):
        if not self.settings["autocap_after_period"]:
            return False
        return self._get_last_non_space_char() == "."

    def _should_autocapitalize_next_letter_at(self, line_index, char_index):
        if not self.settings["autocap_after_period"]:
            return False
        prev = self._get_prev_non_space_char_before(line_index, char_index)
        return prev == "."

    def _apply_auto_i_on_boundary_at(self, line_index, boundary_pos):
        if not self.settings["auto_capitalize_i"]:
            return
        if line_index is None or line_index < 0 or line_index >= len(self.notes):
            return

        line = self.notes[line_index]
        if not line:
            return

        if boundary_pos is None:
            return
        if boundary_pos < 0:
            return
        if boundary_pos >= len(line):
            boundary_pos = len(line) - 1

        i = boundary_pos - 1
        if i < 0:
            return

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

        self.notes[line_index] = line[:start] + "I" + line[end + 1 :]

    def _apply_auto_i_on_boundary(self):
        if not self.settings["auto_capitalize_i"]:
            return
        li = len(self.notes) - 1
        line = self.notes[li]
        if not line:
            return
        self._apply_auto_i_on_boundary_at(li, len(line) - 1)

    def _insert_char(self, ch):
        if ch.isalpha() and self._should_autocapitalize_next_letter():
            ch = ch.upper()

        self.notes[-1] += ch
        self._mark_edited()

        if ch.isspace() or ch in '.,!?;:"()[]{}':
            self._apply_auto_i_on_boundary()

    def _tw_overwrite_selected(self, ch):
        if not self._tw_has_selection():
            li = len(self.notes) - 1
            ci = len(self.notes[li])
            out_ch = ch
            if out_ch.isalpha() and self._should_autocapitalize_next_letter_at(li, ci):
                out_ch = out_ch.upper()

            self.notes[li] += out_ch
            self._mark_edited()

            if out_ch.isspace() or out_ch in '.,!?;:"()[]{}':
                self._apply_auto_i_on_boundary_at(li, len(self.notes[li]) - 1)
            return

        li = self.tw_sel_line
        ci = self.tw_sel_char
        if li is None or ci is None:
            return

        if li < 0 or li >= len(self.notes):
            self._tw_clear_selection()
            return

        line = self.notes[li]
        if ci < 0 or ci >= len(line):
            self._tw_clear_selection()
            return

        out_ch = ch
        if out_ch.isalpha() and self._should_autocapitalize_next_letter_at(li, ci):
            out_ch = out_ch.upper()

        self.notes[li] = line[:ci] + out_ch + line[ci + 1 :]
        self._mark_edited()

        if out_ch.isspace() or out_ch in '.,!?;:"()[]{}':
            self._apply_auto_i_on_boundary_at(li, ci)

        self._tw_move_right()

    # -----------------------
    # Wrapping + tracked rendering
    # -----------------------
    def _text_width(self, s):
        tracking = int(self.settings.get("tracking", 0))
        if not s:
            return 0
        w = 0
        for ch in s:
            w += self._glyph_width(ch)
        w += tracking * max(0, len(s) - 1)
        return w

    def _wrap_lines_with_map(self, wrap_px):
        wrapped = []
        mapping = []

        for src_i, p in enumerate(self.notes):
            if p == "":
                wrapped.append("")
                mapping.append((src_i, 0, 0))
                continue

            tokens = p.split(" ")
            pos = 0
            line = ""
            line_start = 0

            for ti, tok in enumerate(tokens):
                sep = " " if ti < len(tokens) - 1 else ""
                tok_with_sep = tok + sep
                cand = tok_with_sep if not line else (line + tok_with_sep)

                if self._text_width(cand.rstrip()) < wrap_px:
                    if not line:
                        line_start = pos
                    line = cand
                else:
                    flush = line.rstrip()
                    wrapped.append(flush)
                    mapping.append((src_i, line_start, line_start + len(flush)))

                    line = tok_with_sep
                    line_start = pos

                pos += len(tok) + (1 if ti < len(tokens) - 1 else 0)

            flush = line.rstrip()
            wrapped.append(flush)
            mapping.append((src_i, line_start, line_start + len(flush)))

        return wrapped, mapping

    def _render_tracked_line_centered(self, surface, center_x, y, text, color):
        tracking = int(self.settings.get("tracking", 0))
        if text is None:
            text = ""
        if not text:
            return

        total_w = self._text_width(text)
        x = center_x - (total_w // 2)

        for idx, ch in enumerate(text):
            glyph = (
                self._glyph(ch)
                if color == BLACK
                else self.font_body.render(ch, True, color)
            )
            surface.blit(glyph, (x, y))
            x += glyph.get_width()
            if idx != len(text) - 1:
                x += tracking

    def _tracked_char_rect_centered(self, center_x, y, text, char_index):
        if text is None:
            return None
        if char_index < 0 or char_index >= len(text):
            return None

        tracking = int(self.settings.get("tracking", 0))
        total_w = self._text_width(text)
        x = center_x - (total_w // 2)

        for idx, ch in enumerate(text):
            gw = self._glyph_width(ch)
            if idx == char_index:
                return pygame.Rect(x, y, gw, self.font_body.get_height())
            x += gw
            if idx != len(text) - 1:
                x += tracking
        return None

    def _draw_corner_highlight(self, surface, rect):
        if rect is None:
            return
        x, y, w, h = rect.x, rect.y, rect.w, rect.h
        l = 4
        c = BLACK

        pygame.draw.line(surface, c, (x, y), (x + l, y), 1)
        pygame.draw.line(surface, c, (x, y), (x, y + l), 1)
        pygame.draw.line(surface, c, (x + w - l, y), (x + w, y), 1)
        pygame.draw.line(surface, c, (x + w, y), (x + w, y + l), 1)
        pygame.draw.line(surface, c, (x, y + h), (x + l, y + h), 1)
        pygame.draw.line(surface, c, (x, y + h - l), (x, y + h), 1)
        pygame.draw.line(surface, c, (x + w - l, y + h), (x + w, y + h), 1)
        pygame.draw.line(surface, c, (x + w, y + h - l), (x + w, y + h), 1)

    def _maybe_draw_typewriter_selection(
        self, surface, center_x, y, line_text, src_map
    ):
        if not self.settings["typewriter_mode"]:
            return
        if not self._tw_has_selection():
            return
        if not line_text:
            return

        src_line, start, end = src_map
        if src_line != self.tw_sel_line:
            return
        if self.tw_sel_char is None:
            return
        if not (start <= self.tw_sel_char < end):
            return

        local_index = self.tw_sel_char - start
        rect = self._tracked_char_rect_centered(center_x, y, line_text, local_index)
        self._draw_corner_highlight(surface, rect)

    # -----------------------
    # Input
    # -----------------------
    def handle_input(self):
        now = pygame.time.get_ticks()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                raise SystemExit

            if event.type == pygame.KEYDOWN:
                mods = pygame.key.get_mods()

                # HOTKEY: CTRL+R forces full refresh on next sent frame (no on-screen text)
                if event.key == pygame.K_r and (mods & pygame.KMOD_CTRL):
                    self.force_full_next = True
                    return

                # CTRL+Q
                if event.key == pygame.K_q and (mods & pygame.KMOD_CTRL):
                    self.state = "MENU"
                    self.font_adjust_mode = False
                    self._tw_clear_selection()
                    return

                # CTRL+F
                if event.key == pygame.K_f and (mods & pygame.KMOD_CTRL):
                    if self.state == "WRITING":
                        self.font_adjust_mode = not self.font_adjust_mode
                    return

                # CTRL+S
                if event.key == pygame.K_s and (mods & pygame.KMOD_CTRL):
                    if self.state == "WRITING":
                        if self.font_adjust_mode:
                            self._save_settings()
                            self.font_adjust_mode = False
                        else:
                            first4 = self._first_n_words_display(4)
                            has_real_name = (
                                self.current_filename is not None
                                and not self.current_filename.startswith("capsule_")
                            )
                            if (
                                first4 is None
                                and not has_real_name
                                and not self.auto_named
                            ):
                                self._start_naming_prompt()
                            else:
                                self.save_capsule(autosave=False)
                    return

                if self.state == "MENU":
                    self._nav_menu(event.key)
                elif self.state == "BROWSER":
                    self._nav_browser(event.key)
                elif self.state == "SETTINGS":
                    self._nav_settings(event.key)
                elif self.state == "NAMING":
                    self._nav_naming(event)
                elif self.state == "WRITING":
                    if self.font_adjust_mode:
                        if event.key == pygame.K_ESCAPE:
                            self.font_adjust_mode = False
                            return
                        if event.key == pygame.K_LEFT:
                            self.settings["font_size"] = max(
                                self.font_adjust_min,
                                int(self.settings["font_size"]) - 1,
                            )
                            self._apply_font_settings()
                            return
                        if event.key == pygame.K_RIGHT:
                            self.settings["font_size"] = min(
                                self.font_adjust_max,
                                int(self.settings["font_size"]) + 1,
                            )
                            self._apply_font_settings()
                            return
                        if event.key in (pygame.K_UP, pygame.K_DOWN):
                            self._nav_writing(event)
                            return
                        return

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
            and (not self.font_adjust_mode)
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
    # Navigation / Menus
    # -----------------------
    def _nav_menu(self, key):
        if key == pygame.K_UP:
            self.selected_index = (self.selected_index - 1) % len(self.menu_options)
        elif key == pygame.K_DOWN:
            self.selected_index = (self.selected_index + 1) % len(self.menu_options)
        elif key == pygame.K_RETURN:
            choice = self.menu_options[self.selected_index]
            if choice == "exit":
                pygame.quit()
                raise SystemExit
            elif choice == "write":
                self.notes = [""]
                self.current_filename = None
                self.temp_filename = None
                self.auto_named = False
                self.dirty = False
                self.last_edit_time = 0
                self.scroll_lines = 0
                self.font_adjust_mode = False
                self._tw_clear_selection()
                self.state = "WRITING"
            elif choice == "open":
                self._refresh_capsule_list()
                self.browser_index = 0
                self.browser_scroll = 0
                self.state = "BROWSER"
                self._ensure_browser_index_visible()
            elif choice == "settings":
                self.settings_index = 0
                self.state = "SETTINGS"

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

    def _browser_visible_count(self):
        usable = EYE_HEIGHT - self.browser_top_y - self.browser_bottom_pad
        return max(1, usable // self.browser_row_h)

    def _ensure_browser_index_visible(self):
        if not self.capsule_files:
            self.browser_scroll = 0
            return
        vis = self._browser_visible_count()
        max_scroll = max(0, len(self.capsule_files) - vis)
        if self.browser_index < self.browser_scroll:
            self.browser_scroll = self.browser_index
        elif self.browser_index >= self.browser_scroll + vis:
            self.browser_scroll = self.browser_index - vis + 1
        self.browser_scroll = max(0, min(max_scroll, self.browser_scroll))

    def _nav_browser(self, key):
        if key == pygame.K_ESCAPE:
            self.state = "MENU"
            return
        if not self.capsule_files:
            return

        if key == pygame.K_UP:
            self.browser_index = (self.browser_index - 1) % len(self.capsule_files)
            self._ensure_browser_index_visible()
        elif key == pygame.K_DOWN:
            self.browser_index = (self.browser_index + 1) % len(self.capsule_files)
            self._ensure_browser_index_visible()
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
                self.font_adjust_mode = False
                self._tw_clear_selection()
            except Exception:
                self._notify_error("open failed.")

    def _nav_settings(self, key):
        if key == pygame.K_ESCAPE:
            self.state = "MENU"
            return

        if key == pygame.K_UP:
            self.settings_index = (self.settings_index - 1) % len(self.settings_items)
        elif key == pygame.K_DOWN:
            self.settings_index = (self.settings_index + 1) % len(self.settings_items)
        elif key in (pygame.K_RETURN, pygame.K_LEFT, pygame.K_RIGHT):
            _, setting_key = self.settings_items[self.settings_index]
            if setting_key is None:
                self.state = "MENU"
                return
            self.settings[setting_key] = not self.settings[setting_key]
            self._save_settings()
            if setting_key == "typewriter_mode":
                self._tw_clear_selection()

    # -----------------------
    # Naming flow
    # -----------------------
    def _start_naming_prompt(self):
        self.name_buffer = ""
        self.name_error = ""
        self.state = "NAMING"

    def _commit_named_save(self):
        name = self._normalize_display_name(self.name_buffer)
        if not name:
            self.name_error = "enter a name"
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
        except Exception:
            self._notify_error("save failed.")

    def _nav_naming(self, event):
        if event.key == pygame.K_ESCAPE:
            self.state = "WRITING"
            self.name_error = ""
            return

        if event.key == pygame.K_RETURN:
            self._commit_named_save()
            return

        if event.key == pygame.K_BACKSPACE:
            if self.name_buffer:
                self.name_buffer = self.name_buffer[:-1]
            return

        if event.unicode and event.unicode.isprintable():
            if len(self.name_buffer) < 60:
                self.name_buffer += event.unicode

    # -----------------------
    # Writing navigation
    # -----------------------
    def _nav_writing(self, event):
        if event.key == pygame.K_ESCAPE:
            self.state = "MENU"
            self.font_adjust_mode = False
            self._tw_clear_selection()
            return

        if event.key == pygame.K_UP:
            wrap_px = (
                self.hud_panel_rect.width - 50 if self.settings["room_mode"] else 220
            )
            total_lines = len(self._wrap_lines_with_map(wrap_px)[0])
            max_scroll = max(0, total_lines - 1)
            self.scroll_lines = min(max_scroll, self.scroll_lines + 1)
            return

        if event.key == pygame.K_DOWN:
            self.scroll_lines = max(0, self.scroll_lines - 1)
            return

        if self.settings["typewriter_mode"]:
            if event.key == pygame.K_LEFT:
                self._tw_move_left()
                return
            if event.key == pygame.K_RIGHT:
                self._tw_move_right()
                return
            if event.key == pygame.K_BACKSPACE:
                return
            if event.key == pygame.K_RETURN:
                self.notes.append("")
                self._tw_clear_selection()
                self._mark_edited()
                return
            if event.unicode and event.unicode.isprintable():
                self._tw_overwrite_selected(event.unicode)
                return
            return

        if event.key == pygame.K_BACKSPACE:
            return

        if event.key == pygame.K_RETURN:
            self.notes.append("")
            self._mark_edited()
            return

        if event.unicode and event.unicode.isprintable():
            self._insert_char(event.unicode)

    # -----------------------
    # Render base + overlay
    # -----------------------
    def _render_base(self, surface):
        # IMPORTANT: always white background (no gradients / vignettes / room bg)
        surface.fill(WHITE)

        if self.state == "MENU":
            cx, cy = EYE_WIDTH // 2, EYE_HEIGHT // 2
            for i, opt in enumerate(self.menu_options):
                color = BLACK if i == self.selected_index else GRAY
                txt = self.font_header.render(opt, True, color)
                surface.blit(txt, (cx - txt.get_width() // 2, cy - 55 + i * 35))
            return

        if self.state == "BROWSER":
            cx, cy = EYE_WIDTH // 2, EYE_HEIGHT // 2
            if not self.capsule_files:
                txt = self.font_body.render("no capsules", True, GRAY)
                surface.blit(txt, (cx - txt.get_width() // 2, cy))
            else:
                vis = self._browser_visible_count()
                start = max(
                    0, min(self.browser_scroll, max(0, len(self.capsule_files) - vis))
                )
                end = min(len(self.capsule_files), start + vis)

                y0 = self.browser_top_y
                for row, i in enumerate(range(start, end)):
                    f = self.capsule_files[i]
                    display = self._display_name_from_filename(f)
                    color = BLACK if i == self.browser_index else GRAY
                    txt = self.font_body.render(display, True, color)
                    surface.blit(
                        txt, (cx - txt.get_width() // 2, y0 + row * self.browser_row_h)
                    )
            return

        if self.state == "SETTINGS":
            cx = EYE_WIDTH // 2
            title = self.font_header.render("settings", True, BLACK)
            surface.blit(title, (cx - title.get_width() // 2, 35))
            y = 75
            for i, (label, key) in enumerate(self.settings_items):
                selected = i == self.settings_index
                color = BLACK if selected else GRAY
                if key is None:
                    line = label
                else:
                    val = "on" if self.settings.get(key, False) else "off"
                    line = f"{label}: {val}"
                txt = self.font_body.render(line, True, color)
                surface.blit(txt, (cx - txt.get_width() // 2, y))
                y += 24
            return

        if self.state == "NAMING":
            cx, cy = EYE_WIDTH // 2, EYE_HEIGHT // 2
            title = self.font_header.render("name document", True, BLACK)
            surface.blit(title, (cx - title.get_width() // 2, cy - 45))

            prompt = self.font_body.render("name:", True, GRAY)
            surface.blit(prompt, (cx - 120, cy - 10))

            name_txt = self.font_body.render(self.name_buffer or "_", True, BLACK)
            surface.blit(name_txt, (cx - 50, cy - 10))

            hint = self.font_body.render("enter=save  esc=cancel", True, GRAY)
            surface.blit(hint, (cx - hint.get_width() // 2, cy + 25))

            if self.name_error:
                err = self.font_body.render(self.name_error, True, GRAY)
                surface.blit(err, (cx - err.get_width() // 2, cy + 45))
            return

        if self.state == "WRITING":
            if self.settings["room_mode"]:
                # Keep HUD panel behavior (but background stays white)
                self.panel_shadow_surf.fill((0, 0, 0, 0))
                r = self.hud_panel_rect
                shadow = r.move(3, 6)
                pygame.draw.rect(
                    self.panel_shadow_surf, (0, 0, 0, 28), shadow, border_radius=6
                )
                surface.blit(self.panel_shadow_surf, (0, 0))

                pygame.draw.rect(surface, (250, 250, 250), r, border_radius=6)
                pygame.draw.rect(surface, (235, 235, 235), r, width=2, border_radius=6)

                panel_rect = r.inflate(-26, -26)
                prev_clip = surface.get_clip()
                surface.set_clip(panel_rect)

                cx, cy = panel_rect.centerx, panel_rect.centery
                wrap_px = max(120, panel_rect.width - 50)

                lines, maps = self._wrap_lines_with_map(wrap_px)
                if not lines:
                    lines, maps = [""], [(0, 0, 0)]

                bottom_index = max(0, len(lines) - 1 - self.scroll_lines)
                baseline_y = cy + self.typewriter_offset_y

                y = baseline_y
                for idx in range(bottom_index, -1, -1):
                    self._render_tracked_line_centered(
                        surface, cx, y, lines[idx], BLACK
                    )
                    self._maybe_draw_typewriter_selection(
                        surface, cx, y, lines[idx], maps[idx]
                    )
                    y -= self.line_height
                    if y < panel_rect.top + 6:
                        break

                surface.set_clip(prev_clip)
            else:
                cx, cy = EYE_WIDTH // 2, EYE_HEIGHT // 2
                wrap_px = 220
                lines, maps = self._wrap_lines_with_map(wrap_px)
                if not lines:
                    lines, maps = [""], [(0, 0, 0)]

                bottom_index = max(0, len(lines) - 1 - self.scroll_lines)
                baseline_y = cy + self.typewriter_offset_y

                y = baseline_y
                for idx in range(bottom_index, -1, -1):
                    self._render_tracked_line_centered(
                        surface, cx, y, lines[idx], BLACK
                    )
                    self._maybe_draw_typewriter_selection(
                        surface, cx, y, lines[idx], maps[idx]
                    )
                    y -= self.line_height
                    if y < 0:
                        break

    def draw_and_maybe_stream(self):
        # Draw one eye
        self._render_base(self.eye_surface)
        self._render_overlay_unmasked(self.eye_surface)

        # Duplicate to both halves (same as your original emulator)
        self.screen.fill(WHITE)
        self.screen.blit(self.eye_surface, (0, 0))
        self.screen.blit(self.eye_surface, (EYE_WIDTH, 0))
        pygame.display.flip()

        # Stream to e-ink if attached
        if self.streamer is not None:
            frame = pack_1bpp(self.screen, invert=self.stream_invert, mirror_x=True)
            force_full = self.force_full_next
            self.force_full_next = False
            self.streamer.maybe_send(frame, force_full=force_full)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--port",
        default=None,
        help="Serial port for Pico (optional). If omitted, runs pygame only.",
    )
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--invert", action="store_true")
    ap.add_argument("--ack-timeout", type=float, default=30.0)
    ap.add_argument(
        "--fps",
        type=float,
        default=60.0,
        help="Pygame loop FPS (not the e-ink refresh rate).",
    )
    args = ap.parse_args()

    streamer = None
    if args.port:
        streamer = EInkStreamer(args.port, baud=args.baud, ack_timeout=args.ack_timeout)

    try:
        app = MindPalaceEmulator(streamer=streamer, invert=args.invert)
        clock = pygame.time.Clock()
        while True:
            app.handle_input()
            app.draw_and_maybe_stream()
            clock.tick(args.fps)
    finally:
        if streamer is not None:
            streamer.close()


if __name__ == "__main__":
    main()
