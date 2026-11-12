"""macOS desktop-window transport: a window driven as the simulated slave.

Nothing in the standard library can enumerate windows, capture one, or inject
keyboard and mouse events, and the runtime ships no PyObjC binding. A tiny
Swift helper is therefore compiled on first use with ``xcrun swiftc -O`` and
cached under the temporary directory, keyed by a digest of its own source so
any change to the helper recompiles automatically. ``TITELLA_WINDOW_HELPER``
pins that binary to a fixed path, which is what makes a one-off macOS
Accessibility grant survive restarts and temporary-directory cleanups.

The helper exposes one subcommand per primitive and answers JSON on stdout:

``find``
    Resolves the target window (``CGWindowListCopyWindowInfo``).
``focus``
    Activates the owning application before an injection.
``mouse``
    Posts a move, a press, a release, a click or a wheel event (``CGEvent``).
``key``
    Posts one key with ``CGEventFlags`` and a short hold.
``unicode``
    Types exact characters with ``keyboardSetUnicodeString``, which needs no
    knowledge of the host layout, so ``ñ``, ``€``, ``á`` and the rest land as
    those characters instead of as whatever the active layout would produce.
``clip-read``
    Reads ``NSPasteboard.general``: the change count, the current text and,
    when the pasteboard holds one, the PNG/TIFF image as base64.
``clip-read-image``
    Reads only the image on ``NSPasteboard.general`` and fails with
    ``no_image`` when the clipboard holds none.
``clip-write``
    Writes one UTF-8 string and, by default, marks it transient so clipboard
    history managers ignore it.
``clip-write-image``
    Writes one PNG (base64) and, by default, marks it transient. The payload
    is validated as a decodable image before the pasteboard is cleared.

``WindowLink`` duck-types the parts of ``SerialLink`` the rest of the server
uses: the same ``request(channel, op, body)`` envelope, the same persisted
``link_id`` and the same subscription surface, so a window instance is driven
by the ordinary MCP tools. The agent channel is selected by
``config.agent_protocol``: ``none`` (the default) answers ``cdc_unavailable``
on every non-``hid`` channel, while ``clipboard`` carries the slave agent over
the shared clipboard as ``TITELLA_M2S``/``TITELLA_S2M`` packets, so
``slave_bootstrap`` and every ``remote_*`` tool work against a remote-desktop
client such as Citrix Viewer.
"""
from __future__ import annotations

import base64
import collections
import concurrent.futures
import contextlib
import hashlib
import itertools
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from titella_slave_assets.wire import (
    DATA_CHUNK_BYTES,
    DATA_WINDOW_DEFAULT,
    DATA_WINDOW_MAX,
    FIRMWARE_TO_MASTER,
    MASTER_TO_FIRMWARE,
    Deduplicator,
    FrameParser,
    base_frame,
    data_b64,
    encode_frame,
)

from .config import Config
from .serial_link import TransportError

# Bumped alongside an incompatible helper protocol; the source digest already
# forces a recompile on any edit, the version separates whole generations.
HELPER_VERSION = "v1"
HELPER_PREFIX = "titella-macos-window-helper"
HELPER_TIMEOUT_S = 20.0
UNICODE_TIMEOUT_BASE_S = 20.0
CAPTURE_TIMEOUT_S = 30.0
DEFAULT_UNICODE_DELAY_MS = 10

# Clipboard CDC: one packet per batch, acknowledged by the peer's own header.
# The cadences separate "frames are in flight" (resolve calls quickly) from
# "an operation holds the link while it waits for an unsolicited frame" and
# from "a live agent may write at any time"; a link with neither never reads
# or writes the pasteboard at all.
CLIPBOARD_POLL_FAST_S = 0.025
CLIPBOARD_POLL_STEP_S = 0.2
CLIPBOARD_POLL_SLOW_S = 1.0
CLIPBOARD_REWRITE_S = 0.2
CLIPBOARD_INBOUND_GRACE_S = 2.0
CLIPBOARD_M2S_PREFIX = "TITELLA_M2S:"
CLIPBOARD_S2M_PREFIX = "TITELLA_S2M:"
# Both peers flush at most 16 JSON lines per packet; a wider batch would ask
# the other side to parse something it never promises to accept.
CLIPBOARD_PACKET_MAX_FRAMES = 16

# macOS CGEventFlags values, verified against CGEventFlags.mask* at build time:
# control 0x40000, shift 0x20000, alternate 0x80000, command 0x100000.
FLAG_CONTROL = 1 << 18
FLAG_SHIFT = 1 << 17
FLAG_ALTERNATE = 1 << 19
FLAG_COMMAND = 1 << 20

# Button bits tracked on this side so a move between "down" and "up" becomes
# a dragged event, which is how Citrix Viewer and RDP recognize drag-and-drop.
_BUTTON_BITS = {"left": 1, "right": 2, "middle": 4}
_BUTTON_NAMES = {1: "left", 2: "right", 4: "middle"}

# HID operations that inject nothing into the target process, so a quiet
# transport must not disturb its minimized state for them.
_NO_INJECTION_OPS = frozenset({"health", "kb_layout"})

# ANSI virtual keycodes (Carbon Events.h). Only the positions this server
# addresses are listed: named keys first, then every printable ASCII key.
_NAMED_KEYCODES: dict[str, int] = {
    "ENTER": 36, "RETURN": 36, "ESC": 53, "ESCAPE": 53, "TAB": 48, "SPACE": 49,
    "BACKSPACE": 51, "DELETE": 117, "DEL": 117,
    "UP": 126, "DOWN": 125, "LEFT": 123, "RIGHT": 124,
    "UPARROW": 126, "DOWNARROW": 125, "LEFTARROW": 123, "RIGHTARROW": 124,
    "HOME": 115, "END": 119, "PAGEUP": 116, "PAGEDOWN": 121,
    "PGUP": 116, "PGDN": 121,
    "CAPS": 57, "CAPSLOCK": 57,
    "CMD": 55, "COMMAND": 55, "META": 55, "SUPER": 55, "WIN": 55,
    "CTRL": 59, "CONTROL": 59,
    "ALT": 58, "OPTION": 58,
    "SHIFT": 56,
    "RIGHTSHIFT": 60, "RIGHTCTRL": 62, "RIGHTALT": 61,
    # ANSI punctuation, in the positions kb_key addresses by name.
    "MINUS": 28, "EQUAL": 29, "LEFTBRACKET": 33, "RIGHTBRACKET": 30,
    "BACKSLASH": 42, "SEMICOLON": 41, "QUOTE": 39, "COMMA": 43, "PERIOD": 47,
    "SLASH": 44, "GRAVE": 50,
}
_NAMED_KEYCODES.update(
    {f"F{index}": code for index, code in enumerate(
        (122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111), start=1,
    )}
)

# Printable ASCII → (virtual keycode, needs shift for the shifted glyph).
_ANSI_KEYS: dict[str, tuple[int, bool]] = {
    "a": (0, False), "s": (1, False), "d": (2, False), "f": (3, False),
    "h": (4, False), "g": (5, False), "z": (6, False), "x": (7, False),
    "c": (8, False), "v": (9, False), "b": (11, False), "q": (12, False),
    "w": (13, False), "e": (14, False), "r": (15, False), "y": (16, False),
    "t": (17, False), "o": (31, False), "u": (32, False), "i": (34, False),
    "p": (35, False), "l": (37, False), "j": (38, False), "k": (40, False),
    "n": (45, False), "m": (46, False),
    "1": (18, False), "2": (19, False), "3": (20, False), "4": (21, False),
    "5": (22, False), "6": (23, False), "7": (24, False), "8": (25, False),
    "9": (26, False), "0": (27, False),
    "-": (28, False), "_": (28, True), "=": (29, False), "+": (29, True),
    "[": (33, False), "{": (33, True), "]": (30, False), "}": (30, True),
    "\\": (42, False), "|": (42, True), ";": (41, False), ":": (41, True),
    "'": (39, False), '"': (39, True), ",": (43, False), "<": (43, True),
    ".": (47, False), ">": (47, True), "/": (44, False), "?": (44, True),
    "`": (50, False), "~": (50, True), " ": (49, False),
}

# Modifier names accepted by the key tool and the CGEventFlags they set. The
# same names double as pressable keys through _NAMED_KEYCODES.
_MODIFIER_FLAGS: dict[str, int] = {
    "CTRL": FLAG_CONTROL, "CONTROL": FLAG_CONTROL,
    "SHIFT": FLAG_SHIFT,
    "ALT": FLAG_ALTERNATE, "OPTION": FLAG_ALTERNATE, "ALTGR": FLAG_ALTERNATE,
    "CMD": FLAG_COMMAND, "COMMAND": FLAG_COMMAND, "META": FLAG_COMMAND,
    "SUPER": FLAG_COMMAND, "WIN": FLAG_COMMAND,
}

# HID keyboard usage IDs → macOS virtual keycodes. The window transport
# normally receives the literal text (HidController carries it in kb_raw for
# this transport), so this table only serves the steps-only fallback.
_HID_USAGE_KEYCODES: dict[int, int] = {
    0x04: 0, 0x05: 11, 0x06: 8, 0x07: 2, 0x08: 14, 0x09: 3, 0x0A: 5,
    0x0B: 4, 0x0C: 34, 0x0D: 38, 0x0E: 40, 0x0F: 37, 0x10: 46, 0x11: 45,
    0x12: 31, 0x13: 35, 0x14: 12, 0x15: 15, 0x16: 1, 0x17: 17, 0x18: 32,
    0x19: 9, 0x1A: 13, 0x1B: 7, 0x1C: 16, 0x1D: 6,
    0x1E: 18, 0x1F: 19, 0x20: 20, 0x21: 21, 0x22: 22, 0x23: 23, 0x24: 24,
    0x25: 25, 0x26: 26, 0x27: 27,
    0x28: 36, 0x29: 53, 0x2A: 51, 0x2B: 48, 0x2C: 49,
    0x2D: 28, 0x2E: 29, 0x2F: 33, 0x30: 30, 0x31: 42,
    0x33: 41, 0x34: 39, 0x35: 50, 0x36: 43, 0x37: 47, 0x38: 44,
    0x39: 57,
    0x3A: 122, 0x3B: 120, 0x3C: 99, 0x3D: 118, 0x3E: 96, 0x3F: 97,
    0x40: 98, 0x41: 100, 0x42: 101, 0x43: 109, 0x44: 103, 0x45: 111,
    0x4A: 115, 0x4B: 116, 0x4D: 117, 0x4E: 119, 0x4F: 121,
    0x50: 123, 0x51: 124, 0x52: 125, 0x53: 126,
    0x64: 10,
    0xE0: 59, 0xE1: 56, 0xE2: 58, 0xE3: 55, 0xE4: 62, 0xE5: 60,
    0xE6: 61, 0xE7: 54,
}


def _hid_modifier_flags(mod: int) -> int:
    """CGEventFlags for a HID modifier byte (left and right bits collapse)."""
    flags = 0
    if mod & 0x11:  # left/right control
        flags |= FLAG_CONTROL
    if mod & 0x22:  # left/right shift
        flags |= FLAG_SHIFT
    if mod & 0x44:  # left/right alt (right alt is AltGr)
        flags |= FLAG_ALTERNATE
    if mod & 0x88:  # left/right GUI
        flags |= FLAG_COMMAND
    return flags


