#!/usr/bin/env python3
"""
PyBrowser: Native macOS Chromium/Material 3 Web Browser (PyObjC & WebKit).
Self-healing Application Launcher and Entry Point.
"""

import sys
import os
import types
import signal
import traceback

# ============================================================================
# SELF-HEALING SYS.PATH RESOLUTION
# ============================================================================
current_dir = os.path.abspath(os.path.dirname(__file__))
parent_dir = os.path.abspath(os.path.join(current_dir, ".."))

# Case 1: Standard layout (main.py is in PyBrowser/, and pybrowser/ is a subfolder)
if os.path.isdir(os.path.join(current_dir, "pybrowser")):
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)

# Case 2: main.py is sitting inside the pybrowser/ folder itself
elif os.path.basename(current_dir).lower() == "pybrowser" or os.path.isfile(os.path.join(parent_dir, "pybrowser", "browser.py")):
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

# Case 3: Flat layout (all files are in the same folder with no pybrowser/ subfolder)
elif os.path.isfile(os.path.join(current_dir, "browser.py")):
    # Alias the current folder as the 'pybrowser' package so imports work seamlessly
    if "pybrowser" not in sys.modules:
        pkg = types.ModuleType("pybrowser")
        pkg.__path__ = [current_dir]
        sys.modules["pybrowser"] = pkg
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)

import Cocoa
from pybrowser.browser import AppDelegate

def sigint_handler(sig, frame):
    """Handle Unix SIGINT (Ctrl+C) to terminate Cocoa cleanly."""
    if Cocoa.NSApp:
        Cocoa.NSApp.terminate_(None)
    else:
        sys.exit(0)

def main():
    """Initializes the NSApplication run loop and AppDelegate."""
    signal.signal(signal.SIGINT, sigint_handler)
    try:
        app = Cocoa.NSApplication.sharedApplication()
        app.setActivationPolicy_(Cocoa.NSApplicationActivationPolicyRegular)
        
        delegate = AppDelegate.alloc().init()
        app.setDelegate_(delegate)
        app.activateIgnoringOtherApps_(True)

        # Periodic timer allowing Python run loop to process Unix signals
        Cocoa.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.5, True, lambda t: None)
        app.run()
    except Exception:
        traceback.print_exc()

if __name__ == "__main__":
    main()