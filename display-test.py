import pygame
import sys
import os
import datetime
import math
import json

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
GRAY = (220, 220, 220)  # Softer gray for "bloom" feel


class MindPalaceEmulator:
    def __init__(self):
        pygame.init()

        # Set window to exact hardware resolution
        self.screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT))
        pygame.display.set_caption("Mind Palace")

        self.eye_surface = pygame.Surface((EYE_WIDTH, EYE_HEIGHT))

        # Portal mask
        self.mask = self._generate_smooth_portal(EYE_WIDTH, EYE_HEIGHT)

        # Fonts (smaller for VR comfort)
        font_name = pygame.font.match_font("arial", "googlesans", "inter")
        self.font_body = pygame.font.Font(font_name, 14)
        self.font_header = pygame.font.Font(font_name, 18)
        self.line_height = 20

        # Typewriter layout: cursor/last line sits slightly below center
        self.typewriter_offset_y = 18  # px below center

        # State
        self.state = "MENU"
        self.menu_options = ["write", "open", "settings", "exit"]
        self.selected_index = 0

        self.current_filename = None
        self.capsule_files = []
        self.browser_index = 0

        self.notes = [""]  # stored as logical lines; wrapping happens at render
        self.status_msg = ""
        self.msg_timer = 0

        # Autosave-on-pause (silent unless failure)
        self.autosave_delay = 900  # ms after last edit
        self.last_edit_time = 0
        self.dirty = False

        # View scroll: number of wrapped lines above the bottom being viewed
        self.scroll_lines = 0

        # Settings (persisted)
        self.settings = {
            "typewriter_mode": False,
            "auto_capitalize_i": False,
            "always_double_quotes": False,
            "autocap_after_period": False,
        }
        self._load_settings()

        # Settings menu UI
        self.settings_items = [
            ("Typewriter mode", "typewriter_mode"),
            ("Auto capitalize \"i\"", "auto_capitalize_i"),
            ("Always use double quotes", "always_double_quotes"),
            ("Autocapitalize after a .", "autocap_after_period"),
            ("Back", None),
        ]
        self.settings_index = 0

        # Backspace repeat (Word-like: initial delay, then repeat) — only used when typewriter_mode=False
        self.backspace_down = False
        self.backspace_hold_start = 0
        self.backspace_next_repeat = 0

        self.backspace_initial_delay = 260  # ms
        self.backspace_repeat_interval = 32  # ms
        self.backspace_fast_after = 900  # ms held before speeding up
        self.backspace_fast_interval = 22  # ms when sped up

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
                            self.settings[k] = bool(data[k])
        except Exception:
            # silently keep defaults
            pass

    def _save_settings(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2)
        except Exception:
            self._notify_error("settings save failed.")

    # -----------------------
    # Portal mask
    # -----------------------
    def _generate_smooth_portal(self, w, h):
        """
        Smooth radial alpha mask:
          - center clear (alpha 0)
          - smooth fade to black (alpha 255)
        """
        mask = pygame.Surface((w, h), pygame.SRCALPHA)
        cx, cy = w / 2.0, h / 2.0

        clear_radius = h * 0.38
        fade_width = 80.0
        outer_radius = clear_radius + fade_width

        for y in range(h):
            dy = y - cy
            for x in range(w):
                dx = x - cx
                d = math.hypot(dx, dy)

                if d <= clear_radius:
                    a = 0
                elif d >= outer_radius:
                    a = 255
                else:
                    t = (d - clear_radius) / fade_width  # 0..1
                    s = t * t * (3.0 - 2.0 * t)          # smoothstep
                    a = int(255 * s)

                mask.set_at((x, y), (0, 0, 0, a))
        return mask

    # -----------------------
    # Status notifications (errors only)
    # -----------------------
    def _notify_error(self, msg):
        self.status_msg = msg
        self.msg_timer = pygame.time.get_ticks()

    # -----------------------
    # Saving / autosave
    # -----------------------
    def save_capsule(self, autosave=False):
        if self.current_filename is None:
            ts = datetime.datetime.now().strftime("%m%d_%H%M%S")
            self.current_filename = f"capsule_{ts}.txt"

        path = os.path.join(CAPSULE_FOLDER, self.current_filename)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(self.notes))
            self.dirty = False
        except Exception:
            self._notify_error("autosave failed." if autosave else "save failed.")

    def _mark_edited(self):
        self.dirty = True
        self.last_edit_time = pygame.time.get_ticks()
        # any edit snaps view back to the "typewriter position"
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
        last = self._get_last_non_space_char()
        return last == "."

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
        # Settings transforms
        if self.settings["always_double_quotes"] and ch == "'":
            ch = '"'

        if ch.isalpha() and self._should_autocapitalize_next_letter():
            ch = ch.upper()

        self.notes[-1] += ch
        self._mark_edited()

        # Apply auto-capitalize "i" when a boundary is typed
        if ch.isspace() or ch in '.,!?;:"()[]{}':
            self._apply_auto_i_on_boundary()

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

                # CTRL+Q: Global return to menu
                if event.key == pygame.K_q and (mods & pygame.KMOD_CTRL):
                    self.state = "MENU"
                    return

                # CTRL+S: Save (allowed)
                if event.key == pygame.K_s and (mods & pygame.KMOD_CTRL):
                    if self.state == "WRITING":
                        self.save_capsule(autosave=False)
                    return

                if self.state == "MENU":
                    self._nav_menu(event.key)
                elif self.state == "BROWSER":
                    self._nav_browser(event.key)
                elif self.state == "SETTINGS":
                    self._nav_settings(event.key)
                elif self.state == "WRITING":
                    # Start backspace hold only if NOT in typewriter mode
                    if (not self.settings["typewriter_mode"]) and event.key == pygame.K_BACKSPACE:
                        self.backspace_down = True
                        self.backspace_hold_start = now
                        self._delete_char()  # immediate delete
                        self.backspace_next_repeat = now + self.backspace_initial_delay
                    else:
                        self._nav_writing(event)

            if event.type == pygame.KEYUP:
                if self.state == "WRITING" and (not self.settings["typewriter_mode"]) and event.key == pygame.K_BACKSPACE:
                    self.backspace_down = False

        # Handle held backspace repeats (smooth + delayed), only in non-typewriter mode
        if self.state == "WRITING" and (not self.settings["typewriter_mode"]) and self.backspace_down:
            if now >= self.backspace_next_repeat:
                held_for = now - self.backspace_hold_start
                interval = self.backspace_fast_interval if held_for >= self.backspace_fast_after else self.backspace_repeat_interval
                self._delete_char()
                self.backspace_next_repeat = now + interval

        # Autosave when user pauses typing
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
                self.notes, self.current_filename = [""], None
                self.dirty = False
                self.last_edit_time = 0
                self.scroll_lines = 0
                self.state = "WRITING"
            elif choice == "open":
                self.capsule_files = [f for f in os.listdir(CAPSULE_FOLDER) if f.endswith(".txt")]
                self.browser_index = 0
                self.state = "BROWSER"
            elif choice == "settings":
                self.settings_index = 0
                self.state = "SETTINGS"

    def _nav_browser(self, key):
        if key == pygame.K_ESCAPE:
            self.state = "MENU"
        elif self.capsule_files:
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
                    self.current_filename, self.state = f, "WRITING"
                    self.dirty = False
                    self.last_edit_time = pygame.time.get_ticks()
                    self.scroll_lines = 0
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

    def _nav_writing(self, event):
        # ESC to menu
        if event.key == pygame.K_ESCAPE:
            self.state = "MENU"
            return

        # Viewing: up/down scroll through what you've written (works in both modes)
        if event.key == pygame.K_UP:
            total_lines = len(self._get_wrapped_lines())
            max_scroll = max(0, total_lines - 1)
            self.scroll_lines = min(max_scroll, self.scroll_lines + 1)
            return

        if event.key == pygame.K_DOWN:
            self.scroll_lines = max(0, self.scroll_lines - 1)
            return

        # Backspace behavior:
        # - typewriter_mode: no backspace editing
        # - normal mode: handled by hold logic (so ignore KEYDOWN here)
        if event.key == pygame.K_BACKSPACE:
            return

        # New line
        if event.key == pygame.K_RETURN:
            self.notes.append("")
            self._mark_edited()
            return

        # Regular typing
        if event.unicode and event.unicode.isprintable():
            self._insert_char(event.unicode)

    # -----------------------
    # Rendering / wrapping
    # -----------------------
    def _get_wrapped_lines(self):
        wrapped = []
        wrap_px = 220

        for p in self.notes:
            if p == "":
                wrapped.append("")
                continue

            words = p.split(" ")
            line = ""
            for w in words:
                candidate = (line + w).rstrip()
                if self.font_body.size(candidate)[0] < wrap_px:
                    line = (line + w + " ")
                else:
                    wrapped.append(line.rstrip())
                    line = w + " "
            wrapped.append(line.rstrip())
        return wrapped

    def render_eye(self, surface):
        surface.fill(WHITE)
        cx, cy = EYE_WIDTH // 2, EYE_HEIGHT // 2

        # Only show status on errors (brief)
        if self.status_msg and pygame.time.get_ticks() - self.msg_timer < 1400:
            m = self.font_body.render(self.status_msg, True, GRAY)
            surface.blit(m, (cx - m.get_width() // 2, 15))

        if self.state == "MENU":
            for i, opt in enumerate(self.menu_options):
                color = BLACK if i == self.selected_index else GRAY
                txt = self.font_header.render(opt, True, color)
                surface.blit(txt, (cx - txt.get_width() // 2, cy - 55 + i * 35))

        elif self.state == "BROWSER":
            if not self.capsule_files:
                txt = self.font_body.render("no capsules", True, GRAY)
                surface.blit(txt, (cx - txt.get_width() // 2, cy))
            else:
                for i, f in enumerate(self.capsule_files):
                    color = BLACK if i == self.browser_index else GRAY
                    txt = self.font_body.render(f, True, color)
                    surface.blit(txt, (cx - txt.get_width() // 2, 50 + i * 25))

        elif self.state == "SETTINGS":
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

        elif self.state == "WRITING":
            lines = self._get_wrapped_lines()
            if not lines:
                lines = [""]

            # Which wrapped line sits on the typewriter baseline
            bottom_index = max(0, len(lines) - 1 - self.scroll_lines)

            baseline_y = cy + self.typewriter_offset_y

            # Draw upwards from baseline (typewriter feel)
            y = baseline_y
            for idx in range(bottom_index, -1, -1):
                line_text = lines[idx]
                txt = self.font_body.render(line_text, True, BLACK)
                surface.blit(txt, (cx - txt.get_width() // 2, y))
                y -= self.line_height
                if y < 0:
                    break

    def draw(self):
        self.render_eye(self.eye_surface)
        self.eye_surface.blit(self.mask, (0, 0))

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