def macos_key_event(key: str, mods: list[str] | tuple[str, ...] = ()) -> tuple[int, int]:
    """Maps a key name or single character plus modifiers to (keycode, flags).

    Raises ``ValueError`` for an unknown key or modifier so the caller can
    answer ``invalid_argument`` before anything is injected.
    """
    flags = 0
    for mod in mods:
        name = str(mod).strip().upper()
        if not name:
            continue
        if name not in _MODIFIER_FLAGS:
            raise ValueError(f"unknown modifier {mod!r}")
        flags |= _MODIFIER_FLAGS[name]
    if not key:
        raise ValueError("key is required")
    named = key.strip().upper()
    if named in _NAMED_KEYCODES:
        return _NAMED_KEYCODES[named], flags
    if len(key) == 1:
        if key in _ANSI_KEYS:
            keycode, needs_shift = _ANSI_KEYS[key]
            return keycode, flags | (FLAG_SHIFT if needs_shift else 0)
        lowered = key.lower()
        if lowered in _ANSI_KEYS:
            keycode, needs_shift = _ANSI_KEYS[lowered]
            return keycode, flags | (FLAG_SHIFT if needs_shift or key != lowered else 0)
    raise ValueError(f"unknown key {key!r}")


def decode_steps(steps: list[Any]) -> list[tuple[int, int]]:
    """Parses ``"MM:UU"`` raw HID steps into ``(modifier, usage)`` pairs."""
    decoded: list[tuple[int, int]] = []
    for step in steps:
        text = str(step)
        mod_text, _, usage_text = text.partition(":")
        try:
            decoded.append((int(mod_text, 16), int(usage_text, 16)))
        except ValueError as exc:
            raise ValueError(f"invalid HID step {text!r}") from exc
    return decoded


