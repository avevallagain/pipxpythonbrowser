"""
PyBrowser: Native macOS Chromium/Material 3 Web Browser (PyObjC & WebKit).
Main Browser Module with Tab Management, UI Views, Omnibox, and App Delegate.
"""

import os
import sys
import json
import time
import uuid
import re
import ast
import operator
import threading
import ctypes
import base64
import urllib.parse
import urllib.request
import sqlite3
import traceback
import tempfile
import struct
import zipfile
import io
import html
from dataclasses import dataclass, asdict
from ctypes import c_void_p, c_uint32, c_char_p, POINTER, byref, string_at

import Cocoa
import Foundation
import WebKit
import objc

from pybrowser.settings import (
    SETTINGS, UA_PRESETS, DEFAULT_URL, DATA_DIR, SESSION_FILE, BOOKMARKS_FILE,
    SETTINGS_FILE, HISTORY_FILE, ADBLOCK_CACHE_FILE, DNS_CACHE_FILE, USERSCRIPTS_DIR,
    FILTER_LIST_URL, LOCAL_PROXY_PORT, BASE_ADBLOCK_DOMAINS, M3_COLORS, GROUP_COLORS,
    validate_and_get_download_dir, get_settings_html, sanitize_suggested_filename
)
from pybrowser.doh_server import (
    DNS_CACHE, DNS_LOCK, STEVENBLACK_DOMAINS, BASE_ADBLOCK_DOMAINS, ADBLOCK_LOCK,
    WK_RULE_LIST, is_domain_blocked, search_blocked_domains, load_dns_cache,
    start_proxy_server, create_nw_proxy_config, load_adblock_filters_from_disk,
    compile_webkit_adblock_rules, update_adblock_filters_from_web
)
from pybrowser.incognito import create_incognito_data_store, apply_incognito_preferences
from pybrowser.terminal import TERMINAL_MANAGER, dispatch_to_main
from pybrowser.task_manager import ProcessManagerController

# ============================================================================
# UTILITIES, DRAWING & KEYCHAIN
# ============================================================================
def hex_to_nscolor(hex_str):
    if not hex_str:
        return Cocoa.NSColor.clearColor()
    if hex_str.startswith("rgba"):
        parts = hex_str[5:-1].split(",")
        return Cocoa.NSColor.colorWithSRGBRed_green_blue_alpha_(
            float(parts[0]) / 255.0, float(parts[1]) / 255.0, float(parts[2]) / 255.0, float(parts[3])
        )
    clean_hex = hex_str.lstrip('#')
    if len(clean_hex) == 6:
        r, g, b = tuple(int(clean_hex[i:i+2], 16) for i in (0, 2, 4))
        return Cocoa.NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255.0, g / 255.0, b / 255.0, 1.0)
    elif len(clean_hex) == 8:
        r, g, b, a = tuple(int(clean_hex[i:i+2], 16) for i in (0, 2, 4, 6))
        return Cocoa.NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255.0, g / 255.0, b / 255.0, a / 255.0)
    return Cocoa.NSColor.labelColor()

def get_m3_color(token, view, is_incognito=False):
    if is_incognito and token == "tabstrip-bg":
        token = "tabstrip-incognito"
    elif is_incognito and token == "toolbar-bg":
        token = "toolbar-incognito"
    theme = SETTINGS.settings.get("theme_mode", "system")
    is_dark = is_incognito or (theme == "dark")
    if not is_dark and theme == "system" and view and hasattr(view, "effectiveAppearance") and view.effectiveAppearance():
        try:
            is_dark = "Dark" in str(view.effectiveAppearance().name())
        except Exception:
            pass
    colors = M3_COLORS.get(token, ("#FFFFFF", "#000000"))
    return hex_to_nscolor(colors[1 if is_dark else 0])

def atomic_write_json(filepath, data):
    try:
        dir_name = os.path.dirname(filepath) or "."
        fd, temp_path = tempfile.mkstemp(dir=dir_name)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(temp_path, filepath)
    except Exception as e:
        print(f"[Atomic JSON Write Error] {e}")

