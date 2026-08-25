#!/usr/bin/env python3
"""
Sky MCP Server for PC Sky.

This server is for the Windows/PC version of Sky running on the same computer.
It exposes MCP tools for keyboard control, chat, screenshots, and OCR.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import io
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

SERVER_INFO = {"name": "sky-mcp-server", "version": "0.2.0-gpu-merge"}
PROTOCOL_VERSION = "2025-03-26"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
_NUM_RE = re.compile(r"^\d{1,4}$")
_NUM_SPACED_RE = re.compile(r"^[\d\s]{1,6}$")
_CJK_RE = re.compile(r"^[\u4e00-\u9fff]$")

# 坐标置信度参数（单位：字高的倍数，随游戏视角缩放自适应）
_COLUMN_X_TOL = 3.5
_CHAT_GAP_MIN = 0.8
_CHAT_GAP_MAX = 3.0
_NICKNAME_FRAMES = 3
_F_COOLDOWN = 30.0
_DEFAULT_CHAR_HEIGHT = 28.0
_F_DETECT_ENABLED = False  # F 互动检测默认关闭：好友靠近也会出现 F，误报太频繁
_FOCUS_FALLBACK_CLICK = True  # 焦点抢不到时，是否用「点击窗口中心」兜底（可能误触游戏 UI）
_JUNK_CHARS = set("…•·‥。、，、！？!?~-—–_*×/|\\'\"`（）()[]【】<>《》{}+=#$%^&@")
_UI_WORDS = {
    "光遇", "光·遇", "蜡烛", "选择", "退后", "返回", "确定", "取消",
    "晨岛", "云野", "雨林", "霞谷", "暮土", "禁阁", "伊甸",
    "陌生人", "-陌生人", "聊天", "发送", "输入", "输入消息", "好友", "星盘",
    "设置", "退出", "前往", "坐下", "向导", "先祖",
}


class SkyError(RuntimeError):
    pass


def log(message: str) -> None:
    sys.stderr.write(f"[sky-mcp] {message}\n")
    sys.stderr.flush()


def load_optional(module_name: str):
    try:
        return importlib.import_module(module_name)
    except Exception:
        return None


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 9800
    token: str | None = None
    allow_unsafe_http: bool = False
    window_title: str | None = None
    monitor: int = 1
    input_backend: str = "auto"
    screenshot_scale: float = 1.0
    screenshot_max_width: int = 1920
    screenshot_max_height: int = 1080


class PcSkyController:
    def __init__(self, config: ServerConfig):
        self.config = config
        self._pyautogui = None
        self._pydirectinput = None
        self._mss = None
        self._image_cls = None
        self._pyperclip = None
        self._ocr_name = "none"
        self._ocr_device = "unknown"
        self._ocr_engine = None
        self.last_ocr_text: str | None = None
        self.last_ocr_lines: list[dict[str, Any]] | None = None
        self._ocr_lock = threading.Lock()
        self._ocr_poll_interval = 1.0
        self._reported_at: dict[str, float] = {}
        self._report_cooldown = 300.0
        self._sent_cooldown = 60.0
        self._sent_texts: dict[str, float] = {}
        self._line_history: list[list[dict[str, Any]]] = []
        self._nickname_anchors: list[dict[str, Any]] = []
        self._pending_f: dict[str, Any] | None = None
        self._f_reported_positions: dict[str, float] = {}
        self._last_region: dict[str, Any] | None = None
        self._last_image_size: dict[str, Any] | None = None

    def status(self) -> dict[str, Any]:
        return {
            "mode": "pc",
            "platform": sys.platform,
            "input_backend": self._select_input_backend_name(),
            "ocr": self._detect_ocr_name(),
            "ocr_device": self._ocr_device,
            "screenshot_max": {
                "width": self.config.screenshot_max_width,
                "height": self.config.screenshot_max_height,
            },
            "window": self.find_window(),
        }

    def ensure_capture_deps(self) -> None:
        if self._mss is None:
            self._mss = load_optional("mss")
        if self._image_cls is None:
            pil = load_optional("PIL.Image")
            self._image_cls = pil
        if self._mss is None:
            raise SkyError("Missing dependency: mss. Run: pip install -r requirements.txt")
        if self._image_cls is None:
            raise SkyError("Missing dependency: Pillow. Run: pip install -r requirements.txt")

    def _select_input_backend_name(self) -> str:
        return self._resolve_input_backend_name()

    def _resolve_input_backend_name(self, backend: str | None = None) -> str:
        backend = (backend or self.config.input_backend or "auto").lower().strip()
        if backend != "auto":
            if backend not in {"pyautogui", "pydirectinput"}:
                raise SkyError(f"Unsupported input backend: {backend}")
            return backend
        if sys.platform.startswith("win") and load_optional("pydirectinput") is not None:
            return "pydirectinput"
        return "pyautogui"

    def _input_module(self, backend: str | None = None):
        backend = self._resolve_input_backend_name(backend)
        if backend == "pydirectinput":
            if self._pydirectinput is None:
                self._pydirectinput = load_optional("pydirectinput")
            if self._pydirectinput is None:
                raise SkyError("Missing dependency: pydirectinput. Run: pip install -r requirements.txt")
            self._pydirectinput.PAUSE = 0.01
            return self._pydirectinput
        if self._pyautogui is None:
            self._pyautogui = load_optional("pyautogui")
        if self._pyautogui is None:
            raise SkyError("Missing dependency: pyautogui. Run: pip install -r requirements.txt")
        self._pyautogui.FAILSAFE = False
        self._pyautogui.PAUSE = 0.01
        return self._pyautogui

    def _clipboard(self):
        if self._pyperclip is None:
            self._pyperclip = load_optional("pyperclip")
        if self._pyperclip is None:
            raise SkyError("Missing dependency: pyperclip. Run: pip install -r requirements.txt")
        return self._pyperclip

    def _hotkey(self, *keys: str, duration_ms: int = 30, backend: str | None = None) -> None:
        self._press_combo([normalize_key(key) for key in keys], duration_ms, backend=backend)

    def _mouse_module(self):
        if self._pyautogui is None:
            self._pyautogui = load_optional("pyautogui")
        if self._pyautogui is None:
            raise SkyError("Missing dependency: pyautogui. Run: pip install -r requirements.txt")
        self._pyautogui.FAILSAFE = False
        self._pyautogui.PAUSE = 0.01
        return self._pyautogui

    def _press_combo(self, keys: list[str], duration_ms: int = 30, backend: str | None = None) -> None:
        if not keys:
            return
        inp = self._input_module(backend)
        duration = max(0, int(duration_ms)) / 1000
        modifiers = keys[:-1]
        main = keys[-1]
        for mod in modifiers:
            inp.keyDown(mod)
        try:
            inp.keyDown(main)
            time.sleep(duration)
            inp.keyUp(main)
        finally:
            for mod in reversed(modifiers):
                try:
                    inp.keyUp(mod)
                except Exception:
                    pass

    def _tap_key(self, key: str, duration_ms: int = 35, backend: str | None = None) -> None:
        key = normalize_key(key)
        if "+" in key or "-" in key:
            parts = [normalize_key(p) for p in key.replace("+", "-").split("-") if p]
            self._press_combo(parts, duration_ms, backend=backend)
            return
        self._press_combo([key], duration_ms, backend=backend)

    def _paste_text(self, message: str, backend: str | None = None) -> None:
        clip = self._clipboard()
        previous_clipboard = None
        try:
            try:
                previous_clipboard = clip.paste()
            except Exception:
                previous_clipboard = None
            clip.copy(message)
            time.sleep(0.05)
            if sys.platform == "darwin":
                self._hotkey("command", "v", backend=backend)
            else:
                self._hotkey("ctrl", "v", backend=backend)
            time.sleep(0.08)
        finally:
            if previous_clipboard is not None:
                try:
                    clip.copy(previous_clipboard)
                except Exception:
                    pass

    def _foreground_window(self) -> dict[str, Any] | None:
        if not sys.platform.startswith("win"):
            return None
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, len(buf))
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return {"hwnd": int(hwnd), "title": buf.value, "pid": int(pid.value)}
        except Exception:
            return None

    def _force_foreground_window(self, hwnd: int) -> None:
        if not sys.platform.startswith("win"):
            return

        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        sw_restore = 9
        hwnd_topmost = -1
        hwnd_notopmost = -2
        swp_no_move = 0x0002
        swp_no_size = 0x0001
        swp_show_window = 0x0040
        vk_menu = 0x12
        keyeventf_keyup = 0x0002

        user32.ShowWindow(hwnd, sw_restore)
        user32.SetWindowPos(hwnd, hwnd_topmost, 0, 0, 0, 0, swp_no_move | swp_no_size | swp_show_window)
        user32.SetWindowPos(hwnd, hwnd_notopmost, 0, 0, 0, 0, swp_no_move | swp_no_size | swp_show_window)
        user32.BringWindowToTop(hwnd)

        user32.keybd_event(vk_menu, 0, 0, 0)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(vk_menu, 0, keyeventf_keyup, 0)
        time.sleep(0.05)

        foreground = self._foreground_window()
        if foreground and foreground.get("hwnd") == hwnd:
            return

        try:
            foreground_hwnd = user32.GetForegroundWindow()
            foreground_pid = wintypes.DWORD()
            target_pid = wintypes.DWORD()
            foreground_thread = user32.GetWindowThreadProcessId(foreground_hwnd, ctypes.byref(foreground_pid))
            target_thread = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid))
            current_thread = kernel32.GetCurrentThreadId()

            for thread_id in {foreground_thread, target_thread}:
                if thread_id:
                    user32.AttachThreadInput(current_thread, thread_id, True)
            try:
                user32.ShowWindow(hwnd, sw_restore)
                user32.BringWindowToTop(hwnd)
                user32.SetActiveWindow(hwnd)
                user32.SetFocus(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                for thread_id in {foreground_thread, target_thread}:
                    if thread_id:
                        user32.AttachThreadInput(current_thread, thread_id, False)
        except Exception:
            pass

    def _window_handle(self, win) -> int | None:
        hwnd = getattr(win, "_hWnd", None) or getattr(win, "hWnd", None)
        return int(hwnd) if hwnd else None

    def _click_window_center(self, win_info: dict[str, Any]) -> None:
        mouse = self._mouse_module()
        x = int(win_info["left"] + win_info["width"] / 2)
        y = int(win_info["top"] + win_info["height"] / 2)
        mouse.click(x, y)

    def find_window(self) -> dict[str, Any] | None:
        titles = [self.config.window_title] if self.config.window_title else [
            "光·遇",
            "光遇",
            "Sky",
            "Sky: Children of the Light",
            "Sky Children of the Light",
            "光遇",
        ]
        try:
            import pygetwindow as gw
        except Exception:
            return None

        for title in [t for t in titles if t]:
            for win in gw.getWindowsWithTitle(title):
                if win.isMinimized:
                    continue
                return {
                    "title": win.title,
                    "left": int(win.left),
                    "top": int(win.top),
                    "width": int(win.width),
                    "height": int(win.height),
                }
        return None

    def focus_game(self) -> dict[str, Any]:
        try:
            import pygetwindow as gw
        except Exception as exc:
            raise SkyError(f"pygetwindow is required to focus the Sky window: {exc}") from exc

        win_info = self.find_window()
        if not win_info:
            raise SkyError("Sky window not found. Start Sky first, or pass --window-title.")
        matches = gw.getWindowsWithTitle(win_info["title"])
        if not matches:
            raise SkyError("Sky window disappeared before focus.")
        win = matches[0]
        hwnd = self._window_handle(win)
        foreground = self._foreground_window()
        if sys.platform.startswith("win") and hwnd and foreground and foreground.get("hwnd") == hwnd:
            return {**win_info, "focused": True, "foreground": foreground}
        if win.isMinimized:
            win.restore()
        try:
            win.activate()
        except Exception as exc:
            if not (sys.platform.startswith("win") and hwnd):
                raise SkyError(f"Failed to activate Sky window: {exc}") from exc
            try:
                import ctypes
                user32 = ctypes.windll.user32
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
            except Exception as fallback_exc:
                raise SkyError(f"Failed to activate Sky window: {fallback_exc}") from fallback_exc
        if sys.platform.startswith("win") and hwnd:
            self._force_foreground_window(hwnd)
        time.sleep(0.15)
        foreground = self._foreground_window()
        if sys.platform.startswith("win") and hwnd and (not foreground or foreground.get("hwnd") != hwnd):
            log(f"focus_game: foreground is {foreground and foreground.get('title')!r}, trying to bring Sky to front")
            if _FOCUS_FALLBACK_CLICK:
                log("focus_game: fallback click at window center to grab focus")
                self._click_window_center(win_info)
                time.sleep(0.15)
                foreground = self._foreground_window()
            else:
                foreground = self._foreground_window()
        if sys.platform.startswith("win") and hwnd and foreground and foreground.get("hwnd") != hwnd:
            raise SkyError(f"Sky window found but not focused. Foreground window is: {foreground.get('title')}")
        return {**win_info, "focused": True, "foreground": foreground}

    def ensure_game_foreground(self) -> dict[str, Any]:
        win_info = self.find_window()
        if not win_info:
            raise SkyError("Sky window not found. Start Sky first, or pass --window-title.")
        foreground = self._foreground_window()
        if not sys.platform.startswith("win"):
            return {**win_info, "focused": True, "foreground": foreground}
        try:
            import pygetwindow as gw
        except Exception as exc:
            raise SkyError(f"pygetwindow is required to verify the Sky window: {exc}") from exc
        matches = gw.getWindowsWithTitle(win_info["title"])
        hwnd = self._window_handle(matches[0]) if matches else None
        if hwnd and foreground and foreground.get("hwnd") != hwnd:
            raise SkyError(f"Sky window is not foreground. Foreground window is: {foreground.get('title')}")
        return {**win_info, "focused": True, "foreground": foreground}

    def press_key(self, key: str, duration_ms: int = 80, backend: str | None = None, assume_focused: bool = False) -> str:
        if assume_focused:
            self.ensure_game_foreground()
        else:
            self.focus_game()
        key = normalize_key(key)
        self._tap_key(key, duration_ms, backend=backend)
        return f"pressed {key} for {int(duration_ms)}ms via {self._resolve_input_backend_name(backend)}"

    def press_keys(
        self,
        keys: str,
        interval_ms: int = 80,
        duration_ms: int = 60,
        last_duration_ms: int = 120,
        backend: str | None = None,
        assume_focused: bool = False,
    ) -> str:
        if not isinstance(keys, str) or not keys.strip():
            raise SkyError("keys must be a non-empty string like 'fff' or 'f f f'")
        if assume_focused:
            self.ensure_game_foreground()
        else:
            self.focus_game()
        if " " in keys:
            sequence = [normalize_key(p) for p in keys.split() if p]
        else:
            sequence = [normalize_key(ch) for ch in keys if not ch.isspace()]
        if not sequence:
            raise SkyError("keys produced an empty sequence")
        if len(sequence) > 20:
            raise SkyError(f"keys too long: {len(sequence)} presses, at most 20 per call")
        inp = self._input_module(backend)
        if hasattr(inp, "KEY_MAP"):
            for key in sequence:
                if key not in inp.KEY_MAP:
                    raise SkyError(f"unknown key in sequence: {key!r}")
        elif hasattr(inp, "KEYBOARD_KEYS"):
            for key in sequence:
                if key not in inp.KEYBOARD_KEYS:
                    raise SkyError(f"unknown key in sequence: {key!r}")
        interval = max(0, int(interval_ms)) / 1000
        base_duration = max(0, int(duration_ms))
        last_duration = max(0, int(last_duration_ms))
        parts = []
        for index, key in enumerate(sequence):
            hold = last_duration if index == len(sequence) - 1 else base_duration
            self._tap_key(key, hold, backend=backend)
            parts.append(f"{key}:{hold}ms")
            if index < len(sequence) - 1:
                time.sleep(interval)
        return f"pressed {len(sequence)} keys [{', '.join(parts)}] interval={interval_ms}ms via {self._resolve_input_backend_name(backend)}"

    def _find_ocr_lines(self, needle: str) -> list[dict[str, Any]]:
        result = self._ocr_screen_once(read_only=True)
        needle_clean = str(needle).replace(" ", "").strip()
        if not needle_clean:
            return []
        matches: list[dict[str, Any]] = []
        for line in result.get("texts") or []:
            line_text = str(line.get("text", "")).replace(" ", "").strip()
            if needle_clean in line_text:
                screen_x, screen_y = self._screen_point(line.get("x", 0), line.get("y", 0))
                matches.append({
                    "text": str(line.get("text", "")).strip(),
                    "confidence": float(line.get("confidence", 0) or 0),
                    "screen_x": screen_x,
                    "screen_y": screen_y,
                })
        matches.sort(key=lambda m: (m["confidence"], len(m["text"])), reverse=True)
        return matches

    def teleport_to(
        self,
        friend_name: str,
        open_key: str = "g",
        open_delay_ms: int = 800,
        name_offset_x: int = 100,
        name_offset_y: int = 0,
        button_text: str = "传送",
        confirm_key: str = "space",
        confirm_delay_ms: int = 1000,
        confirm_repeat: int = 2,
        confirm_interval_ms: int = 120,
        max_attempts: int = 3,
        backend: str | None = None,
        assume_focused: bool = False,
        dry_run: bool = False,
    ) -> str:
        if not isinstance(friend_name, str) or not friend_name.strip():
            raise SkyError("friend_name must be a non-empty string")
        if assume_focused:
            self.ensure_game_foreground()
        else:
            self.focus_game()
        report: list[str] = []
        open_key = normalize_key(open_key)
        confirm_key = normalize_key(confirm_key)
        attempts = max(1, min(int(max_attempts), 10))
        open_delay = max(0, int(open_delay_ms)) / 1000
        confirm_delay = max(0, int(confirm_delay_ms)) / 1000
        confirm_repeat = max(1, min(int(confirm_repeat), 10))
        confirm_interval = max(0, int(confirm_interval_ms)) / 1000

        self._tap_key(open_key, 80, backend=backend)
        report.append(f"pressed {open_key} to open star map")
        time.sleep(open_delay)

        name_matches: list[dict[str, Any]] = []
        for attempt in range(1, attempts + 1):
            name_matches = self._find_ocr_lines(friend_name)
            if name_matches:
                break
            if attempt < attempts:
                log(f"teleport_to: friend {friend_name!r} not found, retry {attempt}/{attempts}")
                time.sleep(1.0)
        if not name_matches:
            raise SkyError(
                f"teleport_to: friend {friend_name!r} not found on screen after {attempts} attempts "
                f"(pressed {open_key} to open the star map). Make sure their constellation page is visible."
            )
        match = name_matches[0]
        click_x = int(match["screen_x"]) + int(name_offset_x)
        click_y = int(match["screen_y"]) + int(name_offset_y)
        report.append(
            f"found {friend_name!r} at screen ({match['screen_x']}, {match['screen_y']}) conf={match['confidence']:.2f}"
        )
        if dry_run:
            report.append(f"[dry-run] would click ({click_x}, {click_y})")
        else:
            self.click_at(click_x, click_y)
            report.append(f"clicked ({click_x}, {click_y})")
        time.sleep(open_delay)

        button_matches: list[dict[str, Any]] = []
        for attempt in range(1, attempts + 1):
            button_matches = self._find_ocr_lines(button_text)
            if button_matches:
                break
            if attempt < attempts:
                log(f"teleport_to: button {button_text!r} not found, retry {attempt}/{attempts}")
                time.sleep(1.0)
        if not button_matches:
            raise SkyError(
                f"teleport_to: button {button_text!r} not found after {attempts} attempts. "
                f"Friend {friend_name!r} was found at ({match['screen_x']}, {match['screen_y']})."
            )
        bmatch = button_matches[0]
        report.append(
            f"found {button_text!r} at screen ({bmatch['screen_x']}, {bmatch['screen_y']}) conf={bmatch['confidence']:.2f}"
        )
        if dry_run:
            report.append(f"[dry-run] would click ({bmatch['screen_x']}, {bmatch['screen_y']})")
            report.append(f"[dry-run] would press {confirm_key} x{confirm_repeat} to confirm")
        else:
            self.click_at(int(bmatch["screen_x"]), int(bmatch["screen_y"]))
            report.append(f"clicked ({bmatch['screen_x']}, {bmatch['screen_y']})")
            time.sleep(confirm_delay)
            for index in range(confirm_repeat):
                self._tap_key(confirm_key, 100, backend=backend)
                if index < confirm_repeat - 1:
                    time.sleep(confirm_interval)
            report.append(f"pressed {confirm_key} x{confirm_repeat} to confirm")
        return "teleport_to: " + "\n  ".join(report)

    def open_chat(self, key: str = "enter", duration_ms: int = 35, backend: str | None = None) -> str:
        self.focus_game()
        key = normalize_key(key)
        self._tap_key(key, duration_ms, backend=backend)
        return f"tapped {key} for {int(duration_ms)}ms via {self._resolve_input_backend_name(backend)}"

    def click_at(self, x: int, y: int, button: str = "left", clicks: int = 1) -> str:
        mouse = self._mouse_module()
        x, y = int(x), int(y)
        clicks = max(1, int(clicks))
        mouse.click(x, y, clicks=clicks, interval=0.08, button=button)
        return f"clicked ({x}, {y}) button={button} clicks={clicks} via pyautogui"

    def type_text(
        self,
        message: str,
        send: bool = True,
        enter_tap_ms: int = 35,
        backend: str | None = None,
        require_foreground: bool = True,
    ) -> str:
        if not isinstance(message, str) or not message.strip():
            raise SkyError("message must be a non-empty string")
        backend_name = self._resolve_input_backend_name(backend)
        if not send:
            if require_foreground:
                self.ensure_game_foreground()
            self._paste_text(message, backend=backend)
            return f"typed text: {message} via {backend_name}"
        if require_foreground:
            self.ensure_game_foreground()
        segments = self._split_sentences(message) or [message.strip()]
        for index, segment in enumerate(segments):
            self._paste_text(segment, backend=backend)
            self._tap_key("enter", enter_tap_ms, backend=backend)
            self._mark_reported({"text": segment})
            self._mark_sent(segment)
            if index < len(segments) - 1:
                time.sleep(0.5)
        success, matched = self._confirm_sent(segments)
        joined = " / ".join(segments)
        result = "发送成功" if success else "发送失败"
        return f"sent text: {joined} ({len(segments)}条) via {backend_name}\n{result}"

    def send_chat(
        self,
        message: str,
        open_key: str = "enter",
        open_delay_ms: int = 180,
        assume_open: bool = False,
        send: bool = True,
        enter_tap_ms: int = 35,
        backend: str | None = None,
    ) -> str:
        if not isinstance(message, str) or not message.strip():
            raise SkyError("message must be a non-empty string")
        backend_name = self._resolve_input_backend_name(backend)
        if not send:
            if assume_open:
                self.ensure_game_foreground()
            else:
                self.focus_game()
                self._tap_key(open_key, enter_tap_ms, backend=backend)
                time.sleep(max(0, open_delay_ms) / 1000)
            self._paste_text(message, backend=backend)
            return f"typed chat: {message} via {backend_name}"
        segments = self._split_sentences(message) or [message.strip()]
        for index, segment in enumerate(segments):
            if assume_open:
                self.ensure_game_foreground()
            else:
                self.focus_game()
                self._tap_key(open_key, enter_tap_ms, backend=backend)
                time.sleep(max(0, open_delay_ms) / 1000)
            self._paste_text(segment, backend=backend)
            self._tap_key("enter", enter_tap_ms, backend=backend)
            self._mark_reported({"text": segment})
            self._mark_sent(segment)
            if index < len(segments) - 1:
                time.sleep(0.5)
        success, matched = self._confirm_sent(segments)
        joined = " / ".join(segments)
        result = "发送成功" if success else "发送失败"
        return f"sent chat: {joined} ({len(segments)}条) via {backend_name}\n{result}"

    @staticmethod
    def _split_sentences(message: str) -> list[str]:
        parts: list[str] = []
        raw_parts = re.split(r"([。！？!?—]+)", message)
        for index in range(0, len(raw_parts) - 1, 2):
            part = raw_parts[index].strip()
            punct = raw_parts[index + 1]
            if not part:
                continue
            segment = part + "".join(ch for ch in punct if ch in "！？!?")
            while len(segment) > 240:
                parts.append(segment[:240])
                segment = segment[240:]
            if segment:
                parts.append(segment)
        if raw_parts and raw_parts[-1].strip():
            last = raw_parts[-1].strip()
            while len(last) > 240:
                parts.append(last[:240])
                last = last[240:]
            if last:
                parts.append(last)
        return parts

    def _confirm_sent(self, sent_texts: list[str]) -> tuple[bool, list[str]]:
        time.sleep(1.0)
        for attempt in range(2):
            result = self._ocr_screen_once(read_only=True)
            matched: list[str] = []
            for item in result["texts"]:
                raw = str(item.get("text", "")).strip()
                if not raw:
                    continue
                if any(self._text_matches(raw, sent) for sent in sent_texts):
                    matched.append(raw)
            if matched:
                for raw in matched:
                    self._mark_reported({"text": raw})
                log(f"send confirm: matched on attempt {attempt + 1}: {matched!r}")
                return True, matched
        log("send confirm: failed after 2 attempts")
        return False, []

    @staticmethod
    def _text_matches(ocr_line: str, sent: str) -> bool:
        a = ocr_line.replace(" ", "").strip()
        b = sent.replace(" ", "").strip()
        if not b:
            return False
        if len(b) <= 2:
            clean_a = "".join(ch for ch in a if ch not in _JUNK_CHARS)
            clean_b = "".join(ch for ch in b if ch not in _JUNK_CHARS)
            return bool(clean_a) and clean_a == clean_b
        if b in a:
            return True
        return SequenceMatcher(None, a, b).ratio() >= 0.6

    def _limit_image_size(self, image):
        max_width = max(1, int(self.config.screenshot_max_width))
        max_height = max(1, int(self.config.screenshot_max_height))
        scale = min(max_width / image.width, max_height / image.height, 1)
        if scale >= 1:
            return image
        resampling = getattr(self._image_cls, "Resampling", self._image_cls).LANCZOS
        return image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            resampling,
        )

    def _screen_point(self, x: int | float, y: int | float) -> tuple[int, int]:
        region = self._last_region
        size = self._last_image_size
        if not region or not size or not region.get("width") or not size.get("width"):
            return int(x), int(y)
        scale_x = region["width"] / size["width"]
        scale_y = region["height"] / size["height"]
        return (
            int(round(region["left"] + float(x) * scale_x)),
            int(round(region["top"] + float(y) * scale_y)),
        )

    def screenshot_image(self):
        self.ensure_capture_deps()
        with self._mss.mss() as screen:
            win = self.find_window()
            if win:
                region = {
                    "left": win["left"],
                    "top": win["top"],
                    "width": max(1, win["width"]),
                    "height": max(1, win["height"]),
                }
            else:
                monitors = screen.monitors
                index = min(max(1, self.config.monitor), len(monitors) - 1)
                region = monitors[index]
            self._last_region = dict(region)
            shot = screen.grab(region)
            image = self._image_cls.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if self.config.screenshot_scale != 1.0:
            scale = min(max(self.config.screenshot_scale, 0.2), 1.0)
            resampling = getattr(self._image_cls, "Resampling", self._image_cls).LANCZOS
            image = image.resize((int(image.width * scale), int(image.height * scale)), resampling)
        image = self._limit_image_size(image)
        self._last_image_size = {"width": image.width, "height": image.height}
        return image

    def screenshot_base64(self) -> str:
        image = self.screenshot_image()
        buf = io.BytesIO()
        image.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def read_screen(self) -> dict[str, Any]:
        result = self._ocr_screen_once()
        raw_lines = result.get("texts") or []
        all_lines = []
        for line in raw_lines:
            screen_x, screen_y = self._screen_point(line.get("x", 0), line.get("y", 0))
            all_lines.append({
                "text": str(line.get("text", "")).strip(),
                "confidence": line.get("confidence", 0),
                "x": int(line.get("x", 0) or 0),
                "y": int(line.get("y", 0) or 0),
                "height": int(line.get("height", 0) or 0),
                "screen_x": screen_x,
                "screen_y": screen_y,
            })
        new_lines = result.pop("new_lines", [])
        new_lines = self._order_screen_lines(new_lines)
        for line in new_lines:
            screen_x, screen_y = self._screen_point(line.get("x", 0), line.get("y", 0))
            line["screen_x"] = screen_x
            line["screen_y"] = screen_y
        emphasis = result.pop("f_emphasis", None)
        if not result.pop("changed", True):
            log("read_screen: no new content")
            return {
                "texts": [{"text": "No new content", "confidence": 0, "x": 0, "y": 0}],
                "text": "No new content",
                "all_lines": all_lines,
            }
        for line in new_lines:
            self._mark_reported(line)
        if emphasis:
            self._consume_f()
        text = self._format_lines(new_lines)
        if emphasis:
            text = f"{emphasis}\n{text}" if text else emphasis
        if not new_lines and emphasis:
            new_lines = [{"text": emphasis, "confidence": 0, "x": 0, "y": 0}]
        log(f"read_screen: changed ({len(new_lines)} new lines): {text[:80]!r}")
        return {
            "changed": True,
            "ocr": result.get("ocr"),
            "ocr_device": result.get("ocr_device"),
            "image_size": result.get("image_size"),
            "texts": new_lines,
            "text": text,
            "all_lines": all_lines,
            "f_interaction": emphasis,
        }

    def _ocr_screen_once(self, read_only: bool = False) -> dict[str, Any]:
        with self._ocr_lock:
            image = self.screenshot_image()
            with tempfile.NamedTemporaryFile(prefix="sky-mcp-", suffix=".png", delete=False) as tmp:
                path = tmp.name
            try:
                image.save(path)
                texts = self._run_ocr(path)
                if read_only:
                    # 只读确认：发送后读屏确认时不更新记忆，避免把别人的新消息当成已见过
                    return {
                        "ocr": self._detect_ocr_name(),
                        "ocr_device": self._ocr_device,
                        "image_size": {"width": image.width, "height": image.height},
                        "texts": texts,
                        "changed": False,
                        "new_lines": [],
                        "f_emphasis": None,
                    }
                normalized = self._normalize_ocr_text(texts)
                raw_lines = self._clean_text_lines(texts)
                columns = self._group_columns(raw_lines)
                self._update_screen_model(raw_lines, columns)
                confirmed = {
                    index
                    for index, column in enumerate(columns)
                    if self._is_confirmed_column(column)
                }
                lines = self._apply_single_char_rule(raw_lines, columns, confirmed)
                f_emphasis = self._detect_f_emphasis(raw_lines, columns)
                new_lines = self._new_lines(lines)
                self.last_ocr_text = normalized
                self.last_ocr_lines = lines
                return {
                    "ocr": self._detect_ocr_name(),
                    "ocr_device": self._ocr_device,
                    "image_size": {"width": image.width, "height": image.height},
                    "texts": texts,
                    "changed": len(new_lines) > 0 or bool(f_emphasis),
                    "new_lines": new_lines,
                    "f_emphasis": f_emphasis,
                }
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def wait_for_screen_change(self, timeout_seconds: int) -> dict[str, Any]:
        timeout_seconds = max(1, min(int(timeout_seconds), 600))
        start = time.monotonic()
        reference = list(self.last_ocr_lines) if self.last_ocr_lines else []
        deadline = start + timeout_seconds
        collected: list[dict[str, Any]] = []
        prev_new: list[dict[str, Any]] = []
        first_poll = True
        while True:
            if not first_poll:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(self._ocr_poll_interval, remaining))
            first_poll = False
            self._ocr_screen_once()
            current_lines = list(self.last_ocr_lines) if self.last_ocr_lines else []
            new_lines = self._new_lines(current_lines, reference)
            interaction_emphasis = self._pending_emphasis()
            if interaction_emphasis and not prev_new and not new_lines:
                self._consume_f()
                waited = int(time.monotonic() - start)
                log(f"wait_for_screen_change: interaction after {waited}s: {interaction_emphasis}")
                return {
                    "waited_seconds": waited,
                    "changed": True,
                    "texts": [{"text": interaction_emphasis, "confidence": 0, "x": 0, "y": 0}],
                    "text": interaction_emphasis,
                    "f_interaction": interaction_emphasis,
                }
            for line in new_lines:
                if not self._contains_line(collected, line) and self._contains_line(prev_new, line):
                    collected.append(line)
            if new_lines and self._same_screen(new_lines, prev_new):
                for line in collected:
                    self._mark_reported(line)
                waited = int(time.monotonic() - start)
                emphasis = self._pending_emphasis()
                if emphasis:
                    self._consume_f()
                collected = self._order_screen_lines(collected)
                text = self._format_lines(collected)
                if emphasis:
                    text = f"{emphasis}\n{text}" if text else emphasis
                log(f"wait_for_screen_change: new content after {waited}s ({len(collected)} lines)")
                return {
                    "waited_seconds": waited,
                    "changed": True,
                    "texts": collected,
                    "text": text,
                    "f_interaction": emphasis,
                }
            prev_new = new_lines
        if collected:
            for line in collected:
                self._mark_reported(line)
            emphasis = self._pending_emphasis()
            if emphasis:
                self._consume_f()
            collected = self._order_screen_lines(collected)
            text = self._format_lines(collected)
            if emphasis:
                text = f"{emphasis}\n{text}" if text else emphasis
            log(f"wait_for_screen_change: returning {len(collected)} collected lines at timeout")
            return {
                "waited_seconds": timeout_seconds,
                "changed": True,
                "confirmed": False,
                "texts": collected,
                "text": text,
                "f_interaction": emphasis,
            }
        emphasis = self._pending_emphasis()
        if emphasis:
            self._consume_f()
            log(f"wait_for_screen_change: interaction after {timeout_seconds}s: {emphasis}")
            return {
                "texts": [{"text": emphasis, "confidence": 0, "x": 0, "y": 0}],
                "text": emphasis,
                "waited_seconds": timeout_seconds,
                "changed": True,
                "f_interaction": emphasis,
            }
        log(f"wait_for_screen_change: no new content after {timeout_seconds}s")
        return {
            "texts": [{"text": f"No new content for {timeout_seconds}s", "confidence": 0, "x": 0, "y": 0}],
            "text": f"No new content for {timeout_seconds}s",
            "waited_seconds": timeout_seconds,
            "changed": False,
        }

    @staticmethod
    def _normalize_ocr_text(texts: list[dict[str, Any]]) -> str:
        raw = "\n".join(str(item.get("text", "")) for item in texts)
        return " ".join(raw.split())

    @staticmethod
    def _format_lines(lines: list[dict[str, Any]]) -> str:
        return "\n".join(
            str(line.get("text", "")).strip()
            for line in lines
            if str(line.get("text", "")).strip()
        )

    def _order_screen_lines(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ordered = sorted(lines, key=lambda line: (line.get("y", 0), line.get("x", 0)))
        merged: list[dict[str, Any]] = []
        for line in ordered:
            if not merged:
                merged.append(dict(line))
                continue
            last = merged[-1]
            line_height = self._line_height(line)
            last_height = self._line_height(last)
            max_height = max(line_height, last_height)
            if (
                abs(line["x"] - last["x"]) <= max_height
                and 0 < line["y"] - last["y"] <= 1.5 * max_height
                and abs(line_height - last_height) <= 8
            ):
                last["text"] = str(last["text"]) + str(line["text"])
                last["height"] = int(max(int(last.get("height") or 0), int(line.get("height") or 0)))
            else:
                merged.append(dict(line))
        return merged

    @staticmethod
    def _clean_text_lines(texts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for item in texts:
            line = str(item.get("text", "")).strip()
            if not line:
                continue
            try:
                confidence = float(item.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < 0.8:
                continue
            for prefix in ("光·遇", "光遇"):
                if line.startswith(prefix):
                    line = line[len(prefix):]
                    break
            line = line.strip(" \t" + "".join(_JUNK_CHARS))
            if len(line) == 1:
                ch = line[0]
                is_cjk = "\u4e00" <= ch <= "\u9fff"
                is_letter = ch.isascii() and ch.isalpha()
                if not (is_cjk or is_letter):
                    continue
            if _TIME_RE.fullmatch(line):
                continue
            if _NUM_RE.fullmatch(line):
                continue
            if _NUM_SPACED_RE.fullmatch(line):
                continue
            if all(ch in _JUNK_CHARS or ch.isspace() for ch in line):
                continue
            if line in _UI_WORDS:
                continue
            cleaned.append({
                "text": line,
                "confidence": confidence,
                "x": int(item.get("x", 0) or 0),
                "y": int(item.get("y", 0) or 0),
                "height": int(item.get("height", 0) or 0),
            })
        return cleaned

    @staticmethod
    def _element_match(line: dict[str, Any], ref: dict[str, Any]) -> bool:
        ratio = SequenceMatcher(None, line["text"], ref["text"]).ratio()
        return ratio >= 0.9

    def _new_lines(self, lines: list[dict[str, Any]], reference: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        last = self.last_ocr_lines if reference is None else reference
        if not last:
            return [line for line in lines if not self._is_reported(line)]
        unused = list(range(len(last)))
        new: list[dict[str, Any]] = []
        for line in lines:
            matched = False
            for index in unused:
                if self._element_match(line, last[index]):
                    matched = True
                    unused.remove(index)
                    break
            if not matched and not self._is_reported(line):
                new.append(line)
        return new

    def _is_reported(self, line: dict[str, Any]) -> bool:
        reported_at = self._reported_at.get(line["text"])
        if reported_at is not None and (time.monotonic() - reported_at) < self._report_cooldown:
            return True
        match_key = self._match_key(line["text"])
        if not match_key:
            return False
        now = time.monotonic()
        return any(
            now - sent_at < self._sent_cooldown
            and len(match_key) >= 2
            and match_key in sent_key
            for sent_key, sent_at in self._sent_texts.items()
        )

    def _mark_reported(self, line: dict[str, Any]) -> None:
        now = time.monotonic()
        self._reported_at[line["text"]] = now
        for text in [t for t, ts in self._reported_at.items() if now - ts >= self._report_cooldown]:
            del self._reported_at[text]

    def _mark_sent(self, text: str) -> None:
        now = time.monotonic()
        match_key = self._match_key(text)
        if match_key:
            self._sent_texts[match_key] = now
        for key in [k for k, ts in self._sent_texts.items() if now - ts >= self._sent_cooldown]:
            del self._sent_texts[key]

    @staticmethod
    def _match_key(text: str) -> str:
        return "".join(ch for ch in "".join(text.split()) if ch not in _JUNK_CHARS)

    def _same_screen(self, lines: list[dict[str, Any]], reference: list[dict[str, Any]] | None = None) -> bool:
        last = self.last_ocr_lines if reference is None else reference
        if last is None or len(lines) != len(last):
            return False
        unused = list(range(len(last)))
        for line in lines:
            match_index = -1
            for index in unused:
                if self._element_match(line, last[index]):
                    match_index = index
                    break
            if match_index == -1:
                return False
            unused.remove(match_index)
        return True

    def _contains_line(self, lines: list[dict[str, Any]], line: dict[str, Any]) -> bool:
        return any(self._element_match(line, ref) for ref in lines)

    @staticmethod
    def _line_height(line: dict[str, Any]) -> float:
        height = float(line.get("height") or 0)
        return height if height > 0 else _DEFAULT_CHAR_HEIGHT

    def _center_x(self, line: dict[str, Any]) -> float:
        height = self._line_height(line)
        return float(line["x"]) + len(str(line["text"])) * height * 0.5

    def _group_columns(self, lines: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        items = sorted(
            ((self._center_x(line), self._line_height(line), line) for line in lines),
            key=lambda item: item[0],
        )
        columns: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        center = 0.0
        height = 0.0
        for item_center, item_height, line in items:
            if not current:
                current = [line]
                center = item_center
                height = item_height
                continue
            if abs(item_center - center) <= _COLUMN_X_TOL * max(height, item_height):
                current.append(line)
                count = len(current)
                center = (center * (count - 1) + item_center) / count
                height = (height * (count - 1) + item_height) / count
            else:
                columns.append(current)
                current = [line]
                center = item_center
                height = item_height
        if current:
            columns.append(current)
        return columns

    def _is_confirmed_column(self, column: list[dict[str, Any]]) -> bool:
        if len(column) < 2:
            return False
        max_height = max(self._line_height(line) for line in column)
        ys = sorted(line["y"] for line in column)
        for index in range(1, len(ys)):
            gap = ys[index] - ys[index - 1]
            if _CHAT_GAP_MIN * max_height <= gap <= _CHAT_GAP_MAX * max_height:
                return True
        anchor_texts = {anchor["text"] for anchor in self._nickname_anchors}
        return any(line["text"] in anchor_texts for line in column)

    def _apply_single_char_rule(
        self,
        lines: list[dict[str, Any]],
        columns: list[list[dict[str, Any]]],
        confirmed: set[int],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for index, column in enumerate(columns):
            for line in column:
                if len(line["text"]) != 1:
                    result.append(line)
                    continue
                if line["text"] in ("F", "f"):
                    continue
                if index in confirmed:
                    result.append(line)
                elif _CJK_RE.fullmatch(line["text"]) and line["confidence"] >= 0.9:
                    result.append(line)
        return result

    def _update_screen_model(
        self,
        lines: list[dict[str, Any]],
        columns: list[list[dict[str, Any]]],
    ) -> None:
        self._line_history.append([dict(line) for line in lines])
        if len(self._line_history) > 8:
            self._line_history.pop(0)
        self._nickname_anchors = []
        if len(self._line_history) < _NICKNAME_FRAMES:
            return
        prev_frames = self._line_history[-_NICKNAME_FRAMES:-1]
        for column in columns:
            candidates = []
            for line in column:
                text = line["text"]
                if len(text) < 2 or len(text) > 6:
                    continue
                if text in _UI_WORDS:
                    continue
                stable = all(
                    any(self._element_match(line, past) for past in past_frame)
                    for past_frame in prev_frames
                )
                if stable:
                    candidates.append(line)
            if candidates:
                self._nickname_anchors.append(min(candidates, key=lambda line: line["y"]))

    def _detect_f_emphasis(
        self,
        lines: list[dict[str, Any]],
        columns: list[list[dict[str, Any]]],
    ) -> str | None:
        if not _F_DETECT_ENABLED:
            return None
        now = time.monotonic()
        if self._pending_f and (now - self._pending_f["time"]) < 60:
            return self._pending_f["text"]
        column_of: dict[int, int] = {}
        for index, column in enumerate(columns):
            for line in column:
                column_of[id(line)] = index
        anchor_texts = {anchor["text"] for anchor in self._nickname_anchors}
        for f_line in lines:
            if f_line["text"] != "F":
                continue
            pos_key = f"{int(f_line['x']) // 40},{int(f_line['y']) // 40}"
            if now - self._f_reported_positions.get(pos_key, -1e9) < _F_COOLDOWN:
                continue
            nickname = None
            column_index = column_of.get(id(f_line))
            if column_index is not None:
                for line in columns[column_index]:
                    if line["text"] in anchor_texts:
                        nickname = line["text"]
                        break
            emphasis = f"【{nickname}互动】" if nickname else "【互动】"
            self._pending_f = {"text": emphasis, "positions": [pos_key], "time": now}
            return emphasis
        return None

    def _pending_emphasis(self) -> str | None:
        if self._pending_f and (time.monotonic() - self._pending_f["time"]) < 60:
            return self._pending_f["text"]
        return None

    def _consume_f(self) -> None:
        if not self._pending_f:
            return
        now = time.monotonic()
        for pos_key in self._pending_f.get("positions", []):
            self._f_reported_positions[pos_key] = now
        self._pending_f = None
        for pos_key in [k for k, ts in self._f_reported_positions.items() if now - ts > 300]:
            del self._f_reported_positions[pos_key]

    def _detect_ocr_name(self) -> str:
        if self._ocr_engine is not None:
            print("当前OCR:", self._ocr_name)
            return self._ocr_name
        try:
            os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")
            os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
            from paddleocr import PaddleOCR
            try:
                import paddle
                if paddle.device.is_compiled_with_cuda():
                    paddle.set_device("gpu:0")
                self._ocr_device = paddle.get_device()
            except Exception:
                self._ocr_device = "unknown"

            try:
                self._ocr_engine = PaddleOCR(
                    text_detection_model_name="PP-OCRv5_mobile_det",
                    text_recognition_model_name="PP-OCRv5_mobile_rec",
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    text_det_limit_side_len=max(1, int(self.config.screenshot_max_height)),
                    text_det_limit_type="max",
                )
            except Exception:
                self._ocr_engine = PaddleOCR(lang="ch")
            self._ocr_name = f"paddleocr-mobile-{self._ocr_device}"
            return self._ocr_name
        except Exception:
            import traceback
            traceback.print_exc()
        try:
            import pytesseract
            pytesseract.get_tesseract_version()
            self._ocr_engine = pytesseract
            self._ocr_name = "tesseract"
            return self._ocr_name
        except Exception:
            pass
        self._ocr_name = "none"
        return self._ocr_name

    def _run_ocr(self, image_path: str) -> list[dict[str, Any]]:
        engine = self._detect_ocr_name()
        if engine.startswith("paddleocr"):
            return self._run_paddle_ocr(image_path)
        if engine == "tesseract":
            return self._run_tesseract_ocr(image_path)
        return [{"text": "No OCR engine installed. Install PaddleOCR or Tesseract.", "confidence": 0, "x": 0, "y": 0}]

    def _run_paddle_ocr(self, image_path: str) -> list[dict[str, Any]]:
        try:
            result = self._ocr_engine.ocr(image_path, cls=True)
        except Exception:
            result = self._ocr_engine.ocr(image_path)
        if not result:
            return []

        texts: list[dict[str, Any]] = []
        if isinstance(result[0], dict):
            for page in result:
                rec_texts = page.get("rec_texts") or []
                rec_scores = page.get("rec_scores") or []
                rec_boxes = page.get("rec_polys") or page.get("dt_polys") or []
                if rec_boxes:
                    rec_boxes = sorted(
                        rec_boxes,
                        key=lambda pts: (min(float(p[1]) for p in pts), min(float(p[0]) for p in pts)),
                    )
                for index, text in enumerate(rec_texts):
                    points = rec_boxes[index] if index < len(rec_boxes) else []
                    if hasattr(points, "tolist"):
                        points = points.tolist()
                    xs = [p[0] for p in points] if points else [0]
                    ys = [p[1] for p in points] if points else [0]
                    texts.append({
                        "text": str(text),
                        "confidence": float(rec_scores[index]) if index < len(rec_scores) else 0,
                        "x": int(min(xs)),
                        "y": int(min(ys)),
                        "height": int(max(ys) - min(ys)) if ys else 0,
                    })
            return texts

        for line in result[0] or []:
            box, (text, confidence) = line[0], line[1]
            texts.append({
                "text": str(text),
                "confidence": float(confidence),
                "x": int(min(p[0] for p in box)),
                "y": int(min(p[1] for p in box)),
                "height": int(max(p[1] for p in box) - min(p[1] for p in box)),
            })
        return texts

    def _run_tesseract_ocr(self, image_path: str) -> list[dict[str, Any]]:
        text = self._ocr_engine.image_to_string(self._image_cls.open(image_path), lang="chi_sim+eng")
        return [
            {"text": line.strip(), "confidence": 0.5, "x": 0, "y": 0}
            for line in text.splitlines()
            if line.strip()
        ]


def normalize_key(key: str) -> str:
    key = str(key).lower().strip()
    aliases = {
        "return": "enter",
        "esc": "escape",
        "spacebar": "space",
        "cmd": "command",
        "win": "windows",
        "ctrl": "ctrl",
        "control": "ctrl",
        "option": "alt",
    }
    return aliases.get(key, key)


def text_content(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text}]


TOOLS = [
    {
        "name": "status",
        "description": "Check dependencies, selected input backend, OCR engine, and Sky window detection.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "focus_game",
        "description": "Bring the Sky PC window to the foreground.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "press_key",
        "description": "Press a keyboard key in Sky PC. Use WASD for movement, space for jump/fly, F for interaction, Q for honk, Tab for flight mode. Pressing space or enter in menus confirms an option. For rapid key sequences set assume_focused=true to skip the re-focus step.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key name such as w, a, s, d, space, f, q, e, tab, enter, or shift-space."},
                "duration_ms": {"type": "integer", "description": "Hold duration in milliseconds.", "default": 80},
                "backend": {"type": "string", "description": "Input backend override: auto, pydirectinput, or pyautogui.", "default": "auto"},
                "assume_focused": {"type": "boolean", "description": "Set true when the Sky window is already focused to skip the re-focus step (recommended for rapid key sequences).", "default": False},
            },
            "required": ["key"],
        },
    },
    {
        "name": "press_keys",
        "description": "Press a sequence of keys in one call, for rapid repeated presses such as 'fff' (f pressed 3 times). If the string contains spaces it is split by spaces ('f f f', 'space space'); otherwise every character is a separate key. The last press is held longer (last_duration_ms, default 120) than the earlier ones (duration_ms, default 60). Set last_duration_ms equal to duration_ms to make all presses identical.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keys": {"type": "string", "description": "Key sequence: 'fff' presses f 3 times; 'f f f' and 'space space' also work."},
                "interval_ms": {"type": "integer", "description": "Gap between presses in milliseconds.", "default": 80},
                "duration_ms": {"type": "integer", "description": "Hold duration for each press except the last, in milliseconds.", "default": 60},
                "last_duration_ms": {"type": "integer", "description": "Hold duration for the last press, in milliseconds.", "default": 120},
                "assume_focused": {"type": "boolean", "description": "Set true when the Sky window is already focused to skip the re-focus step.", "default": False},
                "backend": {"type": "string", "description": "Input backend override: auto, pydirectinput, or pyautogui.", "default": "auto"},
            },
            "required": ["keys"],
        },
    },
    {
        "name": "teleport_to",
        "description": "One-call teleport flow for the Sky constellation (star map): opens the star map with open_key, finds the friend name on screen via OCR, clicks next to their name (click position = name position + name_offset_x/name_offset_y), finds the teleport button text and clicks it, then presses confirm_key. Returns a step-by-step report with coordinates. Use dry_run=true to only locate coordinates without clicking or confirming.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "friend_name": {"type": "string", "description": "Exact friend name to find on the star map, e.g. A瑜卿."},
                "open_key": {"type": "string", "description": "Key that opens the star map.", "default": "g"},
                "open_delay_ms": {"type": "integer", "description": "Wait after opening the star map and after clicking the friend, in milliseconds.", "default": 800},
                "name_offset_x": {"type": "integer", "description": "Horizontal offset added to the friend name position for the click (star is usually to the right of the name).", "default": 100},
                "name_offset_y": {"type": "integer", "description": "Vertical offset added to the friend name position.", "default": 0},
                "button_text": {"type": "string", "description": "Text of the teleport button to find on the friend page.", "default": "传送"},
                "confirm_key": {"type": "string", "description": "Key pressed to confirm the teleport.", "default": "space"},
                "confirm_delay_ms": {"type": "integer", "description": "Wait after clicking the teleport button before pressing the confirm key. The game needs about 1 second here, otherwise the space is not recognized.", "default": 1000},
                "confirm_repeat": {"type": "integer", "description": "How many times to press the confirm key. The game needs two space presses to confirm.", "default": 2},
                "confirm_interval_ms": {"type": "integer", "description": "Gap between the confirm key presses.", "default": 120},
                "max_attempts": {"type": "integer", "description": "How many times to retry OCR before giving up.", "default": 3},
                "assume_focused": {"type": "boolean", "description": "Set true when the Sky window is already focused to skip the re-focus step.", "default": False},
                "backend": {"type": "string", "description": "Input backend override: auto, pydirectinput, or pyautogui.", "default": "auto"},
                "dry_run": {"type": "boolean", "description": "Set true to only locate the friend and button coordinates without clicking or confirming.", "default": False},
            },
            "required": ["friend_name"],
        },
    },
    {
        "name": "open_chat",
        "description": "Short-tap the Sky chat key. Use this to test whether Enter opens chat; keep duration short so Enter does not become voice/chat-hold behavior.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Chat key, usually enter.", "default": "enter"},
                "duration_ms": {"type": "integer", "description": "Tap duration in milliseconds.", "default": 35},
                "backend": {"type": "string", "description": "Input backend override: auto, pydirectinput, or pyautogui.", "default": "auto"},
            },
        },
    },
    {
        "name": "send_chat",
        "description": "Open Sky chat, paste a message via clipboard, and send it. Long messages are auto-split by sentence (。！？!? or ——), each part sent separately with a 0.5s gap; ？ and ！ are kept at the end of each part. After sending, the server verifies the message appeared on screen and reports 发送成功/发送失败, so you do not need to call read_screen to confirm. Supports Chinese and emoji.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Chat message under 240 characters."},
                "open_key": {"type": "string", "description": "Key used to open chat.", "default": "enter"},
                "open_delay_ms": {"type": "integer", "description": "Delay after opening chat before pasting.", "default": 180},
                "assume_open": {"type": "boolean", "description": "Set true when the chat input is already open; no focus click or open key is sent.", "default": False},
                "send": {"type": "boolean", "description": "Set false to paste without pressing Enter.", "default": True},
                "enter_tap_ms": {"type": "integer", "description": "Short Enter tap duration.", "default": 35},
                "backend": {"type": "string", "description": "Input backend override: auto, pydirectinput, or pyautogui.", "default": "auto"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "type_text",
        "description": "Paste text into an already-open Sky chat input and send. Long messages are auto-split by sentence and sent separately; the server verifies the message appeared on screen and reports 发送成功/发送失败. Use this when the user manually opened the input box.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Chat message under 240 characters."},
                "send": {"type": "boolean", "description": "Set false to paste without pressing Enter.", "default": True},
                "enter_tap_ms": {"type": "integer", "description": "Short Enter tap duration.", "default": 35},
                "backend": {"type": "string", "description": "Input backend override: auto, pydirectinput, or pyautogui.", "default": "auto"},
                "require_foreground": {"type": "boolean", "description": "Require Sky to already be the foreground window before pasting.", "default": True},
            },
            "required": ["message"],
        },
    },
    {
        "name": "read_screen",
        "description": "Screenshot the Sky window and OCR visible text. Returns ONLY the newly detected lines (chat messages; UI noise filtered out) in the 'text' field, and returns 'No new content' when nothing changed. Prefer wait_for_screen_change for monitoring.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "wait_for_screen_change",
        "description": "Wait up to timeout_seconds (default 60) for new content on the Sky screen, such as new chat messages. Polls about every 1 second; each line is confirmed by appearing twice, then all confirmed lines are returned together (in the 'text' field) once the newest content is confirmed. On timeout, returns whatever was already confirmed (marked confirmed=false), or 'No new content for Ns'. Use this to monitor the game in a loop instead of repeatedly calling read_screen.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {"type": "integer", "description": "How long to watch in seconds. Default 60, allowed 1-600.", "default": 60}
            },
        },
    },
    {
        "name": "take_screenshot",
        "description": "Take a screenshot of the Sky window and return it as a PNG image.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "click_at",
        "description": "Click at an absolute screen coordinate (x, y). Use the screen_x / screen_y values returned by read_screen to test clicking UI elements. The Sky window should already be focused (call focus_game first if needed).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "Absolute screen X coordinate (screen_x from read_screen)."},
                "y": {"type": "integer", "description": "Absolute screen Y coordinate (screen_y from read_screen)."},
                "button": {"type": "string", "description": "Mouse button: left, right, or middle.", "default": "left"},
                "clicks": {"type": "integer", "description": "Number of clicks, default 1.", "default": 1},
            },
            "required": ["x", "y"],
        },
    },
]


class McpServer:
    def __init__(self, controller: PcSkyController):
        self.controller = controller

    def handle_tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "status":
            return {"content": text_content(json.dumps(self.controller.status(), ensure_ascii=False, indent=2))}
        if name == "focus_game":
            return {"content": text_content(json.dumps(self.controller.focus_game(), ensure_ascii=False, indent=2))}
        if name == "press_key":
            result = self.controller.press_key(
                arguments["key"],
                arguments.get("duration_ms", 80),
                arguments.get("backend"),
                arguments.get("assume_focused", False),
            )
            return {"content": text_content(result)}
        if name == "press_keys":
            result = self.controller.press_keys(
                arguments["keys"],
                arguments.get("interval_ms", 80),
                arguments.get("duration_ms", 60),
                arguments.get("last_duration_ms", 120),
                arguments.get("backend"),
                arguments.get("assume_focused", False),
            )
            return {"content": text_content(result)}
        if name == "teleport_to":
            result = self.controller.teleport_to(
                arguments["friend_name"],
                arguments.get("open_key", "g"),
                arguments.get("open_delay_ms", 800),
                arguments.get("name_offset_x", 100),
                arguments.get("name_offset_y", 0),
                arguments.get("button_text", "传送"),
                arguments.get("confirm_key", "space"),
                arguments.get("confirm_delay_ms", 1000),
                arguments.get("confirm_repeat", 2),
                arguments.get("confirm_interval_ms", 120),
                arguments.get("max_attempts", 3),
                arguments.get("backend"),
                arguments.get("assume_focused", False),
                arguments.get("dry_run", False),
            )
            return {"content": text_content(result)}
        if name == "open_chat":
            result = self.controller.open_chat(
                arguments.get("key", "enter"),
                arguments.get("duration_ms", 35),
                arguments.get("backend"),
            )
            return {"content": text_content(result)}
        if name == "send_chat":
            result = self.controller.send_chat(
                arguments["message"],
                arguments.get("open_key", "enter"),
                arguments.get("open_delay_ms", 180),
                arguments.get("assume_open", False),
                arguments.get("send", True),
                arguments.get("enter_tap_ms", 35),
                arguments.get("backend"),
            )
            return {"content": text_content(result)}
        if name == "type_text":
            result = self.controller.type_text(
                arguments["message"],
                arguments.get("send", True),
                arguments.get("enter_tap_ms", 35),
                arguments.get("backend"),
                arguments.get("require_foreground", True),
            )
            return {"content": text_content(result)}
        if name == "read_screen":
            result = self.controller.read_screen()
            return {"content": text_content(json.dumps(result, ensure_ascii=False, indent=2))}
        if name == "wait_for_screen_change":
            try:
                timeout = int(arguments.get("timeout_seconds", 60))
            except (TypeError, ValueError):
                timeout = 60
            result = self.controller.wait_for_screen_change(timeout)
            return {"content": text_content(json.dumps(result, ensure_ascii=False, indent=2))}
        if name == "click_at":
            result = self.controller.click_at(
                arguments["x"],
                arguments["y"],
                arguments.get("button", "left"),
                arguments.get("clicks", 1),
            )
            return {"content": text_content(result)}
        if name == "take_screenshot":
            data = self.controller.screenshot_base64()
            return {"content": [{"type": "image", "data": data, "mimeType": "image/png"}]}
        raise SkyError(f"Unknown tool: {name}")

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        msg_id = message.get("id")
        try:
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": SERVER_INFO,
                    },
                }
            if method == "notifications/initialized":
                return None
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
            if method == "tools/call":
                params = message.get("params") or {}
                result = self.handle_tool_call(params.get("name", ""), params.get("arguments") or {})
                result.setdefault("isError", False)
                return {"jsonrpc": "2.0", "id": msg_id, "result": result}
            if method == "ping":
                return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": text_content(f"Error: {exc}"), "isError": True},
            }


def run_stdio(server: McpServer) -> None:
    log("starting stdio transport")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = server.handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def require_authorized(handler: BaseHTTPRequestHandler, config: ServerConfig) -> bool:
    if not config.token:
        return True
    auth = handler.headers.get("Authorization", "")
    expected = f"Bearer {config.token}"
    if auth == expected:
        return True
    handler.send_response(401)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(json.dumps({"error": "missing or invalid bearer token"}).encode("utf-8"))
    return False


def run_http(server: McpServer, config: ServerConfig) -> None:
    if config.host not in LOCAL_HOSTS and not config.token:
        raise SystemExit("Refusing to bind a remote HTTP server without --token.")
    server.controller._detect_ocr_name()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_error(404)
                return
            body = json.dumps({
                "status": "ok",
                "server": SERVER_INFO,
                "ocr": server.controller._detect_ocr_name(),
                "ocr_device": server.controller._ocr_device,
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not require_authorized(self, config):
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                message = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self.send_error(400, "Invalid JSON")
                return
            response = server.handle(message)
            if response is None:
                self.send_response(204)
                self.end_headers()
                return
            payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args):
            log(fmt % args)

    httpd = ThreadingHTTPServer((config.host, config.port), Handler)
    log(f"starting HTTP transport at http://{config.host}:{config.port}")
    if config.token:
        log("HTTP bearer token is enabled")
    httpd.serve_forever()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MCP server for controlling PC Sky.")
    parser.add_argument("--http", nargs="?", const="true", default=None, help="Run HTTP JSON-RPC transport. Optional legacy form: --http 9800.")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host. Use 0.0.0.0 for LAN clients such as Polaris.")
    parser.add_argument("--port", type=int, default=9800, help="HTTP port.")
    parser.add_argument("--token", default=os.environ.get("SKY_MCP_TOKEN"), help="Bearer token for HTTP POST requests.")
    parser.add_argument("--print-token", action="store_true", help="Generate and print a random token, then exit.")
    parser.add_argument("--window-title", default=os.environ.get("SKY_WINDOW_TITLE"), help="Window title substring for Sky.")
    parser.add_argument("--monitor", type=int, default=int(os.environ.get("SKY_MONITOR", "1")), help="Monitor index fallback for screenshots.")
    parser.add_argument("--input-backend", choices=["auto", "pyautogui", "pydirectinput"], default=os.environ.get("SKY_INPUT_BACKEND", "auto"))
    parser.add_argument("--screenshot-scale", type=float, default=float(os.environ.get("SKY_SCREENSHOT_SCALE", "1.0")))
    parser.add_argument("--screenshot-max-width", type=int, default=int(os.environ.get("SKY_SCREENSHOT_MAX_WIDTH", "1920")))
    parser.add_argument("--screenshot-max-height", type=int, default=int(os.environ.get("SKY_SCREENSHOT_MAX_HEIGHT", "1080")))
    args = parser.parse_args()
    if args.http and args.http != "true":
        args.port = int(args.http)
    args.http = bool(args.http)
    return args


def main() -> None:
    args = parse_args()
    if args.print_token:
        print(secrets.token_urlsafe(24))
        return
    config = ServerConfig(
        host=args.host,
        port=args.port,
        token=args.token,
        allow_unsafe_http=False,
        window_title=args.window_title,
        monitor=args.monitor,
        input_backend=args.input_backend,
        screenshot_scale=args.screenshot_scale,
        screenshot_max_width=args.screenshot_max_width,
        screenshot_max_height=args.screenshot_max_height,
    )
    controller = PcSkyController(config)
    server = McpServer(controller)
    if args.http:
        run_http(server, config)
    else:
        run_stdio(server)


if __name__ == "__main__":
    main()
