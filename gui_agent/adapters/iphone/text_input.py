"""iPhone-mirroring text input via osascript (clipboard paste + key codes).

Lifted from the former top-level gui_agent.utils (S3 step 6). These drive the
macOS "iPhone Mirroring" app through System Events, so they are iphone-adapter
I/O. The zero-preempt daemon backend types via the client and bypasses these;
they remain the mirroir-backend fallback used by the executor.
"""

import subprocess
import time


def paste_text(text: str) -> None:
    """Select all existing content then paste text from clipboard (replaces, not appends).

    Ordering: activate first so Universal Clipboard sync completes, THEN set
    the Mac clipboard. This prevents iOS's Universal Clipboard from overwriting
    our clipboard content between pbcopy and Cmd+V.
    """
    subprocess.run([
        "osascript", "-e",
        'tell application "iPhone Mirroring" to activate',
    ], capture_output=True)
    time.sleep(0.4)
    subprocess.run([
        "osascript", "-e",
        'tell application "System Events" to keystroke "a" using command down'
    ], check=True)
    time.sleep(0.2)
    subprocess.run(["pbcopy"], input=text.encode(), check=True)
    time.sleep(1.5)  # Universal Clipboard sync Mac→iOS typically takes ~0.5-1s
    subprocess.run([
        "osascript", "-e",
        'tell application "System Events" to keystroke "v" using command down'
    ], check=True)


def clear_text_field() -> None:
    """Clear the focused text field.

    Strategy: Cmd+A + paste-empty first (atomic replace if selection works),
    then a fallback loop of 100 backspaces to handle apps where Cmd+A does not
    select all (e.g. 美团 search box). The backspace loop is a no-op when the
    field is already empty, so it's safe to always run.
    """
    subprocess.run([
        "osascript", "-e",
        'tell application "iPhone Mirroring" to activate',
    ], capture_output=True)
    time.sleep(0.3)
    subprocess.run([
        "osascript", "-e",
        'tell application "System Events" to keystroke "a" using command down'
    ], check=True)
    time.sleep(0.2)
    subprocess.run(["pbcopy"], input=b"", check=True)
    subprocess.run([
        "osascript", "-e",
        'tell application "System Events" to keystroke "v" using command down'
    ], check=True)
    time.sleep(0.15)
    # Fallback for search fields where Cmd+A doesn't select all:
    # move cursor to end of line, then 30 backspaces.
    # 30 chars covers typical search queries; safe no-op on an empty field.
    subprocess.run([
        "osascript", "-e",
        'tell application "System Events" to key code 124 using command down'
    ], check=True)
    time.sleep(0.05)
    subprocess.run([
        "osascript", "-e",
        'tell application "System Events"\nrepeat 30 times\nkey code 51\nend repeat\nend tell'
    ], check=True)


def press_enter() -> None:
    """Send Return key to the focused field (e.g. submit search)."""
    subprocess.run([
        "osascript", "-e",
        'tell application "iPhone Mirroring" to activate',
    ], capture_output=True)
    time.sleep(0.3)
    subprocess.run([
        "osascript", "-e",
        'tell application "System Events" to key code 36'
    ], check=True)