def download_image_data(img_url, dest_path, webview=None, completion_callback=None):
    if not img_url:
        if completion_callback: completion_callback(False)
        return False
    if img_url.startswith("data:"):
        try:
            raw = base64.b64decode(img_url.split(",", 1)[1])
            with open(dest_path, "wb") as f:
                f.write(raw)
            if completion_callback: completion_callback(True)
            return True
        except Exception:
            if completion_callback: completion_callback(False)
            return False
    if img_url.startswith("blob:"):
        if webview:
            dest_json = json.dumps(dest_path)
            js = f"""(async () => {{
                try {{
                    const res = await fetch("{img_url}");
                    const blob = await res.blob();
                    const reader = new FileReader();
                    reader.onloadend = () => {{
                        window.webkit.messageHandlers.bridge.postMessage({{cmd: 'blob_image_data', data: reader.result, path: {dest_json}}});
                    }};
                    reader.readAsDataURL(blob);
                }} catch(e) {{
                    window.webkit.messageHandlers.bridge.postMessage({{cmd: 'image_download_failed', path: {dest_json}}});
                }}
            }})();"""
            dispatch_to_main(lambda: webview.evaluateJavaScript_completionHandler_(js, None))
            return True
        if completion_callback: completion_callback(False)
        return False

    def worker():
        success = False
        try:
            referer = webview.URL().absoluteString() if (webview and webview.URL()) else img_url
            req = urllib.request.Request(img_url, headers={
                "User-Agent": SETTINGS.get_user_agent(),
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Referer": referer
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                with open(dest_path, "wb") as f:
                    f.write(resp.read())
            success = True
        except Exception:
            if webview:
                dest_json = json.dumps(dest_path)
                js = f"""(async () => {{
                    try {{
                        const res = await fetch("{img_url}", {{ credentials: 'include' }});
                        const blob = await res.blob();
                        const reader = new FileReader();
                        reader.onloadend = () => {{
                            window.webkit.messageHandlers.bridge.postMessage({{cmd: 'blob_image_data', data: reader.result, path: {dest_json}}});
                        }};
                        reader.readAsDataURL(blob);
                    }} catch(err) {{
                        window.webkit.messageHandlers.bridge.postMessage({{cmd: 'image_download_failed', path: {dest_json}}});
                    }}
                }})();"""
                dispatch_to_main(lambda: webview.evaluateJavaScript_completionHandler_(js, None))
                return
        if completion_callback:
            dispatch_to_main(lambda: completion_callback(success))

    threading.Thread(target=worker, daemon=True).start()
    return True

def safe_math_eval(expr):
    expr = expr.strip()
    if not expr or not re.search(r'[\+\-\*/%]', expr):
        return None
    expr = re.sub(r'([\d\.]+)\s*%\s*of\s*([\d\.]+)', r'(\1/100)*\2', expr)
    if not re.match(r'^[\d\.\s\+\-\*/\(\)]+$', expr):
        return None
    try:
        node = ast.parse(expr, mode='eval').body
        def _eval(n):
            if isinstance(n, (ast.Constant, ast.Num)):
                return getattr(n, 'value', getattr(n, 'n', None))
            elif isinstance(n, ast.BinOp):
                op = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}[type(n.op)]
                return op(_eval(n.left), _eval(n.right))
            elif isinstance(n, ast.UnaryOp):
                op = {ast.USub: operator.neg, ast.UAdd: operator.pos}[type(n.op)]
                return op(_eval(n.operand))
            raise ValueError()
        res = _eval(node)
        return str(int(res)) if (isinstance(res, float) and res.is_integer()) else str(round(res, 4))
    except Exception:
        return None

try:
    sec = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Security.framework/Security")
    SecKeychainAddGenericPassword = sec.SecKeychainAddGenericPassword
    SecKeychainAddGenericPassword.argtypes = [c_void_p, c_uint32, c_char_p, c_uint32, c_char_p, c_uint32, c_char_p, c_void_p]
    SecKeychainFindGenericPassword = sec.SecKeychainFindGenericPassword
    SecKeychainFindGenericPassword.argtypes = [c_void_p, c_uint32, c_char_p, c_uint32, c_char_p, POINTER(c_uint32), POINTER(c_void_p), POINTER(c_void_p)]
    SecKeychainItemFreeContent = sec.SecKeychainItemFreeContent
    SecKeychainItemDelete = sec.SecKeychainItemDelete
    SecKeychainItemDelete.argtypes = [c_void_p]
except Exception:
    pass

def save_to_keychain(host, username, password):
    try:
        srv, acc, pwd = host.encode('utf-8'), username.encode('utf-8'), password.encode('utf-8')
        length, data_ptr, item_ref = c_uint32(), c_void_p(), c_void_p()
        if SecKeychainFindGenericPassword(None, len(srv), srv, len(acc), acc, byref(length), byref(data_ptr), byref(item_ref)) == 0:
            if data_ptr.value: SecKeychainItemFreeContent(None, data_ptr)
            if item_ref.value: SecKeychainItemDelete(item_ref.value)
        SecKeychainAddGenericPassword(None, len(srv), srv, len(acc), acc, len(pwd), pwd, None)
    except Exception:
        pass

def get_from_keychain(host, username):
    try:
        srv, acc = host.encode('utf-8'), username.encode('utf-8')
        length, data_ptr, item_ref = c_uint32(), c_void_p(), c_void_p()
        if SecKeychainFindGenericPassword(None, len(srv), srv, len(acc), acc, byref(length), byref(data_ptr), byref(item_ref)) == 0:
            pwd = string_at(data_ptr.value, length.value).decode('utf-8')
            SecKeychainItemFreeContent(None, data_ptr)
            return pwd
    except Exception:
        pass
    return None

# ============================================================================
# USERSCRIPTS & EXTENSIONS (CRX)
# ============================================================================
def parse_userscript_meta(content):
    meta = {"name": "Unnamed Script", "match": [], "run-at": "document-end", "enabled": True}
    in_meta = False
    for line in content.splitlines():
        line = line.strip()
        if line == "// ==UserScript==":
            in_meta = True
        elif line == "// ==/UserScript==":
            break
        elif in_meta and line.startswith("// @"):
            parts = line.split(maxsplit=2)
            if len(parts) >= 3:
                key, val = parts[1][1:], parts[2]
                if key in ("match", "include"):
                    meta["match"].append(val)
                else:
                    meta[key] = val
    return meta

def get_installed_userscripts():
    scripts = []
    if not os.path.exists(USERSCRIPTS_DIR):
        return scripts
    for fname in os.listdir(USERSCRIPTS_DIR):
        if fname.endswith(".user.js") or fname.endswith(".user.js.disabled"):
            real_name = fname[:-9] if fname.endswith(".disabled") else fname
            path = os.path.join(USERSCRIPTS_DIR, fname)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()
                meta = parse_userscript_meta(content)
                meta["file"] = real_name
                meta["actual_path"] = path
                meta["code"] = content
                meta["enabled"] = not fname.endswith(".disabled")
                scripts.append(meta)
            except Exception:
                pass
    return scripts

def install_crx(ext_id_or_url):
    try:
        m = re.search(r'([a-z]{32})', ext_id_or_url.lower())
        ext_id = m.group(1) if m else ext_id_or_url.split("=")[-1].split("/")[-1].split("?")[0].strip()
        url = f"https://clients2.google.com/service/update2/crx?response=redirect&prodversion=131.0&x=id%3D{ext_id}%26installsource%3Dondemand%26uc"
        req = urllib.request.Request(url, headers={"User-Agent": UA_PRESETS["chrome_mac"]})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read()
        if len(data) < 16 or data[:4] != b'Cr24':
            return False
        version = struct.unpack("<I", data[4:8])[0]
        if version == 3:
            zip_data = data[12 + struct.unpack("<I", data[8:12])[0]:]
        elif version == 2:
            zip_data = data[16 + struct.unpack("<I", data[8:12])[0] + struct.unpack("<I", data[12:16])[0]:]
        else:
            return False

        with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
            manifest = json.loads(z.read("manifest.json").decode('utf-8', errors='ignore'))
            name = manifest.get("name", ext_id)
            content_scripts = manifest.get("content_scripts", [])

            combined_js = f"// ==UserScript==\n// @name {name} (CRX)\n// @match <all_urls>\n// ==/UserScript==\n"
            for cs in content_scripts:
                matches, css_files, js_files = cs.get("matches", []), cs.get("css", []), cs.get("js", [])
                match_regex = " || ".join([f"new RegExp('^' + '{m}'.replace(/\\*/g, '.*').replace(/\\//g, '\\\\/') + '$').test(location.href)" for m in matches]) or "true"
                combined_js += f"if ({match_regex}) {{\n"
                for css in css_files:
                    try:
                        css_content = z.read(css).decode('utf-8', errors='ignore').replace('`', '\\`').replace('$', '\\$')
                        combined_js += f"const st = document.createElement('style'); st.textContent = `{css_content}`; (document.head || document.documentElement).appendChild(st);\n"
                    except Exception:
                        pass
                for jf in js_files:
                    try:
                        combined_js += z.read(jf).decode('utf-8', errors='ignore') + "\n"
                    except Exception:
                        pass
                combined_js += "}\n"

            with open(os.path.join(USERSCRIPTS_DIR, f"{ext_id}.user.js"), "w", encoding="utf-8") as f:
                f.write(combined_js)

        dispatch_to_main(reload_all_extensions_views)
        return True
    except Exception as e:
        print(f"[CRX Install Error] {e}")
        return False

def reload_all_extensions_views():
    app_delegate = Cocoa.NSApp.delegate()
    if not app_delegate:
        return
    for wc in getattr(app_delegate, "controllers", []):
        for tab in wc.tabs:
            wv = tab.get("webview")
            if wv and wv.URL() and "browser://extensions" in wv.URL().absoluteString():
                wv.reload()

# ============================================================================
# SQLITE HISTORY & FAVICONS
# ============================================================================
def init_history_db():
    try:
        with sqlite3.connect(HISTORY_FILE) as conn:
            conn.execute("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE, title TEXT,
                    visit_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP, visit_count INTEGER DEFAULT 1
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_time ON history(visit_time DESC);")
    except Exception as e:
        print(f"[History DB Error] {e}")

def record_history(url, title):
    if not url or url.startswith("browser://"):
        return
    try:
        with sqlite3.connect(HISTORY_FILE) as conn:
            conn.execute("""
                INSERT INTO history (url, title, visit_time, visit_count) VALUES (?, ?, CURRENT_TIMESTAMP, 1)
                ON CONFLICT(url) DO UPDATE SET title = excluded.title, visit_time = CURRENT_TIMESTAMP, visit_count = visit_count + 1;
            """, (url, title or url))
    except Exception:
        pass

def query_history(keyword=None):
    try:
        with sqlite3.connect(HISTORY_FILE) as conn:
            cursor = conn.cursor()
            if keyword:
                cursor.execute(
                    "SELECT id, url, title, datetime(visit_time, 'localtime'), visit_count FROM history WHERE url LIKE ? OR title LIKE ? ORDER BY visit_time DESC LIMIT 100",
                    (f"%{keyword}%", f"%{keyword}%")
                )
            else:
                cursor.execute("SELECT id, url, title, datetime(visit_time, 'localtime'), visit_count FROM history ORDER BY visit_time DESC LIMIT 100")
            return cursor.fetchall()
    except Exception:
        return []

def clear_history():
    try:
        with sqlite3.connect(HISTORY_FILE) as conn:
            conn.execute("DELETE FROM history;")
    except Exception:
        pass

class FaviconCache:
    _cache = {}
    _lock = threading.Lock()

    @classmethod
    def clear(cls):
        with cls._lock:
            cls._cache.clear()

    @classmethod
    def get_favicon(cls, host, callback):
        if not host or host in ("localhost", "127.0.0.1", "::1"):
            return
        with cls._lock:
            if host in cls._cache:
                callback(cls._cache[host])
                return
        def fetcher():
            try:
                url = f"https://www.google.com/s2/favicons?domain={urllib.parse.quote(host)}&sz=32"
                req = urllib.request.Request(url, headers={"User-Agent": SETTINGS.get_user_agent()})
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    raw = resp.read()
                    if raw:
                        img = Cocoa.NSImage.alloc().initWithData_(Foundation.NSData.dataWithBytes_length_(raw, len(raw)))
                        if img:
                            with cls._lock:
                                cls._cache[host] = img
                            dispatch_to_main(lambda: callback(img))
            except Exception:
                pass
        threading.Thread(target=fetcher, daemon=True).start()

# ============================================================================
# INJECTED SCRIPTS & FULLSCREEN UTILITIES
# ============================================================================
AUDIO_MONITOR_JS = """
document.addEventListener('play', (e) => {
    if (e.target && !e.target.muted) window.webkit.messageHandlers.audio.postMessage({cmd:'audio_play'});
}, true);
document.addEventListener('pause', () => window.webkit.messageHandlers.audio.postMessage({cmd:'audio_pause'}), true);
document.addEventListener('ended', () => window.webkit.messageHandlers.audio.postMessage({cmd:'audio_pause'}), true);
document.addEventListener('volumechange', (e) => {
    if (e.target && e.target.muted) window.webkit.messageHandlers.audio.postMessage({cmd:'audio_pause'});
}, true);
"""

IMAGE_CONTEXT_MENU_JS = """
(function() {
    document.addEventListener('contextmenu', function(e) {
        let el = e.target, imgSrc = null;
        let img = el.closest('img') || (el.tagName === 'IMG' ? el : null);
        if (img) imgSrc = img.currentSrc || img.src;
        else if (el.tagName === 'PICTURE') {
            let i = el.querySelector('img'); if (i) imgSrc = i.currentSrc || i.src;
        } else if (el.tagName === 'CANVAS') {
            try { imgSrc = el.toDataURL('image/png'); } catch(_) {}
        } else {
            let bg = window.getComputedStyle(el).backgroundImage;
            if (bg && bg.startsWith('url(')) imgSrc = bg.slice(4, -1).replace(/["']/g, "");
        }
        if (imgSrc && window.webkit?.messageHandlers?.bridge) {
            window.webkit.messageHandlers.bridge.postMessage({cmd: 'context_target_image', src: imgSrc});
        }
    }, true);
})();
"""

KEYCHAIN_AUTOSAVE_JS = """
document.addEventListener('submit', (e) => {
    if (!e.target || !e.target.querySelector) return;
    let pw = e.target.querySelector('input[type="password"]');
    let user = e.target.querySelector('input[type="text"], input[type="email"]');
    if (pw && pw.value) {
        window.webkit.messageHandlers.bridge.postMessage({
            cmd: 'prompt_save_password', host: location.hostname,
            username: user ? user.value : 'unknown', password: pw.value
        });
    }
}, true);
window.addEventListener('load', () => {
    if (location.hostname && window.webkit?.messageHandlers?.bridge) {
        window.webkit.messageHandlers.bridge.postMessage({cmd: 'check_autofill', host: location.hostname});
    }
});
"""

COSMETIC_ADBLOCK_CSS = """
.ad, .ads, .advert, .advertising, .ad-banner, .ad-container,
.ad-slot, .ad-wrapper, .sponsored-post, [id^="google_ads_"],
[id^="div-gpt-ad"], ytd-ad-slot-renderer, .ytp-ad-module,
.ytp-ad-overlay-container, ytd-in-feed-ad-layout-renderer,
ytd-banner-promo-renderer, ytd-promoted-video-renderer,
ytd-ad-inline-playback-meta-block, #player-ads, #masthead-ad,
.taboola, .outbrain, .criteo-ad, [class*="sponsored"],
[data-ad-client], [data-ad-slot] {
    display: none !important; visibility: hidden !important;
    height: 0 !important; min-height: 0 !important; max-height: 0 !important;
    pointer-events: none !important; opacity: 0 !important;
}
"""

YOUTUBE_AD_SHIELD_JS = """
(function() {
    if (!location.hostname.includes('youtube.com')) return;
    const skipAds = () => {
        const skipBtn = document.querySelector('.ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern');
        if (skipBtn) skipBtn.click();
        const adShowing = document.querySelector('.ad-showing, .ad-interrupting');
        const video = document.querySelector('video');
        if (adShowing && video && !isNaN(video.duration)) {
            video.muted = true; video.playbackRate = 16.0; video.currentTime = video.duration - 0.1;
        }
        const overlay = document.querySelector('.ytp-ad-overlay-close-button');
        if (overlay) overlay.click();
        const dismissBtn = document.querySelector('ytd-enforcement-message-view-model #dismiss-button');
        if (dismissBtn) dismissBtn.click();
    };
    setInterval(skipAds, 150);
})();
"""

IN_WINDOW_FULLSCREEN_JS = """
(function() {
    let cur = null, origScrollY = 0;
    function ensureStyle() {
        if (!document.getElementById('__pybrowser_fs_style')) {
            const st = document.createElement('style'); st.id = '__pybrowser_fs_style';
            st.textContent = `.__in_window_fullscreen { position: fixed !important; top: 0 !important; left: 0 !important; width: 100vw !important; height: 100vh !important; max-width: 100vw !important; max-height: 100vh !important; margin: 0 !important; padding: 0 !important; border: 0 !important; z-index: 2147483647 !important; background: #000 !important; box-sizing: border-box !important; }`;
            (document.head || document.documentElement).appendChild(st);
        }
    }
    Object.defineProperty(document, 'fullscreenElement', { get: () => cur, configurable: true });
    Object.defineProperty(document, 'webkitFullscreenElement', { get: () => cur, configurable: true });
    Object.defineProperty(document, 'fullscreenEnabled', { get: () => true, configurable: true });
    Object.defineProperty(document, 'webkitFullscreenEnabled', { get: () => true, configurable: true });

    Element.prototype.requestFullscreen = function() {
        ensureStyle();
        if (cur && cur !== this) cur.classList.remove('__in_window_fullscreen');
        cur = this; origScrollY = window.scrollY || window.pageYOffset || 0;
        this.classList.add('__in_window_fullscreen');
        document.dispatchEvent(new Event('fullscreenchange', { bubbles: true }));
        document.dispatchEvent(new Event('webkitfullscreenchange', { bubbles: true }));
        window.dispatchEvent(new Event('resize'));
        if (window.webkit?.messageHandlers?.bridge) window.webkit.messageHandlers.bridge.postMessage({cmd: 'html5_fullscreen_enter'});
        return Promise.resolve();
    };
    Element.prototype.webkitRequestFullscreen = Element.prototype.requestFullscreen;

    document.exitFullscreen = function() {
        if (cur) {
            const el = cur; cur = null;
            el.classList.remove('__in_window_fullscreen');
            document.dispatchEvent(new Event('fullscreenchange', { bubbles: true }));
            document.dispatchEvent(new Event('webkitfullscreenchange', { bubbles: true }));
            window.dispatchEvent(new Event('resize'));
            window.scrollTo({ left: 0, top: origScrollY, behavior: 'instant' });
            if (document.documentElement) document.documentElement.scrollLeft = 0;
            if (document.body) document.body.scrollLeft = 0;
            if (window.webkit?.messageHandlers?.bridge) window.webkit.messageHandlers.bridge.postMessage({cmd: 'html5_fullscreen_exit'});
        }
        return Promise.resolve();
    };
    document.webkitExitFullscreen = document.exitFullscreen;
    document.addEventListener('keydown', function(e) {
        if ((e.key === 'Escape' || e.keyCode === 27) && cur) { e.preventDefault(); e.stopPropagation(); document.exitFullscreen(); }
    }, true);
})();
"""

REFLOW_RESTORE_JS = """
(function() {
    if (document.documentElement) { document.documentElement.style.removeProperty('zoom'); document.documentElement.scrollLeft = 0; }
    if (document.body) { document.body.style.removeProperty('zoom'); document.body.scrollLeft = 0; }
    document.querySelectorAll('.__in_window_fullscreen').forEach(el => el.classList.remove('__in_window_fullscreen'));
    if (window.scrollX !== 0) window.scrollTo(0, window.scrollY || 0);
    window.dispatchEvent(new Event('resize'));
})();
"""

ESCAPE_CHECK_JS = """
(function() {
    if (document.fullscreenElement) { if (document.exitFullscreen) document.exitFullscreen(); return true; }
    const fs = document.querySelector('.__in_window_fullscreen');
    if (fs) { fs.classList.remove('__in_window_fullscreen'); window.dispatchEvent(new Event('resize')); return true; }
    return false;
})();
"""

# ============================================================================
# TAB GROUPS
# ============================================================================
@dataclass
class TabGroup:
    id: str
    title: str
    color_name: str
    collapsed: bool

class TabGroupHeaderView(Cocoa.NSView):
    def initWithFrame_group_controller_(self, frame, group, controller):
        self = objc.super(TabGroupHeaderView, self).initWithFrame_(frame)
        if self:
            self.group, self.controller = group, controller
        return self

    def drawRect_(self, dirtyRect):
        bounds = self.bounds()
        path = Cocoa.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(Cocoa.NSMakeRect(2, 4, bounds.size.width - 4, 20), 4, 4)
        hex_to_nscolor(GROUP_COLORS.get(self.group.color_name, "#5F6368")).setFill()
        path.fill()
        attrs = {Cocoa.NSFontAttributeName: Cocoa.NSFont.boldSystemFontOfSize_(11), Cocoa.NSForegroundColorAttributeName: Cocoa.NSColor.whiteColor()}
        Cocoa.NSAttributedString.alloc().initWithString_attributes_(self.group.title, attrs).drawAtPoint_(Cocoa.NSMakePoint(6, 6))

    def mouseDown_(self, event):
        self.group.collapsed = not self.group.collapsed
        self.controller.relayout_tabs()

# ============================================================================
# MATERIAL 3 CHROME VIEWS & DELEGATES
# ============================================================================
class ChromeTabStripView(Cocoa.NSView):
    """Container view for horizontal or vertical tab bars with double-click support."""
    def initWithFrame_(self, frame):
        self = objc.super(ChromeTabStripView, self).initWithFrame_(frame)
        if self:
            self.controller = None
        return self

    def drawRect_(self, dirtyRect):
        is_inc = self.controller.is_incognito if self.controller else False
        get_m3_color("tabstrip-bg", self, is_inc).setFill()
        Cocoa.NSRectFill(self.bounds())

    def mouseDown_(self, event):
        if event.clickCount() == 2 and self.controller:
            action = SETTINGS.settings.get("tab_bar_double_click_action", "maximize")
            win = self.controller.window()
            if win:
                if action == "fullscreen":
                    win.toggleFullScreen_(None)
                else:
                    win.zoom_(None)
            return
        objc.super(ChromeTabStripView, self).mouseDown_(event)

class ChromeTabView(Cocoa.NSView):
    """Custom Material 3 Tab view supporting favicon, title, audio icon, and close button."""
    def initWithFrame_controller_index_(self, frame, controller, index):
        self = objc.super(ChromeTabView, self).initWithFrame_(frame)
        if self:
            self.controller = controller
            self.index = index
            self.is_hovered = False
            self.tracking_area = None
            self.close_btn_rect = Cocoa.NSZeroRect
            self.audio_btn_rect = Cocoa.NSZeroRect
        return self

    def updateTrackingAreas(self):
        if self.tracking_area:
            self.removeTrackingArea_(self.tracking_area)
        opts = Cocoa.NSTrackingMouseEnteredAndExited | Cocoa.NSTrackingActiveAlways | Cocoa.NSTrackingInVisibleRect
        self.tracking_area = Cocoa.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(Cocoa.NSZeroRect, opts, self, None)
        self.addTrackingArea_(self.tracking_area)
        objc.super(ChromeTabView, self).updateTrackingAreas()

    def mouseEntered_(self, event):
        self.is_hovered = True
        self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        self.is_hovered = False
        self.setNeedsDisplay_(True)

    def drawRect_(self, dirtyRect):
        if not (0 <= self.index < len(self.controller.tabs)):
            return
        tab = self.controller.tabs[self.index]
        is_active = (self.index == self.controller.active_tab_index)
        is_inc = self.controller.is_incognito
        bounds = self.bounds()

        if is_active:
            path = Cocoa.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, 6, 6)
            get_m3_color("active-tab-bg", self, is_inc).setFill()
            path.fill()
        elif self.is_hovered:
            path = Cocoa.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, 6, 6)
            get_m3_color("inactive-tab-hover", self, is_inc).setFill()
            path.fill()

        # Separator for inactive tabs
        if not is_active and not self.is_hovered and bounds.size.width > 50:
            sep = Cocoa.NSBezierPath.bezierPathWithRect_(Cocoa.NSMakeRect(bounds.size.width - 1, 6, 1, bounds.size.height - 12))
            get_m3_color("tab-divider", self, is_inc).setFill()
            sep.fill()

        # Favicon
        icon_x = 8.0
        icon_y = (bounds.size.height - 16) / 2.0
        if tab.get("favicon"):
            tab["favicon"].drawInRect_fromRect_operation_fraction_(
                Cocoa.NSMakeRect(icon_x, icon_y, 16, 16), Cocoa.NSZeroRect, Cocoa.NSCompositeSourceOver, 1.0
            )
        else:
            def_icon = "💤" if tab.get("is_sleeping") else "📄"
            attrs = {Cocoa.NSFontAttributeName: Cocoa.NSFont.systemFontOfSize_(11)}
            Cocoa.NSAttributedString.alloc().initWithString_attributes_(def_icon, attrs).drawAtPoint_(Cocoa.NSMakePoint(icon_x, icon_y - 1))

        if tab.get("pinned", False):
            return

        # Audio / Mute Indicator
        has_audio = tab.get("has_audio", False)
        is_muted = tab.get("is_muted", False)
        right_offset = 24.0

        if has_audio or is_muted:
            audio_str = "🔇" if is_muted else "🔊"
            self.audio_btn_rect = Cocoa.NSMakeRect(bounds.size.width - 40, (bounds.size.height - 16) / 2.0, 16, 16)
            attrs = {Cocoa.NSFontAttributeName: Cocoa.NSFont.systemFontOfSize_(10)}
            Cocoa.NSAttributedString.alloc().initWithString_attributes_(audio_str, attrs).drawAtPoint_(Cocoa.NSMakePoint(self.audio_btn_rect.origin.x, self.audio_btn_rect.origin.y - 1))
            right_offset = 42.0
        else:
            self.audio_btn_rect = Cocoa.NSZeroRect

        # Close Button
        self.close_btn_rect = Cocoa.NSMakeRect(bounds.size.width - 20, (bounds.size.height - 14) / 2.0, 14, 14)
        if self.is_hovered or is_active:
            close_attrs = {
                Cocoa.NSFontAttributeName: Cocoa.NSFont.boldSystemFontOfSize_(11),
                Cocoa.NSForegroundColorAttributeName: Cocoa.NSColor.secondaryLabelColor()
            }
            Cocoa.NSAttributedString.alloc().initWithString_attributes_("✕", close_attrs).drawAtPoint_(Cocoa.NSMakePoint(self.close_btn_rect.origin.x + 2, self.close_btn_rect.origin.y - 1))

        # Title
        title_x = icon_x + 20.0
        max_title_w = max(10.0, bounds.size.width - title_x - right_offset)
        title = tab.get("title", "New Tab")
        text_color = Cocoa.NSColor.labelColor() if is_active else Cocoa.NSColor.secondaryLabelColor()
        title_attrs = {
            Cocoa.NSFontAttributeName: Cocoa.NSFont.systemFontOfSize_(12),
            Cocoa.NSForegroundColorAttributeName: text_color
        }
        astr = Cocoa.NSAttributedString.alloc().initWithString_attributes_(title, title_attrs)
        astr.drawInRect_(Cocoa.NSMakeRect(title_x, (bounds.size.height - 16) / 2.0, max_title_w, 16))

    def mouseDown_(self, event):
        pt = self.convertPoint_fromView_(event.locationInWindow(), None)
        if Cocoa.NSPointInRect(pt, self.close_btn_rect):
            self.controller.close_tab(self.index)
            return
        if self.audio_btn_rect.size.width > 0 and Cocoa.NSPointInRect(pt, self.audio_btn_rect):
            self.controller.toggle_mute(self.index)
            return
        self.controller.switch_to_tab(self.index)

    def menuForEvent_(self, event):
        menu = Cocoa.NSMenu.alloc().init()
        item_dupe = menu.addItemWithTitle_action_keyEquivalent_("Duplicate Tab", "onContextDuplicate:", "")
        item_dupe.setTarget_(self)
        item_pin = menu.addItemWithTitle_action_keyEquivalent_("Unpin Tab" if self.controller.tabs[self.index].get("pinned") else "Pin Tab", "onContextPin:", "")
        item_pin.setTarget_(self)
        item_mute = menu.addItemWithTitle_action_keyEquivalent_("Unmute Tab" if self.controller.tabs[self.index].get("is_muted") else "Mute Tab", "onContextMute:", "")
        item_mute.setTarget_(self)
        item_grp = menu.addItemWithTitle_action_keyEquivalent_("Add Tab to New Group", "onContextGroup:", "")
        item_grp.setTarget_(self)
        menu.addItem_(Cocoa.NSMenuItem.separatorItem())
        item_close = menu.addItemWithTitle_action_keyEquivalent_("Close Tab", "onContextClose:", "")
        item_close.setTarget_(self)
        return menu

    def onContextDuplicate_(self, sender): self.controller.duplicate_tab(self.index)
    def onContextPin_(self, sender): self.controller.toggle_pin_tab(self.index)
    def onContextMute_(self, sender): self.controller.toggle_mute(self.index)
    def onContextGroup_(self, sender): self.controller.create_group_for_tab(self.index)
    def onContextClose_(self, sender): self.controller.close_tab(self.index)

class ChromeToolbarView(Cocoa.NSView):
    """Material 3 Navigation & Action Toolbar."""
    def initWithFrame_(self, frame):
        self = objc.super(ChromeToolbarView, self).initWithFrame_(frame)
        if self:
            self.controller = None
        return self

    def drawRect_(self, dirtyRect):
        is_inc = self.controller.is_incognito if self.controller else False
        get_m3_color("toolbar-bg", self, is_inc).setFill()
        Cocoa.NSRectFill(self.bounds())
        # Bottom divider
        sep = Cocoa.NSBezierPath.bezierPathWithRect_(Cocoa.NSMakeRect(0, 0, self.bounds().size.width, 1))
        get_m3_color("toolbar-separator", self, is_inc).setFill()
        sep.fill()

class BookmarksBarView(Cocoa.NSView):
    """Material 3 Horizontal Bookmarks Bar."""
    def initWithFrame_controller_(self, frame, controller):
        self = objc.super(BookmarksBarView, self).initWithFrame_(frame)
        if self:
            self.controller = controller
            self.reload_bookmarks()
        return self

    def drawRect_(self, dirtyRect):
        is_inc = self.controller.is_incognito if self.controller else False
        get_m3_color("toolbar-bg", self, is_inc).setFill()
        Cocoa.NSRectFill(self.bounds())
        sep = Cocoa.NSBezierPath.bezierPathWithRect_(Cocoa.NSMakeRect(0, 0, self.bounds().size.width, 0.5))
        get_m3_color("toolbar-separator", self, is_inc).setFill()
        sep.fill()

    def reload_bookmarks(self):
        for sub in list(self.subviews()):
            sub.removeFromSuperview()
        bms = []
        if os.path.exists(BOOKMARKS_FILE):
            try:
                with open(BOOKMARKS_FILE, "r", encoding="utf-8") as f:
                    bms = json.load(f)
            except Exception:
                bms = []

        x = 8.0
        h = self.bounds().size.height
        for b in bms:
            if not isinstance(b, dict) or not b.get("url"):
                continue
            title = b.get("title", b["url"])[:20]
            btn = Cocoa.NSButton.alloc().initWithFrame_(Cocoa.NSMakeRect(x, 2, max(60, min(140, len(title) * 8 + 20)), h - 4))
            btn.setTitle_(f"🔖 {title}")
            btn.setBezelStyle_(Cocoa.NSBezelStyleInline)
            btn.setFont_(Cocoa.NSFont.systemFontOfSize_(11))
            btn.setTarget_(self)
            btn.setAction_("onBookmarkClick:")
            btn.setToolTip_(b.get("url", ""))
            self.addSubview_(btn)
            x += btn.bounds().size.width + 4.0
        self.setNeedsDisplay_(True)

    def onBookmarkClick_(self, sender):
        url = sender.toolTip()
        if url and self.controller:
            wv = self.controller.get_active_webview()
            if wv:
                wv.loadRequest_(Foundation.NSURLRequest.requestWithURL_(Foundation.NSURL.URLWithString_(url)))

