"""
PyBrowser Task Manager & Process Controller.
HUD monitor for WebKit tabs, Host memory/CPU metrics, and live PTY sessions.
"""

import Cocoa
import objc
import os
import resource
import gc
import WebKit
from pybrowser.terminal import TERMINAL_MANAGER

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

class ProcessListView(Cocoa.NSView):
    def isFlipped(self):
        return True

class ProcessManagerController(Cocoa.NSWindowController):
    """Native macOS HUD Process & Task Manager."""
    def init(self):
        w, h = 780, 540
        window = Cocoa.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            Cocoa.NSMakeRect(0, 0, w, h),
            Cocoa.NSWindowStyleMaskTitled | Cocoa.NSWindowStyleMaskClosable | 
            Cocoa.NSWindowStyleMaskHUDWindow | Cocoa.NSWindowStyleMaskUtilityWindow | 
            Cocoa.NSWindowStyleMaskResizable,
            Cocoa.NSBackingStoreBuffered, False
        )
        window.setTitle_("PyBrowser Task Manager")
        window.setFloatingPanel_(True)
        window.setLevel_(Cocoa.NSFloatingWindowLevel)
        window.setReleasedWhenClosed_(False)
        window.center()
        
        self = objc.super(ProcessManagerController, self).initWithWindow_(window)
        if self:
            window.setDelegate_(self)
            self.filter_query = ""
            self.timer = None
            self.action_targets = {}
            self.setup_ui()
        return self
        
    def setup_ui(self):
        cv = self.window().contentView()
        w, h = cv.bounds().size.width, cv.bounds().size.height
        
        self.top_bar = Cocoa.NSView.alloc().initWithFrame_(Cocoa.NSMakeRect(0, h - 44, w, 44))
        self.top_bar.setAutoresizingMask_(Cocoa.NSViewWidthSizable | Cocoa.NSViewMinYMargin)
        cv.addSubview_(self.top_bar)
        
        self.search_field = Cocoa.NSSearchField.alloc().initWithFrame_(Cocoa.NSMakeRect(14, 8, 240, 26))
        self.search_field.setPlaceholderString_("Filter tasks & tabs…")
        self.search_field.setTarget_(self)
        self.search_field.setAction_("onSearchChange:")
        self.top_bar.addSubview_(self.search_field)
        
        btn_gc = Cocoa.NSButton.alloc().initWithFrame_(Cocoa.NSMakeRect(w - 290, 8, 140, 26))
        btn_gc.setTitle_("Purge Memory")
        btn_gc.setBezelStyle_(Cocoa.NSBezelStyleRounded)
        btn_gc.setAutoresizingMask_(Cocoa.NSViewMinXMargin)
        btn_gc.setTarget_(self)
        btn_gc.setAction_("onPurgeMemory:")
        self.top_bar.addSubview_(btn_gc)
        
        btn_sleep_all = Cocoa.NSButton.alloc().initWithFrame_(Cocoa.NSMakeRect(w - 145, 8, 135, 26))
        btn_sleep_all.setTitle_("Sleep Background")
        btn_sleep_all.setBezelStyle_(Cocoa.NSBezelStyleRounded)
        btn_sleep_all.setAutoresizingMask_(Cocoa.NSViewMinXMargin)
        btn_sleep_all.setTarget_(self)
        btn_sleep_all.setAction_("onSleepBackgroundTabs:")
        self.top_bar.addSubview_(btn_sleep_all)
        
        self.status_bar = Cocoa.NSView.alloc().initWithFrame_(Cocoa.NSMakeRect(0, 0, w, 28))
        self.status_bar.setAutoresizingMask_(Cocoa.NSViewWidthSizable | Cocoa.NSViewMaxYMargin)
        cv.addSubview_(self.status_bar)
        
        self.status_lbl = Cocoa.NSTextField.labelWithString_("Gathering process metrics…")
        self.status_lbl.setFrame_(Cocoa.NSMakeRect(14, 5, w - 28, 18))
        self.status_lbl.setFont_(Cocoa.NSFont.monospacedSystemFontOfSize_weight_(11, Cocoa.NSFontWeightRegular))
        self.status_lbl.setAutoresizingMask_(Cocoa.NSViewWidthSizable)
        self.status_bar.addSubview_(self.status_lbl)
        
        self.scroll_view = Cocoa.NSScrollView.alloc().initWithFrame_(Cocoa.NSMakeRect(0, 28, w, h - 72))
        self.scroll_view.setHasVerticalScroller_(True)
        self.scroll_view.setAutoresizingMask_(Cocoa.NSViewWidthSizable | Cocoa.NSViewHeightSizable)
        
        self.list_view = ProcessListView.alloc().initWithFrame_(Cocoa.NSMakeRect(0, 0, w, h - 72))
        self.scroll_view.setDocumentView_(self.list_view)
        cv.addSubview_(self.scroll_view)

    def onSearchChange_(self, sender):
        self.filter_query = self.search_field.stringValue().strip().lower()
        self.refresh_data()

    def show(self):
        self.window().makeKeyAndOrderFront_(None)
        self.refresh_data()
        if not self.timer:
            self.timer = Cocoa.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                2.5, True, lambda t: self.refresh_data()
            )

    def windowWillClose_(self, notif):
        if self.timer:
            self.timer.invalidate()
            self.timer = None

    def onPurgeMemory_(self, sender):
        try:
            from pybrowser.doh_server import DNS_CACHE, DNS_LOCK
            with DNS_LOCK:
                DNS_CACHE.clear()
        except Exception:
            pass

        try:
            from pybrowser.browser import FaviconCache
            FaviconCache.clear()
        except Exception:
            pass

        try:
            ds = WebKit.WKWebsiteDataStore.defaultDataStore()
            types = Cocoa.NSSet.setWithArray_([
                WebKit.WKWebsiteDataTypeMemoryCache,
                WebKit.WKWebsiteDataTypeDiskCache
            ])
            ds.removeDataOfTypes_modifiedSince_completionHandler_(
                types, Cocoa.NSDate.dateWithTimeIntervalSince1970_(0), lambda: None
            )
        except Exception:
            pass
        gc.collect()
        self.refresh_data()

    def onSleepBackgroundTabs_(self, sender):
        app_delegate = Cocoa.NSApp.delegate()
        if not app_delegate:
            return
        for wc in getattr(app_delegate, "controllers", []):
            for i, tab in enumerate(wc.tabs):
                if i != wc.active_tab_index and not tab.get("is_sleeping") and not tab.get("has_audio"):
                    wc.sleep_tab(i)
        self.refresh_data()

    def _find_tab(self, wc, tab_id):
        for i, t in enumerate(wc.tabs):
            if t.get("id") == tab_id:
                return i, t
        return None, None

    def refresh_data(self):
        if not self.window().isVisible():
            return
        procs = []
        app_delegate = Cocoa.NSApp.delegate()
        pid = Cocoa.NSProcessInfo.processInfo().processIdentifier()

        mem_rss_mb = 0.0
        cpu_percent = 0.0
        threads_count = 1

        if HAS_PSUTIL:
            try:
                p = psutil.Process()
                mem_rss_mb = p.memory_info().rss / (1024 * 1024)
                cpu_percent = p.cpu_percent(interval=None)
                threads_count = p.num_threads()
            except Exception:
                pass
        else:
            try:
                mem_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
            except Exception:
                pass

        procs.append({
            "name": "PyBrowser (Host Process)",
            "sub": f"PID: {pid} | Threads: {threads_count}",
            "type": "host",
            "stat": f"{cpu_percent:.1f}% CPU" if HAS_PSUTIL else "Active",
            "mem": f"{mem_rss_mb:.1f} MB",
            "is_sleeping": False,
            "has_audio": False
        })

        for sid, session in list(TERMINAL_MANAGER.sessions.items()):
            term_pid = session.proc.pid if session.proc else "-"
            procs.append({
                "name": f"Terminal ({session.cols}×{session.rows})",
                "sub": f"PTY Subprocess (PID: {term_pid})",
                "type": "terminal",
                "stat": "Subprocess Live" if session.alive else "Exited",
                "mem": "Active Shell",
                "is_sleeping": False,
                "has_audio": False,
                "session": session
            })

        total_tabs = 0
        sleeping_tabs = 0
        if app_delegate and hasattr(app_delegate, "controllers"):
            for wc in app_delegate.controllers:
                for i, tab in enumerate(wc.tabs):
                    total_tabs += 1
                    is_active = (i == wc.active_tab_index)
                    is_sleep = tab.get("is_sleeping", False)
                    if is_sleep:
                        sleeping_tabs += 1
                    has_audio = tab.get("has_audio", False)
                    title = tab.get("title") or "New Tab"
                    url = tab.get("url") or "about:blank"

                    if self.filter_query and (self.filter_query not in title.lower() and self.filter_query not in url.lower()):
                        continue

                    procs.append({
                        "name": f"Tab: {title[:48]}",
                        "sub": url[:60],
                        "type": "tab",
                        "stat": "Active View" if is_active else ("Sleeping" if is_sleep else "Background"),
                        "mem": "Hibernated" if is_sleep else "~45 MB WebKit",
                        "is_sleeping": is_sleep,
                        "has_audio": has_audio,
                        "wc": wc,
                        "tab_id": tab.get("id")
                    })

        self.status_lbl.setStringValue_(
            f"Host Memory: {mem_rss_mb:.1f} MB | Terminals: {len(TERMINAL_MANAGER.sessions)} | "
            f"Tabs: {total_tabs} ({sleeping_tabs} Sleeping)"
        )

        self.action_targets.clear()
        for sub in list(self.list_view.subviews()):
            sub.removeFromSuperview()

        row_h = 48
        w = max(self.scroll_view.bounds().size.width, 760)
        y = 0

        for idx_p, p in enumerate(procs):
            row = Cocoa.NSView.alloc().initWithFrame_(Cocoa.NSMakeRect(0, y, w, row_h))
            icon_str = "🖥️" if p["type"] == "host" else ("💻" if p["type"] == "terminal" else ("💤" if p["is_sleeping"] else ("🔊" if p["has_audio"] else "📑")))
            icon_lbl = Cocoa.NSTextField.labelWithString_(icon_str)
            icon_lbl.setFont_(Cocoa.NSFont.systemFontOfSize_(16))
            icon_lbl.setFrame_(Cocoa.NSMakeRect(14, 14, 24, 20))
            row.addSubview_(icon_lbl)

            title_lbl = Cocoa.NSTextField.labelWithString_(p["name"])
            title_lbl.setFont_(Cocoa.NSFont.boldSystemFontOfSize_(12))
            title_lbl.setFrame_(Cocoa.NSMakeRect(44, 24, 300, 18))
            row.addSubview_(title_lbl)

            sub_lbl = Cocoa.NSTextField.labelWithString_(p["sub"])
            sub_lbl.setFont_(Cocoa.NSFont.systemFontOfSize_(11))
            sub_lbl.setTextColor_(Cocoa.NSColor.secondaryLabelColor())
            sub_lbl.setFrame_(Cocoa.NSMakeRect(44, 6, 300, 16))
            row.addSubview_(sub_lbl)

            stat_lbl = Cocoa.NSTextField.labelWithString_(f"{p['stat']} | {p['mem']}")
            stat_lbl.setFont_(Cocoa.NSFont.monospacedSystemFontOfSize_weight_(11, Cocoa.NSFontWeightMedium))
            stat_lbl.setFrame_(Cocoa.NSMakeRect(350, 14, 170, 18))
            row.addSubview_(stat_lbl)

            if p["type"] == "tab":
                self.action_targets[idx_p] = ("tab", p["wc"], p["tab_id"])

                btn_sw = Cocoa.NSButton.alloc().initWithFrame_(Cocoa.NSMakeRect(w - 220, 11, 46, 24))
                btn_sw.setTitle_("Focus")
                btn_sw.setBezelStyle_(Cocoa.NSBezelStyleInline)
                btn_sw.setAutoresizingMask_(Cocoa.NSViewMinXMargin)
                btn_sw.setTarget_(self)
                btn_sw.setAction_("onTabFocus:")
                btn_sw.setTag_(idx_p)
                row.addSubview_(btn_sw)

                btn_sl = Cocoa.NSButton.alloc().initWithFrame_(Cocoa.NSMakeRect(w - 170, 11, 46, 24))
                btn_sl.setTitle_("Wake" if p["is_sleeping"] else "Sleep")
                btn_sl.setBezelStyle_(Cocoa.NSBezelStyleInline)
                btn_sl.setAutoresizingMask_(Cocoa.NSViewMinXMargin)
                btn_sl.setTarget_(self)
                btn_sl.setAction_("onTabToggleSleep:")
                btn_sl.setTag_(idx_p)
                row.addSubview_(btn_sl)

                btn_end = Cocoa.NSButton.alloc().initWithFrame_(Cocoa.NSMakeRect(w - 70, 11, 56, 24))
                btn_end.setTitle_("Close")
                btn_end.setBezelStyle_(Cocoa.NSBezelStyleInline)
                btn_end.setAutoresizingMask_(Cocoa.NSViewMinXMargin)
                btn_end.setTarget_(self)
                btn_end.setAction_("onTabClose:")
                btn_end.setTag_(idx_p)
                row.addSubview_(btn_end)

            elif p["type"] == "terminal":
                self.action_targets[idx_p] = ("terminal", p["session"])
                btn_kill = Cocoa.NSButton.alloc().initWithFrame_(Cocoa.NSMakeRect(w - 70, 11, 56, 24))
                btn_kill.setTitle_("Kill")
                btn_kill.setBezelStyle_(Cocoa.NSBezelStyleInline)
                btn_kill.setAutoresizingMask_(Cocoa.NSViewMinXMargin)
                btn_kill.setTarget_(self)
                btn_kill.setAction_("onTerminalKill:")
                btn_kill.setTag_(idx_p)
                row.addSubview_(btn_kill)

            div = Cocoa.NSBox.alloc().initWithFrame_(Cocoa.NSMakeRect(14, 0, w - 28, 1))
            div.setBoxType_(Cocoa.NSBoxSeparator)
            div.setAutoresizingMask_(Cocoa.NSViewWidthSizable)
            row.addSubview_(div)

            self.list_view.addSubview_(row)
            y += row_h

        self.list_view.setFrameSize_(Cocoa.NSMakeSize(w, max(self.scroll_view.bounds().size.height, y)))
        self.list_view.setNeedsDisplay_(True)

    def onTabFocus_(self, sender):
        tag = sender.tag()
        if tag in self.action_targets:
            ttype, wc, tab_id = self.action_targets[tag]
            if ttype == "tab":
                idx, _ = self._find_tab(wc, tab_id)
                if idx is not None:
                    wc.window().makeKeyAndOrderFront_(None)
                    wc.switch_to_tab(idx)

    def onTabToggleSleep_(self, sender):
        tag = sender.tag()
        if tag in self.action_targets:
            ttype, wc, tab_id = self.action_targets[tag]
            if ttype == "tab":
                idx, tab = self._find_tab(wc, tab_id)
                if idx is not None:
                    if tab.get("is_sleeping"):
                        wc.wake_tab(idx)
                    else:
                        wc.sleep_tab(idx)
                    self.refresh_data()

    def onTabClose_(self, sender):
        tag = sender.tag()
        if tag in self.action_targets:
            ttype, wc, tab_id = self.action_targets[tag]
            if ttype == "tab":
                idx, _ = self._find_tab(wc, tab_id)
                if idx is not None:
                    wc.close_tab(idx)
                    self.refresh_data()

    def onTerminalKill_(self, sender):
        tag = sender.tag()
        if tag in self.action_targets:
            target = self.action_targets[tag]
            if target[0] == "terminal":
                session = target[1]
                if session:
                    session.close()
                self.refresh_data()