_SWIFT_SOURCE = r"""// Titella macOS window helper: find, focus, mouse, key and unicode events.
import AppKit
import Carbon
import CoreGraphics
import Foundation

func jsonEscape(_ text: String) -> String {
    var out = ""
    for scalar in text.unicodeScalars {
        switch scalar {
        case "\"": out += "\\\""
        case "\\": out += "\\\\"
        case "\n": out += "\\n"
        case "\r": out += "\\r"
        case "\t": out += "\\t"
        default:
            if scalar.value < 0x20 {
                out += String(format: "\\u%04x", scalar.value)
            } else {
                out.unicodeScalars.append(scalar)
            }
        }
    }
    return out
}

func jsonString(_ text: String) -> String { "\"" + jsonEscape(text) + "\"" }

func emit(_ payload: [String: Any], status: Int32 = 0) -> Never {
    if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
       let text = String(data: data, encoding: .utf8) {
        FileHandle.standardOutput.write((text + "\n").data(using: .utf8)!)
    } else {
        FileHandle.standardOutput.write("{\"ok\":false,\"code\":\"helper_error\",\"message\":\"json encoding failed\"}\n".data(using: .utf8)!)
    }
    exit(status)
}

func fail(_ code: String, _ message: String) -> Never {
    emit(["ok": false, "code": code, "message": message], status: 1)
}

struct Args {
    var values: [String: String] = [:]

    init(_ argv: [String]) {
        var index = 0
        while index < argv.count {
            let flag = argv[index]
            guard flag.hasPrefix("--") else {
                index += 1
                continue
            }
            let key = String(flag.dropFirst(2))
            if index + 1 < argv.count && !argv[index + 1].hasPrefix("--") {
                values[key] = argv[index + 1]
                index += 2
            } else {
                values[key] = "1"
                index += 1
            }
        }
    }

    func int(_ key: String, _ fallback: Int = 0) -> Int {
        guard let text = values[key], let value = Int(text) else { return fallback }
        return value
    }

    func uint64(_ key: String, _ fallback: UInt64 = 0) -> UInt64 {
        guard let text = values[key], let value = UInt64(text) else { return fallback }
        return value
    }

    func str(_ key: String) -> String? { values[key] }
}

struct WindowMatch {
    let id: Int
    let pid: Int
    let owner: String
    let title: String
    let bounds: CGRect
}

func windowList(onScreenOnly: Bool) -> [[String: Any]] {
    var options: CGWindowListOption = [.excludeDesktopElements]
    if onScreenOnly { options.insert(.optionOnScreenOnly) }
    return CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] ?? []
}

func boundsOf(_ info: [String: Any]) -> CGRect? {
    guard let raw = info[kCGWindowBounds as String] as? [String: Any] else { return nil }
    guard let x = (raw["X"] as? NSNumber)?.doubleValue,
          let y = (raw["Y"] as? NSNumber)?.doubleValue,
          let width = (raw["Width"] as? NSNumber)?.doubleValue,
          let height = (raw["Height"] as? NSNumber)?.doubleValue else { return nil }
    return CGRect(x: x, y: y, width: width, height: height)
}

func commandFind(_ args: Args) -> Never {
    let wantedId = args.str("id").flatMap { Int($0) }
    let wantedApp = args.str("app")?.lowercased()
    let wantedTitle = args.str("title")?.lowercased()
    if wantedId == nil && (wantedApp?.isEmpty ?? true) && (wantedTitle?.isEmpty ?? true) {
        fail("invalid_argument", "find needs --id, --app or --title")
    }

    func matches(_ windows: [[String: Any]]) -> [WindowMatch] {
        var found: [WindowMatch] = []
        for info in windows {
            guard let layer = (info[kCGWindowLayer as String] as? NSNumber)?.intValue, layer == 0 else { continue }
            guard let bounds = boundsOf(info), bounds.width > 16, bounds.height > 16 else { continue }
            let number = (info[kCGWindowNumber as String] as? NSNumber)?.intValue ?? 0
            let pid = (info[kCGWindowOwnerPID as String] as? NSNumber)?.intValue ?? 0
            let owner = info[kCGWindowOwnerName as String] as? String ?? ""
            let title = info[kCGWindowName as String] as? String ?? ""
            if let wanted = wantedId {
                if number != wanted { continue }
            } else {
                if let needle = wantedApp, !needle.isEmpty,
                   !owner.lowercased().contains(needle) { continue }
                if let needle = wantedTitle, !needle.isEmpty,
                   !title.lowercased().contains(needle) { continue }
            }
            found.append(WindowMatch(id: number, pid: pid, owner: owner,
                                     title: title, bounds: bounds))
        }
        // An exact application match wins over a merely containing one; among
        // equals the largest window wins, which drops splash and HUD windows.
        if let needle = wantedApp, !needle.isEmpty {
            let exact = found.filter { $0.owner.lowercased() == needle }
            if !exact.isEmpty { return exact }
        }
        return found
    }

    var candidates = matches(windowList(onScreenOnly: true))
    if candidates.isEmpty {
        // Off-screen windows (another Space, a minimized window) still exist
        // and remain addressable, so only the on-screen filter is dropped.
        candidates = matches(windowList(onScreenOnly: false))
    }
    let best = candidates.max { left, right in
        (left.bounds.width * left.bounds.height) < (right.bounds.width * right.bounds.height)
    }
    guard let match = best else {
        fail("window_not_found", "no window matches the configured selector")
    }
    emit([
        "ok": true,
        "window": [
            "id": match.id,
            "pid": match.pid,
            "owner": match.owner,
            "title": match.title,
            "bounds": [
                "x": Double(match.bounds.origin.x),
                "y": Double(match.bounds.origin.y),
                "width": Double(match.bounds.width),
                "height": Double(match.bounds.height),
            ],
        ],
    ])
}

func activate(_ pid: Int) -> Bool {
    guard pid > 0, let app = NSRunningApplication(processIdentifier: pid_t(pid)) else {
        return false
    }
    return app.activate(options: [.activateIgnoringOtherApps])
}

func commandFocus(_ args: Args) -> Never {
    let pid = args.int("pid")
    guard activate(pid) else {
        fail("focus_failed", "cannot activate the process \(pid)")
    }
    emit(["ok": true])
}

/// Identity of whatever holds the frontmost application right now.
func commandFrontmost(_ args: Args) -> Never {
    guard let app = NSWorkspace.shared.frontmostApplication else {
        emit(["ok": true, "pid": 0, "bundle_id": "", "name": ""])
    }
    emit([
        "ok": true,
        "pid": Int(app.processIdentifier),
        "bundle_id": app.bundleIdentifier ?? "",
        "name": app.localizedName ?? "",
    ])
}

/// Resolves the accessibility window that belongs to the selected target.
///
/// An application can own several windows, so the first one is not necessarily
/// the one that was selected: the title is what the caller already knows and
/// what `CGWindowListCopyWindowInfo` reported for it.
func targetWindow(_ app: AXUIElement, title wanted: String) -> AXUIElement? {
    var value: CFTypeRef?
    var windows: [AXUIElement] = []
    if AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &value) == .success,
       let listed = value as? [AXUIElement] {
        windows = listed
    }
    if windows.isEmpty {
        // A window that lives on another Space (or is minimized) is not in
        // kAXWindowsAttribute, but the application still reports it as its
        // focused window. Without this fallback the transport would refuse a
        // target it can perfectly well capture and drive.
        var focused: CFTypeRef?
        if AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute as CFString, &focused) == .success,
           let window = focused {
            windows = [unsafeBitCast(window, to: AXUIElement.self)]
        }
    }
    if windows.isEmpty {
        return nil
    }
    if wanted.isEmpty {
        return windows.first
    }
    for window in windows {
        var titleValue: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
            window, kAXTitleAttribute as CFString, &titleValue) == .success else { continue }
        if (titleValue as? String) == wanted { return window }
    }
    return windows.first
}

/// Un-minimizes the target window through the accessibility API only.
///
/// It never activates the application, so the operator keeps the window they
/// were working in. Nothing is touched at all when the window is already
/// visible: a visible background window accepts injected events directly, and
/// raising it would make it jump to the front of its own window stack for no
/// reason. The answer reports whether the window had to be restored, which is
/// exactly what the caller needs to put it back afterwards.
func commandWake(_ args: Args) -> Never {
    let pid = pid_t(args.int("pid"))
    guard pid > 0 else { fail("invalid_argument", "wake needs --pid") }
    let app = AXUIElementCreateApplication(pid)
    guard let window = targetWindow(app, title: args.str("title") ?? "") else {
        fail("window_not_found", "the application \(pid) exposes no accessibility window")
    }
    var minimizedValue: CFTypeRef?
    let readMinimized = AXUIElementCopyAttributeValue(
        window, kAXMinimizedAttribute as CFString, &minimizedValue)
    let wasMinimized = readMinimized == .success && (minimizedValue as? Bool) == true
    guard wasMinimized else {
        // Visible: leave it exactly where it is.
        emit(["ok": true, "was_minimized": false])
    }
    let restored = AXUIElementSetAttributeValue(
        window, kAXMinimizedAttribute as CFString, kCFBooleanFalse)
    if restored != .success {
        fail("wake_failed", "the window cannot be restored (accessibility \(restored.rawValue))")
    }
    usleep(180_000)
    // A freshly restored window is not the key window inside its application,
    // and a process with no key window drops injected events. Raising it makes
    // it key again without activating the application.
    let raised = AXUIElementPerformAction(window, kAXRaiseAction as CFString)
    if raised != .success {
        fail("wake_failed", "the window cannot be raised (accessibility \(raised.rawValue))")
    }
    usleep(80_000)
    emit(["ok": true, "was_minimized": true])
}

/// Puts the window back into the minimized state the caller found it in.
func commandSleep(_ args: Args) -> Never {
    let pid = pid_t(args.int("pid"))
    guard pid > 0 else { fail("invalid_argument", "sleep needs --pid") }
    guard args.int("minimized") == 1 else { emit(["ok": true, "minimized": false]) }
    let app = AXUIElementCreateApplication(pid)
    guard let window = targetWindow(app, title: args.str("title") ?? "") else {
        fail("window_not_found", "the application \(pid) exposes no accessibility window")
    }
    let minimized = AXUIElementSetAttributeValue(
        window, kAXMinimizedAttribute as CFString, kCFBooleanTrue)
    if minimized != .success {
        fail("sleep_failed", "the window cannot be minimized (accessibility \(minimized.rawValue))")
    }
    emit(["ok": true, "minimized": true])
}

func post(_ event: CGEvent?) {
    event?.post(tap: .cghidEventTap)
}

func buttonOf(_ name: String) -> (CGMouseButton, CGEventType, CGEventType, CGEventType) {
    switch name {
    case "right":
        return (.right, .rightMouseDown, .rightMouseUp, .rightMouseDragged)
    case "middle":
        return (.center, .otherMouseDown, .otherMouseUp, .otherMouseDragged)
    default:
        return (.left, .leftMouseDown, .leftMouseUp, .leftMouseDragged)
    }
}

func draggedType(_ held: Int) -> (CGMouseButton, CGEventType) {
    if held & 1 != 0 { return (.left, .leftMouseDragged) }
    if held & 2 != 0 { return (.right, .rightMouseDragged) }
    return (.center, .otherMouseDragged)
}

func commandMouse(_ args: Args) -> Never {
    let action = args.str("action") ?? "move"
    let point = CGPoint(x: CGFloat(args.int("x")), y: CGFloat(args.int("y")))
    let name = args.str("button") ?? "left"
    let held = args.int("held")
    let wantsDouble = args.int("double") == 1
    let delta = args.int("delta")
    let targetPid = args.int("to-pid")
    let (button, downType, upType, _) = buttonOf(name)

    /// Delivers one event: straight to the process with ``--to-pid``, which
    /// leaves the operator's cursor and frontmost application untouched, or
    /// through the global tap otherwise.
    func deliver(_ event: CGEvent?) {
        if targetPid > 0 {
            event?.postToPid(pid_t(targetPid))
        } else {
            post(event)
        }
    }

    func move() {
        if targetPid <= 0 {
            CGWarpMouseCursorPosition(point)
            CGAssociateMouseAndMouseCursorPosition(1)
        }
        if held != 0 {
            let (dragButton, dragType) = draggedType(held)
            let event = CGEvent(mouseEventSource: nil, mouseType: dragType,
                                mouseCursorPosition: point, mouseButton: dragButton)
            if dragType == .otherMouseDragged {
                event?.setIntegerValueField(.mouseEventButtonNumber, value: 2)
            }
            deliver(event)
        } else {
            deliver(CGEvent(mouseEventSource: nil, mouseType: .mouseMoved,
                            mouseCursorPosition: point, mouseButton: .left))
        }
    }

    func press() {
        let event = CGEvent(mouseEventSource: nil, mouseType: downType,
                            mouseCursorPosition: point, mouseButton: button)
        event?.setIntegerValueField(.mouseEventClickState, value: 1)
        if downType == .otherMouseDown {
            event?.setIntegerValueField(.mouseEventButtonNumber, value: 2)
        }
        deliver(event)
    }

    func release() {
        let event = CGEvent(mouseEventSource: nil, mouseType: upType,
                            mouseCursorPosition: point, mouseButton: button)
        event?.setIntegerValueField(.mouseEventClickState, value: 1)
        if upType == .otherMouseUp {
            event?.setIntegerValueField(.mouseEventButtonNumber, value: 2)
        }
        deliver(event)
    }

    switch action {
    case "move":
        move()
    case "down":
        move()
        usleep(40_000)
        press()
    case "up":
        move()
        usleep(40_000)
        release()
    case "click":
        move()
        usleep(60_000)
        let firstDown = CGEvent(mouseEventSource: nil, mouseType: downType,
                                mouseCursorPosition: point, mouseButton: button)
        let firstUp = CGEvent(mouseEventSource: nil, mouseType: upType,
                              mouseCursorPosition: point, mouseButton: button)
        firstDown?.setIntegerValueField(.mouseEventClickState, value: 1)
        firstUp?.setIntegerValueField(.mouseEventClickState, value: 1)
        if downType == .otherMouseDown {
            firstDown?.setIntegerValueField(.mouseEventButtonNumber, value: 2)
            firstUp?.setIntegerValueField(.mouseEventButtonNumber, value: 2)
        }
        deliver(firstDown)
        usleep(60_000)
        deliver(firstUp)
        if wantsDouble {
            usleep(60_000)
            let secondDown = CGEvent(mouseEventSource: nil, mouseType: downType,
                                     mouseCursorPosition: point, mouseButton: button)
            let secondUp = CGEvent(mouseEventSource: nil, mouseType: upType,
                                   mouseCursorPosition: point, mouseButton: button)
            // A double click is recognized by the click state of the second
            // pair: both of its events must carry state 2.
            secondDown?.setIntegerValueField(.mouseEventClickState, value: 2)
            secondUp?.setIntegerValueField(.mouseEventClickState, value: 2)
            if downType == .otherMouseDown {
                secondDown?.setIntegerValueField(.mouseEventButtonNumber, value: 2)
                secondUp?.setIntegerValueField(.mouseEventButtonNumber, value: 2)
            }
            deliver(secondDown)
            usleep(60_000)
            deliver(secondUp)
        }
    case "wheel":
        move()
        let event = CGEvent(scrollWheelEvent2Source: nil, units: .pixel,
                            wheelCount: 1, wheel1: Int32(delta * 3), wheel2: 0, wheel3: 0)
        deliver(event)
    default:
        fail("invalid_argument", "unknown mouse action \(action)")
    }
    emit(["ok": true, "delivery": targetPid > 0 ? "pid" : "tap"])
}

func modifierKeycodes(_ flags: CGEventFlags) -> [(CGKeyCode, CGEventFlags)] {
    var list: [(CGKeyCode, CGEventFlags)] = []
    if flags.contains(.maskControl) { list.append((59, .maskControl)) }
    if flags.contains(.maskShift) { list.append((56, .maskShift)) }
    if flags.contains(.maskAlternate) { list.append((61, .maskAlternate)) }
    if flags.contains(.maskCommand) { list.append((55, .maskCommand)) }
    return list
}

func sendWithModifiers(
    keycode: CGKeyCode,
    flags: CGEventFlags,
    holdUs: useconds_t,
    deliver: (CGEvent?) -> Void
) {
    let mods = modifierKeycodes(flags)
    var active: CGEventFlags = []
    for (modKey, bit) in mods {
        active.insert(bit)
        let ev = CGEvent(keyboardEventSource: nil, virtualKey: modKey, keyDown: true)
        ev?.type = .flagsChanged
        ev?.flags = active
        deliver(ev)
        usleep(12_000)
    }
    let down = CGEvent(keyboardEventSource: nil, virtualKey: keycode, keyDown: true)
    down?.flags = flags
    deliver(down)
    usleep(holdUs)
    let up = CGEvent(keyboardEventSource: nil, virtualKey: keycode, keyDown: false)
    up?.flags = flags
    deliver(up)
    if !mods.isEmpty {
        usleep(12_000)
        for (modKey, bit) in mods.reversed() {
            active.remove(bit)
            let ev = CGEvent(keyboardEventSource: nil, virtualKey: modKey, keyDown: false)
            ev?.type = .flagsChanged
            ev?.flags = active
            deliver(ev)
            usleep(10_000)
        }
    }
}

func commandKey(_ args: Args) -> Never {
    let keycode = args.int("keycode")
    let flags = CGEventFlags(rawValue: args.uint64("flags"))
    let holdMs = max(1, args.int("hold-ms", 8))
    let target = args.int("to-pid")
    if target > 0 {
        // postToPid reaches the process window even when another application
        // is frontmost: nothing is stolen from the operator.
        let pid = pid_t(target)
        sendWithModifiers(
            keycode: CGKeyCode(keycode),
            flags: flags,
            holdUs: useconds_t(holdMs * 1000),
            deliver: { $0?.postToPid(pid) }
        )
        emit(["ok": true, "delivery": "pid"])
    }
    if let text = args.str("pid"), let pid = Int(text), pid > 0 {
        _ = activate(pid)
        usleep(20_000)
    }
    sendWithModifiers(
        keycode: CGKeyCode(keycode),
        flags: flags,
        holdUs: useconds_t(holdMs * 1000),
        deliver: post
    )
    emit(["ok": true, "delivery": "tap"])
}

func lookupVirtualKey(_ target: UniChar) -> (CGKeyCode, CGEventFlags)? {
    if target == 10 || target == 13 { return (CGKeyCode(36), []) }
    if target == 9 { return (CGKeyCode(48), []) }
    if target == 8 { return (CGKeyCode(51), []) }
    guard let src = TISCopyCurrentKeyboardLayoutInputSource()?.takeRetainedValue(),
          let ptr = TISGetInputSourceProperty(src, kTISPropertyUnicodeKeyLayoutData) else {
        return nil
    }
    let data = Unmanaged<CFData>.fromOpaque(ptr).takeUnretainedValue() as Data
    return data.withUnsafeBytes { rawBuf -> (CGKeyCode, CGEventFlags)? in
        guard let layout = rawBuf.bindMemory(to: UCKeyboardLayout.self).baseAddress else { return nil }
        let kbdType = UInt32(LMGetKbdType())
        let levels: [(UInt32, CGEventFlags)] = [
            (0, []),
            (UInt32(shiftKey >> 8), .maskShift),
            (UInt32(optionKey >> 8), .maskAlternate),
            (UInt32((shiftKey | optionKey) >> 8), [.maskShift, .maskAlternate]),
        ]
        for (modBits, cgFlags) in levels {
            for kc: UInt16 in 0..<128 {
                var deadKeyState: UInt32 = 0
                var chars = [UniChar](repeating: 0, count: 4)
                var actualLen: Int = 0
                let status = UCKeyTranslate(
                    layout, kc, UInt16(kUCKeyActionDown), modBits, kbdType,
                    UInt32(kUCKeyTranslateNoDeadKeysMask), &deadKeyState, 4, &actualLen, &chars
                )
                if status == noErr && actualLen == 1 && chars[0] == target {
                    return (CGKeyCode(kc), cgFlags)
                }
            }
        }
        return nil
    }
}

func commandUnicode(_ args: Args) -> Never {
    guard let pidText = args.str("pid"), let pid = Int(pidText), pid > 0 else {
        fail("invalid_argument", "unicode needs --pid")
    }
    guard let encoded = args.str("text-b64"), let data = Data(base64Encoded: encoded),
          let text = String(data: data, encoding: .utf8) else {
        fail("invalid_argument", "unicode needs a base64 UTF-8 --text-b64")
    }
    let delayMs = max(0, args.int("delay-ms", 10))
    let wantsEnter = args.int("enter") == 1
    // ``--to-pid`` injects straight into the target process, so the operator
    // keeps the frontmost application and the window they were working in.
    //
    // An X11 client (XQuartz hosting xfreerdp) is the documented exception:
    // ``postToPid`` is dropped by the process because X11 delivers keys from
    // its own focus window. The caller fixes that by focusing the X11 window
    // first (``xdotool windowfocus``) and then posting through the global tap,
    // which reaches the X server without touching the macOS frontmost app.
    let targetPid = args.int("to-pid")
    if targetPid <= 0, !activate(pid) {
        fail("focus_failed", "cannot activate the process \(pid)")
    }
    usleep(20_000)
    let pidTarget = pid_t(targetPid > 0 ? targetPid : pid)
    func send(_ event: CGEvent?) {
        if targetPid > 0 {
            event?.postToPid(pidTarget)
        } else {
            post(event)
        }
    }
    let units = Array(text.utf16)
    var index = 0
    while index < units.count {
        var length = 1
        let unit = units[index]
        if unit >= 0xD800 && unit <= 0xDBFF && index + 1 < units.count {
            let next = units[index + 1]
            if next >= 0xDC00 && next <= 0xDFFF { length = 2 }
        }
        if length == 1, let (keycode, flags) = lookupVirtualKey(unit) {
            sendWithModifiers(
                keycode: keycode,
                flags: flags,
                holdUs: 6_000,
                deliver: send
            )
        } else {
            var buffer = Array(units[index..<(index + length)])
            let down = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true)
            down?.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: &buffer)
            send(down)
            var upBuffer = buffer
            let up = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false)
            up?.keyboardSetUnicodeString(stringLength: upBuffer.count, unicodeString: &upBuffer)
            send(up)
        }
        index += length
        if index < units.count && delayMs > 0 {
            usleep(useconds_t(delayMs * 1000))
        }
    }
    if wantsEnter {
        send(CGEvent(keyboardEventSource: nil, virtualKey: CGKeyCode(36), keyDown: true))
        usleep(80_000)
        send(CGEvent(keyboardEventSource: nil, virtualKey: CGKeyCode(36), keyDown: false))
    }
    emit(["ok": true, "units": units.count,
          "delivery": targetPid > 0 ? "pid" : "tap"])
}

func commandClipRead(_ args: Args) -> Never {
    let pb = NSPasteboard.general
    let text = pb.string(forType: .string) ?? ""
    let imageTypes: [NSPasteboard.PasteboardType] = [.png, .tiff]
    var image_b64 = ""
    for type in imageTypes {
        if let data = pb.data(forType: type) {
            image_b64 = data.base64EncodedString()
            break
        }
    }
    emit([
        "ok": true,
        "change_count": pb.changeCount,
        "text_b64": Data(text.utf8).base64EncodedString(),
        "image_b64": image_b64,
        "image_type": image_b64.isEmpty ? "" : "png-or-tiff",
    ])
}

func commandClipReadImage(_ args: Args) -> Never {
    let pb = NSPasteboard.general
    for type in [NSPasteboard.PasteboardType.png, .tiff] {
        if let data = pb.data(forType: type) {
            emit([
                "ok": true,
                "change_count": pb.changeCount,
                "image_b64": data.base64EncodedString(),
                "type": type.rawValue,
            ])
        }
    }
    fail("no_image", "the clipboard holds no PNG or TIFF image")
}

func commandClipWriteImage(_ args: Args) -> Never {
    guard let encoded = args.str("png-b64"), let data = Data(base64Encoded: encoded) else {
        fail("invalid_argument", "clip-write-image needs a base64 PNG --png-b64")
    }
    // Validate the payload decodes as an image before clearing the pasteboard.
    guard let image = NSImage(data: data) else {
        fail("invalid_argument", "the --png-b64 payload is not a decodable image")
    }
    let pb = NSPasteboard.general
    pb.clearContents()
    _ = pb.setData(data, forType: .png)
    // Remote-desktop clients negotiate the image format with the guest: some
    // forward only the TIFF representation, so both are always published.
    if let tiff = image.tiffRepresentation {
        _ = pb.setData(tiff, forType: .tiff)
    }
    if args.int("transient", 1) == 1 {
        for name in ["org.nspasteboard.TransientType",
                     "org.nspasteboard.AutoGeneratedType",
                     "org.nspasteboard.ConcealedType"] {
            _ = pb.setData(Data(), forType: NSPasteboard.PasteboardType(name))
        }
    }
    emit(["ok": true, "change_count": pb.changeCount])
}

func commandClipWrite(_ args: Args) -> Never {
    guard let encoded = args.str("text-b64"), let data = Data(base64Encoded: encoded),
          let text = String(data: data, encoding: .utf8) else {
        fail("invalid_argument", "clip-write needs a base64 UTF-8 --text-b64")
    }
    let pb = NSPasteboard.general
    pb.clearContents()
    _ = pb.setString(text, forType: .string)
    if args.int("transient", 1) == 1 {
        // Empty data on the de-facto marker types: clipboard history managers
        // (Raycast, Alfred, Maccy, Paste, 1Password) skip the entry entirely,
        // so agent traffic never pollutes the user's clipboard history.
        for name in ["org.nspasteboard.TransientType",
                     "org.nspasteboard.AutoGeneratedType",
                     "org.nspasteboard.ConcealedType"] {
            _ = pb.setData(Data(), forType: NSPasteboard.PasteboardType(name))
        }
    }
    emit(["ok": true, "change_count": pb.changeCount])
}

let argv = Array(CommandLine.arguments.dropFirst())
guard let command = argv.first else {
    fail("invalid_argument", "missing subcommand: find, focus, mouse, key, unicode, clip-read or clip-write")
}
let args = Args(Array(argv.dropFirst()))
switch command {
case "find": commandFind(args)
case "focus": commandFocus(args)
case "frontmost": commandFrontmost(args)
case "wake": commandWake(args)
case "sleep": commandSleep(args)
case "mouse": commandMouse(args)
case "key": commandKey(args)
case "unicode": commandUnicode(args)
case "clip-read": commandClipRead(args)
case "clip-read-image": commandClipReadImage(args)
case "clip-write": commandClipWrite(args)
case "clip-write-image": commandClipWriteImage(args)
default: fail("invalid_argument", "unknown subcommand \(command)")
}
"""