class DownloadDelegate(Cocoa.NSObject):
    """WebKit Download Delegate handling file deduplication and status synchronization."""
    def initWithController_(self, controller):
        self = objc.super(DownloadDelegate, self).init()
        if self:
            self.controller = controller
            self.dest_paths = {}
        return self

    def download_decideDestinationUsingResponse_suggestedFilename_completionHandler_(self, download, response, filename, handler):
        dest_dir = validate_and_get_download_dir()
        base_name = filename or "download"
        dest_path = os.path.join(dest_dir, base_name)
        base, ext = os.path.splitext(dest_path)
        counter = 1
        while os.path.exists(dest_path):
            dest_path = f"{base} ({counter}){ext}"
            counter += 1

        self.dest_paths[download] = dest_path
        self.controller.register_download(os.path.basename(dest_path), dest_path, download)
        handler(Foundation.NSURL.fileURLWithPath_(dest_path))

    def downloadDidFinish_(self, download):
        path = self.dest_paths.pop(download, None)
        self.controller.finish_download(download, "Completed", dl_path=path)

    def download_didFailWithError_resumeData_(self, download, error, resumeData):
        path = self.dest_paths.pop(download, None)
        self.controller.finish_download(download, "Failed", dl_path=path)

# ============================================================================
# SCHEME HANDLER
# ============================================================================
class InternalSchemeHandler(Cocoa.NSObject):
    def init(self):
        self = objc.super(InternalSchemeHandler, self).init()
        if self:
            self.active_tasks, self.lock = set(), threading.Lock()
        return self

    def webView_startURLSchemeTask_(self, webView, task):
        with self.lock:
            self.active_tasks.add(task)
        try:
            url = task.request().URL().absoluteString()
            if "settings" in url:
                with ADBLOCK_LOCK:
                    sb = len(STEVENBLACK_DOMAINS)
                    base = len(BASE_ADBLOCK_DOMAINS)
                    cust = len(SETTINGS.get_custom_adblock_domains())
                    act = sb + base + cust
                html_str = get_settings_html(sb, base, cust, act)
            elif "bookmarks" in url:
                html_str = self.get_bookmarks_html()
            elif "downloads" in url:
                html_str = self.get_downloads_html()
            elif "extensions" in url:
                html_str = self.get_extensions_html()
            elif "terminal" in url:
                html_str = self.get_terminal_html()
            elif "processes" in url:
                dispatch_to_main(lambda: Cocoa.NSApp.delegate().process_manager.show())
                html_str = "<!DOCTYPE html><html><body style='font-family:-apple-system,sans-serif;padding:40px;background:#FAF9F6;color:#222;'><h2>Task Manager launched in a separate window.</h2></body></html>"
            else:
                html_str = self.get_history_html()

            data = html_str.encode('utf-8')
            resp = Foundation.NSURLResponse.alloc().initWithURL_MIMEType_expectedContentLength_textEncodingName_(
                task.request().URL(), "text/html", len(data), "utf-8"
            )
            with self.lock:
                if task not in self.active_tasks:
                    return
            task.didReceiveResponse_(resp)
            task.didReceiveData_(Foundation.NSData.dataWithBytes_length_(data, len(data)))
            task.didFinish()
        except Exception:
            try:
                task.didFailWithError_(Foundation.NSError.errorWithDomain_code_userInfo_("PyBrowserError", -1, None))
            except Exception:
                pass
        finally:
            with self.lock:
                self.active_tasks.discard(task)

    def webView_stopURLSchemeTask_(self, webView, task):
        with self.lock:
            self.active_tasks.discard(task)

    def get_terminal_html(self):
        t = SETTINGS.settings.get("terminal_theme", "luna")
        bg = "#000000" if t == "kitty" else ("#1e1e1e" if t == "normal" else "#1e1e2e")
        fg = "#dddddd" if t == "kitty" else ("#cccccc" if t == "normal" else "#c792ea")
        return f"<!DOCTYPE html><html><head><meta charset='utf-8'><link rel='stylesheet' href='https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css' /><script src='https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js'></script><script src='https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js'></script><style>*{{box-sizing:border-box;margin:0;padding:0}}html,body,#t{{width:100vw;height:100vh;background:{bg};overflow:hidden}}</style></head><body><div id='t'></div><script>const term=new Terminal({{fontFamily:'Menlo,Monaco,monospace',fontSize:13,theme:{{background:'{bg}',foreground:'{fg}'}}}});const fit=new FitAddon.FitAddon();term.loadAddon(fit);term.open(document.getElementById('t'));fit.fit();window.addEventListener('resize',()=>{{fit.fit();window.webkit.messageHandlers.terminalResize.postMessage({{cols:term.cols,rows:term.rows}})}});term.onData(d=>window.webkit.messageHandlers.terminalInput.postMessage(d));window.writeToTerm=b=>{{const s=atob(b),u=new Uint8Array(s.length);for(let i=0;i<s.length;i++)u[i]=s.charCodeAt(i);term.write(new TextDecoder().decode(u))}};</script></body></html>"

    def get_bookmarks_html(self):
        bms = []
        if os.path.exists(BOOKMARKS_FILE):
            try:
                with open(BOOKMARKS_FILE, "r", encoding="utf-8") as f:
                    bms = json.load(f)
            except Exception:
                pass
        items = []
        for b in bms:
            if isinstance(b, dict) and b.get("url"):
                u = b["url"]
                t = b.get("title", u)
                safe_u = json.dumps(u)
                items.append(f"<li><a href='{html.escape(u)}'>{html.escape(t)}</a> <button onclick='window.webkit.messageHandlers.bridge.postMessage({{cmd:\"delete_bookmark\",url:{safe_u}}});setTimeout(()=>location.reload(),100)'>Delete</button></li>")
        rows = "".join(items)
        return f"<!DOCTYPE html><html><body style='font-family:-apple-system,sans-serif;padding:30px;'><h2>Bookmarks</h2><div style='margin-bottom:15px;'><input id='t' placeholder='Title'><input id='u' placeholder='https://...'><button onclick='window.webkit.messageHandlers.bridge.postMessage({{cmd:\"add_bookmark\",title:document.getElementById(\"t\").value,url:document.getElementById(\"u\").value}});setTimeout(()=>location.reload(),100)'>Add</button></div><ul>{rows or '<li>No bookmarks</li>'}</ul></body></html>"

    def get_history_html(self):
        records = query_history()
        rows = "".join([f"<tr><td>{r[3]}</td><td><a href='{html.escape(r[1])}'>{html.escape(r[2])}</a></td><td><button onclick=\"window.webkit.messageHandlers.bridge.postMessage({{cmd:'delete_history_item',id:{r[0]}}});this.closest('tr').remove()\">✕</button></td></tr>" for r in records])
        return f"<!DOCTYPE html><html><body style='font-family:-apple-system,sans-serif;padding:30px;'><h2>History</h2><button onclick=\"window.webkit.messageHandlers.bridge.postMessage({{cmd:'clear_browsing_data',targets:['history']}});setTimeout(()=>location.reload(),100)\">Clear All</button><table style='width:100%;margin-top:15px;'>{rows or '<tr><td>No history</td></tr>'}</table></body></html>"

    def get_downloads_html(self):
        return "<!DOCTYPE html><html><body style='font-family:-apple-system,sans-serif;padding:30px;'><h2>Downloads</h2><div id='dl-list'>No active downloads.</div><script>const dls={};window.updateDownload=(id,name,path,status,progress)=>{dls[id]={name,path,status,progress};render()};function render(){const el=document.getElementById('dl-list');el.innerHTML=Object.values(dls).map(d=>`<div style='border:1px solid #ccc;padding:10px;margin-bottom:8px;'><b>${d.name}</b> (${d.status}) <button onclick=\"window.webkit.messageHandlers.bridge.postMessage({cmd:'dl_show',path:'${d.path}'})\">Show in Finder</button></div>`).join('')};window.webkit.messageHandlers.bridge.postMessage({cmd:'dl_request_sync'});</script></body></html>"

    def get_extensions_html(self):
        scripts = get_installed_userscripts()
        items = []
        for s in scripts:
            f = s["file"]
            safe_f = json.dumps(f)
            checked = "checked" if s["enabled"] else ""
            items.append(
                f"<div style='border:1px solid #ccc;padding:10px;margin-bottom:8px;'>"
                f"<b>{html.escape(s['name'])}</b> ({html.escape(f)}) "
                f"<label><input type='checkbox' {checked} onchange='window.webkit.messageHandlers.bridge.postMessage({{cmd:\"toggle_script\",file:{safe_f},enabled:this.checked}})'> Enabled</label> "
                f"<button onclick='if(confirm(\"Delete?\")){{window.webkit.messageHandlers.bridge.postMessage({{cmd:\"delete_script\",file:{safe_f}}});setTimeout(()=>location.reload(),100)}}'>Delete</button>"
                f"</div>"
            )
        rows = "".join(items)
        return f"<!DOCTYPE html><html><body style='font-family:-apple-system,sans-serif;padding:30px;'><h2>Extensions & Userscripts</h2><button onclick='let id=prompt(\"CRX ID or URL:\");if(id)window.webkit.messageHandlers.bridge.postMessage({{cmd:\"install_crx\",id:id}})'>Install CRX</button><div style='margin-top:15px;'>{rows or 'No userscripts installed.'}</div></body></html>"

# ============================================================================
# WEBKIT SUBCLASS & IMAGE CONTEXT MENU
# ============================================================================
class BrowserWebView(WebKit.WKWebView):
    """Subclass intercepting context menus to provide robust image handling."""
    def willOpenMenu_withEvent_(self, menu, event):
        self.open_image_item = None
        download_item = None

        for item in menu.itemArray():
            ident = str(item.identifier()) if item.identifier() else ""
            title = str(item.title()).lower() if item.title() else ""
            if "openimageinnewwindow" in ident.lower() or "open image in new" in title:
                self.open_image_item = item
                item.setTitle_("Open Image in New Tab")
            elif "downloadimage" in ident.lower() or "download image" in title:
                download_item = item

        if self.open_image_item:
            if download_item:
                download_item.setTarget_(self)
                download_item.setAction_("onContextDownloadImage:")
            else:
                dl = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Download Image", "onContextDownloadImage:", "")
                dl.setTarget_(self)
                idx = menu.indexOfItem_(self.open_image_item)
                menu.insertItem_atIndex_(dl, idx + 1)
                download_item = dl

            save_as = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Save Image As…", "onContextSaveImageAs:", "")
            save_as.setTarget_(self)
            idx = menu.indexOfItem_(download_item)
            menu.insertItem_atIndex_(save_as, idx + 1)

            if not any("copy image address" in str(it.title()).lower() for it in menu.itemArray()):
                copy_addr = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Copy Image Address", "onContextCopyImageAddress:", "")
                copy_addr.setTarget_(self)
                menu.insertItem_atIndex_(copy_addr, idx + 2)
        else:
            controller = getattr(self, "controller", None)
            target_img_url = getattr(controller, "last_context_image_url", None) if controller else None
            if target_img_url:
                menu.addItem_(Cocoa.NSMenuItem.separatorItem())
                menu.addItemWithTitle_action_keyEquivalent_("Download Image", "onDirectDownloadImage:", "").setTarget_(self)
                menu.addItemWithTitle_action_keyEquivalent_("Save Image As…", "onDirectSaveImageAs:", "").setTarget_(self)
                menu.addItemWithTitle_action_keyEquivalent_("Copy Image Address", "onDirectCopyImageAddress:", "").setTarget_(self)

    def onContextDownloadImage_(self, sender):
        if self.open_image_item:
            self.pending_image_action = "download"
            Cocoa.NSApp.sendAction_to_from_(self.open_image_item.action(), self.open_image_item.target(), self.open_image_item)

    def onContextSaveImageAs_(self, sender):
        if self.open_image_item:
            self.pending_image_action = "save_as"
            Cocoa.NSApp.sendAction_to_from_(self.open_image_item.action(), self.open_image_item.target(), self.open_image_item)

    def onContextCopyImageAddress_(self, sender):
        if self.open_image_item:
            self.pending_image_action = "copy_address"
            Cocoa.NSApp.sendAction_to_from_(self.open_image_item.action(), self.open_image_item.target(), self.open_image_item)

    def onDirectDownloadImage_(self, sender):
        if hasattr(self, "controller") and self.controller and self.controller.last_context_image_url:
            self.controller.perform_download_image(self.controller.last_context_image_url)

    def onDirectSaveImageAs_(self, sender):
        if hasattr(self, "controller") and self.controller and self.controller.last_context_image_url:
            self.controller.perform_save_image_as(self.controller.last_context_image_url)

    def onDirectCopyImageAddress_(self, sender):
        if hasattr(self, "controller") and self.controller and self.controller.last_context_image_url:
            self.controller.perform_copy_image_address(self.controller.last_context_image_url)

# ============================================================================
# OMNIBOX & SUGGESTIONS
# ============================================================================
class ChromeOmniboxField(Cocoa.NSTextField):
    def initWithFrame_(self, frame):
        self = objc.super(ChromeOmniboxField, self).initWithFrame_(frame)
        if self:
            self.setFocusRingType_(Cocoa.NSFocusRingTypeNone)
            self.setBordered_(False)
            self.setDrawsBackground_(False)
            self.setFont_(Cocoa.NSFont.systemFontOfSize_(12))
            self.raw_url = ""
        return self

    def becomeFirstResponder(self):
        if self.superview():
            self.superview().setNeedsDisplay_(True)
        self.setStringValue_(self.raw_url)
        self.setTextColor_(Cocoa.NSColor.labelColor())
        self.performSelector_withObject_afterDelay_("selectText:", self, 0.0)
        return objc.super(ChromeOmniboxField, self).becomeFirstResponder()

    def resignFirstResponder(self):
        if self.superview():
            self.superview().setNeedsDisplay_(True)
        self.setStringValue_(self.raw_url or "")
        return objc.super(ChromeOmniboxField, self).resignFirstResponder()

    def set_url(self, url):
        self.raw_url = url
        fr = self.window().firstResponder() if self.window() else None
        if fr != self and fr != self.currentEditor():
            self.setStringValue_(url or "")

class OmniboxPopupPanel(Cocoa.NSPanel):
    def canBecomeKeyWindow(self): return False
    def hasKeyAppearance(self): return True

