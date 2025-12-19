import pygame
import pygame.gfxdraw
import sys
import os
import datetime
import math
import json
import re

# --- HARDWARE EXACT CONFIGURATION ---
# The window is EXACTLY the display resolution.
# Each eye gets exactly 396 x 272 (Total 792x272).
WINDOW_WIDTH = 792
WINDOW_HEIGHT = 272
EYE_WIDTH = WINDOW_WIDTH // 2
EYE_HEIGHT = WINDOW_HEIGHT

CAPSULE_FOLDER = "capsules"
if not os.path.exists(CAPSULE_FOLDER):
    os.makedirs(CAPSULE_FOLDER)

SETTINGS_FILE = os.path.join(CAPSULE_FOLDER, "settings.json")

# COLORS
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
GRAY = (220, 220, 220)


class MindPalaceEmulator:
    def __init__(self):
        pygame.init()

        self.screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT))
        pygame.display.set_caption("Mind Palace")

        self.eye_surface = pygame.Surface((EYE_WIDTH, EYE_HEIGHT), flags=0)

        # Font adjustment bounds MUST exist before any font sizing logic
        self.font_adjust_min = 10
        self.font_adjust_max = 24

        # Settings (persisted)
        self.settings = {
            "typewriter_mode": False,        # if on: no backspace editing
            "room_mode": False,              # if on: draw 3D room + floating panel
            "font_size": 14,                 # CTRL+F then ←/→ ; CTRL+S saves
            "tracking": 1,                   # typewriter-ish letter spacing
            "auto_capitalize_i": False,
            "always_double_quotes": False,
            "autocap_after_period": False,
        }
        self._load_settings()

        # Fonts / spacing
        self.font_body = None
        self.font_header = None
        self.font_ui = None  # fixed UI font (does NOT change with font_size)
        self.line_height = 20

        # Apply fonts from settings (safe now that min/max exist)
        self._apply_font_settings()

        # Precomputed vignette masks (generated once; runtime is just a blit)
        self.portal_mask = self._generate_smooth_vignette_mask(
            EYE_WIDTH, EYE_HEIGHT,
            clear_radius_ratio=0.38,
            fade_width_px=80,
            small_base=160
        )
        self.room_mask = self._generate_smooth_vignette_mask(
            EYE_WIDTH, EYE_HEIGHT,
            clear_radius_ratio=0.60,   # further out than portal mode
            fade_width_px=130,
            small_base=160
        )

        # Pre-rendered room background and precomputed panel polygon
        self.room_bg = pygame.Surface((EYE_WIDTH, EYE_HEIGHT), flags=0)
        self.room_panel_poly = None
        self.room_panel_rect = None
        self._build_room_static()

        # Shadow surface (alpha) for floating panel
        self.panel_shadow_surf = pygame.Surface((EYE_WIDTH, EYE_HEIGHT), pygame.SRCALPHA)

        # Typewriter layout baseline below center
        self.typewriter_offset_y = 18

        # State
        self.state = "MENU"
        self.menu_options = ["write", "open", "settings", "exit"]
        self.selected_index = 0

        self.current_filename = None
        self.temp_filename = None
        self.auto_named = False

        self.capsule_files = []
        self.browser_index = 0

        self.notes = [""]
        self.status_msg = ""   # errors only
        self.msg_timer = 0

        # Autosave-on-pause
        self.autosave_delay = 900
        self.last_edit_time = 0
        self.dirty = False

        self.scroll_lines = 0

        self.settings_items = [
            ("Typewriter mode", "typewriter_mode"),
            ("Room", "room_mode"),
            ("Auto capitalize \"i\"", "auto_capitalize_i"),
            ("Always use double quotes", "always_double_quotes"),
            ("Autocapitalize after a .", "autocap_after_period"),
            ("Back", None),
        ]
        self.settings_index = 0

        # Backspace repeat
        self.backspace_down = False
        self.backspace_hold_start = 0
        self.backspace_next_repeat = 0
        self.backspace_initial_delay = 260
        self.backspace_repeat_interval = 32
        self.backspace_fast_after = 900
        self.backspace_fast_interval = 22

        # Naming prompt
        self.name_buffer = ""
        self.name_error = ""

        # Font adjustment mode
        self.font_adjust_mode = False

        # Glyph cache for performance/stability
        self._glyph_surf = {}
        self._glyph_w = {}
        self._rebuild_glyph_cache()

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
            pygame.font.match_font("couriernew") or
            pygame.font.match_font("courier") or
            pygame.font.match_font("consolas") or
            pygame.font.match_font("menlo") or
            pygame.font.match_font("monospace") or
            pygame.font.match_font("arial")
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

        # UI stays fixed size and does NOT change when font_size changes
        self.font_ui = pygame.font.Font(mono, 12)

        self.line_height = int(body_size * 1.25) + 6

        # Rebuild glyph cache when font changes
        if hasattr(self, "_glyph_surf"):
            self._rebuild_glyph_cache()

    # -----------------------
    # Fast vignette masks (generated once; small -> smoothscale)
    # -----------------------
    def _generate_smooth_vignette_mask(self, w, h, clear_radius_ratio, fade_width_px, small_base=160):
        sw = small_base
        sh = max(1, int(small_base * (h / float(w))))
        small = pygame.Surface((sw, sh), pygame.SRCALPHA)

        cx, cy = (sw - 1) / 2.0, (sh - 1) / 2.0
        clear_radius = (min(sw, sh) * clear_radius_ratio)

        scale = sw / float(w)
        fade_width = max(6.0, fade_width_px * scale)
        outer_radius = clear_radius + fade_width

        for y in range(sh):
            dy = y - cy
            for x in range(sw):
                dx = x - cx
                d = math.hypot(dx, dy)

                if d <= clear_radius:
                    a = 0
                elif d >= outer_radius:
                    a = 255
                else:
                    t = (d - clear_radius) / fade_width
                    s = t * t * (3.0 - 2.0 * t)  # smoothstep
                    a = int(255 * s)

                small.set_at((x, y), (0, 0, 0, a))

        return pygame.transform.smoothscale(small, (w, h))

    # -----------------------
    # Room: pre-rendered 3D box + floating panel
    # Uses pygame.draw.aaline (gfxdraw.aaline doesn't exist in your build)
    # -----------------------
    def _project(self, x, y, z, cx, cy, f):
        if z <= 0.05:
            z = 0.05
        sx = cx + (x * f / z)
        sy = cy + (y * f / z)
        return (int(round(sx)), int(round(sy)))

    def _draw_closed_aapoly(self, surf, pts, color):
        # aapolygon exists; draw polygon outline too for crisp corner connections
        pygame.gfxdraw.aapolygon(surf, pts, color)
        pygame.draw.polygon(surf, color, pts, width=1)

    def _draw_aaline(self, surf, p1, p2, color):
        # Use pygame.draw.aaline (available in pygame 2.6.1)
        pygame.draw.aaline(surf, color, p1, p2)
        pygame.draw.line(surf, color, p1, p2, 1)

    def _build_room_static(self):
        w, h = EYE_WIDTH, EYE_HEIGHT
        surf = self.room_bg
        surf.fill(WHITE)

        cx, cy = w // 2, h // 2
        f = 260

        room_w = 2.2
        room_h = 1.65
        room_d = 4.2

        z_near = 1.6
        z_far = z_near + room_d

        nlt = (-room_w,  room_h, z_near)
        nrt = ( room_w,  room_h, z_near)
        nrb = ( room_w, -room_h, z_near)
        nlb = (-room_w, -room_h, z_near)

        flt = (-room_w,  room_h, z_far)
        frt = ( room_w,  room_h, z_far)
        frb = ( room_w, -room_h, z_far)
        flb = (-room_w, -room_h, z_far)

        P = {}
        for name, v in zip(
            ["nlt", "nrt", "nrb", "nlb", "flt", "frt", "frb", "flb"],
            [nlt,  nrt,  nrb,  nlb,  flt,  frt,  frb,  flb]
        ):
            P[name] = self._project(v[0], v[1], v[2], cx, cy, f)

        # Faces
        pygame.draw.polygon(surf, (252, 252, 252), [P["flt"], P["frt"], P["frb"], P["flb"]])  # back
        pygame.draw.polygon(surf, (248, 248, 248), [P["nlb"], P["nrb"], P["frb"], P["flb"]])  # floor
        pygame.draw.polygon(surf, (251, 251, 251), [P["nlt"], P["nrt"], P["frt"], P["flt"]])  # ceiling
        pygame.draw.polygon(surf, (249, 249, 249), [P["nlt"], P["nlb"], P["flb"], P["flt"]])  # left
        pygame.draw.polygon(surf, (249, 249, 249), [P["nrt"], P["nrb"], P["frb"], P["frt"]])  # right

        edge = (238, 238, 238)

        near_poly = [P["nlt"], P["nrt"], P["nrb"], P["nlb"]]
        far_poly = [P["flt"], P["frt"], P["frb"], P["flb"]]

        self._draw_closed_aapoly(surf, near_poly, edge)
        self._draw_closed_aapoly(surf, far_poly, edge)

        self._draw_aaline(surf, P["nlt"], P["flt"], edge)
        self._draw_aaline(surf, P["nrt"], P["frt"], edge)
        self._draw_aaline(surf, P["nrb"], P["frb"], edge)
        self._draw_aaline(surf, P["nlb"], P["flb"], edge)

        # Floating panel straight-on (no slant)
        panel_z = 2.1
        panel_w = 1.45
        panel_h = 0.90
        panel_center_y = -0.18

        corners = [
            (-panel_w,  panel_h, panel_z),
            ( panel_w,  panel_h, panel_z),
            ( panel_w, -panel_h, panel_z),
            (-panel_w, -panel_h, panel_z),
        ]

        poly = []
        for x, y, z in corners:
            poly.append(self._project(x, y + panel_center_y, z, cx, cy, f))

        self.room_panel_poly = poly
        self.room_panel_rect = pygame.Rect(
            min(p[0] for p in poly),
            min(p[1] for p in poly),
            max(p[0] for p in poly) - min(p[0] for p in poly),
            max(p[1] for p in poly) - min(p[1] for p in poly),
        )

    # -----------------------
    # Overlay (unmasked): errors, CAPS, font-adjust hint only
    # NOTE: No "ROOM" label anywhere.
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
            hint = self.font_ui.render(f"FONT {self.settings['font_size']}  (←/→)  CTRL+S save", True, GRAY)
            surface.blit(hint, (surface.get_width() // 2 - hint.get_width() // 2, surface.get_height() - 18))

    # -----------------------
    # Glyph cache (performance + stable)
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
            cleaned = tok.strip(" \t\r\n.,!?;:\"()[]{}")
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
    # Editing (backspace) — only in non-typewriter mode
    # -----------------------
    def _delete_char(self):
        if self.notes[-1]:
            self.notes[-1] = self.notes[-1][:-1]
            self._mark_edited()
        elif len(self.notes) > 1:
            self.notes.pop()
            self._mark_edited()

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

    def _should_autocapitalize_next_letter(self):
        if not self.settings["autocap_after_period"]:
            return False
        return self._get_last_non_space_char() == "."

    def _apply_auto_i_on_boundary(self):
        if not self.settings["auto_capitalize_i"]:
            return
        line = self.notes[-1]
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

        word = line[start:end + 1]
        if word == "i":
            self.notes[-1] = line[:start] + "I" + line[end + 1:]

    def _insert_char(self, ch):
        if self.settings["always_double_quotes"] and ch == "'":
            ch = '"'

        if ch.isalpha() and self._should_autocapitalize_next_letter():
            ch = ch.upper()

        self.notes[-1] += ch
        self._mark_edited()

        if ch.isspace() or ch in '.,!?;:"()[]{}':
            self._apply_auto_i_on_boundary()

    # -----------------------
    # Open menu helpers
    # -----------------------
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

    # -----------------------
    # Naming flow (CTRL+S with <4 words)
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

    def _wrap_lines(self, wrap_px):
        wrapped = []
        for p in self.notes:
            if p == "":
                wrapped.append("")
                continue

            words = p.split(" ")
            line = ""
            for w in words:
                cand = (w + " ") if not line else (line + w + " ")
                if self._text_width(cand.rstrip()) < wrap_px:
                    line = cand
                else:
                    wrapped.append(line.rstrip())
                    line = w + " "
            wrapped.append(line.rstrip())
        return wrapped

    def _render_tracked_line_centered(self, surface, center_x, y, text, color):
        tracking = int(self.settings.get("tracking", 0))
        if text is None:
            text = ""
        if not text:
            return

        total_w = self._text_width(text)
        x = center_x - (total_w // 2)

        for idx, ch in enumerate(text):
            glyph = self._glyph(ch) if color == BLACK else self.font_body.render(ch, True, color)
            surface.blit(glyph, (x, y))
            x += glyph.get_width()
            if idx != len(text) - 1:
                x += tracking

    # -----------------------
    # Input
    # -----------------------
    def handle_input(self):
        now = pygame.time.get_ticks()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit()

            if event.type == pygame.KEYDOWN:
                mods = pygame.key.get_mods()

                # CTRL+Q
                if event.key == pygame.K_q and (mods & pygame.KMOD_CTRL):
                    self.state = "MENU"
                    self.font_adjust_mode = False
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
                            has_real_name = self.current_filename is not None and not self.current_filename.startswith("capsule_")
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
                    self._nav_settings(event.key)
                elif self.state == "NAMING":
                    self._nav_naming(event)
                elif self.state == "WRITING":
                    if self.font_adjust_mode:
                        if event.key == pygame.K_ESCAPE:
                            self.font_adjust_mode = False
                            return
                        if event.key == pygame.K_LEFT:
                            self.settings["font_size"] = max(self.font_adjust_min, int(self.settings["font_size"]) - 1)
                            self._apply_font_settings()
                            return
                        if event.key == pygame.K_RIGHT:
                            self.settings["font_size"] = min(self.font_adjust_max, int(self.settings["font_size"]) + 1)
                            self._apply_font_settings()
                            return
                        if event.key in (pygame.K_UP, pygame.K_DOWN):
                            self._nav_writing(event)
                            return
                        return

                    if (not self.settings["typewriter_mode"]) and event.key == pygame.K_BACKSPACE:
                        self.backspace_down = True
                        self.backspace_hold_start = now
                        self._delete_char()
                        self.backspace_next_repeat = now + self.backspace_initial_delay
                    else:
                        self._nav_writing(event)

            if event.type == pygame.KEYUP:
                if self.state == "WRITING" and (not self.settings["typewriter_mode"]) and event.key == pygame.K_BACKSPACE:
                    self.backspace_down = False

        if self.state == "WRITING" and (not self.settings["typewriter_mode"]) and (not self.font_adjust_mode) and self.backspace_down:
            if now >= self.backspace_next_repeat:
                held_for = now - self.backspace_hold_start
                interval = self.backspace_fast_interval if held_for >= self.backspace_fast_after else self.backspace_repeat_interval
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
                sys.exit()
            elif choice == "write":
                self.notes = [""]
                self.current_filename = None
                self.temp_filename = None
                self.auto_named = False
                self.dirty = False
                self.last_edit_time = 0
                self.scroll_lines = 0
                self.font_adjust_mode = False
                self.state = "WRITING"
            elif choice == "open":
                self._refresh_capsule_list()
                self.browser_index = 0
                self.state = "BROWSER"
            elif choice == "settings":
                self.settings_index = 0
                self.state = "SETTINGS"

    def _nav_browser(self, key):
        if key == pygame.K_ESCAPE:
            self.state = "MENU"
            return
        if not self.capsule_files:
            return
        if key == pygame.K_UP:
            self.browser_index = (self.browser_index - 1) % len(self.capsule_files)
        elif key == pygame.K_DOWN:
            self.browser_index = (self.browser_index + 1) % len(self.capsule_files)
        elif key == pygame.K_RETURN:
            f = self.capsule_files[self.browser_index]
            try:
                with open(os.path.join(CAPSULE_FOLDER, f), "r", encoding="utf-8") as file:
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

    def _nav_writing(self, event):
        if event.key == pygame.K_ESCAPE:
            self.state = "MENU"
            self.font_adjust_mode = False
            return

        if event.key == pygame.K_UP:
            wrap_px = (self.room_panel_rect.width - 60) if (self.settings["room_mode"] and self.room_panel_rect) else 220
            total_lines = len(self._wrap_lines(wrap_px))
            max_scroll = max(0, total_lines - 1)
            self.scroll_lines = min(max_scroll, self.scroll_lines + 1)
            return

        if event.key == pygame.K_DOWN:
            self.scroll_lines = max(0, self.scroll_lines - 1)
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
    # Render base (masked) + overlay (unmasked)
    # -----------------------
    def _render_base(self, surface):
        if self.settings["room_mode"]:
            surface.blit(self.room_bg, (0, 0))
        else:
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
                for i, f in enumerate(self.capsule_files):
                    display = self._display_name_from_filename(f)
                    color = BLACK if i == self.browser_index else GRAY
                    txt = self.font_body.render(display, True, color)
                    surface.blit(txt, (cx - txt.get_width() // 2, 50 + i * 25))
            return

        if self.state == "SETTINGS":
            cx = EYE_WIDTH // 2
            title = self.font_header.render("settings", True, BLACK)
            surface.blit(title, (cx - title.get_width() // 2, 35))
            y = 75
            for i, (label, key) in enumerate(self.settings_items):
                selected = (i == self.settings_index)
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

        # WRITING
        if self.state == "WRITING":
            if self.settings["room_mode"]:
                # Alpha shadow
                self.panel_shadow_surf.fill((0, 0, 0, 0))
                shadow_poly = [(x + 3, y + 6) for (x, y) in self.room_panel_poly]
                pygame.gfxdraw.filled_polygon(self.panel_shadow_surf, shadow_poly, (0, 0, 0, 26))
                pygame.gfxdraw.aapolygon(self.panel_shadow_surf, shadow_poly, (0, 0, 0, 26))
                surface.blit(self.panel_shadow_surf, (0, 0))

                # Panel quad
                pygame.gfxdraw.filled_polygon(surface, self.room_panel_poly, (250, 250, 250))
                pygame.gfxdraw.aapolygon(surface, self.room_panel_poly, (235, 235, 235))
                pygame.draw.polygon(surface, (235, 235, 235), self.room_panel_poly, width=2)

                panel_rect = self.room_panel_rect.inflate(-26, -26)
                prev_clip = surface.get_clip()
                surface.set_clip(panel_rect)

                cx, cy = panel_rect.centerx, panel_rect.centery
                wrap_px = max(120, panel_rect.width - 50)

                lines = self._wrap_lines(wrap_px)
                if not lines:
                    lines = [""]

                bottom_index = max(0, len(lines) - 1 - self.scroll_lines)
                baseline_y = cy + self.typewriter_offset_y

                y = baseline_y
                for idx in range(bottom_index, -1, -1):
                    self._render_tracked_line_centered(surface, cx, y, lines[idx], BLACK)
                    y -= self.line_height
                    if y < panel_rect.top + 6:
                        break

                surface.set_clip(prev_clip)
            else:
                cx, cy = EYE_WIDTH // 2, EYE_HEIGHT // 2
                wrap_px = 220
                lines = self._wrap_lines(wrap_px)
                if not lines:
                    lines = [""]

                bottom_index = max(0, len(lines) - 1 - self.scroll_lines)
                baseline_y = cy + self.typewriter_offset_y

                y = baseline_y
                for idx in range(bottom_index, -1, -1):
                    self._render_tracked_line_centered(surface, cx, y, lines[idx], BLACK)
                    y -= self.line_height
                    if y < 0:
                        break

    def draw(self):
        self._render_base(self.eye_surface)

        # Apply vignette
        if self.settings["room_mode"]:
            self.eye_surface.blit(self.room_mask, (0, 0))
        else:
            self.eye_surface.blit(self.portal_mask, (0, 0))

        # Overlay on top (unmasked)
        self._render_overlay_unmasked(self.eye_surface)

        self.screen.blit(self.eye_surface, (0, 0))
        self.screen.blit(self.eye_surface, (EYE_WIDTH, 0))
        pygame.display.flip()


if __name__ == "__main__":
    app = MindPalaceEmulator()
    clock = pygame.time.Clock()
    while True:
        app.handle_input()
        app.draw()
        clock.tick(60)