_COMPILE_LOCK = threading.Lock()


def helper_digest() -> str:
    return hashlib.sha256(_SWIFT_SOURCE.encode("utf-8")).hexdigest()[:12]


def helper_path() -> Path:
    """Cache path of the compiled helper, keyed by the helper source digest.

    ``TITELLA_WINDOW_HELPER`` pins the binary to a fixed absolute path, so the
    macOS Accessibility grant can be given once to a stable file instead of the
    digest-named copy in the temporary directory, which macOS purges and which
    changes name whenever the helper source does.
    """
    override = os.environ.get("TITELLA_WINDOW_HELPER", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return Path(tempfile.gettempdir()) / f"{HELPER_PREFIX}-{HELPER_VERSION}-{helper_digest()}"


def _helper_stamp(binary: Path) -> Path:
    """Digest sidecar used to notice a pinned helper going stale."""
    return binary.with_name(f"{binary.name}.digest")


def _helper_is_current(binary: Path) -> bool:
    """True when ``binary`` can be executed as-is for the current helper source.

    A digest-named temporary helper never goes stale; a pinned one keeps its
    path across source changes, so it carries a ``.digest`` sidecar and is
    recompiled when the source no longer matches.
    """
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        return False
    if not os.environ.get("TITELLA_WINDOW_HELPER", "").strip():
        return True
    try:
        return _helper_stamp(binary).read_text(encoding="ascii").strip() == helper_digest()
    except OSError:
        return False


def ensure_helper() -> Path:
    """Compiles the Swift helper on first use and returns its cached path."""
    binary = helper_path()
    if _helper_is_current(binary):
        return binary
    if sys.platform != "darwin":
        raise TransportError(
            "window_transport_unsupported",
            "the window transport needs macOS: CGWindowListCopyWindowInfo and CGEvent are macOS-only",
            False,
        )
    with _COMPILE_LOCK:
        if _helper_is_current(binary):
            return binary
        source = binary.with_suffix(".swift")
        staging = binary.with_suffix(f".{os.getpid()}.tmp")
        binary.parent.mkdir(parents=True, exist_ok=True)
        try:
            source.write_text(_SWIFT_SOURCE, encoding="utf-8")
            completed = subprocess.run(
                ["xcrun", "swiftc", "-O", "-o", str(staging), str(source)],
                capture_output=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            staging.unlink(missing_ok=True)
            raise TransportError(
                "helper_compile_failed",
                f"the macOS window helper cannot be compiled: {exc}",
                False,
            ) from exc
        if completed.returncode != 0 or not staging.is_file():
            staging.unlink(missing_ok=True)
            detail = completed.stderr.decode("utf-8", errors="replace").strip()[:2000]
            raise TransportError(
                "helper_compile_failed",
                f"the macOS window helper cannot be compiled: {detail}",
                False,
            )
        os.replace(staging, binary)
        if os.environ.get("TITELLA_WINDOW_HELPER", "").strip():
            _helper_stamp(binary).write_text(f"{helper_digest()}\n", encoding="ascii")
    return binary


class WindowLink:
    """Window transport: the ``SerialLink`` surface over one macOS window."""

    transport = "window"

    def __init__(
        self,
        config: Config,
        *,
        window_provider: Callable[[], dict[str, Any]] | None = None,
        capture_provider: Callable[[dict[str, Any], Path], None] | None = None,
        helper_provider: Callable[[list[str]], dict[str, Any]] | None = None,
        clipboard_provider: Callable[[list[str]], dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.state_dir = config.ensure_state_dir()
        self.link_path = self.state_dir / "active-link-id"
        self.link_id = self._load_or_create_link_id()
        self.port: str | None = self._selector()
        self.connected = False
        self.state = "disconnected"
        self.agent_protocol = config.agent_protocol
        # "quiet" delivers every event straight to the target process and never
        # raises the window, so the operator keeps working in whichever
        # application is frontmost. "raise" is the classic behaviour.
        self.quiet = getattr(config, "window_activation", "quiet") == "quiet"
        self._buttons_mask = 0
        self._last_x: float | None = None
        self._last_y: float | None = None
        self._layout = config.keyboard_layout
        # Test seams: every primitive the helper implements can be replaced
        # so the transport is exercised without a GUI or a compiled binary.
        # ``clipboard_provider`` answers ``clip-read``/``clip-write`` only and
        # stands in for the whole pasteboard; when it is absent those
        # subcommands go through ``helper_provider`` and then the real helper.
        self._window_provider = window_provider
        self._capture_provider = capture_provider
        self._helper_provider = helper_provider
        self._clipboard_provider = clipboard_provider
        # Clipboard CDC engine: one worker owns the pasteboard while the
        # channel is in use, so reads and writes never interleave.
        self._cond = threading.Condition(threading.Lock())
        self._outbound: collections.deque[dict[str, Any]] = collections.deque()
        self._pending_requests: dict[int, concurrent.futures.Future[dict[str, Any]]] = {}
        self._acks: dict[tuple[str, int], concurrent.futures.Future[None]] = {}
        self._handlers: dict[tuple[str, str | None], list[Callable[[dict[str, Any]], Any]]] = {}
        self._state_handlers: list[Callable[[str], Any]] = []
        self._request_ids = itertools.count(1)
        self._clip_parser = FrameParser(FIRMWARE_TO_MASTER, ignore_invalid=True)
        self._dedup = Deduplicator()
        self._m2s_seq = 0
        self._last_s2m_seq = 0
        self._m2s_acked = 0
        self._written_packet: dict[str, Any] | None = None
        self._saved_user_clipboard: str | None = None
        self._active_ops = 0
        self._agent_attached = False
        self._last_inbound = 0.0
        self.last_seen = 0.0
        self._worker: threading.Thread | None = None
        self._stop_engine = False

    # ── link identity ───────────────────────────────────────────────────
    def _load_or_create_link_id(self) -> str:
        try:
            value = self.link_path.read_text(encoding="ascii").strip()
            if len(value) == 32 and all(c in "0123456789abcdef" for c in value):
                return value
        except OSError:
            pass
        value = secrets.token_hex(16)
        fd = os.open(self.link_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, value.encode("ascii"))
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        return value

    def _selector(self) -> str:
        if self.config.window_app:
            return self.config.window_app
        if self.config.window_title:
            return self.config.window_title
        if self.config.window_id is not None:
            return str(self.config.window_id)
        return "window"

    # ── helper plumbing ─────────────────────────────────────────────────
    def _run_helper(self, argv: list[str], *, timeout: float = HELPER_TIMEOUT_S) -> dict[str, Any]:
        if self._helper_provider is not None:
            result = self._helper_provider(list(argv))
            if not isinstance(result, dict):
                raise TransportError("helper_failed", "the window helper returned no object", False)
            return result
        binary = ensure_helper()
        try:
            completed = subprocess.run(
                [str(binary), *argv],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TransportError("helper_failed", f"the window helper failed: {exc}", True) from exc
        payload = _parse_helper_stdout(completed.stdout)
        if payload is None:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()[:2000]
            raise TransportError(
                "helper_failed",
                f"the window helper returned no JSON ({completed.returncode}): {detail}",
                True,
            )
        return payload

    def _inject(self, argv: list[str], *, timeout: float = HELPER_TIMEOUT_S) -> None:
        payload = self._run_helper(argv, timeout=timeout)
        if not payload.get("ok"):
            raise TransportError(
                str(payload.get("code", "helper_failed")),
                str(payload.get("message", "the window helper refused the injection")),
                False,
            )

    def _focus(self, window: dict[str, Any]) -> None:
        """Brings the owning application forward before a press or a keystroke.

        With ``window_activation = "quiet"`` (the default) nothing is brought
        forward: every event is delivered straight to the target process with
        ``postToPid``, so the operator keeps the frontmost application, the
        cursor and the window they were working in.
        """
        if self.quiet:
            return
        self._inject(["focus", "--pid", str(int(window.get("pid") or 0))])

    def _target(self, window: dict[str, Any]) -> list[str]:
        """Extra helper arguments that deliver events straight to the process.

        An X11 client such as XQuartz hosting xfreerdp is the one exception:
        the process drops ``postToPid`` keystrokes, because X11 dispatches them
        from its own focus window instead of from wherever the pid asks. The
        event therefore has to go through the global tap, after the X11 focus is
        moved onto the client window — both without touching the operator's
        frontmost application.
        """
        if not self.quiet:
            return []
        if self._is_x11(window):
            self._focus_x11(window)
            return []
        return ["--to-pid", str(int(window.get("pid") or 0))]

    def _is_x11(self, window: dict[str, Any]) -> bool:
        """True when the owning process is an X server client (XQuartz)."""
        return str(window.get("owner") or "").strip().lower() == "xquartz"

    def _focus_x11(self, window: dict[str, Any]) -> None:
        """Points the X server's input focus at the client window.

        The window number reported by CoreGraphics is the X11 window id, so a
        plain ``xdotool windowfocus`` sets the focus the X server routes keys
        from. It changes nothing on the macOS side: another application stays
        frontmost. An XQuartz install without XTEST cannot synthesise keys, so
        this only moves focus; the keystrokes themselves leave through the
        global tap in :meth:`_inject`.
        """
        window_id = window.get("id")
        if not window_id:
            return
        environment = dict(os.environ)
        if not environment.get("DISPLAY"):
            # XQuartz publishes its socket outside /tmp/.X11-unix; the launchd
            # path is the one a normal login session uses.
            environment["DISPLAY"] = f"/tmp/launch-{os.environ.get('USER', '')}/org.xquartz:0"
        try:
            subprocess.run(
                ["xdotool", "windowfocus", str(int(window_id))],
                capture_output=True,
                timeout=5,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            # No xdotool or no X server: the global tap still carries the event
            # to whichever X11 window the server last had focused.
            pass

    def _wake(self, window: dict[str, Any]) -> bool:
        """Restores a minimized target so it can receive events again.

        Returns ``True`` when the window had to be un-minimized, which is what
        the caller needs in order to put it back afterwards.

        A window without an accessibility tree (X11 clients such as XQuartz
        answer ``AXUIElementCreateApplication`` with no windows) cannot be
        queried for minimisation. That is not a failure: an unqueryable window
        is treated as visible, because the only thing a wrong guess costs is
        the ``sleep`` that would follow a restore that never happened.
        """
        if not self.quiet:
            return False
        payload = self._run_helper([
            "wake",
            "--pid", str(int(window.get("pid") or 0)),
            "--title", str(window.get("title") or ""),
        ])
        if payload.get("ok"):
            return bool(payload.get("was_minimized"))
        if str(payload.get("code", "")) == "window_not_found":
            # No accessibility tree: nothing to restore, nothing to put back.
            return False
        raise TransportError(
            str(payload.get("code", "wake_failed")),
            str(payload.get("message", "the target window cannot be restored")),
            True,
        )

    def _sleep(self, window: dict[str, Any], was_minimized: bool) -> None:
        """Puts the target back into the minimized state the caller found it in."""
        if not self.quiet or not was_minimized:
            return
        self._run_helper([
            "sleep",
            "--pid", str(int(window.get("pid") or 0)),
            "--title", str(window.get("title") or ""),
            "--minimized", "1",
        ])

    # ── window discovery ────────────────────────────────────────────────
    def find_window(self) -> dict[str, Any]:
        """Resolves the configured window; ``{"ok": false, ...}`` when absent."""
        if self._window_provider is not None:
            result = self._window_provider()
            if not isinstance(result, dict):
                raise TransportError("helper_failed", "the window provider returned no object", False)
        else:
            argv = ["find"]
            if self.config.window_id is not None:
                argv += ["--id", str(self.config.window_id)]
            else:
                if self.config.window_app:
                    argv += ["--app", self.config.window_app]
                if self.config.window_title:
                    argv += ["--title", self.config.window_title]
            result = self._run_helper(argv)
        window = result.get("window") if result.get("ok") else None
        if isinstance(window, dict) and window.get("id") is not None:
            self.connected = True
            self.state = "connected"
            self.port = f"window:{window['id']}"
            return {"ok": True, "window": dict(window)}
        self.connected = False
        self.state = "disconnected"
        return {
            "ok": False,
            "code": str(result.get("code", "window_not_found")),
            "message": str(result.get("message", "the configured window is not open")),
        }

    def capture_window_png(
        self, output_path: Path
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Writes one PNG of the window; returns ``(window, failure)``."""
        found = self.find_window()
        if not found.get("ok"):
            return None, {
                "ok": False,
                "status": "error",
                "error": {
                    "code": str(found.get("code", "window_not_found")),
                    "message": str(found.get("message", "the configured window is not open")),
                    "retryable": True,
                },
            }
        window = dict(found["window"])
        if self._capture_provider is not None:
            self._capture_provider(window, output_path)
            return window, None
        if sys.platform != "darwin":
            return None, _capture_failure(
                "window_transport_unsupported",
                "window capture needs macOS (screencapture -l)",
                retryable=False,
            )
        try:
            completed = subprocess.run(
                ["/usr/sbin/screencapture", "-l", str(int(window["id"])), "-o", "-x", str(output_path)],
                capture_output=True,
                timeout=CAPTURE_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, _capture_failure("screen_capture_failed", f"the window capture failed: {exc}")
        if completed.returncode != 0 or not output_path.is_file():
            detail = completed.stderr.decode("utf-8", errors="replace").strip()[:2000]
            return None, _capture_failure(
                "screen_capture_failed",
                f"the window capture failed ({completed.returncode}): {detail}",
            )
        return window, None

    # ── SerialLink-compatible surface ───────────────────────────────────
    def start(self) -> None:
        return None

    def wait_connected(self, timeout: float = 5.0) -> bool:
        try:
            self.find_window()
        except TransportError:
            self.connected = False
            self.state = "disconnected"
        return self.connected

    def subscribe(
        self,
        channel: str,
        handler: Callable[[dict[str, Any]], Any],
        session_id: str | None = None,
    ) -> Callable[[], None]:
        """Registers a frame handler, with the ``(channel, session)`` fallback.

        The handlers only receive frames the clipboard engine dispatches; on a
        link without the clipboard channel nothing ever dispatches, so the
        registration stays inert instead of inventing frames.
        """
        key = (channel, session_id)
        with self._cond:
            self._handlers.setdefault(key, []).append(handler)

        def unsubscribe() -> None:
            with self._cond:
                handlers = self._handlers.get(key, [])
                if handler in handlers:
                    handlers.remove(handler)
                if not handlers:
                    self._handlers.pop(key, None)

        return unsubscribe

    def subscribe_state(self, handler: Callable[[str], Any]) -> Callable[[], None]:
        with self._cond:
            self._state_handlers.append(handler)
            state = self.state
        handler(state)

        def unsubscribe() -> None:
            with self._cond:
                if handler in self._state_handlers:
                    self._state_handlers.remove(handler)

        return unsubscribe

    def close(self, clean: bool = False) -> None:
        with self._cond:
            self._stop_engine = True
            self._cond.notify_all()
            worker = self._worker
        # The worker may be inside a helper call; a bounded join keeps close
        # responsive without leaving a thread that still writes the pasteboard.
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2)
        self._fail_pending(TransportError("shutdown", "link closed", False))
        # A burst interrupted by a close must not leave its packet (or lose the
        # operator's text) on the pasteboard.
        self._quiesce_restore()
        if clean:
            self.link_path.unlink(missing_ok=True)

    # ── request dispatch ────────────────────────────────────────────────
    def request(
        self,
        channel: str,
        op: str,
        body: dict[str, Any] | None = None,
        timeout: float = 6.0,
    ) -> dict[str, Any]:
        if channel != "hid":
            return self._agent_request(channel, op, dict(body or {}), timeout)
        found = self.find_window()
        if not found.get("ok"):
            raise TransportError(
                str(found.get("code", "window_not_found")),
                str(found.get("message", "the configured window is not open")),
                True,
            )
        window = found["window"]
        body = dict(body or {})
        handler = getattr(self, f"_op_{op}", None)
        if handler is None:
            raise TransportError(
                "unsupported_hid_op",
                f"the window transport does not implement hid.{op}",
                False,
            )
        if not self.quiet or op in _NO_INJECTION_OPS:
            return {"body": handler(body, window)}
        # Quiet mode: a minimized window has no key window inside its process,
        # so injected events are silently dropped. Restore it just long enough
        # to deliver the operation and put it back exactly as it was, without
        # touching the operator's frontmost application.
        was_minimized = self._wake(window)
        try:
            return {"body": handler(body, window)}
        finally:
            self._sleep(window, was_minimized)

    def send_data(self, channel: str, session_id: str, seq: int, data: bytes) -> None:
        """Sends one block and waits for its acknowledgement."""
        self._require_agent_channel()
        if len(data) > DATA_CHUNK_BYTES:
            raise TransportError("chunk_too_large", "chunk over 512 bytes", False)
        key = (session_id, seq)
        for attempt in range(self.config.ack_retries + 1):
            future: concurrent.futures.Future[None] = concurrent.futures.Future()
            with self._cond:
                self._acks[key] = future
                self._active_ops += 1
                self._outbound.append(base_frame(
                    self.link_id, channel, "data",
                    session_id=session_id, seq=seq, data_b64=data_b64(data),
                ))
                self._wake_engine_locked()
            try:
                future.result(timeout=self.config.ack_timeout_s)
                return
            except concurrent.futures.TimeoutError:
                continue
            finally:
                with self._cond:
                    if self._acks.get(key) is future:
                        self._acks.pop(key, None)
                    self._active_ops -= 1
        raise TransportError(
            "ack_timeout", f"no ACK for {session_id}:{seq}", True, session_id=session_id,
        )

    def stream_data(
        self,
        channel: str,
        session_id: str,
        read_chunk: Callable[[], bytes],
        window: int = DATA_WINDOW_DEFAULT,
        on_ack: Callable[[int], None] | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> int:
        """Streams blocks with a sliding window of individual ACKs.

        ``read_chunk`` returns the next block (<=512 bytes) or ``b""`` at the
        end; it is read only when there is room in the window. Expired blocks
        are resent in sequence order and the attempt is exhausted after
        ``ack_retries`` resends. Returns the acknowledged bytes.
        """
        self._require_agent_channel()
        if window < 1 or window > DATA_WINDOW_MAX:
            raise ValueError(f"window must be between 1 and {DATA_WINDOW_MAX}")
        # seq -> [data, future, retries, deadline]
        pending: dict[int, list[Any]] = {}
        next_seq = 0
        eof = False
        acked_bytes = 0
        timeout_s = self.config.ack_timeout_s
        try:
            while pending or not eof:
                if stop_check is not None and stop_check():
                    raise TransportError(
                        "cancelled", "transfer cancelled", False, session_id=session_id,
                    )
                while not eof and len(pending) < window:
                    chunk = read_chunk()
                    if not chunk:
                        eof = True
                        break
                    if len(chunk) > DATA_CHUNK_BYTES:
                        raise TransportError("chunk_too_large", "chunk over 512 bytes", False)
                    seq = next_seq
                    next_seq += 1
                    future: concurrent.futures.Future[None] = concurrent.futures.Future()
                    with self._cond:
                        self._acks[(session_id, seq)] = future
                        self._active_ops += 1
                        self._outbound.append(base_frame(
                            self.link_id, channel, "data",
                            session_id=session_id, seq=seq, data_b64=data_b64(chunk),
                        ))
                        self._wake_engine_locked()
                    pending[seq] = [chunk, future, 0, time.monotonic() + timeout_s]
                if not pending:
                    break
                # Wake up as soon as any ACK arrives, without losing notices.
                with self._cond:
                    while True:
                        if any(entry[1].done() for entry in pending.values()):
                            break
                        remaining = min(entry[3] for entry in pending.values()) - time.monotonic()
                        if remaining <= 0:
                            break
                        self._cond.wait(remaining)
                now = time.monotonic()
                for seq in [item for item in pending if pending[item][1].done()]:
                    data, future, _attempts, _deadline = pending.pop(seq)
                    with self._cond:
                        if self._acks.get((session_id, seq)) is future:
                            self._acks.pop((session_id, seq), None)
                        self._active_ops -= 1
                    exc = future.exception()
                    if exc is not None:
                        raise exc
                    acked_bytes += len(data)
                    if on_ack is not None:
                        on_ack(acked_bytes)
                for seq in sorted(item for item in pending if now >= pending[item][3]):
                    data, future, attempts, _deadline = pending[seq]
                    if attempts >= self.config.ack_retries:
                        raise TransportError(
                            "ack_timeout",
                            f"no ACK for {session_id}:{seq}",
                            True,
                            session_id=session_id,
                        )
                    pending[seq][2] = attempts + 1
                    pending[seq][3] = now + timeout_s
                    with self._cond:
                        self._outbound.append(base_frame(
                            self.link_id, channel, "data",
                            session_id=session_id, seq=seq, data_b64=data_b64(data),
                        ))
                        self._wake_engine_locked()
            return acked_bytes
        finally:
            with self._cond:
                for seq in list(pending):
                    future = pending[seq][1]
                    self._acks.pop((session_id, seq), None)
                    self._active_ops -= 1
                    if not future.done():
                        future.set_exception(TransportError(
                            "cancelled", "transfer interrupted", False,
                            session_id=session_id,
                        ))

    # ── clipboard CDC engine ────────────────────────────────────────────
    def _require_agent_channel(self) -> None:
        """Rejects the remote channels the configured protocol cannot carry."""
        if self.agent_protocol == "clipboard":
            return
        if self.agent_protocol == "visual":
            raise TransportError(
                "cdc_unavailable",
                "visual agent_protocol is not implemented yet; use agent_protocol='clipboard'",
                False,
            )
        raise TransportError(
            "cdc_unavailable",
            "window transport has no CDC channel with agent_protocol=none; "
            "set agent_protocol='clipboard' to enable the slave agent",
            False,
        )

    def _agent_request(
        self, channel: str, op: str, body: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        """One correlated request over the clipboard, waiting for its frame."""
        self._require_agent_channel()
        with self._cond:
            if len(self._pending_requests) >= 256:
                raise TransportError("too_many_requests", "too many pending requests", True)
            request_id = next(self._request_ids)
            future: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
            self._pending_requests[request_id] = future
            try:
                frame = base_frame(
                    self.link_id, channel, "request",
                    request_id=request_id, op=op, body=body,
                )
            except ValueError as exc:
                self._pending_requests.pop(request_id, None)
                raise TransportError(
                    str(getattr(exc, "code", "invalid_frame")),
                    str(getattr(exc, "message", str(exc))),
                    False,
                ) from exc
            self._active_ops += 1
            self._outbound.append(frame)
            self._wake_engine_locked()
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            error = TransportError(
                "request_timeout", f"timeout on {channel}.{op}", True, request_id=request_id,
            )
            with self._cond:
                pending = self._pending_requests.pop(request_id, None)
            if pending is not None and not pending.done():
                pending.set_exception(error)
            raise error from None
        finally:
            with self._cond:
                self._pending_requests.pop(request_id, None)
                self._active_ops -= 1
                self._cond.notify_all()

    @contextlib.contextmanager
    def clipboard_activity(self):
        """Keeps the exchange polling while a multi-step operation waits.

        A bootstrap types the stub and then waits for a frame the slave writes
        on its own (``stage.ready``, ``loader.ready``, ``control.hello``); no
        request is in flight during that wait, so the caller declares the
        window instead of the link inventing background polling.
        """
        if self.agent_protocol != "clipboard":
            yield
            return
        with self._cond:
            self._active_ops += 1
            self._wake_engine_locked()
        try:
            yield
        finally:
            with self._cond:
                self._active_ops -= 1
                self._cond.notify_all()

    def clipboard_detach(self) -> None:
        """Stops the idle polling of a live agent that has been stopped."""
        with self._cond:
            self._agent_attached = False
            self._cond.notify_all()

    def _clipboard_call(self, argv: list[str]) -> dict[str, Any]:
        if self._clipboard_provider is not None:
            result = self._clipboard_provider(list(argv))
            if not isinstance(result, dict):
                raise TransportError(
                    "clipboard_failed", "the clipboard provider returned no object", False,
                )
            return result
        return self._run_helper(argv)

    def _clipboard_text(self) -> str:
        """The current pasteboard text, empty when it holds no string."""
        payload = self._clipboard_call(["clip-read"])
        if not payload.get("ok"):
            raise TransportError(
                "clipboard_failed",
                str(payload.get("message", "the pasteboard could not be read")),
                True,
            )
        encoded = str(payload.get("text_b64") or "")
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8", errors="replace")
        except ValueError:
            return ""

    def _clipboard_write(self, text: str, *, transient: bool) -> None:
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        payload = self._clipboard_call([
            "clip-write", "--text-b64", encoded, "--transient", "1" if transient else "0",
        ])
        if not payload.get("ok"):
            raise TransportError(
                "clipboard_failed",
                str(payload.get("message", "the pasteboard could not be written")),
                True,
            )

    def clipboard_write_text(self, text: str, *, transient: bool = True) -> None:
        """Publishes text on the pasteboard for the slave's stage-1 launcher.

        The bootstrap types only `iex (Get-Clipboard -Raw)`; the stub itself
        arrives through here, so a client that mangles brackets or commas on the
        keyboard cannot corrupt it.
        """
        self._clipboard_write(text, transient=transient)

    def clipboard_read_image(self) -> bytes | None:
        """The PNG/TIFF image on the pasteboard, or None when there is none."""
        payload = self._clipboard_call(["clip-read-image"])
        if not payload.get("ok"):
            if payload.get("code") == "no_image":
                return None
            raise TransportError(
                "clipboard_failed",
                str(payload.get("message", "the pasteboard could not be read")),
                True,
            )
        encoded = str(payload.get("image_b64") or "")
        if not encoded:
            return None
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError:
            return None

    def clipboard_write_image(self, image: bytes, *, transient: bool = True) -> None:
        """Puts one PNG (or TIFF) image on the pasteboard."""
        encoded = base64.b64encode(image).decode("ascii")
        payload = self._clipboard_call([
            "clip-write-image", "--png-b64", encoded,
            "--transient", "1" if transient else "0",
        ])
        if not payload.get("ok"):
            raise TransportError(
                "clipboard_failed",
                str(payload.get("message", "the pasteboard could not be written")),
                True,
            )

    def _snapshot_user_clipboard(self) -> None:
        """Saves the operator's text before the first packet replaces it."""
        if self._saved_user_clipboard is not None:
            return
        try:
            text = self._clipboard_text()
        except TransportError:
            return
        if not text.startswith("TITELLA_"):
            self._saved_user_clipboard = text

    def _write_packet(self, frames: list[dict[str, Any]]) -> None:
        """Writes one M2S packet; every header carries the consumed S2M seq.

        The clipboard is one shared slot, so the channel is strictly
        half-duplex: the master writes a packet, the slave consumes it and
        answers, and only then may the master write again. A write is therefore
        refused while the peer's packet is still unread — otherwise the two
        sides overwrite each other and whichever write wins destroys the other's
        payload.
        """
        self._snapshot_user_clipboard()
        if frames:
            # Half-duplex: never write a body over a peer packet still waiting
            # to be read. The outbound frames stay queued and go out on the next
            # pass, once the peer's packet has been consumed.
            if self._peer_owns_slot():
                with self._cond:
                    for frame in reversed(frames):
                        self._outbound.appendleft(frame)
                return
            self._m2s_seq += 1
            seq = self._m2s_seq
            body = b"".join(encode_frame(frame, MASTER_TO_FIRMWARE) for frame in frames)
            text = (
                f"{CLIPBOARD_M2S_PREFIX}{self.link_id}:{seq}:{self._last_s2m_seq}\n"
                + body.decode("utf-8")
            )
            self._clipboard_write(text, transient=True)
            self._written_packet = {"seq": seq, "text": text, "at": time.monotonic()}
            return
        # A pure transport ACK: no body lines, sequence 0 by contract. It is the
        # reply that hands the slot back, so it must not land on an unread peer
        # packet either.
        if self._peer_owns_slot():
            return
        text = f"{CLIPBOARD_M2S_PREFIX}{self.link_id}:0:{self._last_s2m_seq}\n"
        self._clipboard_write(text, transient=True)

    def _peer_owns_slot(self) -> bool:
        """True while the peer's S2M packet sits unread on the pasteboard.

        Half-duplex guard: a packet the master has not consumed yet belongs to
        the peer, and writing over it would lose it for ever because neither
        side keeps a retransmission buffer across a lost read.
        """
        try:
            text = self._clipboard_text()
        except TransportError:
            return False
        prefix = f"{CLIPBOARD_S2M_PREFIX}{self.link_id}:"
        if not text.startswith(prefix):
            return False
        header = text.split("\n", 1)[0]
        parts = header.split(":")
        if len(parts) < 3:
            return False
        try:
            seq = int(parts[2])
        except ValueError:
            return False
        return seq > self._last_s2m_seq

    def _quiesce_restore(self) -> None:
        """Puts the operator's clipboard back once nothing is in flight."""
        snapshot = self._saved_user_clipboard
        if snapshot is None:
            return
        self._saved_user_clipboard = None
        self._written_packet = None
        try:
            # The operator's own content is never marked transient: clipboard
            # history managers must keep treating it as a normal copy.
            self._clipboard_write(snapshot, transient=False)
        except TransportError:
            pass

    def _wake_engine_locked(self) -> None:
        """Starts the single worker (condition held) and wakes it."""
        worker = self._worker
        if worker is None or not worker.is_alive():
            self._worker = threading.Thread(
                target=self._clipboard_worker, name="window-clipboard-cdc", daemon=True,
            )
            self._worker.start()
        self._cond.notify_all()

    def _engine_busy(self) -> bool:
        if self._outbound or self._pending_requests or self._acks or self._active_ops:
            return True
        return (time.monotonic() - self._last_inbound) < CLIPBOARD_INBOUND_GRACE_S

    def _clipboard_worker(self) -> None:
        """Single owner of the pasteboard while the clipboard channel is used.

        It writes pending frames, reads the peer's packets, resolves the
        futures they answer, acknowledges both directions and restores the
        operator's clipboard when the burst ends. With no agent attached and
        nothing in flight it parks on the condition and never touches the
        pasteboard at all.
        """
        while True:
            with self._cond:
                while (
                    not self._stop_engine
                    and not self._outbound
                    and not self._engine_busy()
                    and not self._agent_attached
                ):
                    self._cond.wait(0.5)
                if self._stop_engine:
                    return
                frames: list[dict[str, Any]] = []
                while self._outbound and len(frames) < CLIPBOARD_PACKET_MAX_FRAMES:
                    frames.append(self._outbound.popleft())
            try:
                if frames:
                    self._write_packet(frames)
                self._poll_inbound()
                self._maybe_rewrite()
            except TransportError:
                # A failed helper call is retried on the next cadence; pending
                # futures time out on their own instead of a worker dying.
                pass
            except Exception:
                pass
            busy_now = self._engine_busy()
            if not busy_now:
                # Nothing is in flight: no packet may be rewritten afterwards.
                self._written_packet = None
                self._quiesce_restore()
            with self._cond:
                if self._stop_engine:
                    return
                if self._outbound or self._pending_requests or self._acks:
                    timeout = CLIPBOARD_POLL_FAST_S
                elif busy_now:
                    timeout = CLIPBOARD_POLL_STEP_S
                elif self._agent_attached:
                    timeout = CLIPBOARD_POLL_SLOW_S
                else:
                    timeout = None
            if timeout is not None:
                time.sleep(timeout)

    def _poll_inbound(self) -> None:
        """Reads one S2M packet, dispatches its frames and acknowledges it."""
        text = self._clipboard_text()
        prefix = f"{CLIPBOARD_S2M_PREFIX}{self.link_id}:"
        if not text.startswith(prefix):
            return
        # A clipboard sync may normalize the line endings of the packet (a
        # Windows peer keeps CRLF); JSON escapes every control character, so a
        # raw CR can only be a line separator and is never part of a frame.
        text = text.replace("\r\n", "\n")
        header, _separator, body = text.partition("\n")
        parts = header.split(":")
        if len(parts) < 4:
            return
        try:
            s2m_seq = int(parts[2])
            ack_m2s = int(parts[3])
        except ValueError:
            return
        self._m2s_acked = max(self._m2s_acked, ack_m2s)
        if s2m_seq == 0:
            return
        if s2m_seq <= self._last_s2m_seq:
            # The slave has not seen our ACK yet. Repeat it only while this
            # side is actually waiting, never in an idle loop: the slave's next
            # exchange re-reads it anyway.
            if self._engine_busy():
                self._write_packet([])
            return
        self._last_s2m_seq = s2m_seq
        for line in body.split("\n"):
            if not line.strip():
                continue
            # One line per frame: the bounded parser never buffers a whole
            # packet, so a wide window cannot overflow it.
            for frame in self._clip_parser.feed((line + "\n").encode("utf-8")):
                self._dispatch_frame(frame)
        with self._cond:
            if self._outbound:
                # The next packet header carries this acknowledgement already.
                return
        self._write_packet([])

    def _maybe_rewrite(self) -> None:
        """Re-writes an unacknowledged packet so a lost sync is recovered."""
        written = self._written_packet
        if written is None:
            return
        if self._m2s_acked >= int(written["seq"]):
            self._written_packet = None
            return
        if time.monotonic() - float(written["at"]) >= CLIPBOARD_REWRITE_S:
            self._clipboard_write(str(written["text"]), transient=True)
            written["at"] = time.monotonic()

    def _dispatch_frame(self, frame: dict[str, Any]) -> None:
        if frame.get("link_id") != self.link_id:
            return
        now = time.monotonic()
        self.last_seen = now
        self._last_inbound = now
        self._agent_attached = True
        self._set_state("connected")
        kind = frame.get("type")
        if kind in {"response", "error"} and isinstance(frame.get("request_id"), int):
            with self._cond:
                future = self._pending_requests.pop(frame["request_id"], None)
            if future is None or future.done():
                return
            if kind == "error":
                body = frame.get("body") or {}
                future.set_exception(TransportError(
                    str(body.get("code", "remote_error")),
                    str(body.get("message", "remote error")),
                    bool(body.get("retryable", False)),
                    request_id=frame["request_id"],
                ))
            else:
                future.set_result(frame)
            return
        if kind == "ack":
            key = (frame.get("session_id"), frame.get("ack_seq"))
            with self._cond:
                future = self._acks.get(key)
                if future is not None and not future.done():
                    future.set_result(None)
                self._cond.notify_all()
            return
        if kind == "data":
            key = (frame["link_id"], frame.get("session_id"), frame.get("seq"))
            # An already-applied duplicate is acknowledged again without
            # reapplying its bytes; a new frame is registered only when a
            # handler accepted it, so a rejected slot is never falsely ACKed.
            if not self._dedup.seen(key):
                if not self._notify(frame):
                    return
                self._dedup.add(key)
            self._queue_frame(base_frame(
                self.link_id, frame["channel"], "ack",
                session_id=frame["session_id"], ack_seq=frame["seq"],
            ))
            return
        self._notify(frame)

    def _queue_frame(self, frame: dict[str, Any]) -> None:
        with self._cond:
            self._outbound.append(frame)
            self._wake_engine_locked()

    def _notify(self, frame: dict[str, Any]) -> bool:
        session_id = frame.get("session_id") if isinstance(frame.get("session_id"), str) else None
        with self._cond:
            handlers = list(self._handlers.get((frame["channel"], session_id), ()))
            if session_id is not None:
                # Without a session both keys are the same one: a handler must
                # never be called twice for one frame.
                handlers += list(self._handlers.get((frame["channel"], None), ()))
        try:
            for handler in handlers:
                if handler(frame) is False:
                    return False
        except Exception:
            return False
        return True

    def _set_state(self, state: str) -> None:
        with self._cond:
            if self.state == state:
                return
            self.state = state
            handlers = list(self._state_handlers)
        for handler in handlers:
            try:
                handler(state)
            except Exception:
                pass

    def _fail_pending(self, exc: TransportError) -> None:
        with self._cond:
            requests = list(self._pending_requests.values())
            self._pending_requests.clear()
            acks = list(self._acks.values())
            self._acks.clear()
            self._cond.notify_all()
        for future in requests:
            if not future.done():
                future.set_exception(exc)
        for future in acks:
            if not future.done():
                future.set_exception(exc)

    # ── HID operations ──────────────────────────────────────────────────
    def _op_health(self, body: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "hid_ready",
            "firmware": "window-1.0",
            "protocol": 1,
            "window": dict(window),
        }

    def _op_release_all(self, body: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
        held = self._buttons_mask
        released: list[str] = []
        if held:
            x, y = self._pointer_position(window)
            for bit, name in _BUTTON_NAMES.items():
                if held & bit:
                    self._inject([
                        "mouse", "--action", "up", "--x", str(x), "--y", str(y),
                        "--button", name, "--held", str(held),
                        *self._target(window),
                    ])
                    released.append(name)
        self._buttons_mask = 0
        return {"ok": True, "released": released}

    def _op_kb_layout(self, body: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
        layout = str(body.get("layout", "")).strip()
        if layout not in {"es_ES", "en_US"}:
            raise ValueError("layout must be es_ES or en_US")
        self._layout = layout
        return {"ok": True, "layout": layout}

    def _op_kb_text(self, body: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
        text = str(body.get("text", ""))
        delay = int(body.get("delay", DEFAULT_UNICODE_DELAY_MS) or 0)
        enter = bool(body.get("enter", False))
        self._type_unicode(window, text, delay, enter)
        return {"ok": True, "characters": len(text), "enter": enter}

    def _op_kb_raw(self, body: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
        text = body.get("text")
        if isinstance(text, str):
            # HidController carries the validated literal text for this
            # transport: exact characters beat any keycode reconstruction.
            return self._op_kb_text(body, window)
        steps = decode_steps(list(body.get("steps") or []))
        delay = int(body.get("delay", DEFAULT_UNICODE_DELAY_MS) or 0)
        typed = 0
        for mod, usage in steps:
            keycode = _HID_USAGE_KEYCODES.get(usage)
            if keycode is None:
                raise TransportError(
                    "unsupported_hid_step",
                    f"HID usage 0x{usage:02x} has no macOS keycode on the window transport",
                    False,
                )
            self._inject([
                "key", "--pid", str(int(window.get("pid") or 0)),
                "--keycode", str(keycode),
                "--flags", str(_hid_modifier_flags(mod)),
                *self._target(window),
            ])
            typed += 1
            if delay > 0:
                time.sleep(delay / 1000.0)
        if body.get("enter"):
            self._inject([
                "key", "--pid", str(int(window.get("pid") or 0)),
                "--keycode", "36", "--flags", "0",
                *self._target(window),
            ])
            typed += 1
        return {"ok": True, "steps": len(steps), "keys": typed}

    def _op_kb_key(self, body: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
        key = str(body.get("key", ""))
        mods = list(body.get("mods") or [])
        keycode, flags = macos_key_event(key, mods)
        self._inject([
            "key", "--pid", str(int(window.get("pid") or 0)),
            "--keycode", str(keycode), "--flags", str(flags),
            *self._target(window),
        ])
        return {"ok": True, "key": key, "keycode": keycode, "flags": flags}

    def _op_mouse_abs(self, body: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
        x, y = int(body.get("x", 0)), int(body.get("y", 0))
        if not 0 <= x <= 32767 or not 0 <= y <= 32767:
            raise ValueError("mouse_abs coordinates must be within 0..32767")
        bounds = _window_bounds(window)
        screen_x, screen_y = self._hid_to_screen(x, y, bounds)
        # Clamped to the window: a HID coordinate can never land outside the
        # frame it was calibrated against.
        screen_x = _clamp(screen_x, bounds["x"], bounds["x"] + max(1.0, bounds["width"] - 1.0))
        screen_y = _clamp(screen_y, bounds["y"], bounds["y"] + max(1.0, bounds["height"] - 1.0))
        return self._pointer(window, round(screen_x), round(screen_y),
                             button=body.get("button"), state=str(body.get("state", "keep")),
                             double=bool(body.get("double", False)))

    def _op_mouse_rel(self, body: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
        dx, dy = int(body.get("x", 0)), int(body.get("y", 0))
        if not -127 <= dx <= 127 or not -127 <= dy <= 127:
            raise ValueError("mouse_rel requires x/y between -127 and 127")
        bounds = _window_bounds(window)
        current_x, current_y = self._pointer_position(window)
        screen_x = _clamp(current_x + dx, bounds["x"], bounds["x"] + max(1.0, bounds["width"] - 1.0))
        screen_y = _clamp(current_y + dy, bounds["y"], bounds["y"] + max(1.0, bounds["height"] - 1.0))
        return self._pointer(window, round(screen_x), round(screen_y),
                             button=body.get("button"), state=str(body.get("state", "keep")),
                             double=False)

    def _op_mouse_click(self, body: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
        button = str(body.get("button", "left") or "left")
        if button not in _BUTTON_BITS:
            raise ValueError("unknown button")
        state = str(body.get("state", "click") or "click")
        if state not in {"click", "down", "up"}:
            raise ValueError("state must be click, down or up")
        x, y = self._pointer_position(window)
        return self._pointer(window, x, y, button=button, state=state,
                             double=bool(body.get("double", False)))

    def _op_mouse_wheel(self, body: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
        delta = int(body.get("delta", 0))
        if not -127 <= delta <= 127:
            raise ValueError("delta must be between -127 and 127")
        x, y = self._pointer_position(window)
        self._inject([
            "mouse", "--action", "wheel", "--x", str(x), "--y", str(y),
            "--delta", str(delta), "--held", str(self._buttons_mask),
            *self._target(window),
        ])
        return {"ok": True, "delta": delta, "screen": [x, y], "window_id": int(window["id"])}

    # ── pointer bookkeeping ─────────────────────────────────────────────
    def _pointer(
        self,
        window: dict[str, Any],
        screen_x: int,
        screen_y: int,
        *,
        button: str | None,
        state: str,
        double: bool,
    ) -> dict[str, Any]:
        """Posts one pointer action and keeps the held-button model coherent."""
        if button is not None and button not in _BUTTON_BITS:
            raise ValueError("unknown button")
        if state not in {"keep", "down", "up", "click"}:
            raise ValueError("state must be keep, down, up or click")
        name = button or "left"
        bit = _BUTTON_BITS[name]
        held_before = self._buttons_mask
        if state == "down":
            # Activate before the first press of a drag or a click.
            self._focus(window)
            self._inject([
                "mouse", "--action", "down", "--x", str(screen_x), "--y", str(screen_y),
                "--button", name, "--held", str(held_before),
                *self._target(window),
            ])
            self._buttons_mask = held_before | bit
            action = "down"
        elif state == "up":
            # The move inside the release is still a drag: the button is held
            # until the release event itself.
            self._inject([
                "mouse", "--action", "up", "--x", str(screen_x), "--y", str(screen_y),
                "--button", name, "--held", str(held_before),
                *self._target(window),
            ])
            self._buttons_mask = held_before & ~bit
            action = "up"
        elif state == "click":
            self._focus(window)
            self._inject([
                "mouse", "--action", "click", "--x", str(screen_x), "--y", str(screen_y),
                "--button", name, "--held", str(held_before),
                "--double", "1" if double else "0",
                *self._target(window),
            ])
            action = "click"
        else:
            # "keep" carries the button without releasing it. A carried button
            # that is not down yet starts the drag where the pointer already
            # is, so the press lands before the move; a move with any button
            # held is a dragged event, which is what Citrix Viewer and RDP
            # read as drag-and-drop.
            if button is not None and not (held_before & bit):
                start_x, start_y = self._pointer_position(window)
                self._focus(window)
                self._inject([
                    "mouse", "--action", "down", "--x", str(start_x), "--y", str(start_y),
                    "--button", name, "--held", str(held_before),
                    *self._target(window),
                ])
                self._buttons_mask = held_before | bit
            self._inject([
                "mouse", "--action", "move", "--x", str(screen_x), "--y", str(screen_y),
                "--held", str(self._buttons_mask),
                *self._target(window),
            ])
            action = "drag" if self._buttons_mask else "move"
        self._last_x, self._last_y = float(screen_x), float(screen_y)
        return {
            "ok": True,
            "action": action,
            "screen": [screen_x, screen_y],
            "button": name,
            "held": self._buttons_mask,
            "window_id": int(window["id"]),
        }

    def _pointer_position(self, window: dict[str, Any]) -> tuple[int, int]:
        if self._last_x is not None and self._last_y is not None:
            return round(self._last_x), round(self._last_y)
        bounds = _window_bounds(window)
        return (
            round(bounds["x"] + bounds["width"] / 2.0),
            round(bounds["y"] + bounds["height"] / 2.0),
        )

    def _type_unicode(
        self, window: dict[str, Any], text: str, delay_ms: int, enter: bool
    ) -> None:
        """Types ``text`` as exact Unicode characters, Enter included."""
        pid = int(window.get("pid") or 0)
        if not text and not enter:
            return
        argv = [
            "unicode", "--pid", str(pid),
            "--text-b64", base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "--delay-ms", str(max(0, delay_ms)),
            "--enter", "1" if enter else "0",
            *self._target(window),
        ]
        units = len(text.encode("utf-16-le")) // 2
        timeout = UNICODE_TIMEOUT_BASE_S + units * (max(0, delay_ms) + 4) / 1000.0
        self._inject(argv, timeout=timeout)

    # ── coordinate mapping ──────────────────────────────────────────────
    def _capture_state(self) -> dict[str, Any] | None:
        """The last capture geometry, written by ``ScreenCapture.record``."""
        try:
            data = json.loads((self.state_dir / ".capture_res.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            width, height = int(data["w"]), int(data["h"])
        except (KeyError, TypeError, ValueError):
            return None
        if width <= 1 or height <= 1:
            return None
        return data

    def _hid_to_screen(self, hid_x: int, hid_y: int, bounds: dict[str, float]) -> tuple[float, float]:
        """Maps a 0..32767 HID coordinate back into screen points.

        The calibration comes from the last capture: the HID space is relative
        to the captured (possibly cropped) frame, and the raw frame maps 1:1
        onto the window bounds the helper reports, so a cropped capture still
        lands on the exact pixel.
        """
        state = self._capture_state()
        if state is None:
            return (
                bounds["x"] + hid_x / 32767.0 * max(1.0, bounds["width"] - 1.0),
                bounds["y"] + hid_y / 32767.0 * max(1.0, bounds["height"] - 1.0),
            )
        captured_width = int(state["w"])
        captured_height = int(state["h"])
        pixel_x = hid_x * max(1, captured_width - 1) / 32767.0
        pixel_y = hid_y * max(1, captured_height - 1) / 32767.0
        crop = state.get("crop_box")
        if isinstance(crop, dict):
            pixel_x += float(crop.get("x", 0))
            pixel_y += float(crop.get("y", 0))
            raw_width = float(state.get("raw_w") or (float(crop.get("x", 0)) + captured_width))
            raw_height = float(state.get("raw_h") or (float(crop.get("y", 0)) + captured_height))
        else:
            raw_width = float(state.get("raw_w") or captured_width)
            raw_height = float(state.get("raw_h") or captured_height)
        scale_x = bounds["width"] / max(1.0, raw_width)
        scale_y = bounds["height"] / max(1.0, raw_height)
        return (
            bounds["x"] + pixel_x * scale_x,
            bounds["y"] + pixel_y * scale_y,
        )


def _parse_helper_stdout(raw: bytes) -> dict[str, Any] | None:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _capture_failure(code: str, message: str, *, retryable: bool = True) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "error",
        "error": {"code": code, "message": message, "retryable": retryable},
    }


def _window_bounds(window: dict[str, Any]) -> dict[str, float]:
    bounds = window.get("bounds") if isinstance(window.get("bounds"), dict) else {}
    return {
        "x": float(bounds.get("x", 0) or 0),
        "y": float(bounds.get("y", 0) or 0),
        "width": float(bounds.get("width", 1) or 1),
        "height": float(bounds.get("height", 1) or 1),
    }


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


__all__ = [
    "WindowLink",
    "decode_steps",
    "ensure_helper",
    "helper_digest",
    "helper_path",
    "macos_key_event",
]