class OmniboxSuggestionView(Cocoa.NSView):
    def initWithFrame_controller_(self, frame, controller):
        self = objc.super(OmniboxSuggestionView, self).initWithFrame_(frame)
        if self:
            self.controller, self.suggestions, self.selected_index, self.tracking_area = controller, [], -1, None
        return self

    def isFlipped(self): return True
    def acceptsFirstMouse_(self, event): return True

    def updateTrackingAreas(self):
        if self.tracking_area: self.removeTrackingArea_(self.tracking_area)
        opts = Cocoa.NSTrackingMouseMoved | Cocoa.NSTrackingActiveAlways | Cocoa.NSTrackingInVisibleRect
        self.tracking_area = Cocoa.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(Cocoa.NSZeroRect, opts, self, None)
        self.addTrackingArea_(self.tracking_area)
        objc.super(OmniboxSuggestionView, self).updateTrackingAreas()

    def mouseMoved_(self, event):
        loc = self.convertPoint_fromView_(event.locationInWindow(), None)
        idx = int(loc.y // 28)
        if 0 <= idx < len(self.suggestions) and self.selected_index != idx:
            self.selected_index = idx; self.setNeedsDisplay_(True)

    def mouseDown_(self, event):
        loc = self.convertPoint_fromView_(event.locationInWindow(), None)
        idx = int(loc.y // 28)
        if 0 <= idx < len(self.suggestions):
            self.controller.omnibox_panel.select_suggestion(self.suggestions[idx])

    def drawRect_(self, dirtyRect):
        Cocoa.NSColor.clearColor().setFill()
        Cocoa.NSRectFill(self.bounds())
        for i, sugg in enumerate(self.suggestions):
            rect = Cocoa.NSMakeRect(0, i * 28, self.bounds().size.width, 28)
            if i == self.selected_index:
                hex_to_nscolor("#1A73E8").setFill()
                Cocoa.NSRectFill(rect)
                text_color = Cocoa.NSColor.whiteColor()
            else:
                text_color = Cocoa.NSColor.labelColor()

            icon = "🔍" if sugg["type"] == "search" else ("🕒" if sugg["type"] == "history" else ("🔖" if sugg["type"] == "bookmark" else ("🧮" if sugg["type"] == "math" else "⚡")))
            attrs = {Cocoa.NSFontAttributeName: Cocoa.NSFont.systemFontOfSize_(13), Cocoa.NSForegroundColorAttributeName: text_color}
            Cocoa.NSAttributedString.alloc().initWithString_attributes_(f"{icon}  {sugg['text']}", attrs).drawAtPoint_(Cocoa.NSMakePoint(12, rect.origin.y + 6))

class OmniboxPanelCoordinator:
    def __init__(self, window_controller):
        self.wc = window_controller
        self.suggestion_window, self.suggestion_view = None, None
        self.current_req_id, self.typed_query = 0, ""

    def is_visible(self):
        return bool(self.suggestion_window and self.suggestion_window.isVisible())

    def handle_text_change(self, query):
        if not SETTINGS.settings.get("omnibox_suggestions_enabled", True) or not query or not query.strip():
            self.hide()
            return

        query_str = query.strip()
        self.typed_query = query_str
        self.current_req_id += 1
        req_id = self.current_req_id

        results = []
        for qa in ["@settings", "@history", "@bookmarks", "@extensions", "@downloads", "@terminal", "@processes"]:
            if qa.startswith(query_str.lower()):
                results.append({"type": "action", "text": qa, "url": f"browser://{qa[1:]}"})

        math_res = safe_math_eval(query_str)
        if math_res:
            results.append({"type": "math", "text": f"= {math_res}", "url": math_res, "is_eval": True})

        try:
            with open(BOOKMARKS_FILE, "r", encoding="utf-8") as f:
                bms = json.load(f)
            for b in bms:
                if isinstance(b, dict):
                    t, u = b.get("title", ""), b.get("url", "")
                    if query_str.lower() in t.lower() or query_str.lower() in u.lower():
                        results.append({"type": "bookmark", "text": t or u, "url": u})
        except Exception:
            pass

        for h in query_history(query_str)[:4]:
            results.append({"type": "history", "text": h[2], "url": h[1]})

        def fetch_remote():
            try:
                url = f"http://suggestqueries.google.com/complete/search?client=chrome&q={urllib.parse.quote(query_str)}"
                req = urllib.request.Request(url, headers={"User-Agent": UA_PRESETS["chrome_mac"]})
                with urllib.request.urlopen(req, timeout=0.8) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    remote = [{"type": "search", "text": s, "url": s} for s in data[1][:5]]
                    if req_id == self.current_req_id:
                        dispatch_to_main(lambda: self.show(results + remote))
            except Exception:
                if req_id == self.current_req_id:
                    dispatch_to_main(lambda: self.show(results))

        threading.Thread(target=fetch_remote, daemon=True).start()

    def show(self, suggestions):
        if not suggestions:
            self.hide()
            return
        if not self.suggestion_window:
            self.suggestion_window = OmniboxPopupPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                Cocoa.NSMakeRect(0, 0, 100, 100), Cocoa.NSWindowStyleMaskBorderless, Cocoa.NSBackingStoreBuffered, False
            )
            self.suggestion_window.setOpaque_(False)
            self.suggestion_window.setAcceptsMouseMovedEvents_(True)
            self.suggestion_window.setBackgroundColor_(Cocoa.NSColor.clearColor())
            self.suggestion_window.setHasShadow_(True)

            vev = Cocoa.NSVisualEffectView.alloc().initWithFrame_(self.suggestion_window.contentView().bounds())
            vev.setMaterial_(Cocoa.NSVisualEffectMaterialPopover)
            vev.setBlendingMode_(Cocoa.NSVisualEffectBlendingModeBehindWindow)
            vev.setState_(Cocoa.NSVisualEffectStateActive)
            vev.setWantsLayer_(True)
            vev.layer().setCornerRadius_(8)
            self.suggestion_window.setContentView_(vev)

            self.suggestion_view = OmniboxSuggestionView.alloc().initWithFrame_controller_(vev.bounds(), self.wc)
            vev.addSubview_(self.suggestion_view)

        self.suggestion_view.suggestions = suggestions[:8]
        self.suggestion_view.selected_index = -1

        rect_in_window = self.wc.omnibox_container.convertRect_toView_(self.wc.omnibox_container.bounds(), None)
        rect = self.wc.window().convertRectToScreen_(rect_in_window)
        popup_h = len(self.suggestion_view.suggestions) * 28

        self.suggestion_window.setFrame_display_(Cocoa.NSMakeRect(rect.origin.x, rect.origin.y - popup_h - 4, rect.size.width, popup_h), True)
        self.suggestion_view.setFrame_(self.suggestion_window.contentView().bounds())
        self.suggestion_view.setNeedsDisplay_(True)
        if self.suggestion_window.parentWindow() != self.wc.window():
            self.wc.window().addChildWindow_ordered_(self.suggestion_window, Cocoa.NSWindowAbove)
        self.suggestion_window.orderFront_(None)

    def hide(self):
        if self.suggestion_window and self.suggestion_window.isVisible():
            if self.suggestion_window.parentWindow():
                self.wc.window().removeChildWindow_(self.suggestion_window)
            self.suggestion_window.orderOut_(None)

    def select_suggestion(self, sugg):
        if sugg.get("is_eval"):
            val = sugg.get("url", "")
            self.wc.omnibox.setStringValue_(val)
            pb = Cocoa.NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(val, Cocoa.NSPasteboardTypeString)
            self.hide()
            return
        target = sugg.get("url") or sugg.get("text")
        self.wc.omnibox.setStringValue_(target)
        editor = self.wc.omnibox.currentEditor()
        if editor:
            editor.setString_(target)
        self.hide()
        self.wc.onOmniboxEnter_(self.wc.omnibox)

    def handle_command_selector(self, commandSelector):
        sel = str(commandSelector)
        if self.is_visible():
            if sel == "moveDown:":
                self.suggestion_view.selected_index = min(len(self.suggestion_view.suggestions)-1, self.suggestion_view.selected_index + 1)
                if self.suggestion_view.selected_index >= 0:
                    s = self.suggestion_view.suggestions[self.suggestion_view.selected_index]
                    self.wc.omnibox.setStringValue_(s.get("url") or s.get("text"))
                self.suggestion_view.setNeedsDisplay_(True)
                return True
            elif sel == "moveUp:":
                self.suggestion_view.selected_index = max(-1, self.suggestion_view.selected_index - 1)
                if self.suggestion_view.selected_index >= 0:
                    s = self.suggestion_view.suggestions[self.suggestion_view.selected_index]
                    self.wc.omnibox.setStringValue_(s.get("url") or s.get("text"))
                else:
                    self.wc.omnibox.setStringValue_(self.typed_query)
                self.suggestion_view.setNeedsDisplay_(True)
                return True
            elif sel == "insertNewline:":
                if self.suggestion_view.selected_index >= 0:
                    self.select_suggestion(self.suggestion_view.suggestions[self.suggestion_view.selected_index])
                    return True
            elif sel == "cancelOperation:":
                self.hide()
                return True
        return False

# ============================================================================
# MATERIAL PROGRESS BAR & FIND BAR
# ============================================================================
class MaterialProgressBar(Cocoa.NSView):
    def initWithFrame_(self, frame):
        self = objc.super(MaterialProgressBar, self).initWithFrame_(frame)
        if self:
            self.progress = 0.0
        return self

    def setProgress_(self, val):
        self.progress = min(1.0, max(0.0, float(val)))
        self.setNeedsDisplay_(True)

    def drawRect_(self, dirtyRect):
        if self.progress <= 0.0:
            return
        w, h = self.bounds().size.width * self.progress, self.bounds().size.height
        path = Cocoa.NSBezierPath.bezierPathWithRect_(Cocoa.NSMakeRect(0, 0, w, h))
        is_dark = False
        try:
            if self.effectiveAppearance():
                is_dark = "Dark" in str(self.effectiveAppearance().name())
        except Exception:
            pass
        hex_to_nscolor("#8AB4F8" if is_dark else "#1A73E8").setFill()
        path.fill()

class FindBarView(Cocoa.NSView):
    def initWithFrame_controller_(self, frame, controller):
        self = objc.super(FindBarView, self).initWithFrame_(frame)
        if self:
            self.controller = controller
            self.setup_ui()
        return self

    def drawRect_(self, dirtyRect):
        path = Cocoa.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 8, 8)
        get_m3_color("toolbar-bg", self).setFill()
        path.fill()
        path.setLineWidth_(1.0)
        get_m3_color("toolbar-separator", self).setStroke()
        path.stroke()

    def setup_ui(self):
        self.search_field = Cocoa.NSSearchField.alloc().initWithFrame_(Cocoa.NSMakeRect(10, 10, 200, 22))
        self.search_field.setTarget_(self)
        self.search_field.setAction_("onSearch:")
        self.search_field.setPlaceholderString_("Find in page…")
        self.addSubview_(self.search_field)
        self.close_btn = Cocoa.NSButton.alloc().initWithFrame_(Cocoa.NSMakeRect(218, 10, 24, 22))
        self.close_btn.setTitle_("✕")
        self.close_btn.setBezelStyle_(Cocoa.NSBezelStyleInline)
        self.close_btn.setTarget_(self)
        self.close_btn.setAction_("onClose:")
        self.addSubview_(self.close_btn)

    def onSearch_(self, sender):
        query = self.search_field.stringValue()
        wv = self.controller.get_active_webview()
        if wv and query.strip():
            cfg = WebKit.WKFindConfiguration.alloc().init()
            cfg.setWraps_(True)
            wv.findString_withConfiguration_completionHandler_(query, cfg, lambda res: None)

    def onClose_(self, sender):
        self.setHidden_(True)
        wv = self.controller.get_active_webview()
        if wv:
            self.controller.window().makeFirstResponder_(wv)

# ============================================================================
# QUICK SWITCHER & MEDIA CONTROLS (PIP)
# ============================================================================
class QuickSwitcherListView(Cocoa.NSView):
    def initWithFrame_controller_(self, frame, controller):
        self = objc.super(QuickSwitcherListView, self).initWithFrame_(frame)
        if self:
            self.controller = controller
        return self

    def isFlipped(self): return True

    def drawRect_(self, dirtyRect):
        for i, tab in enumerate(self.controller.filtered_tabs):
            rect = Cocoa.NSMakeRect(0, i * 36, self.bounds().size.width, 36)
            if i == self.controller.selected_index:
                hex_to_nscolor("#1A73E8").setFill()
                Cocoa.NSRectFill(rect)
                tc, sub_color = Cocoa.NSColor.whiteColor(), Cocoa.NSColor.whiteColor()
            else:
                tc, sub_color = Cocoa.NSColor.labelColor(), Cocoa.NSColor.secondaryLabelColor()

            attrs = {Cocoa.NSFontAttributeName: Cocoa.NSFont.boldSystemFontOfSize_(13), Cocoa.NSForegroundColorAttributeName: tc}
            Cocoa.NSAttributedString.alloc().initWithString_attributes_(tab["title"][:60], attrs).drawAtPoint_(Cocoa.NSMakePoint(20, rect.origin.y + 3))
            sub_attrs = {Cocoa.NSFontAttributeName: Cocoa.NSFont.systemFontOfSize_(11), Cocoa.NSForegroundColorAttributeName: sub_color}
            Cocoa.NSAttributedString.alloc().initWithString_attributes_(tab["url"][:80], sub_attrs).drawAtPoint_(Cocoa.NSMakePoint(20, rect.origin.y + 19))

class QuickSwitcherController(Cocoa.NSWindowController):
    def init(self):
        w, h = 600, 400
        window = Cocoa.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            Cocoa.NSMakeRect(0, 0, w, h), Cocoa.NSWindowStyleMaskBorderless | Cocoa.NSWindowStyleMaskFullSizeContentView, Cocoa.NSBackingStoreBuffered, False
        )
        window.setOpaque_(False)
        window.setBackgroundColor_(Cocoa.NSColor.clearColor())
        window.setHasShadow_(True)
        window.center()
        self = objc.super(QuickSwitcherController, self).initWithWindow_(window)
        if self:
            self.setup_ui()
        return self

    def setup_ui(self):
        vev = Cocoa.NSVisualEffectView.alloc().initWithFrame_(self.window().contentView().bounds())
        vev.setMaterial_(Cocoa.NSVisualEffectMaterialPopover)
        vev.setBlendingMode_(Cocoa.NSVisualEffectBlendingModeBehindWindow)
        vev.setState_(Cocoa.NSVisualEffectStateActive)
        vev.setWantsLayer_(True)
        vev.layer().setCornerRadius_(12)
        vev.layer().setMasksToBounds_(True)
        self.window().setContentView_(vev)

        self.search_field = Cocoa.NSTextField.alloc().initWithFrame_(Cocoa.NSMakeRect(20, 345, 560, 35))
        self.search_field.setFont_(Cocoa.NSFont.systemFontOfSize_(20))
        self.search_field.setFocusRingType_(Cocoa.NSFocusRingTypeNone)
        self.search_field.setBordered_(False)
        self.search_field.setDrawsBackground_(False)
        self.search_field.setPlaceholderString_("Search Open Tabs…")
        self.search_field.setDelegate_(self)
        vev.addSubview_(self.search_field)

        self.list_view = QuickSwitcherListView.alloc().initWithFrame_controller_(Cocoa.NSMakeRect(0, 0, 600, 330), self)
        vev.addSubview_(self.list_view)
        self.tabs, self.filtered_tabs, self.selected_index = [], [], 0

    def show(self):
        self.tabs = []
        app_delegate = Cocoa.NSApp.delegate()
        if app_delegate and hasattr(app_delegate, "controllers"):
            for wc in app_delegate.controllers:
                for i, t in enumerate(wc.tabs):
                    self.tabs.append({"title": t.get("title", "Tab"), "url": t.get("url", ""), "wc": wc, "index": i})
        self.filtered_tabs = self.tabs.copy()
        self.selected_index = 0
        self.search_field.setStringValue_("")
        self.list_view.setNeedsDisplay_(True)
        self.window().makeKeyAndOrderFront_(None)
        self.window().makeFirstResponder_(self.search_field)

    def controlTextDidChange_(self, notif):
        if notif.object() == self.search_field:
            q = self.search_field.stringValue().lower().strip()
            self.filtered_tabs = self.tabs.copy() if not q else [t for t in self.tabs if q in t["title"].lower() or q in t["url"].lower()]
            self.selected_index = 0
            self.list_view.setNeedsDisplay_(True)

    def control_textView_doCommandBySelector_(self, control, textView, commandSelector):
        sel = str(commandSelector)
        if sel == "moveDown:":
            self.selected_index = min(len(self.filtered_tabs) - 1, self.selected_index + 1)
            self.list_view.setNeedsDisplay_(True)
            return True
        elif sel == "moveUp:":
            self.selected_index = max(0, self.selected_index - 1)
            self.list_view.setNeedsDisplay_(True)
            return True
        elif sel == "insertNewline:":
            if self.filtered_tabs and self.selected_index >= 0:
                t = self.filtered_tabs[self.selected_index]
                t["wc"].window().makeKeyAndOrderFront_(None)
                t["wc"].switch_to_tab(t["index"])
                self.window().orderOut_(None)
            return True
        elif sel == "cancelOperation:":
            self.window().orderOut_(None)
            return True
        return False

class PiPPanelController(Cocoa.NSWindowController):
    def initWithBrowser_(self, browser_controller):
        w, h = 240, 80
        window = Cocoa.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            Cocoa.NSMakeRect(0, 0, w, h), Cocoa.NSWindowStyleMaskTitled | Cocoa.NSWindowStyleMaskClosable | Cocoa.NSWindowStyleMaskHUDWindow | Cocoa.NSWindowStyleMaskUtilityWindow,
            Cocoa.NSBackingStoreBuffered, False
        )
        window.setTitle_("Media Controls")
        window.setFloatingPanel_(True)
        window.setLevel_(Cocoa.NSFloatingWindowLevel)
        window.setReleasedWhenClosed_(False)
        self = objc.super(PiPPanelController, self).initWithWindow_(window)
        if self:
            self.browser = browser_controller
            self.setup_ui()
        return self

    def setup_ui(self):
        cv = self.window().contentView()
        def make_btn(title, x, action):
            btn = Cocoa.NSButton.alloc().initWithFrame_(Cocoa.NSMakeRect(x, 20, 44, 34))
            btn.setTitle_(title)
            btn.setBezelStyle_(Cocoa.NSBezelStyleRounded)
            btn.setTarget_(self)
            btn.setAction_(action)
            cv.addSubview_(btn)
            return btn
        self.btn_back = make_btn("↺10", 20, "onScrubBack:")
        self.btn_play = make_btn("⏯", 95, "onPlayPause:")
        self.btn_fwd = make_btn("10↻", 170, "onScrubFwd:")

    def toggle(self):
        if self.window().isVisible():
            self.window().orderOut_(None)
        else:
            self.window().makeKeyAndOrderFront_(None)

    def exec_js(self, js):
        app_delegate = Cocoa.NSApp.delegate()
        wc = Cocoa.NSApp.keyWindow().delegate() if (Cocoa.NSApp.keyWindow() and isinstance(Cocoa.NSApp.keyWindow().delegate(), BrowserWindowController)) else (app_delegate.controllers[0] if (app_delegate and app_delegate.controllers) else None)
        if wc:
            wv = wc.get_active_webview()
            if wv:
                wv.evaluateJavaScript_completionHandler_(js, None)

    def onPlayPause_(self, s): self.exec_js("var v = document.querySelector('video, audio'); if(v) v.paused ? v.play() : v.pause();")
    def onScrubBack_(self, s): self.exec_js("var v = document.querySelector('video, audio'); if(v) v.currentTime -= 10;")
    def onScrubFwd_(self, s): self.exec_js("var v = document.querySelector('video, audio'); if(v) v.currentTime += 10;")

