#!/usr/bin/env python3
"""
Swift - fast app control for Claude on Windows (MCP server, stdio).

Why it is fast:
  * `see` reads a window's UI through Windows UI Automation in ONE cached
    cross-process call and returns a compact numbered text list. No screenshots.
  * `act` runs a whole batch of steps in one call (click, set, type, key,
    wait_for, ...) and returns the fresh UI state at the end.
  * Background by default: Claude works in ONE target app without bringing it
    to the front or taking the real mouse/keyboard, so the user can use other
    apps at the same time.
  * Clicks use UI Automation patterns (Invoke/Toggle/Select) when possible, so
    the real mouse is not moved. A small "Claude" cursor overlay shows where
    Claude is working, next to the user's own cursor.

Safety:
  * Ctrl+Alt+P pauses / resumes all actions.
  * Password fields are never typed into.

Only dependency: comtypes  (pip install comtypes)
"""
import sys
import os
import json
import time
import base64
import zlib
import struct
import threading
import queue

_OUT = sys.stdout.buffer          # protocol channel
sys.stdout = sys.stderr           # anything printed by libraries goes to stderr
sys.coinit_flags = 0              # COM multithreaded, must be set before comtypes import

IS_WIN = sys.platform == "win32"
VERSION = "1.2.0"


def log(*a):
    print("[swift]", *a, file=sys.stderr, flush=True)


class ToolError(Exception):
    pass


# --------------------------------------------------------------------------
# Pure helpers (no Windows needed)
# --------------------------------------------------------------------------
ROLE = {
    50000: "button", 50002: "checkbox", 50003: "combo", 50004: "edit",
    50005: "link", 50006: "image", 50007: "item", 50008: "list", 50009: "menu",
    50010: "menubar", 50011: "menuitem", 50013: "radio", 50015: "slider",
    50016: "spinner", 50018: "tabs", 50019: "tab", 50020: "text",
    50021: "toolbar", 50023: "tree", 50024: "treeitem", 50025: "custom",
    50026: "group", 50029: "row", 50030: "document", 50031: "splitbutton",
    50032: "window", 50033: "pane", 50035: "header", 50037: "titlebar",
}
ROLE_NAMES = set(ROLE.values())
INTERACTIVE = {50000, 50002, 50003, 50004, 50005, 50007, 50011, 50013, 50015,
               50016, 50019, 50024, 50029, 50031}


def png_encode(w, h, rgb):
    stride = w * 3
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 6)) +
            chunk(b"IEND", b""))