# ============================================================================
# BROWSER WINDOW CONTROLLER
# ============================================================================
class BrowserWindowController(Cocoa.NSWindowController):
    def init(self):
        return self.initWithIncognito_(False)

    def initWithIncognito_(self, is_incognito=False):
        self = objc.super(BrowserWindowController, self).init()
        if self:
            self.is_incognito = is_incognito
            self.tabs, self.groups, self.closed_tabs = [], [], []
            self.active_tab_index, self.downloads = -1, []
            self.tab_bar_visible, self.is_fullscreen = True, False
            self.last_context_image_url = None
            self.download_delegate = DownloadDelegate.alloc().initWithController_(self)
            self.omnibox_panel = OmniboxPanelCoordinator(self)
            self.tab_manager = self
            self.setup_window()
        return self

    def setup_window(self):
        w, h = 1200, 800
        window = Cocoa.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            Cocoa.NSMakeRect(100, 100, w, h),
            Cocoa.NSWindowStyleMaskFullSizeContentView | Cocoa.NSWindowStyleMaskTitled |
            Cocoa.NSWindowStyleMaskClosable | Cocoa.NSWindowStyleMaskMiniaturizable | Cocoa.NSWindowStyleMaskResizable,
            Cocoa.NSBackingStoreBuffered, False
        )
        window.setTitlebarAppearsTransparent_(True)
        window.setTitleVisibility_(Cocoa.NSWindowTitleHidden)
        self.setWindow_(window)
        window.setCollectionBehavior_(Cocoa.NSWindowCollectionBehaviorFullScreenPrimary)
        window.setDelegate_(self)

        self.root_view = Cocoa.NSView.alloc().initWithFrame_(Cocoa.NSMakeRect(0, 0, w, h))
        window.setContentView_(self.root_view)
        self.setup_ui(w, h)

        if not self.restore_session():
            self.load_initial_tabs()
        window.center()
        wmode = SETTINGS.settings.get("startup_window_mode", "normal")
        if wmode == "maximized":
            window.zoom_(None)
        elif wmode == "fullscreen":
            window.toggleFullScreen_(None)

        self.sleep_timer = Cocoa.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(60.0, True, lambda t: self.check_sleeping_tabs())
        window.makeKeyAndOrderFront_(None)

    def load_initial_tabs(self):
        behavior = SETTINGS.settings.get("startup_behavior", "new_tab")
        if behavior == "custom_url":
            self.add_tab(SETTINGS.settings.get("startup_url", DEFAULT_URL))
            return
        self.add_tab(DEFAULT_URL)

    def save_session(self):
        if self.is_incognito:
            return
        data = {
            "active_index": self.active_tab_index,
            "tabs": [{"url": t.get("url"), "group_id": t.get("group_id"), "pinned": t.get("pinned", False)} for t in self.tabs if t.get("url") and not t.get("url").startswith("browser://")],
            "groups": [asdict(g) for g in self.groups]
        }
        atomic_write_json(SESSION_FILE, data)

    def restore_session(self):
        if self.is_incognito or SETTINGS.settings.get("startup_behavior") != "restore":
            return False
        if not os.path.exists(SESSION_FILE):
            return False
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.groups = [TabGroup(**g) for g in data.get("groups", [])]
                tabs_data = data.get("tabs", [])
                if not tabs_data:
                    return False
                for t in tabs_data:
                    self.add_tab(t.get("url", DEFAULT_URL), pinned=t.get("pinned", False))
                    if t.get("group_id"):
                        self.tabs[-1]["group_id"] = t.get("group_id")
                idx = data.get("active_index", 0)
                self.switch_to_tab(max(0, min(idx, len(self.tabs) - 1)))
                return True
            elif isinstance(data, list) and data:
                for u in data:
                    self.add_tab(u)
                return True
        except Exception as e:
            print(f"[Restore Session Error] {e}")
        return False

    def setup_ui(self, w, h):
        cv = self.root_view
        self.tab_strip = ChromeTabStripView.alloc().initWithFrame_(Cocoa.NSMakeRect(0, h - 34, w, 34))
        self.tab_strip.controller = self
        cv.addSubview_(self.tab_strip)

        self.toolbar = ChromeToolbarView.alloc().initWithFrame_(Cocoa.NSMakeRect(0, h - 68, w, 34))
        self.toolbar.controller = self
        cv.addSubview_(self.toolbar)

        self.progress_bar = MaterialProgressBar.alloc().initWithFrame_(Cocoa.NSMakeRect(0, 0, w, 2.5))
        self.progress_bar.setHidden_(True)
        self.toolbar.addSubview_(self.progress_bar)

        bm_visible = SETTINGS.settings.get("bookmarks_bar_visible", True)
        self.bookmarks_bar = BookmarksBarView.alloc().initWithFrame_controller_(Cocoa.NSMakeRect(0, h - 92, w, 24), self)
        self.bookmarks_bar.setHidden_(not bm_visible)
        cv.addSubview_(self.bookmarks_bar)

        top_bars_h = 92 if bm_visible else 68
        self.web_container = Cocoa.NSView.alloc().initWithFrame_(Cocoa.NSMakeRect(0, 0, w, h - top_bars_h))
        cv.addSubview_(self.web_container)

        self.find_bar = FindBarView.alloc().initWithFrame_controller_(Cocoa.NSMakeRect(w - 260, h - 140, 250, 42), self)
        self.find_bar.setHidden_(True)
        cv.addSubview_(self.find_bar)

        def btn(title, x, action, tip=""):
            b = Cocoa.NSButton.alloc().initWithFrame_(Cocoa.NSMakeRect(x, 4, 26, 26))
            b.setTitle_(title)
            if tip:
                b.setToolTip_(tip)
            b.setBezelStyle_(Cocoa.NSBezelStyleCircular)
            b.setTarget_(self)
            b.setAction_(action)
            self.toolbar.addSubview_(b)
            return b

        btn("◀", 8, "onGoBack:")
        btn("▶", 38, "onGoForward:")
        btn("↻", 68, "onReload:")
        self.home_btn = btn("🏠", 98, "onGoHome:", "Home Page")
        self.home_btn.setHidden_(not SETTINGS.settings.get("show_home_button", False))

        omni_x = 132 if SETTINGS.settings.get("show_home_button", False) else 102
        self.omnibox_container = Cocoa.NSView.alloc().initWithFrame_(Cocoa.NSMakeRect(omni_x, 3, w - (omni_x + 190), 28))
        self.toolbar.addSubview_(self.omnibox_container)

        self.site_info_btn = Cocoa.NSButton.alloc().initWithFrame_(Cocoa.NSMakeRect(4, 4, 20, 20))
        self.site_info_btn.setTitle_("🎛️")
        self.site_info_btn.setBordered_(False)
        self.site_info_btn.setFont_(Cocoa.NSFont.systemFontOfSize_(12))
        self.site_info_btn.setTarget_(self)
        self.site_info_btn.setAction_("onSiteInfo:")
        self.omnibox_container.addSubview_(self.site_info_btn)

        self.omnibox = ChromeOmniboxField.alloc().initWithFrame_(Cocoa.NSMakeRect(28, 6, self.omnibox_container.bounds().size.width - 56, 16))
        self.omnibox.setTarget_(self)
        self.omnibox.setAction_("onOmniboxEnter:")
        self.omnibox.setDelegate_(self)
        self.omnibox_container.addSubview_(self.omnibox)

        self.star_btn = Cocoa.NSButton.alloc().initWithFrame_(Cocoa.NSMakeRect(self.omnibox_container.bounds().size.width - 26, 4, 20, 20))
        self.star_btn.setTitle_("☆")
        self.star_btn.setToolTip_("Bookmark (Cmd+D)")
        self.star_btn.setBordered_(False)
        self.star_btn.setFont_(Cocoa.NSFont.systemFontOfSize_(13))
        self.star_btn.setTarget_(self)
        self.star_btn.setAction_("onBookmarkCurrentPage:")
        self.star_btn.setAutoresizingMask_(Cocoa.NSViewMinXMargin)
        self.omnibox_container.addSubview_(self.star_btn)

        self.pip_btn = btn("⧉", w - 175, "onTogglePiP:", "Media Controls")
        self.dl_btn = btn("⬇", w - 145, "onShowDownloads:", "Downloads")
        self.share_btn = btn("↗", w - 115, "onShareCurrentPage:", "Share Page")
        self.zen_btn = btn("⛶", w - 85, "onToggleZenMode:", "Zen Mode (Opt+Esc)")
        self.menu_btn = btn("⋮", w - 40, "onMenu:", "Menu")

    def relayout_views(self):
        w, h = self.root_view.bounds().size.width, self.root_view.bounds().size.height
        bm_visible = SETTINGS.settings.get("bookmarks_bar_visible", True)
        is_vert = SETTINGS.settings.get("vertical_tabs_enabled", False)
        vert_w = 48 if SETTINGS.settings.get("vertical_tabs_collapsed", False) else 220

        if not self.tab_bar_visible:
            self.tab_strip.setHidden_(True)
            self.toolbar.setHidden_(True)
            self.bookmarks_bar.setHidden_(True)
            self.web_container.setFrame_(Cocoa.NSMakeRect(0, 0, w, h))
        elif is_vert:
            self.tab_strip.setHidden_(False)
            self.tab_strip.setFrame_(Cocoa.NSMakeRect(0, 0, vert_w, h))
            cx, cw, y = vert_w, w - vert_w, h - 34
            self.toolbar.setHidden_(False)
            self.toolbar.setFrame_(Cocoa.NSMakeRect(cx, y, cw, 34))
            if bm_visible:
                y -= 24
                self.bookmarks_bar.setHidden_(False)
                self.bookmarks_bar.setFrame_(Cocoa.NSMakeRect(cx, y, cw, 24))
            else:
                self.bookmarks_bar.setHidden_(True)
            self.web_container.setFrame_(Cocoa.NSMakeRect(cx, 0, cw, y))
        else:
            self.tab_strip.setHidden_(False)
            self.toolbar.setHidden_(False)
            self.bookmarks_bar.setHidden_(not bm_visible)
            top_h = 92 if bm_visible else 68
            self.tab_strip.setFrame_(Cocoa.NSMakeRect(0, h - 34, w, 34))
            self.toolbar.setFrame_(Cocoa.NSMakeRect(0, h - 68, w, 34))
            if bm_visible:
                self.bookmarks_bar.setFrame_(Cocoa.NSMakeRect(0, h - 92, w, 24))
            self.web_container.setFrame_(Cocoa.NSMakeRect(0, 0, w, h - top_h))

        show_home = SETTINGS.settings.get("show_home_button", False)
        if hasattr(self, 'home_btn'):
            self.home_btn.setHidden_(not show_home)
        omni_x = 132 if show_home else 102
        tb_w = self.toolbar.bounds().size.width
        if hasattr(self, 'omnibox_container'):
            box_w = max(180, tb_w - (omni_x + 195))
            self.omnibox_container.setFrame_(Cocoa.NSMakeRect(omni_x, 3, box_w, 28))
            self.omnibox.setFrame_(Cocoa.NSMakeRect(28, 6, box_w - 56, 16))
            self.star_btn.setFrameOrigin_(Cocoa.NSMakePoint(box_w - 26, 4))

        if hasattr(self, 'pip_btn'): self.pip_btn.setFrameOrigin_(Cocoa.NSMakePoint(tb_w - 175, 4))
        if hasattr(self, 'dl_btn'): self.dl_btn.setFrameOrigin_(Cocoa.NSMakePoint(tb_w - 145, 4))
        if hasattr(self, 'share_btn'): self.share_btn.setFrameOrigin_(Cocoa.NSMakePoint(tb_w - 115, 4))
        if hasattr(self, 'zen_btn'): self.zen_btn.setFrameOrigin_(Cocoa.NSMakePoint(tb_w - 85, 4))
        if hasattr(self, 'menu_btn'): self.menu_btn.setFrameOrigin_(Cocoa.NSMakePoint(tb_w - 40, 4))
        if hasattr(self, 'progress_bar'): self.progress_bar.setFrame_(Cocoa.NSMakeRect(0, 0, tb_w, 2.5))
        if hasattr(self, 'find_bar'): self.find_bar.setFrameOrigin_(Cocoa.NSMakePoint(self.root_view.bounds().size.width - 260, self.web_container.bounds().size.height - 50))

        wv = self.get_active_webview()
        if wv:
            wv.setFrame_(self.web_container.bounds())
        self.relayout_tabs()

    def relayout_tabs(self):
        for sub in list(self.tab_strip.subviews()):
            sub.removeFromSuperview()
        w = self.tab_strip.bounds().size.width
        is_vert = SETTINGS.settings.get("vertical_tabs_enabled", False)
        x = 8.0 if self.is_fullscreen else (0.0 if is_vert else 78.0)
        tab_h = 28 if not is_vert else 32
        y = self.tab_strip.bounds().size.height - 38 - tab_h if is_vert else 3

        vis = [t for t in self.tabs if not t.get("group_id") or not self.get_group(t["group_id"]).collapsed]
        pinned_cnt = sum(1 for t in vis if t.get("pinned", False))
        tab_w = max(60, min(240, (w - x - 44 - (pinned_cnt * 36)) / max(1, len(vis) - pinned_cnt))) if not is_vert else w

        if is_vert:
            tb = Cocoa.NSButton.alloc().initWithFrame_(Cocoa.NSMakeRect(8, self.tab_strip.bounds().size.height - 32, 24, 24))
            tb.setTitle_("≡")
            tb.setBezelStyle_(Cocoa.NSBezelStyleCircular)
            tb.setTarget_(self)
            tb.setAction_("onToggleVerticalTabs:")
            self.tab_strip.addSubview_(tb)

        curr_grp = None
        for i, tab in enumerate(self.tabs):
            if tab.get("group_id"):
                grp = self.get_group(tab["group_id"])
                if grp and curr_grp != grp.id:
                    curr_grp = grp.id
                    if is_vert:
                        self.tab_strip.addSubview_(TabGroupHeaderView.alloc().initWithFrame_group_controller_(Cocoa.NSMakeRect(0, y, w, 28), grp, self))
                        y -= 30
                    else:
                        self.tab_strip.addSubview_(TabGroupHeaderView.alloc().initWithFrame_group_controller_(Cocoa.NSMakeRect(x, 3, 60, 28), grp, self))
                        x += 64
                if grp and grp.collapsed:
                    continue

            cur_w = 36 if tab.get("pinned", False) else tab_w
            if is_vert:
                tab_v = ChromeTabView.alloc().initWithFrame_controller_index_(Cocoa.NSMakeRect(0, y, w, tab_h), self, i)
                self.tab_strip.addSubview_(tab_v)
                y -= (tab_h + 2)
            else:
                tab_v = ChromeTabView.alloc().initWithFrame_controller_index_(Cocoa.NSMakeRect(x, 3, cur_w, tab_h), self, i)
                self.tab_strip.addSubview_(tab_v)
                x += cur_w + 2

        btn_add = Cocoa.NSButton.alloc().initWithFrame_(Cocoa.NSMakeRect(8 if is_vert else x + 4, y if is_vert else 4, 26, 26))
        btn_add.setTitle_("+")
        btn_add.setBezelStyle_(Cocoa.NSBezelStyleCircular)
        btn_add.setTarget_(self)
        btn_add.setAction_("onNewTab:")
        self.tab_strip.addSubview_(btn_add)
        self.tab_strip.setNeedsDisplay_(True)

    def onToggleVerticalTabs_(self, sender):
        SETTINGS.settings["vertical_tabs_collapsed"] = not SETTINGS.settings.get("vertical_tabs_collapsed", False)
        SETTINGS.save()
        self.relayout_views()

    def add_tab(self, url, pinned=False):
        config = WebKit.WKWebViewConfiguration.alloc().init()
        if self.is_incognito:
            apply_incognito_preferences(config)
        else:
            ds = WebKit.WKWebsiteDataStore.defaultDataStore()
            pcfg = create_nw_proxy_config("127.0.0.1", LOCAL_PROXY_PORT)
            if pcfg and hasattr(ds, "setProxyConfigurations_"):
                try:
                    ds.setProxyConfigurations_([pcfg])
                except Exception:
                    pass
            config.setWebsiteDataStore_(ds)
            try:
                config.preferences().setValue_forKey_(True, "developerExtrasEnabled")
                config.preferences().setValue_forKey_(True, "fullScreenEnabled")
                if hasattr(config.preferences(), "setJavaScriptCanOpenWindowsAutomatically_"):
                    config.preferences().setJavaScriptCanOpenWindowsAutomatically_(not SETTINGS.settings.get("block_popups", True))
                if hasattr(config.preferences(), "setJavaScriptEnabled_"):
                    config.preferences().setJavaScriptEnabled_(SETTINGS.settings.get("js_enabled", True))
                if hasattr(config.preferences(), "setElementFullscreenEnabled_"):
                    config.preferences().setElementFullscreenEnabled_(True)
            except Exception:
                pass

        config.setURLSchemeHandler_forURLScheme_(InternalSchemeHandler.alloc().init(), "browser")
        uc = WebKit.WKUserContentController.alloc().init()
        uc.addScriptMessageHandler_name_(self, "bridge")
        uc.addScriptMessageHandler_name_(self, "audio")
        uc.addScriptMessageHandler_name_(self, "terminalInput")
        uc.addScriptMessageHandler_name_(self, "terminalResize")

        uc.addUserScript_(WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(AUDIO_MONITOR_JS, WebKit.WKUserScriptInjectionTimeAtDocumentEnd, False))
        uc.addUserScript_(WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(IMAGE_CONTEXT_MENU_JS, WebKit.WKUserScriptInjectionTimeAtDocumentStart, False))
        uc.addUserScript_(WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(KEYCHAIN_AUTOSAVE_JS, WebKit.WKUserScriptInjectionTimeAtDocumentEnd, False))
        uc.addUserScript_(WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(IN_WINDOW_FULLSCREEN_JS, WebKit.WKUserScriptInjectionTimeAtDocumentEnd, False))

        if SETTINGS.settings.get("cosmetic_adblock_enabled", True):
            cosmetic_js = f"(function() {{ const st = document.createElement('style'); st.id = '__pybrowser_cosmetic_adblock'; st.textContent = `{COSMETIC_ADBLOCK_CSS}`; (document.head || document.documentElement).appendChild(st); }})();"
            uc.addUserScript_(WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(cosmetic_js, WebKit.WKUserScriptInjectionTimeAtDocumentStart, False))

        if SETTINGS.settings.get("yt_adblock_enabled", True):
            uc.addUserScript_(WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(YOUTUBE_AD_SHIELD_JS, WebKit.WKUserScriptInjectionTimeAtDocumentEnd, False))

        for script in get_installed_userscripts():
            if not script["enabled"]:
                continue
            matches = script.get("match", [])
            conds = []
            for m in matches:
                if m == "<all_urls>":
                    conds.append("true")
                else:
                    escaped_slash = r"\/"
                    reg = m.replace("*", ".*").replace("/", escaped_slash)
                    conds.append(f"new RegExp('^{reg}$').test(location.href)")
            cond_str = " || ".join(conds) if conds else "true"
            wrapped = f"if ({cond_str}) {{\n{script['code']}\n}}"
            uc.addUserScript_(WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(wrapped, WebKit.WKUserScriptInjectionTimeAtDocumentEnd, False))

        if WK_RULE_LIST and SETTINGS.settings.get("adblock_enabled", True):
            uc.addContentRuleList_(WK_RULE_LIST)
        config.setUserContentController_(uc)

        wv = BrowserWebView.alloc().initWithFrame_configuration_(self.web_container.bounds(), config)
        wv.controller = self
        wv.setAutoresizingMask_(Cocoa.NSViewWidthSizable | Cocoa.NSViewHeightSizable)
        wv.setNavigationDelegate_(self)
        wv.setUIDelegate_(self)
        wv.setCustomUserAgent_(SETTINGS.get_user_agent())
        if hasattr(wv, "setInspectable_"):
            try:
                wv.setInspectable_(True)
            except Exception:
                pass

        initial_zoom = float(SETTINGS.settings.get("default_zoom", 0.8))
        tab = {
            "id": str(uuid.uuid4()), "webview": wv, "url": url, "title": "New Tab",
            "favicon": None, "pinned": pinned, "group_id": None, "zoom": initial_zoom,
            "is_sleeping": False, "has_audio": False, "is_muted": False, "last_active_timestamp": time.time()
        }
        self.apply_zoom_to_webview(wv, tab)
        for k in ("URL", "title", "estimatedProgress"):
            try:
                wv.addObserver_forKeyPath_options_context_(self, k, Cocoa.NSKeyValueObservingOptionNew, None)
            except Exception:
                pass

        self.tabs.append(tab)
        self.relayout_tabs()
        self.switch_to_tab(len(self.tabs) - 1)
        req_url = url
        if SETTINGS.settings.get("https_only", True) and req_url.startswith("http://") and not req_url.startswith("http://localhost") and not req_url.startswith("http://127.0.0.1"):
            req_url = "https://" + req_url[7:]
        wv.loadRequest_(Foundation.NSURLRequest.requestWithURL_(Foundation.NSURL.URLWithString_(req_url)))
        self.save_session()

    def get_tab_index_by_id(self, tab_id):
        for i, t in enumerate(self.tabs):
            if t.get("id") == tab_id:
                return i
        return None

    def switch_to_tab(self, index):
        if not (0 <= index < len(self.tabs)):
            return
        self.active_tab_index = index
        self.omnibox_panel.hide()
        for sub in list(self.web_container.subviews()):
            sub.removeFromSuperview()
        tab = self.tabs[index]
        tab["last_active_timestamp"] = time.time()
        if tab.get("is_sleeping"):
            self.wake_tab(index)
        wv = tab["webview"]
        wv.setFrame_(self.web_container.bounds())
        self.web_container.addSubview_(wv)
        self.omnibox.set_url(tab.get("url", ""))
        self.apply_zoom_to_webview(wv, tab)
        self.update_star_status()
        self.update_progress_for_webview(wv)
        self.relayout_tabs()

    def close_tab(self, index):
        if 0 <= index < len(self.tabs):
            tab = self.tabs.pop(index)
            if tab.get("url") and not tab["url"].startswith("browser://"):
                self.closed_tabs.append(tab["url"])
            wv = tab.get("webview")
            if wv:
                TERMINAL_MANAGER.close_for_webview(wv)
                for k in ("URL", "title", "estimatedProgress"):
                    try:
                        wv.removeObserver_forKeyPath_(self, k)
                    except Exception:
                        pass
            if not self.tabs:
                self.window().close()
            else:
                if index < self.active_tab_index:
                    self.active_tab_index -= 1
                    self.relayout_tabs()
                elif index == self.active_tab_index:
                    self.switch_to_tab(max(0, min(index, len(self.tabs) - 1)))
                else:
                    self.relayout_tabs()
            self.save_session()

    def reopen_last_closed_tab(self):
        if self.closed_tabs:
            self.add_tab(self.closed_tabs.pop())

    def toggle_pin_tab(self, index):
        if 0 <= index < len(self.tabs):
            self.tabs[index]["pinned"] = not self.tabs[index].get("pinned", False)
            self.relayout_tabs()
            self.save_session()

    def duplicate_tab(self, index):
        if 0 <= index < len(self.tabs) and self.tabs[index].get("url"):
            self.add_tab(self.tabs[index]["url"])

    def toggle_mute(self, index):
        if 0 <= index < len(self.tabs):
            tab = self.tabs[index]
            tab["is_muted"] = not tab.get("is_muted", False)
            wv = tab.get("webview")
            if wv:
                if hasattr(wv, "setMediaMuted_"):
                    wv.setMediaMuted_(tab["is_muted"])
                else:
                    wv.evaluateJavaScript_completionHandler_(f"document.querySelectorAll('audio, video').forEach(el => el.muted = {'true' if tab['is_muted'] else 'false'});", None)
            self.relayout_tabs()

    def create_group_for_tab(self, index):
        if 0 <= index < len(self.tabs):
            gid = str(uuid.uuid4())
            self.groups.append(TabGroup(gid, "Group", "Blue", False))
            self.tabs[index]["group_id"] = gid
            self.relayout_tabs()
            self.save_session()

    def get_group(self, gid):
        return next((g for g in self.groups if g.id == gid), None)

    def check_sleeping_tabs(self):
        now = time.time()
        for i, tab in enumerate(self.tabs):
            if i == self.active_tab_index or tab.get("has_audio") or tab.get("is_sleeping"):
                continue
            if now - tab.get("last_active_timestamp", now) > 1800:
                self.sleep_tab(i)

    def sleep_tab(self, index):
        if not (0 <= index < len(self.tabs)) or index == self.active_tab_index:
            return
        tab = self.tabs[index]
        if tab.get("is_sleeping"):
            return
        wv = tab.get("webview")
        if wv:
            if wv.URL() and not wv.URL().absoluteString().startswith("about:"):
                tab["url"] = wv.URL().absoluteString()
            tab["is_sleeping"] = True
            wv.loadRequest_(Foundation.NSURLRequest.requestWithURL_(Foundation.NSURL.URLWithString_("about:blank")))
        else:
            tab["is_sleeping"] = True
        self.relayout_tabs()

    def wake_tab(self, index):
        if not (0 <= index < len(self.tabs)):
            return
        tab = self.tabs[index]
        if not tab.get("is_sleeping"):
            return
        tab["is_sleeping"] = False
        wv = tab.get("webview")
        if wv and tab.get("url"):
            wv.loadRequest_(Foundation.NSURLRequest.requestWithURL_(Foundation.NSURL.URLWithString_(tab["url"])))
        self.relayout_tabs()

    def is_url_bookmarked(self, url):
        if not url:
            return False
        try:
            with open(BOOKMARKS_FILE, "r", encoding="utf-8") as f:
                bms = json.load(f)
                if isinstance(bms, list):
                    return any(isinstance(b, dict) and b.get("url") == url for b in bms)
        except Exception:
            return False
        return False

    def update_star_status(self):
        wv = self.get_active_webview()
        url = wv.URL().absoluteString() if wv and wv.URL() else ""
        if hasattr(self, 'star_btn'):
            self.star_btn.setTitle_("★" if self.is_url_bookmarked(url) else "☆")

    def onBookmarkCurrentPage_(self, sender=None):
        wv = self.get_active_webview()
        if not wv or not wv.URL():
            return
        url = wv.URL().absoluteString()
        if not url or url.startswith("browser://"):
            return
        title = str(wv.title()) if wv.title() else url

        try:
            with open(BOOKMARKS_FILE, "r", encoding="utf-8") as f:
                bms = json.load(f)
                if not isinstance(bms, list):
                    bms = []
        except Exception:
            bms = []

        if self.is_url_bookmarked(url):
            bms = [b for b in bms if isinstance(b, dict) and b.get("url") != url]
        else:
            bms.append({"title": title, "url": url})

        atomic_write_json(BOOKMARKS_FILE, bms)
        self.bookmarks_bar.reload_bookmarks()
        self.update_star_status()

    def perform_download_image(self, img_url):
        if not SETTINGS.settings.get("downloads_enabled", True) or not img_url:
            return
        wv = self.get_active_webview()
        suggested = sanitize_suggested_filename(img_url)
        dest_dir = validate_and_get_download_dir()
        dest_path = os.path.join(dest_dir, suggested)
        base, ext = os.path.splitext(dest_path)
        counter = 1
        while os.path.exists(dest_path):
            dest_path = f"{base} ({counter}){ext}"
            counter += 1

        dl_id = self.register_download(os.path.basename(dest_path), dest_path, None)
        download_image_data(img_url, dest_path, webview=wv, completion_callback=lambda ok: self.finish_download(None, "Completed" if ok else "Failed", dl_id=dl_id))

    def perform_save_image_as(self, img_url):
        if not SETTINGS.settings.get("downloads_enabled", True) or not img_url:
            return
        wv = self.get_active_webview()
        suggested = sanitize_suggested_filename(img_url)
        default_dir = validate_and_get_download_dir()

        panel = Cocoa.NSSavePanel.savePanel()
        panel.setNameFieldStringValue_(suggested)
        panel.setDirectoryURL_(Foundation.NSURL.fileURLWithPath_(default_dir))
        if panel.runModal() == Cocoa.NSModalResponseOK and panel.URL():
            dest_path = panel.URL().path()
            dl_id = self.register_download(os.path.basename(dest_path), dest_path, None)
            download_image_data(img_url, dest_path, webview=wv, completion_callback=lambda ok: self.finish_download(None, "Completed" if ok else "Failed", dl_id=dl_id))

    def perform_copy_image_address(self, img_url):
        if not img_url:
            return
        pb = Cocoa.NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(img_url, Cocoa.NSPasteboardTypeString)

    def apply_zoom_to_webview(self, wv, tab=None):
        if not wv:
            return
        target_zoom = tab.get("zoom", float(SETTINGS.settings.get("default_zoom", 0.8))) if tab else float(SETTINGS.settings.get("default_zoom", 0.8))
        target_zoom = max(0.5, min(2.0, target_zoom))
        try:
            if hasattr(wv, "setAllowsMagnification_"): wv.setAllowsMagnification_(False)
            if hasattr(wv, "setMagnification_"): wv.setMagnification_(1.0)
            wv.setPageZoom_(target_zoom)
        except Exception:
            pass

    def restore_normal_zoom(self):
        wv = self.get_active_webview()
        if wv:
            self.apply_zoom_to_webview(wv)
            wv.evaluateJavaScript_completionHandler_(REFLOW_RESTORE_JS, None)

    def handle_fullscreen_enter(self, auto_hide_bars=True):
        if auto_hide_bars and self.tab_bar_visible:
            self.tab_bar_visible = False
            self.relayout_views()
        wv = self.get_active_webview()
        if wv:
            try:
                wv.setFrame_(self.web_container.bounds())
                wv.evaluateJavaScript_completionHandler_("if (window.setTerminalFullscreen) window.setTerminalFullscreen(true);", None)
            except Exception:
                pass

    def handle_fullscreen_exit(self, restore_bars=True):
        if restore_bars and not self.tab_bar_visible:
            self.tab_bar_visible = True
            self.relayout_views()
        wv = self.get_active_webview()
        if wv:
            wv.evaluateJavaScript_completionHandler_("if (window.setTerminalFullscreen) window.setTerminalFullscreen(false);", None)
        self.restore_normal_zoom()

    def handle_escape_key(self):
        if self.find_bar and not self.find_bar.isHidden():
            self.find_bar.setHidden_(True)
            return
        wv = self.get_active_webview()
        if not self.tab_bar_visible:
            self.handle_fullscreen_exit(restore_bars=True)
            return
        if wv:
            def on_done(res, err):
                if res or not self.tab_bar_visible:
                    self.handle_fullscreen_exit(restore_bars=True)
                else:
                    self.restore_normal_zoom()
            wv.evaluateJavaScript_completionHandler_(ESCAPE_CHECK_JS, on_done)
        else:
            if not self.tab_bar_visible:
                self.handle_fullscreen_exit(restore_bars=True)
            else:
                self.restore_normal_zoom()

    def toggle_tab_bar(self):
        self.tab_bar_visible = not self.tab_bar_visible
        self.relayout_views()
        wv = self.get_active_webview()
        if wv:
            is_fs = not self.tab_bar_visible
            wv.evaluateJavaScript_completionHandler_(f"if (window.setTerminalFullscreen) window.setTerminalFullscreen({'true' if is_fs else 'false'});", None)

    def onToggleZenMode_(self, sender=None): self.toggle_tab_bar()

    def onZoomIn_(self, sender=None):
        if 0 <= self.active_tab_index < len(self.tabs):
            tab = self.tabs[self.active_tab_index]
            z = min(2.0, round(tab.get("zoom", 0.8) + 0.1, 2))
            tab["zoom"] = z
            self.apply_zoom_to_webview(tab.get("webview"), tab)

    def onZoomOut_(self, sender=None):
        if 0 <= self.active_tab_index < len(self.tabs):
            tab = self.tabs[self.active_tab_index]
            z = max(0.5, round(tab.get("zoom", 0.8) - 0.1, 2))
            tab["zoom"] = z
            self.apply_zoom_to_webview(tab.get("webview"), tab)

    def onResetZoom_(self, sender=None):
        if 0 <= self.active_tab_index < len(self.tabs):
            tab = self.tabs[self.active_tab_index]
            tab["zoom"] = float(SETTINGS.settings.get("default_zoom", 0.8))
            self.apply_zoom_to_webview(tab.get("webview"), tab)
        self.restore_normal_zoom()

    def onInspectElement_(self, sender=None):
        wv = self.get_active_webview()
        if wv:
            try:
                Cocoa.NSApp.sendAction_to_from_(Cocoa.NSSelectorFromString("_showInspector:"), wv, None)
            except Exception:
                pass

    def onMenu_(self, sender):
        menu = Cocoa.NSMenu.alloc().init()
        menu.addItemWithTitle_action_keyEquivalent_("New Tab", "onNewTab:", "t").setTarget_(self)
        menu.addItemWithTitle_action_keyEquivalent_("New Window", "onNewWindow:", "n").setTarget_(Cocoa.NSApp.delegate())
        inc_item = menu.addItemWithTitle_action_keyEquivalent_("New Incognito Window", "onNewIncognitoWindow:", "N")
        inc_item.setKeyEquivalentModifierMask_(Cocoa.NSEventModifierFlagCommand | Cocoa.NSEventModifierFlagShift)
        inc_item.setTarget_(Cocoa.NSApp.delegate())
        menu.addItemWithTitle_action_keyEquivalent_("New Terminal Tab", "openTerminal:", "T").setTarget_(self)
        menu.addItem_(Cocoa.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("Bookmark Current Page", "onBookmarkCurrentPage:", "d").setTarget_(self)
        bm_text = "Hide Bookmarks Bar" if SETTINGS.settings.get("bookmarks_bar_visible", True) else "Show Bookmarks Bar"
        menu.addItemWithTitle_action_keyEquivalent_(bm_text, "toggleBookmarksBar:", "b").setTarget_(self)
        menu.addItemWithTitle_action_keyEquivalent_("Toggle Zen Mode", "onToggleZenMode:", "").setTarget_(self)
        menu.addItemWithTitle_action_keyEquivalent_("Find in Page", "onFindInPage:", "f").setTarget_(self)
        menu.addItem_(Cocoa.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("Settings", "openSettings:", ",").setTarget_(self)
        menu.addItemWithTitle_action_keyEquivalent_("History", "openHistory:", "y").setTarget_(self)
        menu.addItemWithTitle_action_keyEquivalent_("Bookmarks", "openBookmarks:", "O").setTarget_(self)
        menu.addItemWithTitle_action_keyEquivalent_("Downloads", "openDownloads:", "D").setTarget_(self)
        menu.addItemWithTitle_action_keyEquivalent_("Extensions", "openExtensions:", "e").setTarget_(self)
        menu.addItemWithTitle_action_keyEquivalent_("Task Manager", "showTaskManager:", "").setTarget_(Cocoa.NSApp.delegate())
        menu.addItem_(Cocoa.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("Inspect Element", "onInspectElement:", "i").setTarget_(self)
        menu.addItem_(Cocoa.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("Quit PyBrowser", "terminate:", "q").setTarget_(Cocoa.NSApp)
        menu.popUpMenuPositioningItem_atLocation_inView_(None, Cocoa.NSMakePoint(0, 0), sender)

    def toggleBookmarksBar_(self, sender=None):
        SETTINGS.settings["bookmarks_bar_visible"] = not SETTINGS.settings.get("bookmarks_bar_visible", True)
        SETTINGS.save()
        self.relayout_views()

    def openTerminal_(self, s=None): self.add_tab("browser://terminal")
    def openSettings_(self, s=None): self.add_tab("browser://settings")
    def openHistory_(self, s=None): self.add_tab("browser://history")
    def openBookmarks_(self, s=None): self.add_tab("browser://bookmarks")
    def openDownloads_(self, s=None): self.add_tab("browser://downloads")
    def openExtensions_(self, s=None): self.add_tab("browser://extensions")
    def onNewTab_(self, s=None): self.add_tab(DEFAULT_URL)
    def onCloseTab_(self, s=None): self.close_tab(self.active_tab_index)
    def onReopenTab_(self, s=None): self.reopen_last_closed_tab()

    def onGoBack_(self, s=None):
        wv = self.get_active_webview()
        if wv and wv.canGoBack(): wv.goBack_(s)

    def onGoForward_(self, s=None):
        wv = self.get_active_webview()
        if wv and wv.canGoForward(): wv.goForward_(s)

    def onReload_(self, s=None):
        wv = self.get_active_webview()
        if wv: wv.reload()

    def onGoHome_(self, s=None):
        url = SETTINGS.settings.get("home_url", "https://www.google.com")
        wv = self.get_active_webview()
        if wv: wv.loadRequest_(Foundation.NSURLRequest.requestWithURL_(Foundation.NSURL.URLWithString_(url)))

    def onFocusOmnibox_(self, s=None): self.window().makeFirstResponder_(self.omnibox)
    def onFindInPage_(self, s=None):
        self.find_bar.setHidden_(False)
        self.window().makeFirstResponder_(self.find_bar.search_field)

    def onTogglePiP_(self, s=None):
        Cocoa.NSApp.delegate().pip_panel.toggle()
        wv = self.get_active_webview()
        if wv:
            wv.evaluateJavaScript_completionHandler_("const v = document.querySelector('video'); if (v && v.webkitSetPresentationMode) v.webkitSetPresentationMode(v.webkitPresentationMode === 'picture-in-picture' ? 'inline' : 'picture-in-picture');", None)

    def onShareCurrentPage_(self, sender):
        wv = self.get_active_webview()
        if wv and wv.URL():
            Cocoa.NSSharingServicePicker.alloc().initWithItems_([wv.URL()]).showRelativeToRect_ofView_preferredEdge_(sender.bounds(), sender, Cocoa.NSMinYEdge)

    def onShowDownloads_(self, sender):
        menu = Cocoa.NSMenu.alloc().init()
        if not self.downloads:
            menu.addItemWithTitle_action_keyEquivalent_("No Recent Downloads", "", "").setEnabled_(False)
        else:
            for dl in reversed(self.downloads[-10:]):
                item = menu.addItemWithTitle_action_keyEquivalent_(f"{dl['name']} ({dl['status']})", "revealDownload:", "")
                item.setRepresentedObject_(dl['path'])
                item.setTarget_(self)
        menu.addItem_(Cocoa.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("Open Downloads Manager", "openDownloads:", "").setTarget_(self)
        menu.popUpMenuPositioningItem_atLocation_inView_(None, Cocoa.NSMakePoint(0, 0), sender)

    def revealDownload_(self, sender):
        path = sender.representedObject()
        if path and os.path.exists(path):
            Cocoa.NSWorkspace.sharedWorkspace().selectFile_inFileViewerRootedAtPath_(path, None)

    def onSiteInfo_(self, sender):
        wv = self.get_active_webview()
        if not wv: return
        url = wv.URL().absoluteString() if wv.URL() else "browser://newtab"
        parsed = Foundation.NSURL.URLWithString_(url)
        menu = Cocoa.NSMenu.alloc().init()
        menu.addItemWithTitle_action_keyEquivalent_(f"Host: {parsed.host() or 'Internal Page'}", "", "").setEnabled_(False)
        menu.addItemWithTitle_action_keyEquivalent_("Connection: Secure (HTTPS)" if parsed.scheme() == "https" else "Connection: Insecure / Local", "", "").setEnabled_(False)
        menu.addItem_(Cocoa.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("Copy Current URL", "onCopyUrl:", "").setTarget_(self)
        menu.popUpMenuPositioningItem_atLocation_inView_(None, Cocoa.NSMakePoint(0, 0), sender)

    def onCopyUrl_(self, sender):
        wv = self.get_active_webview()
        if wv and wv.URL():
            pb = Cocoa.NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(wv.URL().absoluteString(), Cocoa.NSPasteboardTypeString)

    def onSwitchTab1_(self, s): self.switch_to_tab(0)
    def onSwitchTab2_(self, s): self.switch_to_tab(1)
    def onSwitchTab3_(self, s): self.switch_to_tab(2)
    def onSwitchTab4_(self, s): self.switch_to_tab(3)
    def onSwitchTab5_(self, s): self.switch_to_tab(4)
    def onSwitchTab6_(self, s): self.switch_to_tab(5)
    def onSwitchTab7_(self, s): self.switch_to_tab(6)
    def onSwitchTab8_(self, s): self.switch_to_tab(7)
    def onSwitchTab9_(self, s):
        if self.tabs: self.switch_to_tab(len(self.tabs) - 1)

    def get_active_webview(self):
        if 0 <= self.active_tab_index < len(self.tabs):
            return self.tabs[self.active_tab_index].get("webview")
        return None

    def onOmniboxEnter_(self, sender):
        self.omnibox_panel.hide()
        val = self.omnibox.stringValue().strip()
        if not val:
            return
        if val.startswith("chrome://"): val = "browser://" + val[9:]
        elif val.startswith("edge://"): val = "browser://" + val[7:]
        if val.startswith("localhost") or val.startswith("127.0.0.1"): val = "http://" + val
        elif not (val.startswith("http://") or val.startswith("https://") or val.startswith("browser://")):
            val = "https://" + val if ("." in val and " " not in val) else SETTINGS.get_search_url(val)

        if SETTINGS.settings.get("search_in_new_tab", False):
            self.add_tab(val)
        else:
            wv = self.get_active_webview()
            if wv:
                wv.loadRequest_(Foundation.NSURLRequest.requestWithURL_(Foundation.NSURL.URLWithString_(val)))
                self.window().makeFirstResponder_(self.web_container)

    def controlTextDidChange_(self, notif):
        if notif.object() == self.omnibox:
            self.omnibox_panel.handle_text_change(self.omnibox.stringValue())

    def control_textView_doCommandBySelector_(self, control, textView, commandSelector):
        return self.omnibox_panel.handle_command_selector(commandSelector)

    def register_download(self, name, path, obj):
        dl_id = str(uuid.uuid4())
        self.downloads.append({"id": dl_id, "name": name, "path": path, "status": "Downloading", "progress": 0.0, "obj": obj})
        self.dl_btn.setTitle_(f"⬇ ({len(self.downloads)})")
        self.sync_downloads_page()
        return dl_id

    def finish_download(self, obj, status, dl_id=None, dl_path=None):
        for d in reversed(self.downloads):
            if (dl_id and d.get("id") == dl_id) or (dl_path and d.get("path") == dl_path) or (obj and d.get("obj") == obj):
                d["status"], d["progress"] = status, 1.0
                break
        self.sync_downloads_page()

    def sync_downloads_page(self):
        for t in self.tabs:
            wv = t.get("webview")
            if wv and wv.URL() and "browser://downloads" in wv.URL().absoluteString():
                for d in self.downloads:
                    id_json = json.dumps(d.get('id'))
                    name_json = json.dumps(d.get('name'))
                    path_json = json.dumps(d.get('path'))
                    status_json = json.dumps(d.get('status'))
                    prog = d.get('progress', 0.0)
                    wv.evaluateJavaScript_completionHandler_(f"window.updateDownload({id_json}, {name_json}, {path_json}, {status_json}, {prog});", None)

    def show_password_banner(self, host, username, password):
        if not host or not password:
            return
        if hasattr(self, 'pw_banner') and self.pw_banner and self.pw_banner.superview():
            return
        w = self.web_container.bounds().size.width
        self.pw_banner = Cocoa.NSVisualEffectView.alloc().initWithFrame_(Cocoa.NSMakeRect(0, self.web_container.bounds().size.height - 40, w, 40))
        self.pw_banner.setMaterial_(Cocoa.NSVisualEffectMaterialPopover)
        self.pw_banner.setAutoresizingMask_(Cocoa.NSViewWidthSizable | Cocoa.NSViewMinYMargin)

        lbl = Cocoa.NSTextField.labelWithString_(f"Save password for {host} in PyBrowser Keychain?")
        lbl.setFrameOrigin_(Cocoa.NSMakePoint(20, 11))
        lbl.setFont_(Cocoa.NSFont.systemFontOfSize_(12))
        self.pw_banner.addSubview_(lbl)

        btn_save = Cocoa.NSButton.alloc().initWithFrame_(Cocoa.NSMakeRect(w - 90, 8, 75, 24))
        btn_save.setTitle_("Save")
        btn_save.setBezelStyle_(Cocoa.NSBezelStyleRounded)
        btn_save.setAutoresizingMask_(Cocoa.NSViewMinXMargin)
        btn_save.setAction_("onSavePassword:")
        btn_save.setTarget_(self)
        self.pw_banner.addSubview_(btn_save)

        btn_never = Cocoa.NSButton.alloc().initWithFrame_(Cocoa.NSMakeRect(w - 175, 8, 75, 24))
        btn_never.setTitle_("Never")
        btn_never.setBezelStyle_(Cocoa.NSBezelStyleRounded)
        btn_never.setAutoresizingMask_(Cocoa.NSViewMinXMargin)
        btn_never.setAction_("onDismissPassword:")
        btn_never.setTarget_(self)
        self.pw_banner.addSubview_(btn_never)

        self.pending_pw = {"host": host, "username": username, "password": password}
        self.web_container.addSubview_(self.pw_banner)

    def onSavePassword_(self, sender):
        if hasattr(self, 'pending_pw'):
            p = self.pending_pw
            save_to_keychain(p["host"], p["username"], p["password"])
            SETTINGS.settings.setdefault("saved_logins", {})[p["host"]] = p["username"]
            SETTINGS.save()
        self.onDismissPassword_(sender)

    def onDismissPassword_(self, sender):
        if hasattr(self, 'pw_banner') and self.pw_banner:
            self.pw_banner.removeFromSuperview()
            self.pw_banner = None

    def update_progress_for_webview(self, wv):
        if wv != self.get_active_webview():
            return
        if not wv.isLoading():
            self.progress_bar.setHidden_(True)
            self.progress_bar.setProgress_(0.0)
            return
        p = max(0.1, float(wv.estimatedProgress()))
        self.progress_bar.setHidden_(False)
        self.progress_bar.setProgress_(p)
        if p >= 1.0:
            Cocoa.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.2, False, lambda t: self.progress_bar.setHidden_(True))

    def userContentController_didReceiveScriptMessage_(self, ucc, message):
        name = message.name()
        if name == "terminalInput":
            TERMINAL_MANAGER.write_active(message.webView(), message.body())
            return
        elif name == "terminalResize":
            b = message.body()
            TERMINAL_MANAGER.get_or_create(message.webView(), b.get("cols", 80), b.get("rows", 24))
            return
        elif name == "audio":
            b = message.body() if isinstance(message.body(), dict) else {}
            has_a = (b.get("cmd") == "audio_play")
            for t in self.tabs:
                if t.get("webview") == message.webView():
                    t["has_audio"] = has_a
                    break
            self.relayout_tabs()
            return

        body = message.body()
        cmd = body.get("cmd") if isinstance(body, dict) else None

        if cmd == "open_url":
            if body.get("url"):
                self.add_tab(body.get("url"))
        elif cmd == "toggle_fullscreen_terminal":
            self.toggle_tab_bar()
        elif cmd == "context_target_image":
            self.last_context_image_url = body.get("src")
        elif cmd == "blob_image_data":
            data_url, dest_path = body.get("data"), body.get("path")
            if data_url and dest_path:
                try:
                    raw = base64.b64decode(data_url.split(",", 1)[1])
                    with open(dest_path, "wb") as f:
                        f.write(raw)
                    self.finish_download(None, "Completed", dl_path=dest_path)
                except Exception as e:
                    print(f"[Blob Image Save Error] {e}")
                    self.finish_download(None, "Failed", dl_path=dest_path)
        elif cmd == "image_download_failed":
            self.finish_download(None, "Failed", dl_path=body.get("path"))
        elif cmd == "toggle_script":
            fname = body.get("file")
            if fname:
                p1 = os.path.join(USERSCRIPTS_DIR, fname)
                p2 = os.path.join(USERSCRIPTS_DIR, fname + ".disabled")
                if os.path.exists(p2): os.rename(p2, p1)
                elif os.path.exists(p1): os.rename(p1, p2)
        elif cmd == "delete_script":
            fname = body.get("file")
            if fname:
                for p in (os.path.join(USERSCRIPTS_DIR, fname), os.path.join(USERSCRIPTS_DIR, fname + ".disabled")):
                    if os.path.exists(p): os.remove(p)
        elif cmd == "install_crx":
            cid = body.get("id")
            if cid:
                threading.Thread(target=lambda: install_crx(cid), daemon=True).start()
        elif cmd == "prompt_save_password":
            self.show_password_banner(body.get("host", ""), body.get("username", ""), body.get("password", ""))
        elif cmd == "check_autofill":
            host = body.get("host")
            saved_user = SETTINGS.settings.get("saved_logins", {}).get(host)
            if saved_user:
                pwd = get_from_keychain(host, saved_user)
                if pwd:
                    wv = self.get_active_webview()
                    if wv:
                        wv.evaluateJavaScript_completionHandler_(f"let pw = document.querySelector('input[type=password]'); let user = document.querySelector('input[type=text], input[type=email]'); if(pw) pw.value = '{pwd}'; if(user) user.value = '{saved_user}';", None)
        elif cmd == "save_setting":
            key, val = body.get("key"), body.get("val")
            if key == "download_path":
                val = os.path.expanduser(str(val).strip())
            SETTINGS.settings[key] = val
            SETTINGS.save()
            if key in ("bookmarks_bar_visible", "show_home_button", "vertical_tabs_enabled"):
                self.relayout_views()
            elif key == "theme_mode":
                app_delegate = Cocoa.NSApp.delegate()
                for c in getattr(app_delegate, "controllers", []):
                    c.tab_strip.setNeedsDisplay_(True)
                    c.toolbar.setNeedsDisplay_(True)
                    c.bookmarks_bar.setNeedsDisplay_(True)
                    for sub in c.tab_strip.subviews():
                        sub.setNeedsDisplay_(True)
            elif key in ("default_zoom",):
                wv = self.get_active_webview()
                if wv:
                    self.apply_zoom_to_webview(wv)
            elif key in ("user_agent_mode", "custom_user_agent"):
                ua = SETTINGS.get_user_agent()
                for t in self.tabs:
                    if t.get("webview"):
                        t["webview"].setCustomUserAgent_(ua)
            elif key == "doh_provider":
                with DNS_LOCK:
                    DNS_CACHE.clear()
            elif key in ("adblock_enabled", "adblock_stevenblack_enabled", "adblock_base_enabled", "custom_adblock_domains"):
                compile_webkit_adblock_rules()
        elif cmd == "choose_download_directory":
            panel = Cocoa.NSOpenPanel.openPanel()
            panel.setCanChooseDirectories_(True)
            panel.setCanChooseFiles_(False)
            panel.setAllowsMultipleSelection_(False)
            panel.setTreatsFilePackagesAsDirectories_(True)
            panel.setPrompt_("Select Download Location")
            cur_dir = validate_and_get_download_dir()
            panel.setDirectoryURL_(Foundation.NSURL.fileURLWithPath_(cur_dir))
            if panel.runModal() == Cocoa.NSModalResponseOK and panel.URL():
                chosen_path = panel.URL().path()
                if os.access(chosen_path, os.W_OK):
                    SETTINGS.settings["download_path"] = chosen_path
                    SETTINGS.save()
                    wv = self.get_active_webview()
                    if wv:
                        path_json = json.dumps(chosen_path)
                        wv.evaluateJavaScript_completionHandler_(
                            f"if (document.getElementById('dl-path-input')) document.getElementById('dl-path-input').value = {path_json}; if (document.getElementById('dl-path-display')) document.getElementById('dl-path-display').innerText = {path_json}; showToast('Download directory updated');", None
                        )
                else:
                    wv = self.get_active_webview()
                    if wv:
                        wv.evaluateJavaScript_completionHandler_("showToast('Error: Selected directory is not writable');", None)
        elif cmd == "search_blocked_domains":
            matches = search_blocked_domains(body.get("query", ""), limit=100)
            json_res = json.dumps(matches)
            wv = self.get_active_webview()
            if wv:
                wv.evaluateJavaScript_completionHandler_(f"window.renderBlockedResults({json_res});", None)
        elif cmd == "update_adblock_filters":
            def fetch_task():
                count = update_adblock_filters_from_web()
                def on_done():
                    wv = self.get_active_webview()
                    if wv:
                        wv.evaluateJavaScript_completionHandler_(f"const b = document.getElementById('adblock-count-badge'); if (b) b.innerText = '{count:,} rules'; const btn = document.getElementById('update-filters-btn'); if (btn) {{ btn.innerText = 'Update Lists Now'; btn.disabled = false; }} if (window.showToast) showToast('Adblock filters updated');", None)
                dispatch_to_main(on_done)
            threading.Thread(target=fetch_task, daemon=True).start()
        elif cmd == "clear_browsing_data":
            targets = body.get("targets", [])
            if "history" in targets:
                clear_history()
            if "favicons" in targets:
                FaviconCache.clear()
            if "dns_cache" in targets:
                with DNS_LOCK:
                    DNS_CACHE.clear()
            if "cookies_cache" in targets:
                ds = WebKit.WKWebsiteDataStore.defaultDataStore()
                ds.removeDataOfTypes_modifiedSince_completionHandler_(WebKit.WKWebsiteDataStore.allWebsiteDataTypes(), Foundation.NSDate.dateWithTimeIntervalSince1970_(0), lambda: None)
        elif cmd == "reset_defaults":
            SETTINGS.reset_defaults()
            self.relayout_views()
            for tab in self.tabs:
                wv = tab.get("webview")
                if wv:
                    self.apply_zoom_to_webview(wv)
            wv = self.get_active_webview()
            if wv:
                wv.reload()
        elif cmd == "html5_fullscreen_enter":
            self.handle_fullscreen_enter(auto_hide_bars=True)
        elif cmd == "html5_fullscreen_exit":
            self.handle_fullscreen_exit(restore_bars=True)
        elif cmd == "dl_request_sync":
            self.sync_downloads_page()
        elif cmd == "dl_show":
            p = body.get("path")
            if p and os.path.exists(p):
                Cocoa.NSWorkspace.sharedWorkspace().selectFile_inFileViewerRootedAtPath_(p, None)
            else:
                dest = validate_and_get_download_dir()
                if os.path.exists(dest):
                    Cocoa.NSWorkspace.sharedWorkspace().openFile_(dest)
        elif cmd == "dl_cancel":
            dl_id = body.get("id")
            for d in self.downloads:
                if d["id"] == dl_id and d["status"] == "Downloading":
                    if d.get("obj") and hasattr(d["obj"], "cancel"):
                        d["obj"].cancel()
                    self.finish_download(d.get("obj"), "Canceled", dl_id=dl_id)
                    break
        elif cmd == "delete_bookmark":
            target_url = body.get("url")
            if target_url:
                try:
                    with open(BOOKMARKS_FILE, "r", encoding="utf-8") as f:
                        bms = json.load(f)
                except Exception:
                    bms = []
                atomic_write_json(BOOKMARKS_FILE, [b for b in bms if isinstance(b, dict) and b.get("url") != target_url])
                self.bookmarks_bar.reload_bookmarks()
                self.update_star_status()
        elif cmd == "add_bookmark":
            u = body.get("url")
            t = body.get("title") or u
            if u:
                try:
                    with open(BOOKMARKS_FILE, "r", encoding="utf-8") as f:
                        bms = json.load(f)
                except Exception:
                    bms = []
                if not any(isinstance(b, dict) and b.get("url") == u for b in bms):
                    bms.append({"title": t, "url": u})
                    atomic_write_json(BOOKMARKS_FILE, bms)
                    self.bookmarks_bar.reload_bookmarks()
                    self.update_star_status()
        elif cmd == "delete_history_item":
            item_id = body.get("id")
            if item_id is not None:
                try:
                    with sqlite3.connect(HISTORY_FILE) as conn:
                        conn.execute("DELETE FROM history WHERE id = ?;", (item_id,))
                except Exception:
                    pass

    # WebKit Delegates
    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(self, wv, cfg, act, feat):
        pending_action = getattr(wv, "pending_image_action", None)
        if pending_action:
            wv.pending_image_action = None
            img_url = act.request().URL().absoluteString() if act.request().URL() else None
            if img_url:
                if pending_action == "save_as": self.perform_save_image_as(img_url)
                elif pending_action == "download": self.perform_download_image(img_url)
                elif pending_action == "copy_address": self.perform_copy_image_address(img_url)
            return None

        url = act.request().URL().absoluteString() if act.request().URL() else None
        if url:
            self.add_tab(url)
        return None

    def webView_decidePolicyForNavigationAction_decisionHandler_(self, wv, action, handler):
        mods = action.modifierFlags() if hasattr(action, "modifierFlags") else 0
        if bool(mods & Cocoa.NSEventModifierFlagCommand) and action.navigationType() == WebKit.WKNavigationTypeLinkActivated:
            url = action.request().URL().absoluteString() if action.request().URL() else None
            if url:
                self.add_tab(url)
                handler(WebKit.WKNavigationActionPolicyCancel)
                return
        handler(WebKit.WKNavigationActionPolicyAllow)

    def webView_decidePolicyForNavigationResponse_decisionHandler_(self, wv, resp, handler):
        if not resp.canShowMIMEType():
            handler(WebKit.WKNavigationResponsePolicyDownload if SETTINGS.settings.get("downloads_enabled", True) else WebKit.WKNavigationResponsePolicyCancel)
            return
        if isinstance(resp.response(), Foundation.NSHTTPURLResponse):
            disp = resp.response().allHeaderFields().get("Content-Disposition", "") or resp.response().allHeaderFields().get("content-disposition", "")
            if "attachment" in str(disp).lower():
                handler(WebKit.WKNavigationResponsePolicyDownload if SETTINGS.settings.get("downloads_enabled", True) else WebKit.WKNavigationResponsePolicyCancel)
                return
        handler(WebKit.WKNavigationResponsePolicyAllow)

    def webView_runOpenPanelWithParameters_initiatedByFrame_completionHandler_(self, wv, params, frame, handler):
        panel = Cocoa.NSOpenPanel.openPanel()
        panel.setAllowsMultipleSelection_(params.allowsMultipleSelection())
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        handler(panel.URLs() if panel.runModal() == Cocoa.NSModalResponseOK else None)

    def webView_navigationAction_didBecomeDownload_(self, w, a, d):
        d.setDelegate_(self.download_delegate)

    def webView_navigationResponse_didBecomeDownload_(self, w, r, d):
        d.setDelegate_(self.download_delegate)

    def webView_didStartProvisionalNavigation_(self, wv, nav):
        if wv == self.get_active_webview():
            self.progress_bar.setHidden_(False)
            self.progress_bar.setProgress_(0.15)
        self.apply_zoom_to_webview(wv)

    def webView_didFinishNavigation_(self, wv, nav):
        tab = self.tabs[self.active_tab_index] if 0 <= self.active_tab_index < len(self.tabs) else None
        self.apply_zoom_to_webview(wv, tab)
        self.update_star_status()
        if wv == self.get_active_webview():
            self.progress_bar.setProgress_(1.0)
            Cocoa.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.2, False, lambda t: self.progress_bar.setHidden_(True))
        if not self.is_incognito and wv.URL() and SETTINGS.settings.get("history_enabled", True):
            record_history(wv.URL().absoluteString(), wv.title() or wv.URL().absoluteString())
        self.save_session()

    def webView_didFailProvisionalNavigation_withError_(self, wv, nav, err):
        if wv == self.get_active_webview():
            self.progress_bar.setHidden_(True)
            self.progress_bar.setProgress_(0.0)

    def webView_didFailNavigation_withError_(self, wv, nav, err):
        if wv == self.get_active_webview():
            self.progress_bar.setHidden_(True)
            self.progress_bar.setProgress_(0.0)

    def webViewWebContentProcessDidTerminate_(self, wv):
        TERMINAL_MANAGER.close_for_webview(wv)
        wv.reload()

    def observeValueForKeyPath_ofObject_change_context_(self, keyPath, obj, change, context):
        if keyPath == "URL" and obj.URL():
            for t in self.tabs:
                if t.get("webview") == obj:
                    if t.get("is_sleeping"):
                        break
                    t["url"] = obj.URL().absoluteString()
                    if obj.URL().host():
                        FaviconCache.get_favicon(obj.URL().host(), lambda img, tab=t: self.set_favicon(tab, img))
                    break
            if self.get_active_webview() == obj:
                tab = self.tabs[self.active_tab_index] if 0 <= self.active_tab_index < len(self.tabs) else {}
                if not tab.get("is_sleeping"):
                    self.omnibox.set_url(obj.URL().absoluteString())
                self.update_star_status()
        elif keyPath == "title" and obj.title():
            for t in self.tabs:
                if t.get("webview") == obj:
                    if not t.get("is_sleeping"):
                        t["title"] = str(obj.title())
                    break
            self.relayout_tabs()
        elif keyPath == "estimatedProgress":
            self.update_progress_for_webview(obj)
        elif keyPath == "fractionCompleted":
            try:
                frac = float(obj.fractionCompleted())
                for d in self.downloads:
                    if d.get("obj") and hasattr(d["obj"], "progress") and d["obj"].progress() == obj:
                        d["progress"] = frac
                        self.sync_downloads_page()
                        break
            except Exception:
                pass

    def set_favicon(self, tab, img):
        tab["favicon"] = img
        self.relayout_tabs()

    def windowDidEnterFullScreen_(self, notif):
        self.is_fullscreen = True
        self.relayout_views()
        self.restore_normal_zoom()

    def windowDidExitFullScreen_(self, notif):
        self.is_fullscreen = False
        self.relayout_views()
        self.restore_normal_zoom()

    def windowDidResignKey_(self, notif):
        if hasattr(self, 'omnibox_panel') and self.omnibox_panel.is_visible():
            self.omnibox_panel.hide()

    def windowDidResize_(self, notif):
        self.relayout_views()
        if self.omnibox_panel.is_visible():
            self.omnibox_panel.hide()

    def windowDidMove_(self, notif):
        if self.omnibox_panel.is_visible():
            self.omnibox_panel.hide()

    def windowWillClose_(self, notif):
        self.save_session()
        for t in self.tabs:
            wv = t.get("webview")
            if wv:
                TERMINAL_MANAGER.close_for_webview(wv)
                for k in ("URL", "title", "estimatedProgress"):
                    try:
                        wv.removeObserver_forKeyPath_(self, k)
                    except Exception:
                        pass
        app_delegate = Cocoa.NSApp.delegate()
        if app_delegate and hasattr(app_delegate, "controllers") and self in app_delegate.controllers:
            app_delegate.controllers.remove(self)

# ============================================================================
# APP DELEGATE
# ============================================================================
class AppDelegate(Cocoa.NSObject):
    def applicationDidFinishLaunching_(self, notif):
        try:
            init_history_db()
            load_adblock_filters_from_disk()
            compile_webkit_adblock_rules()
            load_dns_cache()
            start_proxy_server()

            self.controllers = []
            self.process_manager = ProcessManagerController.alloc().init()
            self.quick_switcher = QuickSwitcherController.alloc().init()
            self.setup_menu()

            main_window = BrowserWindowController.alloc().init()
            self.controllers.append(main_window)
            self.pip_panel = PiPPanelController.alloc().initWithBrowser_(main_window)
            Cocoa.NSApp.activateIgnoringOtherApps_(True)

            def click_monitor(event):
                for c in self.controllers:
                    if c.omnibox_panel.is_visible():
                        if c.omnibox_panel.suggestion_window and event.window() == c.omnibox_panel.suggestion_window:
                            return event
                        pt = event.locationInWindow()
                        if event.window() == c.window():
                            in_omni = Cocoa.NSPointInRect(pt, c.omnibox_container.convertRect_toView_(c.omnibox_container.bounds(), None))
                            if in_omni:
                                return event
                        c.omnibox_panel.hide()
                return event
            Cocoa.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(Cocoa.NSEventMaskLeftMouseDown, click_monitor)

            def key_monitor(event):
                flags = event.modifierFlags()
                cmd = bool(flags & Cocoa.NSEventModifierFlagCommand)
                shift = bool(flags & Cocoa.NSEventModifierFlagShift)
                opt = bool(flags & Cocoa.NSEventModifierFlagOption)
                key = event.keyCode()
                chars = event.charactersIgnoringModifiers().lower() if event.charactersIgnoringModifiers() else ""
                win = Cocoa.NSApp.keyWindow()
                wc = win.delegate() if win and isinstance(win.delegate(), BrowserWindowController) else None

                if key == 53:  # Escape
                    if shift and not cmd and not opt:
                        self.process_manager.show()
                        return None
                    if opt and wc:
                        wc.toggle_tab_bar()
                        return None
                    if wc:
                        wc.handle_escape_key()
                        return None

                # Non-menu specific shortcuts
                if cmd and shift and not opt:
                    if key == 0 or chars == 'a':
                        self.quick_switcher.show()
                        return None

                return event
            Cocoa.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(Cocoa.NSEventMaskKeyDown, key_monitor)

            def background_filter_check():
                if not os.path.exists(ADBLOCK_CACHE_FILE) or (time.time() - os.path.getmtime(ADBLOCK_CACHE_FILE) > 604800):
                    update_adblock_filters_from_web()
            threading.Thread(target=background_filter_check, daemon=True).start()

        except Exception:
            traceback.print_exc()

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
        if not flag:
            self.onNewWindow_(None)
            return True
        return True

    def onNewWindow_(self, sender):
        wc = BrowserWindowController.alloc().initWithIncognito_(False)
        self.controllers.append(wc)

    def onNewIncognitoWindow_(self, sender):
        wc = BrowserWindowController.alloc().initWithIncognito_(True)
        self.controllers.append(wc)

    def onNewTerminal_(self, sender):
        win = Cocoa.NSApp.keyWindow()
        wc = win.delegate() if win and isinstance(win.delegate(), BrowserWindowController) else (self.controllers[0] if self.controllers else None)
        if wc:
            wc.add_tab("browser://terminal")

    def onCloseTab_(self, sender=None):
        win = Cocoa.NSApp.keyWindow()
        wc = win.delegate() if win and isinstance(win.delegate(), BrowserWindowController) else None
        if wc and 0 <= wc.active_tab_index < len(wc.tabs):
            wc.close_tab(wc.active_tab_index)

    def onReopenTab_(self, sender=None):
        win = Cocoa.NSApp.keyWindow()
        wc = win.delegate() if win and isinstance(win.delegate(), BrowserWindowController) else None
        if wc:
            wc.reopen_last_closed_tab()

    def showTaskManager_(self, sender):
        self.process_manager.show()

    def quickSwitcher_(self, sender):
        self.quick_switcher.show()

    def setup_menu(self):
        main_menu = Cocoa.NSMenu.alloc().init()
        app_item = Cocoa.NSMenuItem.alloc().init()
        app_menu = Cocoa.NSMenu.alloc().init()
        app_menu.addItemWithTitle_action_keyEquivalent_("Quit PyBrowser", "terminate:", "q")
        app_item.setSubmenu_(app_menu)
        main_menu.addItem_(app_item)

        file_item = Cocoa.NSMenuItem.alloc().init()
        file_menu = Cocoa.NSMenu.alloc().initWithTitle_("File")
        file_menu.addItemWithTitle_action_keyEquivalent_("New Tab", "onNewTab:", "t")
        file_menu.addItemWithTitle_action_keyEquivalent_("New Terminal Tab", "onNewTerminal:", "T").setTarget_(self)
        file_menu.addItemWithTitle_action_keyEquivalent_("New Window", "onNewWindow:", "n").setTarget_(self)
        inc_item = file_menu.addItemWithTitle_action_keyEquivalent_("New Incognito Window", "onNewIncognitoWindow:", "N")
        inc_item.setKeyEquivalentModifierMask_(Cocoa.NSEventModifierFlagCommand | Cocoa.NSEventModifierFlagShift)
        inc_item.setTarget_(self)
        file_menu.addItem_(Cocoa.NSMenuItem.separatorItem())
        file_menu.addItemWithTitle_action_keyEquivalent_("Close Tab", "onCloseTab:", "w").setTarget_(self)
        reopen_item = file_menu.addItemWithTitle_action_keyEquivalent_("Reopen Closed Tab", "onReopenTab:", "T")
        reopen_item.setKeyEquivalentModifierMask_(Cocoa.NSEventModifierFlagCommand | Cocoa.NSEventModifierFlagShift)
        reopen_item.setTarget_(self)
        file_item.setSubmenu_(file_menu)
        main_menu.addItem_(file_item)

        edit_item = Cocoa.NSMenuItem.alloc().init()
        edit_menu = Cocoa.NSMenu.alloc().initWithTitle_("Edit")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Undo", "undo:", "z")
        redo_item = edit_menu.addItemWithTitle_action_keyEquivalent_("Redo", "redo:", "Z")
        redo_item.setKeyEquivalentModifierMask_(Cocoa.NSEventModifierFlagCommand | Cocoa.NSEventModifierFlagShift)
        edit_menu.addItem_(Cocoa.NSMenuItem.separatorItem())
        edit_menu.addItemWithTitle_action_keyEquivalent_("Cut", "cut:", "x")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Copy", "copy:", "c")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Paste", "paste:", "v")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Select All", "selectAll:", "a")
        edit_menu.addItem_(Cocoa.NSMenuItem.separatorItem())
        edit_menu.addItemWithTitle_action_keyEquivalent_("Find in Page", "onFindInPage:", "f")
        edit_item.setSubmenu_(edit_menu)
        main_menu.addItem_(edit_item)

        view_item = Cocoa.NSMenuItem.alloc().init()
        view_menu = Cocoa.NSMenu.alloc().initWithTitle_("View")
        view_menu.addItemWithTitle_action_keyEquivalent_("Reload Page", "onReload:", "r")
        view_menu.addItemWithTitle_action_keyEquivalent_("Focus Omnibox", "onFocusOmnibox:", "l")
        view_menu.addItemWithTitle_action_keyEquivalent_("Picture-in-Picture", "onTogglePiP:", "p")
        view_menu.addItem_(Cocoa.NSMenuItem.separatorItem())
        view_menu.addItemWithTitle_action_keyEquivalent_("Zoom In", "onZoomIn:", "+")
        view_menu.addItemWithTitle_action_keyEquivalent_("Zoom Out", "onZoomOut:", "-")
        view_menu.addItemWithTitle_action_keyEquivalent_("Actual Size", "onResetZoom:", "0")
        view_menu.addItem_(Cocoa.NSMenuItem.separatorItem())
        inspect_item = view_menu.addItemWithTitle_action_keyEquivalent_("Inspect Element", "onInspectElement:", "i")
        inspect_item.setKeyEquivalentModifierMask_(Cocoa.NSEventModifierFlagCommand | Cocoa.NSEventModifierFlagOption)
        view_item.setSubmenu_(view_menu)
        main_menu.addItem_(view_item)

        hist_item = Cocoa.NSMenuItem.alloc().init()
        hist_menu = Cocoa.NSMenu.alloc().initWithTitle_("History")
        hist_menu.addItemWithTitle_action_keyEquivalent_("Show Full History", "openHistory:", "y")
        hist_menu.addItem_(Cocoa.NSMenuItem.separatorItem())
        hist_menu.addItemWithTitle_action_keyEquivalent_("Back", "onGoBack:", "[")
        hist_menu.addItemWithTitle_action_keyEquivalent_("Forward", "onGoForward:", "]")
        hist_item.setSubmenu_(hist_menu)
        main_menu.addItem_(hist_item)

        win_item = Cocoa.NSMenuItem.alloc().init()
        win_menu = Cocoa.NSMenu.alloc().initWithTitle_("Window")
        for i in range(1, 10):
            win_menu.addItemWithTitle_action_keyEquivalent_(f"Tab {i}", f"onSwitchTab{i}:", str(i))
        win_menu.addItem_(Cocoa.NSMenuItem.separatorItem())
        win_menu.addItemWithTitle_action_keyEquivalent_("Quick Switcher", "quickSwitcher:", "k")
        win_menu.addItemWithTitle_action_keyEquivalent_("Task Manager", "showTaskManager:", "").setTarget_(self)
        win_item.setSubmenu_(win_menu)
        main_menu.addItem_(win_item)

        Cocoa.NSApp.setMainMenu_(main_menu)