def clip(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def format_items(items, limit):
    lines = []
    for it in items[:limit]:
        parts = ["%d %s" % (it["id"], it["role"])]
        if it["name"]:
            parts.append('"%s"' % clip(it["name"], 70))
        if it.get("value"):
            parts.append("= %s" % clip(it["value"], 50))
        if it.get("state"):
            parts.append("[%s]" % it["state"])
        if not it["enabled"]:
            parts.append("(disabled)")
        if it.get("pw"):
            parts.append("(password)")
        parts.append("@%d,%d" % (it["x"], it["y"]))
        lines.append(" ".join(parts))
    if len(items) > limit:
        lines.append("… %d more not shown (use `query` to filter)" % (len(items) - limit))
    return "\n".join(lines)


def match_item(items, target):
    """Find an element by name. 'Save' or 'button:Save'."""
    role, name = None, target
    if ":" in target:
        r, n = target.split(":", 1)
        if r.strip().lower() in ROLE_NAMES:
            role, name = r.strip().lower(), n.strip()
    nl = name.lower()
    cands = [i for i in items if i["name"] and (not role or i["role"] == role)]
    tests = (lambda a: a == nl, lambda a: a.startswith(nl), lambda a: nl in a)
    for test in tests:
        hits = [i for i in cands if test(i["name"].lower())]
        if hits:
            hits.sort(key=lambda i: (i["role"] == "text", not i["enabled"]))
            return hits[0]
    return None


# --------------------------------------------------------------------------
# Windows backend
# --------------------------------------------------------------------------
if IS_WIN:
    import ctypes
    from ctypes import wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    ULONG_PTR = ctypes.c_size_t
    LONG_PTR = ctypes.c_ssize_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                    ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                    ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class _IU(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wt.DWORD), ("u", _IU)]

    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _p(dll, name, res, *args):
        f = getattr(dll, name)
        f.restype = res
        f.argtypes = list(args)
        return f

    GetForegroundWindow = _p(user32, "GetForegroundWindow", wt.HWND)
    SetForegroundWindow = _p(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)
    IsWindow = _p(user32, "IsWindow", wt.BOOL, wt.HWND)
    IsWindowVisible = _p(user32, "IsWindowVisible", wt.BOOL, wt.HWND)
    IsIconic = _p(user32, "IsIconic", wt.BOOL, wt.HWND)
    ShowWindow = _p(user32, "ShowWindow", wt.BOOL, wt.HWND, ctypes.c_int)
    GetWindowTextLengthW = _p(user32, "GetWindowTextLengthW", ctypes.c_int, wt.HWND)
    GetWindowTextW = _p(user32, "GetWindowTextW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
    GetWindowThreadProcessId = _p(user32, "GetWindowThreadProcessId", wt.DWORD, wt.HWND,
                                  ctypes.POINTER(wt.DWORD))
    GetWindowRect = _p(user32, "GetWindowRect", wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT))
    EnumWindows = _p(user32, "EnumWindows", wt.BOOL, WNDENUMPROC, wt.LPARAM)
    SetCursorPos = _p(user32, "SetCursorPos", wt.BOOL, ctypes.c_int, ctypes.c_int)
    GetCursorPos = _p(user32, "GetCursorPos", wt.BOOL, ctypes.POINTER(wt.POINT))
    SendInput = _p(user32, "SendInput", wt.UINT, wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    VkKeyScanW = _p(user32, "VkKeyScanW", ctypes.c_short, wt.WCHAR)
    keybd_event = _p(user32, "keybd_event", None, wt.BYTE, wt.BYTE, wt.DWORD, ULONG_PTR)
    GetDC = _p(user32, "GetDC", wt.HDC, wt.HWND)
    ReleaseDC = _p(user32, "ReleaseDC", ctypes.c_int, wt.HWND, wt.HDC)
    GetSystemMetrics = _p(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
    RegisterHotKey = _p(user32, "RegisterHotKey", wt.BOOL, wt.HWND, ctypes.c_int, wt.UINT, wt.UINT)
    GetMessageW = _p(user32, "GetMessageW", ctypes.c_int, ctypes.POINTER(wt.MSG), wt.HWND,
                     wt.UINT, wt.UINT)
    GetParent = _p(user32, "GetParent", wt.HWND, wt.HWND)
    GetWindowLongW = _p(user32, "GetWindowLongW", wt.LONG, wt.HWND, ctypes.c_int)
    SetWindowLongW = _p(user32, "SetWindowLongW", wt.LONG, wt.HWND, ctypes.c_int, wt.LONG)
    OpenProcess = _p(kernel32, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
    QueryFullProcessImageNameW = _p(kernel32, "QueryFullProcessImageNameW", wt.BOOL, wt.HANDLE,
                                    wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD))
    CloseHandle = _p(kernel32, "CloseHandle", wt.BOOL, wt.HANDLE)
    CreateCompatibleDC = _p(gdi32, "CreateCompatibleDC", wt.HDC, wt.HDC)
    CreateCompatibleBitmap = _p(gdi32, "CreateCompatibleBitmap", wt.HBITMAP, wt.HDC,
                                ctypes.c_int, ctypes.c_int)
    SelectObject = _p(gdi32, "SelectObject", wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
    SetStretchBltMode = _p(gdi32, "SetStretchBltMode", ctypes.c_int, wt.HDC, ctypes.c_int)
    StretchBlt = _p(gdi32, "StretchBlt", wt.BOOL, wt.HDC, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int, wt.HDC, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int, wt.DWORD)
    GetDIBits = _p(gdi32, "GetDIBits", ctypes.c_int, wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT,
                   ctypes.c_void_p, ctypes.c_void_p, wt.UINT)
    DeleteObject = _p(gdi32, "DeleteObject", wt.BOOL, wt.HGDIOBJ)
    DeleteDC = _p(gdi32, "DeleteDC", wt.BOOL, wt.HDC)
    PostMessageW = _p(user32, "PostMessageW", wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    SendMessageTimeoutW = _p(user32, "SendMessageTimeoutW", LONG_PTR, wt.HWND, wt.UINT, wt.WPARAM,
                             ctypes.c_wchar_p, wt.UINT, wt.UINT, ctypes.POINTER(ULONG_PTR))
    ScreenToClient = _p(user32, "ScreenToClient", wt.BOOL, wt.HWND, ctypes.POINTER(wt.POINT))
    RealChildWindowFromPoint = _p(user32, "RealChildWindowFromPoint", wt.HWND, wt.HWND, wt.POINT)
    WindowFromPoint = _p(user32, "WindowFromPoint", wt.HWND, wt.POINT)
    GetAncestor = _p(user32, "GetAncestor", wt.HWND, wt.HWND, wt.UINT)
    SetWindowPos = _p(user32, "SetWindowPos", wt.BOOL, wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                      ctypes.c_int, ctypes.c_int, wt.UINT)
    PrintWindow = _p(user32, "PrintWindow", wt.BOOL, wt.HWND, wt.HDC, wt.UINT)

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND),
                    ("hwndFocus", wt.HWND), ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
                    ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]

    GetGUIThreadInfo = _p(user32, "GetGUIThreadInfo", wt.BOOL, wt.DWORD,
                          ctypes.POINTER(GUITHREADINFO))

    # ---------------- state ----------------
    class S:
        target = None        # hwnd Claude is working in
        items = {}           # id -> record from the latest snapshot
        paused = False
        fg = False           # True only while an `act` call asked for foreground mode
        uia = None
        UIA = None
        cache = None
        overlay = None

    # ---------------- UI Automation ----------------
    P_ENABLED, P_OFFSCREEN, P_PASSWORD = 30010, 30022, 30019
    P_VALUE, P_VALUE_RO, P_TOGGLE = 30045, 30046, 30086
    P_IS_INVOKE, P_IS_VALUE, P_IS_TOGGLE, P_IS_SELITEM, P_IS_EXPAND = 30031, 30043, 30041, 30036, 30028
    P_SELECTED, P_EXPANDED = 30079, 30070
    CACHE_PROPS = [30001, 30003, 30005, 30020, P_ENABLED, P_OFFSCREEN, P_PASSWORD, P_VALUE,
                   P_VALUE_RO, P_TOGGLE, P_IS_INVOKE, P_IS_VALUE, P_IS_TOGGLE,
                   P_IS_SELITEM, P_IS_EXPAND, P_SELECTED, P_EXPANDED]
    PAT_INVOKE, PAT_VALUE, PAT_EXPAND, PAT_SELITEM, PAT_TOGGLE = 10000, 10002, 10005, 10010, 10015

    def uia():
        if S.uia is None:
            try:
                import comtypes
                import comtypes.client
            except ImportError:
                raise ToolError("The 'comtypes' package is missing. Run install.bat "
                                "(or: python -m pip install comtypes) and restart Claude.")
            S.UIA = comtypes.client.GetModule("UIAutomationCore.dll")
            S.uia = comtypes.client.CreateObject(
                "{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=S.UIA.IUIAutomation)
            cr = S.uia.CreateCacheRequest()
            for p in CACHE_PROPS:
                cr.AddProperty(p)
            S.cache = cr
        return S.uia

    def _cp(el, pid, default=None):
        try:
            v = el.GetCachedPropertyValue(pid)
            return default if v is None else v
        except Exception:
            return default

    # ---------------- windows ----------------
    def win_title(hw):
        n = GetWindowTextLengthW(hw)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        GetWindowTextW(hw, buf, n + 1)
        return buf.value

    def win_pid(hw):
        pid = wt.DWORD()
        GetWindowThreadProcessId(hw, ctypes.byref(pid))
        return pid.value

    _proc_cache = {}

    def proc_name(pid):
        if pid in _proc_cache:
            return _proc_cache[pid]
        name = ""
        h = OpenProcess(0x1000, False, pid)
        if h:
            buf = ctypes.create_unicode_buffer(520)
            size = wt.DWORD(520)
            if QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value)
            CloseHandle(h)
        _proc_cache[pid] = name
        return name

    def win_rect(hw):
        r = wt.RECT()
        GetWindowRect(hw, ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom

    def top_windows(titled=True):
        out = []

        def cb(hw, _):
            if IsWindowVisible(hw):
                t = win_title(hw)
                if t or not titled:
                    l, tp, r, b = win_rect(hw)
                    if r - l > 1 and b - tp > 1:
                        out.append((hw, t, win_pid(hw)))
            return True

        EnumWindows(WNDENUMPROC(cb), 0)
        return out

    def find_window(name):
        nl = name.lower().strip()
        wins = [w for w in top_windows() if w[1] != "Claude cursor"]
        for test in (lambda t, p: t.lower() == nl or os.path.splitext(p.lower())[0] == nl,
                     lambda t, p: nl in t.lower(),
                     lambda t, p: nl in p.lower()):
            for hw, t, pid in wins:
                if test(t, proc_name(pid)):
                    return hw
        return None

    def focus_window(hw):
        if IsIconic(hw):
            ShowWindow(hw, 9)
        if GetForegroundWindow() != hw:
            SetForegroundWindow(hw)
            if GetForegroundWindow() != hw:
                keybd_event(0x12, 0, 0, 0)
                keybd_event(0x12, 0, 2, 0)
                SetForegroundWindow(hw)
            time.sleep(0.06)
        S.target = hw

    def select_window(hw):
        """Background mode: work in this window without bringing it to the front."""
        if IsIconic(hw):
            ShowWindow(hw, 4)                                   # restore, no activation
            SetWindowPos(hw, wt.HWND(1), 0, 0, 0, 0, 0x0013)    # keep it behind the user's app
            time.sleep(0.25)
        S.target = hw

    def use_window(hw):
        if S.fg:
            focus_window(hw)
        else:
            select_window(hw)

    NEEDS_FG = ('This step cannot be done in the background for this app (it needs the real '
                'mouse/keyboard). Find another way (menus, buttons, `set`), or retry with '
                '"foreground": true, which brings the app to the front and interrupts the user; '
                'ask them first.')

    def need_fg():
        if not S.fg:
            raise ToolError(NEEDS_FG)

    def target():
        if S.target and IsWindow(S.target):
            return S.target
        S.target = None
        hw = GetForegroundWindow()
        if not hw:
            raise ToolError("No window is active. Use the `windows` tool, then focus one.")
        return hw

    def is_covered(x, y):
        top = GetAncestor(WindowFromPoint(wt.POINT(int(x), int(y))), 2)
        return bool(top) and win_pid(top) != win_pid(target())

    def ensure_front():
        hw = target()
        if GetForegroundWindow() != hw and win_pid(GetForegroundWindow()) != win_pid(hw):
            focus_window(hw)
        return hw

    # ---------------- snapshot ----------------
    def snapshot(query=None, include_text=True):
        a = uia()
        hw = target()
        pid = win_pid(hw)
        hwnds = [hw] + [w[0] for w in top_windows(titled=False)
                        if w[2] == pid and w[0] != hw][:4]   # menus, dialogs, popups
        items, seen_prev = [], None
        ql = query.lower() if query else None
        n = 0
        for h in hwnds:
            try:
                root = a.ElementFromHandle(h)
                arr = root.FindAllBuildCache(4, a.ControlViewCondition, S.cache)
                count = arr.Length
            except Exception:
                continue
            wl, wtp, wr, wb = win_rect(h)
            for i in range(count):
                try:
                    el = arr.GetElement(i)
                    ct = el.CachedControlType
                    name = el.CachedName or ""
                    r = el.CachedBoundingRectangle
                except Exception:
                    continue
                if r.right - r.left < 2 or r.bottom - r.top < 2:
                    continue
                if _cp(el, P_OFFSCREEN, False):
                    continue
                x, y = (r.left + r.right) // 2, (r.top + r.bottom) // 2
                if not (wl - 4 <= x <= wr + 4 and wtp - 4 <= y <= wb + 4):
                    continue
                pats = set()
                if _cp(el, P_IS_INVOKE, False): pats.add("invoke")
                if _cp(el, P_IS_TOGGLE, False): pats.add("toggle")
                if _cp(el, P_IS_SELITEM, False): pats.add("select")
                if _cp(el, P_IS_EXPAND, False): pats.add("expand")
                if _cp(el, P_IS_VALUE, False) and not _cp(el, P_VALUE_RO, True): pats.add("value")
                is_text = ct in (50020, 50030)
                if not (ct in INTERACTIVE or pats or (include_text and is_text and name)):
                    continue
                if is_text and name == seen_prev:
                    continue            # label that just repeats its parent control
                if ql and ql not in name.lower():
                    continue
                state = None
                if "toggle" in pats:
                    state = "on" if _cp(el, P_TOGGLE, 0) == 1 else "off"
                elif "select" in pats and _cp(el, P_SELECTED, False):
                    state = "selected"
                elif "expand" in pats and ct in (50003, 50011, 50024, 50031):
                    state = "open" if _cp(el, P_EXPANDED, 0) == 1 else None
                pw = bool(_cp(el, P_PASSWORD, False))
                val = "" if pw else (_cp(el, P_VALUE, "") if ct in (50004, 50003, 50016, 50015) else "")
                n += 1
                items.append({"id": n, "el": el, "role": ROLE.get(ct, "control"), "name": name,
                              "x": x, "y": y, "enabled": bool(_cp(el, P_ENABLED, True)),
                              "pw": pw, "pats": pats, "value": val, "state": state, "ct": ct,
                              "hwnd": h, "nh": _cp(el, 30020, 0) or 0})
                seen_prev = name
        S.items = {it["id"]: it for it in items}
        l, t, r, b = win_rect(hw)
        header = 'window "%s" (%s) at %d,%d size %dx%d' % (
            clip(win_title(hw), 60), proc_name(pid), l, t, r - l, b - t)
        return header, items

    def see_text(query=None, include_text=True, limit=300):
        t0 = time.perf_counter()
        header, items = snapshot(query, include_text)
        ms = (time.perf_counter() - t0) * 1000
        body = format_items(items, limit) or (
            "(no readable elements: this app does not expose its UI. "
            "Use `screenshot` and click with coordinates.)")
        return "%s  [%d elements, %.0f ms]\n%s" % (header, len(items), ms, body)

    def resolve(tgt, timeout=2.0):
        """int id from the last `see`, a name, 'role:name', or [x, y]."""
        if isinstance(tgt, (list, tuple)) and len(tgt) == 2:
            return {"id": 0, "el": None, "role": "point", "name": "", "x": int(tgt[0]),
                    "y": int(tgt[1]), "enabled": True, "pw": False, "pats": set(), "ct": 0,
                    "hwnd": None, "nh": 0}
        if isinstance(tgt, bool):
            raise ToolError("bad target")
        if isinstance(tgt, int) or (isinstance(tgt, str) and tgt.isdigit()):
            it = S.items.get(int(tgt))
            if not it:
                raise ToolError("No element with id %s in the latest snapshot." % tgt)
            return it
        if not isinstance(tgt, str) or not tgt:
            raise ToolError("Target must be an id, a name, or [x, y].")
        end = time.time() + timeout
        while True:
            _, items = snapshot()
            it = match_item(items, tgt)
            if it:
                return it
            if time.time() >= end:
                raise ToolError('Could not find "%s" on screen.' % tgt)
            time.sleep(0.15)

    # ---------------- input ----------------
    def _send(inputs):
        n = len(inputs)
        if n:
            SendInput(n, (INPUT * n)(*inputs), ctypes.sizeof(INPUT))

    def _k(vk=0, scan=0, flags=0):
        i = INPUT()
        i.type = 1
        i.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        return i

    def _m(flags, data=0):
        i = INPUT()
        i.type = 0
        i.mi = MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
        return i

    VK = {"enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
          "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "space": 0x20, "up": 0x26,
          "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
          "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D, "ctrl": 0x11, "control": 0x11,
          "shift": 0x10, "alt": 0x12, "win": 0x5B, "cmd": 0x5B}
    for _i in range(1, 13):
        VK["f%d" % _i] = 0x6F + _i
    EXTENDED = {0x26, 0x28, 0x25, 0x27, 0x24, 0x23, 0x21, 0x22, 0x2D, 0x2E, 0x5B}

    def _vk(name):
        n = name.lower()
        if n in VK:
            return VK[n]
        if len(name) == 1:
            if name.isalnum():
                return ord(name.upper())
            v = VkKeyScanW(name)
            if v != -1:
                return v & 0xFF
        raise ToolError('Unknown key "%s".' % name)

    def press(combo):
        keys = [_vk(k.strip()) for k in combo.split("+") if k.strip()]
        down = [_k(v, 0, 1 if v in EXTENDED else 0) for v in keys]
        up = [_k(v, 0, 2 | (1 if v in EXTENDED else 0)) for v in reversed(keys)]
        _send(down + up)
        time.sleep(0.03)

    def type_text(text):
        buf = []
        for ch in text:
            if ch == "\n":
                buf += [_k(0x0D), _k(0x0D, 0, 2)]
            elif ch == "\t":
                buf += [_k(0x09), _k(0x09, 0, 2)]
            elif ch != "\r":
                b = ch.encode("utf-16-le")
                for unit in struct.unpack("<%dH" % (len(b) // 2), b):
                    buf += [_k(0, unit, 4), _k(0, unit, 4 | 2)]
            if len(buf) >= 200:
                _send(buf)
                buf = []
                time.sleep(0.01)
        _send(buf)
        time.sleep(0.02)

    def mouse_click(x, y, button="left", double=False):
        ensure_front()
        old = wt.POINT()
        GetCursorPos(ctypes.byref(old))
        SetCursorPos(int(x), int(y))
        time.sleep(0.02)
        dn, up = (0x0008, 0x0010) if button == "right" else (0x0002, 0x0004)
        _send([_m(dn), _m(up)])
        if double:
            time.sleep(0.05)
            _send([_m(dn), _m(up)])
        time.sleep(0.03)
        SetCursorPos(old.x, old.y)      # give the user's cursor back

    def wheel(x, y, notches):
        ensure_front()
        old = wt.POINT()
        GetCursorPos(ctypes.byref(old))
        SetCursorPos(int(x), int(y))
        time.sleep(0.02)
        _send([_m(0x0800, int(notches) * 120)])
        time.sleep(0.03)
        SetCursorPos(old.x, old.y)

    # ---------------- background input (posted messages, no focus, no real mouse) ----------------
    def deep_child(hw, sx, sy):
        cur = hw
        for _ in range(12):
            pt = wt.POINT(int(sx), int(sy))
            ScreenToClient(cur, ctypes.byref(pt))
            ch = RealChildWindowFromPoint(cur, pt)
            if not ch or ch == cur:
                break
            cur = ch
        pt = wt.POINT(int(sx), int(sy))
        ScreenToClient(cur, ctypes.byref(pt))
        return cur, pt.x, pt.y

    def _lp(x, y):
        return ((y & 0xFFFF) << 16) | (x & 0xFFFF)

    def posted_click(hw, x, y, button="left", double=False):
        ch, cx, cy = deep_child(hw or target(), x, y)
        lp = _lp(cx, cy)
        dn, up, dbl, mk = (0x0204, 0x0205, 0x0206, 2) if button == "right" else (0x0201, 0x0202, 0x0203, 1)
        PostMessageW(ch, 0x0200, 0, lp)
        PostMessageW(ch, dn, mk, lp)
        PostMessageW(ch, up, 0, lp)
        if double:
            PostMessageW(ch, dbl, mk, lp)
            PostMessageW(ch, up, 0, lp)
        time.sleep(0.05)

    def posted_wheel(hw, x, y, notches):
        ch, _, _ = deep_child(hw or target(), x, y)
        PostMessageW(ch, 0x020A, ((int(notches) * 120) & 0xFFFF) << 16, _lp(int(x), int(y)))
        time.sleep(0.05)

    def focus_hwnd():
        hw = target()
        gi = GUITHREADINFO()
        gi.cbSize = ctypes.sizeof(GUITHREADINFO)
        tid = GetWindowThreadProcessId(hw, None)
        if GetGUIThreadInfo(tid, ctypes.byref(gi)) and gi.hwndFocus:
            return gi.hwndFocus
        return hw

    def posted_text(h, text):
        for ch in text:
            if ch == "\n":
                PostMessageW(h, 0x0100, 0x0D, 0x001C0001)
                PostMessageW(h, 0x0101, 0x0D, 0xC01C0001 - (1 << 32))
            elif ch != "\r":
                b = ch.encode("utf-16-le")
                for unit in struct.unpack("<%dH" % (len(b) // 2), b):
                    PostMessageW(h, 0x0102, unit, 1)
        time.sleep(0.05)

    def posted_key(h, combo):
        keys = [k.strip() for k in combo.split("+") if k.strip()]
        if len(keys) != 1 or keys[0].lower() in ("ctrl", "control", "shift", "alt", "win", "cmd"):
            need_fg()
        vk = _vk(keys[0])
        ext = (1 << 24) if vk in EXTENDED else 0
        PostMessageW(h, 0x0100, vk, 1 | ext)
        PostMessageW(h, 0x0101, vk, (0xC0000001 | ext) - (1 << 32))
        time.sleep(0.03)

    # ---------------- element actions ----------------
    def _pattern(el, pid, iface):
        try:
            p = el.GetCurrentPattern(pid)
            if not p:
                return None
            return p.QueryInterface(getattr(S.UIA, iface))
        except Exception:
            return None

    def _run_bounded(fn, timeout=1.5):
        """Invoke can block while a modal dialog is open; don't wait for it."""
        box = {}

        def run():
            try:
                import comtypes
                comtypes.CoInitializeEx(0)
            except Exception:
                pass
            try:
                fn()
            except Exception as e:
                box["err"] = e

        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(timeout)
        if "err" in box:
            raise box["err"]
        return not th.is_alive()

    def point_cursor(it):
        if S.overlay and (S.fg or not is_covered(it["x"], it["y"])):
            S.overlay.move(it["x"], it["y"])

    def do_click(it, button="left", double=False, mouse=False):
        point_cursor(it)
        if not it["enabled"]:
            raise ToolError('"%s" is disabled.' % it["name"])
        el = it["el"]
        if el is not None and not mouse and not double and button == "left":
            pats, ct = it["pats"], it["ct"]
            try:
                if "invoke" in pats:
                    p = _pattern(el, PAT_INVOKE, "IUIAutomationInvokePattern")
                    if p:
                        _run_bounded(p.Invoke)
                        return "invoked"
                if "toggle" in pats:
                    p = _pattern(el, PAT_TOGGLE, "IUIAutomationTogglePattern")
                    if p:
                        _run_bounded(p.Toggle)
                        return "toggled"
                if "expand" in pats and ct in (50003, 50011, 50031):
                    p = _pattern(el, PAT_EXPAND, "IUIAutomationExpandCollapsePattern")
                    if p:
                        _run_bounded(p.Collapse if it.get("state") == "open" else p.Expand)
                        return "expanded"
                if "select" in pats:
                    p = _pattern(el, PAT_SELITEM, "IUIAutomationSelectionItemPattern")
                    if p:
                        _run_bounded(p.Select)
                        return "selected"
            except ToolError:
                raise
            except Exception:
                pass                     # fall through to a click
        if S.fg:
            mouse_click(it["x"], it["y"], button, double)
            return "clicked"
        if mouse:
            need_fg()
        posted_click(it["hwnd"], it["x"], it["y"], button, double)
        return "clicked in background (check the result)"

    def do_set(it, text):
        point_cursor(it)
        if it["pw"]:
            raise ToolError("That is a password field. Ask the user to type it themselves.")
        el = it["el"]
        if el is not None and "value" in it["pats"]:
            p = _pattern(el, PAT_VALUE, "IUIAutomationValuePattern")
            if p:
                try:
                    p.SetValue(text)
                    return "set"
                except Exception:
                    pass
        if not S.fg:
            if it["nh"] and it["nh"] != it["hwnd"]:
                res = ULONG_PTR()
                if SendMessageTimeoutW(it["nh"], 0x000C, 0, text, 0x0002, 1500, ctypes.byref(res)):
                    return "set"
            need_fg()
        mouse_click(it["x"], it["y"])
        press("ctrl+a")
        type_text(text)
        return "typed"

    def do_focus_el(it):
        point_cursor(it)
        if it["pw"]:
            raise ToolError("That is a password field. Ask the user to type it themselves.")
        ensure_front()
        try:
            if it["el"] is None:
                raise RuntimeError()
            it["el"].SetFocus()
        except Exception:
            mouse_click(it["x"], it["y"])

    def open_app(name):
        hw = find_window(name)
        if hw:
            use_window(hw)
            return "using existing window"
        before = {w[0] for w in top_windows()}
        user_fg = GetForegroundWindow()
        how = "launched"
        try:
            try:
                os.startfile(name, show_cmd=(1 if S.fg else 4))
            except TypeError:
                os.startfile(name)
        except OSError:
            if not S.fg:
                raise ToolError('Could not launch "%s" directly. Give a full path, or retry with '
                                '"foreground": true to use Start search (interrupts the user).' % name)
            press("win")
            time.sleep(0.6)
            type_text(name)
            time.sleep(0.9)
            press("enter")
            how = "launched from Start search"
        end = time.time() + 10
        while time.time() < end:
            time.sleep(0.25)
            hw = find_window(name)
            if not hw:
                new = [w[0] for w in top_windows()
                       if w[0] not in before and w[1] != "Claude cursor"]
                hw = new[0] if new else None
            if hw:
                time.sleep(0.3)
                if S.fg:
                    focus_window(hw)
                else:
                    S.target = hw
                    if user_fg and GetForegroundWindow() != user_fg and IsWindow(user_fg):
                        SetWindowPos(hw, wt.HWND(1), 0, 0, 0, 0, 0x0013)
                        SetForegroundWindow(user_fg)      # hand focus back to the user
                    how += " in the background"
                return how
        raise ToolError('Started "%s" but no window appeared within 10 s.' % name)

    # ---------------- batch runner ----------------
    def run_step(st):
        if not isinstance(st, dict):
            raise ToolError("Each step must be an object.")
        if "open" in st:
            return open_app(str(st["open"]))
        if "focus" in st:
            hw = find_window(str(st["focus"]))
            if not hw:
                raise ToolError('No open window matches "%s".' % st["focus"])
            use_window(hw)
            return "selected"
        if "wait" in st:
            time.sleep(min(float(st["wait"]), 15000) / 1000.0)
            return "waited"
        if "wait_for" in st:
            resolve(str(st["wait_for"]), timeout=min(float(st.get("timeout", 5000)), 30000) / 1000.0)
            return "appeared"
        if "click" in st:
            return do_click(resolve(st["click"]), st.get("button", "left"),
                            bool(st.get("double")), bool(st.get("mouse")))
        if "set" in st:
            return do_set(resolve(st["set"]), str(st.get("text", "")))
        if "type" in st:
            text = str(st["type"])
            it = resolve(st["into"]) if "into" in st else None
            if S.fg:
                if it:
                    do_focus_el(it)
                else:
                    ensure_front()
                type_text(text)
                return "typed"
            if it:
                point_cursor(it)
                if it["pw"]:
                    raise ToolError("That is a password field. Ask the user to type it themselves.")
                if "value" in it["pats"] and "\n" not in text and "\t" not in text:
                    p = _pattern(it["el"], PAT_VALUE, "IUIAutomationValuePattern")
                    if p:
                        try:
                            p.SetValue((it.get("value") or "") + text)
                            return "typed"
                        except Exception:
                            pass
                h = it["nh"] if it["nh"] and it["nh"] != it["hwnd"] else None
                if not h:
                    posted_click(it["hwnd"], it["x"], it["y"])
                    h = focus_hwnd()
            else:
                h = focus_hwnd()
            posted_text(h, text)
            return "typed in background (check the result)"
        if "key" in st:
            keys = st["key"] if isinstance(st["key"], list) else str(st["key"]).split()
            if S.fg:
                ensure_front()
                for combo in keys:
                    press(combo)
                return "pressed"
            h = focus_hwnd()
            for combo in keys:
                posted_key(h, combo)
            return "pressed in background (check the result)"
        if "scroll" in st:
            if "at" in st:
                it = resolve(st["at"])
                x, y = it["x"], it["y"]
            else:
                l, t, r, b = win_rect(target())
                x, y = (l + r) // 2, (t + b) // 2
            if S.fg:
                wheel(x, y, int(st["scroll"]))
            else:
                posted_wheel(None, x, y, int(st["scroll"]))
            return "scrolled"
        raise ToolError("Unknown step: %s" % json.dumps(st)[:120])

    def act(steps, see_after=True, foreground=False):
        S.fg = bool(foreground)
        if S.paused:
            raise ToolError("Paused by the user (Ctrl+Alt+P resumes). Nothing was done.")
        t0 = time.perf_counter()
        out, failed = [], False
        for i, st in enumerate(steps, 1):
            if S.paused:
                out.append("%d. STOPPED: paused by the user (Ctrl+Alt+P)" % i)
                failed = True
                break
            try:
                out.append("%d. %s" % (i, run_step(st)))
            except ToolError as e:
                out.append("%d. FAILED: %s  (remaining steps skipped)" % (i, e))
                failed = True
                break
            except Exception as e:
                out.append("%d. FAILED: %s: %s  (remaining steps skipped)" % (i, type(e).__name__, e))
                failed = True
                break
        ms = (time.perf_counter() - t0) * 1000
        text = "\n".join(out) + "\n[%d steps, %.0f ms]" % (len(out), ms)
        if see_after:
            time.sleep(0.12)
            S.fg = False
            try:
                text += "\n\n" + see_text()
            except Exception as e:
                text += "\n\n(could not read the window: %s)" % e
        S.fg = False
        return text, failed

    # ---------------- screenshot ----------------
    def screenshot(whole_screen=False, max_width=1280):
        hw = None
        if whole_screen:
            l, t = GetSystemMetrics(76), GetSystemMetrics(77)
            w, h = GetSystemMetrics(78), GetSystemMetrics(79)
        else:
            hw = target()
            l, t, r, b = win_rect(hw)
            w, h = r - l, b - t
        if w <= 0 or h <= 0:
            raise ToolError("Window has no visible area.")
        scale = min(1.0, float(max_width) / w)
        dw, dh = max(1, int(w * scale)), max(1, int(h * scale))
        sdc = GetDC(None)
        mdc = CreateCompatibleDC(sdc)
        bmp = CreateCompatibleBitmap(sdc, dw, dh)
        old = SelectObject(mdc, bmp)
        SetStretchBltMode(mdc, 4)
        if hw:                               # PrintWindow works even if the window is covered
            fdc = CreateCompatibleDC(sdc)
            fbmp = CreateCompatibleBitmap(sdc, w, h)
            fold = SelectObject(fdc, fbmp)
            if PrintWindow(hw, fdc, 2):
                StretchBlt(mdc, 0, 0, dw, dh, fdc, 0, 0, w, h, 0x00CC0020)
            else:
                StretchBlt(mdc, 0, 0, dw, dh, sdc, l, t, w, h, 0x00CC0020 | 0x40000000)
            SelectObject(fdc, fold)
            DeleteObject(fbmp)
            DeleteDC(fdc)
        else:
            StretchBlt(mdc, 0, 0, dw, dh, sdc, l, t, w, h, 0x00CC0020 | 0x40000000)
        SelectObject(mdc, old)
        bmi = ctypes.create_string_buffer(
            struct.pack("<IiiHHIIiiII", 40, dw, -dh, 1, 32, 0, 0, 0, 0, 0, 0) + b"\0" * 12)
        buf = ctypes.create_string_buffer(dw * dh * 4)
        GetDIBits(sdc, bmp, 0, dh, buf, bmi, 0)
        DeleteObject(bmp)
        DeleteDC(mdc)
        ReleaseDC(None, sdc)
        raw = buf.raw
        rgb = bytearray(dw * dh * 3)
        rgb[0::3] = raw[2::4]
        rgb[1::3] = raw[1::4]
        rgb[2::3] = raw[0::4]
        note = ("Screenshot %dx%d. To click a point seen at image pixel (px, py) use "
                "{\"click\": [%d + px * %.4f, %d + py * %.4f]}." % (dw, dh, l, 1 / scale, t, 1 / scale))
        return png_encode(dw, dh, rgb), note

    # ---------------- Claude cursor overlay ----------------
    class Overlay(threading.Thread):
        def __init__(self):
            super().__init__(daemon=True)
            self.q = queue.Queue()
            self.start()

        def move(self, x, y):
            self.q.put(("move", int(x), int(y)))

        def label(self, text):
            self.q.put(("label", text))

        def run(self):
            try:
                import tkinter as tk
                KEY, ORANGE = "#010203", "#D97757"
                root = tk.Tk()
                root.title("Claude cursor")
                root.overrideredirect(True)
                root.attributes("-topmost", True)
                root.config(bg=KEY)
                root.attributes("-transparentcolor", KEY)
                W, H = 118, 58
                c = tk.Canvas(root, width=W, height=H, bg=KEY, highlightthickness=0)
                c.pack()
                c.create_polygon(2, 2, 9, 24, 13, 15, 23, 11, fill=ORANGE, outline="white", width=2)
                x0, y0, x1, y1, r = 20, 24, 112, 52, 9
                pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
                       x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
                c.create_polygon(pts, smooth=True, fill=ORANGE, outline="")
                txt = c.create_text((x0 + x1) // 2, (y0 + y1) // 2, text="Claude", fill="white",
                                    font=("Segoe UI", 10, "bold"))
                st = {"x": root.winfo_screenwidth() // 2, "y": root.winfo_screenheight() // 2,
                      "sx": 0, "sy": 0, "tx": 0, "ty": 0, "t0": 0.0, "moving": False,
                      "last": 0.0, "shown": False}
                root.geometry("%dx%d+%d+%d" % (W, H, st["x"], st["y"]))
                root.update_idletasks()
                try:
                    hw = GetParent(root.winfo_id()) or root.winfo_id()
                    ex = GetWindowLongW(hw, -20)
                    SetWindowLongW(hw, -20, ex | 0x80000 | 0x20 | 0x08000000 | 0x80)
                except Exception:
                    pass
                root.withdraw()

                def tick():
                    try:
                        while True:
                            cmd = self.q.get_nowait()
                            if cmd[0] == "move":
                                st.update(sx=st["x"], sy=st["y"], tx=cmd[1], ty=cmd[2],
                                          t0=time.time(), moving=True, last=time.time())
                                if not st["shown"]:
                                    root.deiconify()
                                    root.attributes("-topmost", True)
                                    st["shown"] = True
                            elif cmd[0] == "label":
                                c.itemconfig(txt, text=cmd[1])
                                st["last"] = time.time()
                                if not st["shown"]:
                                    root.deiconify()
                                    st["shown"] = True
                    except queue.Empty:
                        pass
                    if st["moving"]:
                        f = min(1.0, (time.time() - st["t0"]) / 0.14)
                        e = 1 - (1 - f) ** 3
                        st["x"] = int(st["sx"] + (st["tx"] - st["sx"]) * e)
                        st["y"] = int(st["sy"] + (st["ty"] - st["sy"]) * e)
                        root.geometry("+%d+%d" % (st["x"] - 2, st["y"] - 2))
                        if f >= 1.0:
                            st["moving"] = False
                    elif st["shown"] and time.time() - st["last"] > 6 and not S.paused:
                        root.withdraw()
                        st["shown"] = False
                    root.after(10, tick)

                tick()
                root.mainloop()
            except Exception as e:
                log("cursor overlay disabled:", e)

    def hotkey_thread():
        # Ctrl+Alt+P toggles pause
        if not RegisterHotKey(None, 1, 0x0002 | 0x0001 | 0x4000, ord("P")):
            log("could not register Ctrl+Alt+P (already used by another app)")
            return
        msg = wt.MSG()
        while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:
                S.paused = not S.paused
                log("paused" if S.paused else "resumed")
                if S.overlay:
                    S.overlay.label("Paused" if S.paused else "Claude")

    def startup():
        if os.environ.get("SWIFT_CURSOR", "1") != "0":
            S.overlay = Overlay()
        threading.Thread(target=hotkey_thread, daemon=True).start()


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------
TARGET_DOC = ("A target is an element id from the latest `see` (number), an element name "
              "like \"Save\" or \"button:Save\" (looked up live, waits up to 2 s for it to "
              "appear, so it works for menus and dialogs opened by an earlier step), or "
              "[x, y] screen coordinates.")

WORKFLOW = 'WORKFLOW FOR EVERY TASK: (1) Call `look` once on the target app: it returns a screenshot plus the numbered element list. (2) Study the screenshot. If you are not sure what a button, icon or menu does, search the web for that app (its docs, UI guides) before touching anything; do not guess by clicking around. (3) Plan the whole task, then send ALL the steps in one `act` batch (use element names and wait_for for things that only appear mid-way). Only look again if a step fails or the result is not what you expected.'

TOOLS = [
    {
        "name": "look",
        "description": (
            "START HERE. Takes a screenshot of the target app window AND reads its UI elements "
            "(numbered, with positions) in one call, so you can see what the app looks like and "
            "know what every control is called. Pass `app` to pick the app (opens it or uses "
            "its open window, in the background). " + WORKFLOW),
        "inputSchema": {"type": "object", "properties": {
            "app": {"type": "string", "description": "App name, window title or path. Omit to use the current target."},
            "max_width": {"type": "integer", "description": "Screenshot width, default 1280."}}},
    },
    {
        "name": "see",
        "description": (
            "Text-only re-check (faster than `look`, no image): read the app window as a compact numbered list of its UI elements "
            "(buttons, fields, menus, text) with ids and screen positions. Fast: no "
            "screenshot. Works on Claude's target window even when it is behind other "
            "windows (not minimized), otherwise the foreground window. Use `act` with {\"focus\": ...} or {\"open\": ...} first to "
            "choose the app."),
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Only list elements whose name contains this text."},
            "include_text": {"type": "boolean", "description": "Include plain text labels (default true)."},
            "limit": {"type": "integer", "description": "Max elements to list (default 300)."}}},
    },
    {
        "name": "act",
        "description": (
            "Run a batch of UI steps in ONE call, then return the updated window contents. "
            "BACKGROUND BY DEFAULT: Claude works in one target app without bringing it to the "
            "front, moving the real mouse or using the real keyboard, so the user can keep using "
            "other apps. Background clicks/typing are delivered directly to the app; some apps "
            "ignore them, so check the returned window contents. Shortcuts with modifiers "
            "(ctrl+s) do not work in the background: use menus/buttons instead. Only if a step "
            "says it needs the foreground, and the user agrees, retry with \"foreground\": true. "
            "Put as many steps as you can predict into one batch. Stops at the first failure. "
            "Steps (one key each):\n"
            "  {\"open\": \"notepad\"}  open an app by name/path and make it the target\n"
            "  {\"focus\": \"window title or app\"}  make an open window the target\n"
            "  {\"click\": target}  optional \"button\":\"right\", \"double\":true, \"mouse\":true (force a real mouse click)\n"
            "  {\"set\": target, \"text\": \"...\"}  replace a field's value instantly\n"
            "  {\"type\": \"text\"}  optional \"into\": target; \\n presses Enter\n"
            "  {\"key\": \"ctrl+s\"}  or several: \"ctrl+a delete\"\n"
            "  {\"scroll\": -3}  wheel notches (negative = down), optional \"at\": target\n"
            "  {\"wait_for\": \"element name\", \"timeout\": 5000}\n"
            "  {\"wait\": 300}  milliseconds\n" + TARGET_DOC + " "
            "The user can pause everything with Ctrl+Alt+P. Password fields are refused."),
        "inputSchema": {"type": "object", "required": ["steps"], "properties": {
            "steps": {"type": "array", "items": {"type": "object"}},
            "see_after": {"type": "boolean", "description": "Return the window contents afterwards (default true)."},
            "foreground": {"type": "boolean", "description": "Default false. true = bring the app to the front and use the real mouse/keyboard (interrupts the user)."}}},
    },
    {
        "name": "windows",
        "description": "List open app windows (title and process) so you can pick one to focus.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "screenshot",
        "description": (
            "Fallback for apps that do not expose their UI to `see` (games, 3D viewports, "
            "canvases). Captures the target window even if it is covered. Returns an image plus the formula to turn "
            "image pixels into click coordinates. Slower than `see`; use only when needed."),
        "inputSchema": {"type": "object", "properties": {
            "whole_screen": {"type": "boolean"},
            "max_width": {"type": "integer", "description": "Default 1280."}}},
    },
]


def text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


def call_tool(name, args):
    if not IS_WIN:
        return text_result("Swift only runs on Windows.", True)
    try:
        if name == "look":
            S.fg = False
            if args.get("app"):
                open_app(str(args["app"]))
                time.sleep(0.4)
            data, note = screenshot(False, int(args.get("max_width", 1280)))
            return {"content": [
                {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": "image/png"},
                {"type": "text", "text": note + "\n\n" + see_text() + "\n\nNext: if any control "
                 "is unclear, look the app up on the web, then send the full plan as one `act` batch."}],
                "isError": False}
        if name == "see":
            return text_result(see_text(args.get("query"), args.get("include_text", True),
                                        int(args.get("limit", 300))))
        if name == "act":
            steps = args.get("steps")
            if not isinstance(steps, list) or not steps:
                raise ToolError("`steps` must be a non-empty list.")
            text, failed = act(steps, args.get("see_after", True), args.get("foreground", False))
            return text_result(text, failed)
        if name == "windows":
            fg = GetForegroundWindow()
            lines = []
            for hw, title, pid in top_windows():
                if title == "Claude cursor":
                    continue
                mark = " (foreground)" if hw == fg else ""
                mark += " (current target)" if hw == S.target else ""
                lines.append('"%s" - %s%s' % (clip(title, 80), proc_name(pid), mark))
            return text_result("\n".join(lines) or "(no windows)")
        if name == "screenshot":
            data, note = screenshot(bool(args.get("whole_screen")), int(args.get("max_width", 1280)))
            return {"content": [
                {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": "image/png"},
                {"type": "text", "text": note}], "isError": False}
        return text_result("Unknown tool: %s" % name, True)
    except ToolError as e:
        return text_result(str(e), True)
    except Exception as e:
        import traceback
        log(traceback.format_exc())
        return text_result("%s: %s" % (type(e).__name__, e), True)


# --------------------------------------------------------------------------
# MCP stdio loop (newline-delimited JSON-RPC 2.0)
# --------------------------------------------------------------------------
def send(obj):
    _OUT.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    _OUT.flush()


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None                      # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "swift", "version": VERSION},
                  "instructions": ("Fast control of Windows apps, in the background on one "
                                   "target app so the user can use other apps meanwhile. " + WORKFLOW)}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = call_tool(params.get("name"), params.get("arguments") or {})
    elif method == "resources/list":
        result = {"resources": []}
    elif method == "prompts/list":
        result = {"prompts": []}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": "Method not found: %s" % method}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    if IS_WIN:
        startup()
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except Exception:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        try:
            resp = handle(msg)
        except Exception as e:
            resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            send(resp)


if __name__ == "__main__":
    main()
