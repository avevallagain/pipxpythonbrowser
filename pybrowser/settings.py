"""
PyBrowser Settings & Configuration Module.
Handles settings storage, directory resolution, and the Material 3 Settings Page.
"""

import os
import json
import time
import urllib.parse
import re
import html
import tempfile
import shutil

UA_PRESETS = {
    "safari_18": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "chrome_mac": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "chrome_win": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "firefox_mac": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
}
DEFAULT_URL = "https://www.google.com"

DATA_DIR = os.path.expanduser("~/data")
os.makedirs(DATA_DIR, exist_ok=True)

_cur_dir = os.path.dirname(os.path.abspath(__file__))
_old_candidates = [
    os.path.abspath(os.path.join(_cur_dir, "..", "browser_data")),
    os.path.abspath(os.path.join(os.getcwd(), "browser_data")),
]
for _old_dir in _old_candidates:
    if os.path.exists(_old_dir):
        for _fname in ["settings.json", "bookmarks.json", "session.json", "history.db", "adblock_hosts.txt", "dns_cache.json"]:
            _src = os.path.join(_old_dir, _fname)
            _dst = os.path.join(DATA_DIR, _fname)
            if os.path.exists(_src) and not os.path.exists(_dst):
                try:
                    shutil.copy2(_src, _dst)
                except Exception:
                    pass
        break

SESSION_FILE       = os.path.join(DATA_DIR, "session.json")
BOOKMARKS_FILE     = os.path.join(DATA_DIR, "bookmarks.json")
SETTINGS_FILE      = os.path.join(DATA_DIR, "settings.json")
HISTORY_FILE       = os.path.join(DATA_DIR, "history.db")
ADBLOCK_CACHE_FILE = os.path.join(DATA_DIR, "adblock_hosts.txt")
DNS_CACHE_FILE     = os.path.join(DATA_DIR, "dns_cache.json")
USERSCRIPTS_DIR    = os.path.join(DATA_DIR, "userscripts")
os.makedirs(USERSCRIPTS_DIR, exist_ok=True)

FILTER_LIST_URL = "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts"
LOCAL_PROXY_PORT = 8888

BASE_ADBLOCK_DOMAINS = {
    "doubleclick.net", "googleadservices.com", "googlesyndication.com",
    "adsystem.com", "tracker.com", "analytics.google.com", "scorecardresearch.com",
    "quantserve.com", "zedo.com", "adbrite.com", "amazon-adsystem.com", "criteo.com",
    "taboola.com", "outbrain.com", "adroll.com", "adnxs.com", "rubiconproject.com",
    "pubmatic.com", "openx.net", "casalemedia.com", "serving-sys.com", "moatads.com"
}

M3_COLORS = {
    "tabstrip-bg": ("#DFE3E8", "#1F1F1F"),
    "tabstrip-incognito": ("#141416", "#141416"),
    "active-tab-bg": ("#FFFFFF", "#2D2F31"),
    "inactive-tab-hover": ("rgba(0,0,0,0.08)", "rgba(255,255,255,0.08)"),
    "toolbar-bg": ("#FFFFFF", "#282A2C"),
    "toolbar-incognito": ("#1F1F21", "#1F1F21"),
    "toolbar-separator": ("rgba(0,0,0,0.08)", "rgba(255,255,255,0.06)"),
    "omnibox-bg-rest": ("#EDF2F7", "#1E1F22"),
    "omnibox-bg-focus": ("#FFFFFF", "#1E1F22"),
    "omnibox-text-host": ("#1F1F1F", "#E3E3E3"),
    "omnibox-text-path": ("#757575", "#8E918F"),
    "tab-divider": ("rgba(0,0,0,0.15)", "rgba(255,255,255,0.15)"),
}

GROUP_COLORS = {
    "Grey": "#5F6368", "Blue": "#1A73E8", "Red": "#D93025", "Yellow": "#F9AB00",
    "Green": "#1E8E3E", "Pink": "#E52592", "Purple": "#9334E6", "Cyan": "#12B5CB"
}

def validate_and_get_download_dir(target_path=None):
    if not target_path:
        target_path = SETTINGS.settings.get("download_path", os.path.expanduser("~/Downloads"))
    expanded = os.path.expanduser(str(target_path).strip())
    if expanded.startswith("/Volumes/") and not os.path.exists(expanded):
        print(f"[PyBrowser] Warning: External volume at '{expanded}' is unmounted. Falling back to ~/Downloads.")
        return os.path.expanduser("~/Downloads")
    try:
        os.makedirs(expanded, exist_ok=True)
        if os.access(expanded, os.W_OK):
            return expanded
    except Exception as e:
        print(f"[PyBrowser] Warning: Cannot write to '{expanded}': {e}. Falling back to ~/Downloads.")
    return os.path.expanduser("~/Downloads")

def sanitize_suggested_filename(img_url, default="image.png"):
    try:
        parsed = urllib.parse.urlparse(img_url)
        p_name = os.path.basename(parsed.path)
        if p_name and "." in p_name:
            filename = p_name.split("?")[0]
        else:
            filename = f"image_{int(time.time())}.png"
        return filename
    except Exception:
        return default

class SettingsManager:
    def __init__(self):
        self.defaults = {
            "search_engine_preset": "google",
            "custom_search_url": "https://www.google.com/search?q=%s",
            "search_in_new_tab": False,
            "omnibox_suggestions_enabled": True,
            "startup_behavior": "new_tab",
            "startup_url": "https://www.google.com",
            "startup_window_mode": "normal",
            "downloads_enabled": True,
            "download_path": os.path.expanduser("~/Downloads"),
            "js_enabled": True,
            "adblock_enabled": True,
            "adblock_stevenblack_enabled": True,
            "adblock_base_enabled": True,
            "cosmetic_adblock_enabled": True,
            "yt_adblock_enabled": True,
            "bookmarks_bar_visible": True,
            "vertical_tabs_enabled": False,
            "vertical_tabs_collapsed": False,
            "show_home_button": False,
            "home_url": "https://www.google.com",
            "default_zoom": 0.8,
            "doh_provider": "cloudflare",
            "doh_custom_url": "",
            "custom_adblock_domains": "",
            "adblock_last_updated": "Built-in Lists",
            "theme_mode": "system",
            "terminal_theme": "luna",
            "https_only": True,
            "block_popups": True,
            "webgl_enabled": True,
            "user_agent_mode": "safari_18",
            "custom_user_agent": "",
            "tab_bar_double_click_action": "maximize",
            "saved_logins": {},
            "history_enabled": True
        }
        self.settings = dict(self.defaults)
        self.load()

    def load(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    saved_data = json.load(f)
                    if isinstance(saved_data, dict):
                        self.settings.update(saved_data)
            except Exception as e:
                print(f"[Settings Load Warning] {e}")
        try:
            z = float(self.settings.get("default_zoom", 0.8))
            self.settings["default_zoom"] = max(0.5, min(2.0, z))
        except Exception:
            self.settings["default_zoom"] = 0.8
        self.save()

    def save(self):
        try:
            dir_name = os.path.dirname(SETTINGS_FILE) or "."
            fd, temp_path = tempfile.mkstemp(dir=dir_name)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=2)
            os.replace(temp_path, SETTINGS_FILE)
        except Exception as e:
            print(f"[Settings Save Error] {e}")

    def reset_defaults(self):
        self.settings = dict(self.defaults)
        self.save()

    def get_search_url(self, query):
        preset = self.settings.get("search_engine_preset", "google")
        encoded = urllib.parse.quote(query)
        if preset == "custom":
            c = self.settings.get("custom_search_url", "").strip()
            if c:
                return c.replace("%s", encoded) if "%s" in c else (c + encoded)
        presets = {
            "google": "https://www.google.com/search?q=",
            "duckduckgo": "https://duckduckgo.com/?q=",
            "bing": "https://www.bing.com/search?q=",
            "brave": "https://search.brave.com/search?q=",
            "ecosia": "https://www.ecosia.org/search?q=",
            "kagi": "https://kagi.com/search?q="
        }
        return presets.get(preset, "https://www.google.com/search?q=") + encoded

    def get_custom_adblock_domains(self):
        domains = set()
        custom = self.settings.get("custom_adblock_domains", "")
        if custom:
            for d in re.split(r'[\r\n,]+', custom):
                d = d.strip().lower()
                if d:
                    domains.add(d)
        return domains

    def get_user_agent(self):
        mode = self.settings.get("user_agent_mode", "safari_18")
        if mode == "custom":
            return self.settings.get("custom_user_agent", "").strip() or UA_PRESETS["safari_18"]
        return UA_PRESETS.get(mode, UA_PRESETS["safari_18"])

SETTINGS = SettingsManager()

def get_settings_html(sb_count, base_count, custom_count, active_rules_count):
    s = SETTINGS.settings
    engine_preset = s.get("search_engine_preset", "google")
    custom_search_url = s.get("custom_search_url", "https://www.google.com/search?q=%s")
    doh_provider = s.get("doh_provider", "cloudflare")
    doh_custom_url = s.get("doh_custom_url", "")
    startup_behavior = s.get("startup_behavior", "new_tab")
    startup_window_mode = s.get("startup_window_mode", "normal")
    theme_mode = s.get("theme_mode", "system")
    terminal_theme = s.get("terminal_theme", "luna")
    ua_mode = s.get("user_agent_mode", "safari_18")
    tab_action = s.get("tab_bar_double_click_action", "maximize")
    downloads_enabled = s.get("downloads_enabled", True)
    cur_dl_path = s.get("download_path", os.path.expanduser("~/Downloads"))
    cur_zoom = str(s.get("default_zoom", 1.0))

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Settings</title>
<style>
  :root {{ --bg:#F8F9FA; --surface:#FFFFFF; --sidebar-bg:#F8F9FA; --text:#1F1F1F; --text-sec:#5E6368; --border:#E0E2E0; --border-subtle:#F1F3F4; --primary:#1A73E8; --primary-hover:#1557B0; --primary-container:#D3E3FD; --on-primary-container:#041E49; --card-shadow:0 1px 3px rgba(0,0,0,0.05); --input-bg:#FFFFFF; --track-off:#C4C7C5; --thumb-off:#747775; }}
  @media (prefers-color-scheme:dark) {{ :root {{ --bg:#1A1A1C; --surface:#282A2C; --sidebar-bg:#1A1A1C; --text:#E3E3E3; --text-sec:#9AA0A6; --border:rgba(255,255,255,0.08); --border-subtle:rgba(255,255,255,0.04); --primary:#8AB4F8; --primary-hover:#A8C7FA; --primary-container:#004A77; --on-primary-container:#D3E3FD; --card-shadow:0 1px 4px rgba(0,0,0,0.3); --input-bg:#1F1F21; --track-off:#444746; --thumb-off:#8E918F; }} }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:-apple-system,sans-serif; background:var(--bg); color:var(--text); display:flex; height:100vh; overflow:hidden; user-select:none; }}
  #sidebar {{ width:270px; min-width:270px; background:var(--sidebar-bg); border-right:1px solid var(--border); padding:24px 16px; display:flex; flex-direction:column; overflow-y:auto; }}
  .app-title {{ font-size:19px; font-weight:600; padding:0 14px 20px 14px; display:flex; align-items:center; gap:10px; color:var(--text); border-bottom:1px solid var(--border); margin-bottom:14px; }}
  .nav-item {{ display:flex; align-items:center; gap:12px; padding:10px 16px; font-size:13.5px; font-weight:500; border-radius:20px; cursor:pointer; color:var(--text-sec); transition:background 0.15s, color 0.15s; margin-bottom:4px; }}
  .nav-item:hover {{ background:rgba(0,0,0,0.04); color:var(--text); }}
  .nav-item.active {{ background:var(--primary-container); color:var(--on-primary-container); font-weight:600; }}
  #content {{ flex:1; overflow-y:auto; padding:36px 48px; scroll-behavior:smooth; }}
  .container {{ max-width:820px; margin:0 auto; }}
  .section {{ margin-bottom:34px; }}
  .section-title {{ font-size:16px; font-weight:600; margin-bottom:14px; color:var(--text); display:flex; align-items:center; gap:8px; }}
  .card {{ background:var(--surface); border-radius:16px; border:1px solid var(--border); box-shadow:var(--card-shadow); }}
  .row {{ display:flex; justify-content:space-between; align-items:center; padding:14px 20px; border-bottom:1px solid var(--border-subtle); }}
  .row:last-child {{ border-bottom:none; }}
  .row-col {{ display:flex; flex-direction:column; padding:16px 20px; border-bottom:1px solid var(--border-subtle); gap:10px; }}
  .label-group {{ display:flex; flex-direction:column; gap:3px; max-width:65%; }}
  .label-title {{ font-size:13.5px; font-weight:500; }}
  .label-desc {{ font-size:12px; color:var(--text-sec); line-height:1.4; }}
  .switch {{ position:relative; display:inline-block; width:44px; height:24px; }}
  .switch input {{ opacity:0; width:0; height:0; }}
  .slider {{ position:absolute; cursor:pointer; inset:0; background-color:var(--track-off); transition:0.2s; border-radius:24px; }}
  .slider:before {{ position:absolute; content:""; height:16px; width:16px; left:4px; bottom:4px; background-color:var(--thumb-off); transition:0.2s; border-radius:50%; }}
  input:checked + .slider {{ background-color:var(--primary); }}
  input:checked + .slider:before {{ transform:translateX(20px); background-color:#FFFFFF; }}
  .m3-select, .m3-input {{ background:var(--input-bg); border:1px solid var(--border); border-radius:8px; padding:8px 12px; font-size:13px; color:var(--text); outline:none; }}
  .btn {{ background:var(--primary); color:#FFF; border:none; border-radius:18px; padding:8px 18px; font-size:13px; font-weight:500; cursor:pointer; }}
  .btn:hover {{ background:var(--primary-hover); }}
  .btn-danger {{ background:#D93025; color:#FFF; }}
  #toast {{ position:fixed; bottom:24px; right:24px; background:#323232; color:#FFF; padding:12px 22px; border-radius:10px; font-size:13px; opacity:0; transition:opacity 0.25s, transform 0.25s; pointer-events:none; transform:translateY(10px); z-index:9999; }}
  #toast.show {{ opacity:1; transform:translateY(0); }}
  .badge {{ display:inline-block; padding:3px 8px; font-size:11px; font-weight:600; border-radius:10px; background:var(--primary-container); color:var(--on-primary-container); }}
  .check-row {{ display:flex; align-items:center; gap:10px; margin-bottom:8px; font-size:13px; }}
  .rule-inspector-box {{ background:var(--bg); border:1px solid var(--border); border-radius:10px; padding:14px; margin-top:10px; font-size:12px; max-height:180px; overflow-y:auto; font-family:monospace; }}
</style></head><body>
<div id="sidebar">
  <div class="app-title">⚙️ Settings</div>
  <div class="nav-item active" onclick="jumpTo('sec-search', this)">🔍 Search & Omnibox</div>
  <div class="nav-item" onclick="jumpTo('sec-appearance', this)">🎨 Appearance & Theme</div>
  <div class="nav-item" onclick="jumpTo('sec-privacy', this)">🛡️ uBlock & Privacy</div>
  <div class="nav-item" onclick="jumpTo('sec-general', this)">⚡ General & Startup</div>
  <div class="nav-item" onclick="jumpTo('sec-downloads', this)">⬇️ Downloads & Drives</div>
  <div class="nav-item" onclick="jumpTo('sec-network', this)">🌐 Network & Advanced</div>
  <div class="nav-item" onclick="jumpTo('sec-clear', this)">🧹 Clear Browsing Data</div>
  <div class="nav-item" onclick="jumpTo('sec-reset', this)">🔄 Reset Defaults</div>
</div>
<div id="content"><div class="container">

  <div id="sec-search" class="section">
    <div class="section-title">🔍 Search & Omnibox</div>
    <div class="card">
      <div class="row">
        <div class="label-group"><div class="label-title">Default Search Provider</div><div class="label-desc">Used automatically when non-URL keywords are typed</div></div>
        <select class="m3-select" onchange="onSearchPresetChange(this.value)">
          <option value="google" {'selected' if engine_preset == 'google' else ''}>Google</option>
          <option value="duckduckgo" {'selected' if engine_preset == 'duckduckgo' else ''}>DuckDuckGo</option>
          <option value="bing" {'selected' if engine_preset == 'bing' else ''}>Bing</option>
          <option value="brave" {'selected' if engine_preset == 'brave' else ''}>Brave</option>
          <option value="ecosia" {'selected' if engine_preset == 'ecosia' else ''}>Ecosia</option>
          <option value="kagi" {'selected' if engine_preset == 'kagi' else ''}>Kagi</option>
          <option value="custom" {'selected' if engine_preset == 'custom' else ''}>Custom Template</option>
        </select>
      </div>
      <div id="custom-search-row" class="row" style="display: {'flex' if engine_preset == 'custom' else 'none'};">
        <div class="label-group"><div class="label-title">Custom Search URL Template</div><div class="label-desc">Use <code>%s</code> as query placeholder</div></div>
        <input type="text" class="m3-input" style="width:280px;" value="{html.escape(custom_search_url)}" onchange="saveSetting('custom_search_url', this.value, 'Custom search URL saved')">
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">Search in New Tab</div><div class="label-desc">Open Omnibox search results in a new tab</div></div>
        <label class="switch">
          <input type="checkbox" {'checked' if s.get('search_in_new_tab', False) else ''} onchange="saveSetting('search_in_new_tab', this.checked, 'Search target updated')">
          <span class="slider"></span>
        </label>
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">Omnibox Suggestions</div><div class="label-desc">Display search & history autocomplete dropdown</div></div>
        <label class="switch">
          <input type="checkbox" {'checked' if s.get('omnibox_suggestions_enabled', True) else ''} onchange="saveSetting('omnibox_suggestions_enabled', this.checked, 'Suggestions toggled')">
          <span class="slider"></span>
        </label>
      </div>
    </div>
  </div>

  <div id="sec-appearance" class="section">
    <div class="section-title">🎨 Appearance & Window</div>
    <div class="card">
      <div class="row">
        <div class="label-group"><div class="label-title">Theme Mode</div><div class="label-desc">Color palette for browser controls & UI</div></div>
        <select class="m3-select" onchange="saveSetting('theme_mode', this.value, 'Theme updated')">
          <option value="system" {'selected' if theme_mode == 'system' else ''}>System Default</option>
          <option value="light" {'selected' if theme_mode == 'light' else ''}>Light</option>
          <option value="dark" {'selected' if theme_mode == 'dark' else ''}>Dark</option>
        </select>
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">Default Zoom Level</div><div class="label-desc">Default page scaling factor for new web views</div></div>
        <select class="m3-select" onchange="saveSetting('default_zoom', parseFloat(this.value), 'Zoom level updated')">
          <option value="0.75" {'selected' if cur_zoom == '0.75' else ''}>75%</option>
          <option value="0.8" {'selected' if cur_zoom in ('0.8', '0.80') else ''}>80%</option>
          <option value="1.0" {'selected' if cur_zoom in ('1', '1.0', '1.00') else ''}>100% (Default)</option>
          <option value="1.25" {'selected' if cur_zoom == '1.25' else ''}>125%</option>
          <option value="1.5" {'selected' if cur_zoom == '1.5' else ''}>150%</option>
        </select>
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">Vertical Tabs</div><div class="label-desc">Display tabs in a collapsible sidebar on the left</div></div>
        <label class="switch">
          <input type="checkbox" {'checked' if s.get('vertical_tabs_enabled', False) else ''} onchange="saveSetting('vertical_tabs_enabled', this.checked, 'Vertical tabs toggled')">
          <span class="slider"></span>
        </label>
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">Tab Bar Double-Click</div><div class="label-desc">Action when empty tab bar area is double-clicked</div></div>
        <select class="m3-select" onchange="saveSetting('tab_bar_double_click_action', this.value, 'Tab action saved')">
          <option value="maximize" {'selected' if tab_action == 'maximize' else ''}>Zoom / Maximize</option>
          <option value="fullscreen" {'selected' if tab_action == 'fullscreen' else ''}>Toggle Fullscreen</option>
        </select>
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">Show Home Button</div><div class="label-desc">Display toolbar button pointing to home URL</div></div>
        <label class="switch">
          <input type="checkbox" {'checked' if s.get('show_home_button', False) else ''} onchange="saveSetting('show_home_button', this.checked, 'Home button toggled')">
          <span class="slider"></span>
        </label>
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">Home Page URL</div><div class="label-desc">Webpage loaded on clicking home button</div></div>
        <input type="text" class="m3-input" style="width:260px;" value="{html.escape(s.get('home_url', 'https://www.google.com'))}" onchange="saveSetting('home_url', this.value, 'Home URL saved')">
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">Terminal Theme</div><div class="label-desc">Color palette for browser://terminal</div></div>
        <select class="m3-select" onchange="saveSetting('terminal_theme', this.value, 'Terminal theme updated')">
          <option value="luna" {'selected' if terminal_theme == 'luna' else ''}>Luna (Moon & Lavender, Default)</option>
          <option value="kitty" {'selected' if terminal_theme == 'kitty' else ''}>Kitty (Pure Black High Contrast)</option>
          <option value="normal" {'selected' if terminal_theme == 'normal' else ''}>Standard Gray</option>
        </select>
      </div>
    </div>
  </div>

  <div id="sec-privacy" class="section">
    <div class="section-title">🛡️ uBlock-Grade Privacy & Adblock <span class="badge" id="adblock-count-badge">{active_rules_count:,} rules</span></div>
    <div class="card">
      <div class="row">
        <div class="label-group"><div class="label-title">Master Adblock</div><div class="label-desc">Network rule lists and element blocker</div></div>
        <label class="switch">
          <input type="checkbox" {'checked' if s.get('adblock_enabled', True) else ''} onchange="saveSetting('adblock_enabled', this.checked, 'Adblock toggled')">
          <span class="slider"></span>
        </label>
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">StevenBlack Unified Hosts ({sb_count:,} domains)</div><div class="label-desc">Community tracker and malware blocklists</div></div>
        <label class="switch">
          <input type="checkbox" {'checked' if s.get('adblock_stevenblack_enabled', True) else ''} onchange="saveSetting('adblock_stevenblack_enabled', this.checked, 'StevenBlack toggled')">
          <span class="slider"></span>
        </label>
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">Cosmetic Element Blocker</div><div class="label-desc">Hides sponsored banners and ad containers</div></div>
        <label class="switch">
          <input type="checkbox" {'checked' if s.get('cosmetic_adblock_enabled', True) else ''} onchange="saveSetting('cosmetic_adblock_enabled', this.checked, 'Cosmetic adblock toggled')">
          <span class="slider"></span>
        </label>
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">YouTube Ad Shield</div><div class="label-desc">Auto-skips YouTube video advertisements & banners</div></div>
        <label class="switch">
          <input type="checkbox" {'checked' if s.get('yt_adblock_enabled', True) else ''} onchange="saveSetting('yt_adblock_enabled', this.checked, 'YouTube shield toggled')">
          <span class="slider"></span>
        </label>
      </div>
      <div class="row-col">
        <div class="label-group" style="max-width: 100%;">
          <div class="label-title">Rule & Domain Inspector</div>
          <div class="label-desc">Inspect whether any domain is currently blocked and identify its rule list</div>
        </div>
        <div style="display:flex; gap:10px;">
          <input type="text" id="inspector-query" class="m3-input" style="flex:1;" placeholder="Search blocked domain (e.g. doubleclick, tracker, analytics)..." onkeydown="if(event.key==='Enter') searchDomain()">
          <button class="btn" onclick="searchDomain()">Inspect Domain</button>
        </div>
        <div id="inspector-results" class="rule-inspector-box" style="display:none;"></div>
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">Record Browsing History</div><div class="label-desc">Save visited pages to local SQLite database</div></div>
        <label class="switch">
          <input type="checkbox" {'checked' if s.get('history_enabled', True) else ''} onchange="saveSetting('history_enabled', this.checked, 'History recording toggled')">
          <span class="slider"></span>
        </label>
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">DNS over HTTPS (DoH) Provider</div><div class="label-desc">Encrypted DNS resolution bypassing ISP inspection</div></div>
        <select class="m3-select" onchange="onDoHChange(this.value)">
          <option value="cloudflare" {'selected' if doh_provider == 'cloudflare' else ''}>Cloudflare (1.1.1.1)</option>
          <option value="cloudflare_security" {'selected' if doh_provider == 'cloudflare_security' else ''}>Cloudflare Security (Malware Blocking)</option>
          <option value="google" {'selected' if doh_provider == 'google' else ''}>Google (8.8.8.8)</option>
          <option value="quad9" {'selected' if doh_provider == 'quad9' else ''}>Quad9 (9.9.9.9)</option>
          <option value="adguard" {'selected' if doh_provider == 'adguard' else ''}>AdGuard DNS</option>
          <option value="custom" {'selected' if doh_provider == 'custom' else ''}>Custom Provider</option>
        </select>
      </div>
      <div id="custom-doh-row" class="row" style="display: {'flex' if doh_provider == 'custom' else 'none'};">
        <div class="label-group"><div class="label-title">Custom DoH URL</div><div class="label-desc">Must support DNS-over-HTTPS JSON API</div></div>
        <input type="text" class="m3-input" style="width:280px;" value="{html.escape(doh_custom_url)}" placeholder="https://..." onchange="saveSetting('doh_custom_url', this.value, 'Custom DoH URL saved')">
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">HTTPS-Only Mode</div><div class="label-desc">Upgrades unencrypted HTTP requests to secure HTTPS</div></div>
        <label class="switch">
          <input type="checkbox" {'checked' if s.get('https_only', True) else ''} onchange="saveSetting('https_only', this.checked, 'HTTPS-only toggled')">
          <span class="slider"></span>
        </label>
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">Update Blocklists</div><div class="label-desc">Last updated: <span id="adblock-update-date">{s.get('adblock_last_updated', 'Built-in')}</span></div></div>
        <button class="btn" id="update-filters-btn" onclick="updateAdblockFilters()">Update Lists Now</button>
      </div>
      <div class="row-col">
        <div class="label-group" style="max-width: 100%;">
          <div class="label-title">Custom Blocked Domains</div>
          <div class="label-desc">Add additional domains to block (comma- or line-separated)</div>
        </div>
        <textarea class="m3-input" style="width:100%; height:75px; resize:vertical;" placeholder="adserver.com&#10;tracker.telemetry.io" onchange="saveSetting('custom_adblock_domains', this.value, 'Custom domains saved')">{html.escape(s.get('custom_adblock_domains', ''))}</textarea>
      </div>
    </div>
  </div>

  <div id="sec-general" class="section">
    <div class="section-title">⚡ General & Startup</div>
    <div class="card">
      <div class="row">
        <div class="label-group"><div class="label-title">Startup Behavior</div><div class="label-desc">What pages to open upon browser launch</div></div>
        <select class="m3-select" onchange="onStartupChange(this.value)">
          <option value="new_tab" {'selected' if startup_behavior == 'new_tab' else ''}>Open New Tab</option>
          <option value="restore" {'selected' if startup_behavior == 'restore' else ''}>Restore Previous Session</option>
          <option value="custom_url" {'selected' if startup_behavior == 'custom_url' else ''}>Open Specific URL</option>
        </select>
      </div>
      <div id="custom-startup-row" class="row" style="display: {'flex' if startup_behavior == 'custom_url' else 'none'};">
        <div class="label-group"><div class="label-title">Startup URL</div><div class="label-desc">Webpage loaded on initial application launch</div></div>
        <input type="text" class="m3-input" style="width:280px;" value="{html.escape(s.get('startup_url', 'https://www.google.com'))}" onchange="saveSetting('startup_url', this.value, 'Startup URL saved')">
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">Window State on Launch</div><div class="label-desc">Window geometry upon application launch</div></div>
        <select class="m3-select" onchange="saveSetting('startup_window_mode', this.value, 'Window launch mode saved')">
          <option value="normal" {'selected' if startup_window_mode == 'normal' else ''}>Standard Window</option>
          <option value="maximized" {'selected' if startup_window_mode == 'maximized' else ''}>Maximized</option>
          <option value="fullscreen" {'selected' if startup_window_mode == 'fullscreen' else ''}>Full Screen</option>
        </select>
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">Enable JavaScript</div><div class="label-desc">Allow web pages to execute client-side scripts</div></div>
        <label class="switch">
          <input type="checkbox" {'checked' if s.get('js_enabled', True) else ''} onchange="saveSetting('js_enabled', this.checked, 'JavaScript setting updated')">
          <span class="slider"></span>
        </label>
      </div>
    </div>
  </div>

  <div id="sec-downloads" class="section">
    <div class="section-title">⬇️ Downloads & Storage Devices</div>
    <div class="card">
      <div class="row">
        <div class="label-group"><div class="label-title">Allow File Downloads</div><div class="label-desc">Permit websites and images to trigger downloads</div></div>
        <label class="switch">
          <input type="checkbox" {'checked' if downloads_enabled else ''} onchange="saveSetting('downloads_enabled', this.checked, 'Downloads toggled')">
          <span class="slider"></span>
        </label>
      </div>
      <div class="row">
        <div class="label-group">
          <div class="label-title">Download Location</div>
          <div class="label-desc">Directory or mounted external drive (/Volumes/...) for saved files</div>
          <div id="dl-path-display" style="font-size:11.5px; color:var(--primary); font-family:monospace; margin-top:4px;">{html.escape(cur_dl_path)}</div>
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <input type="text" id="dl-path-input" class="m3-input" style="width:240px;" value="{html.escape(cur_dl_path)}"
            onchange="saveSetting('download_path', this.value, 'Download path saved')"
            onkeydown="if(event.key==='Enter') saveSetting('download_path', this.value, 'Download path saved')">
          <button class="btn" onclick="browseDownloadFolder()">Browse…</button>
        </div>
      </div>
    </div>
  </div>

  <div id="sec-network" class="section">
    <div class="section-title">🌐 Network & Advanced</div>
    <div class="card">
      <div class="row">
        <div class="label-group"><div class="label-title">Browser Identity (User-Agent)</div><div class="label-desc">Emulate different browsers for compatibility</div></div>
        <select class="m3-select" onchange="onUAModeChange(this.value)">
          <option value="safari_18" {'selected' if ua_mode == 'safari_18' else ''}>Safari 18.0 Desktop (macOS Default)</option>
          <option value="chrome_mac" {'selected' if ua_mode == 'chrome_mac' else ''}>Google Chrome (macOS)</option>
          <option value="chrome_win" {'selected' if ua_mode == 'chrome_win' else ''}>Google Chrome (Windows)</option>
          <option value="firefox_mac" {'selected' if ua_mode == 'firefox_mac' else ''}>Firefox (macOS)</option>
          <option value="custom" {'selected' if ua_mode == 'custom' else ''}>Custom String</option>
        </select>
      </div>
      <div id="custom-ua-row" class="row" style="display: {'flex' if ua_mode == 'custom' else 'none'};">
        <div class="label-group"><div class="label-title">Custom User-Agent Header</div><div class="label-desc">Raw User-Agent string sent with requests</div></div>
        <input type="text" class="m3-input" style="width:280px;" value="{html.escape(s.get('custom_user_agent', ''))}" onchange="saveSetting('custom_user_agent', this.value, 'Custom User-Agent saved')">
      </div>
      <div class="row">
        <div class="label-group"><div class="label-title">WebGL Hardware Acceleration</div><div class="label-desc">Enable 3D canvas and accelerated GPU graphics</div></div>
        <label class="switch">
          <input type="checkbox" {'checked' if s.get('webgl_enabled', True) else ''} onchange="saveSetting('webgl_enabled', this.checked, 'WebGL setting updated')">
          <span class="slider"></span>
        </label>
      </div>
    </div>
  </div>

  <div id="sec-clear" class="section">
    <div class="section-title">🧹 Clear Browsing Data</div>
    <div class="card">
      <div class="row-col">
        <div class="check-row">
          <input type="checkbox" id="clr-history" checked>
          <label for="clr-history"><strong>Browsing History</strong> (Stored SQLite database visits)</label>
        </div>
        <div class="check-row">
          <input type="checkbox" id="clr-cookies" checked>
          <label for="clr-cookies"><strong>Cookies & Cache</strong> (Website storage and session cache)</label>
        </div>
        <div class="check-row">
          <input type="checkbox" id="clr-favicons" checked>
          <label for="clr-favicons"><strong>Favicon Cache</strong> (Memory site icons)</label>
        </div>
        <div class="check-row">
          <input type="checkbox" id="clr-dns" checked>
          <label for="clr-dns"><strong>Local DNS Cache</strong> (Resolved IP lookups)</label>
        </div>
        <div style="margin-top: 10px;">
          <button class="btn btn-danger" onclick="executeClearData()">Clear Selected Data Now</button>
        </div>
      </div>
    </div>
  </div>

  <div id="sec-reset" class="section">
    <div class="section-title">🔄 Reset Defaults</div>
    <div class="card">
      <div class="row">
        <div class="label-group"><div class="label-title">Restore Factory Settings</div><div class="label-desc">Revert search engine, theme, zoom, and flags to factory defaults</div></div>
        <button class="btn btn-danger" onclick="executeResetDefaults()">Reset All Settings</button>
      </div>
    </div>
  </div>

</div></div>
<div id="toast">Settings updated</div>
<script>
  function showToast(msg) {{
    const t = document.getElementById('toast');
    t.innerText = msg || 'Saved';
    t.classList.add('show');
    clearTimeout(window._toastTimer);
    window._toastTimer = setTimeout(() => t.classList.remove('show'), 2400);
  }}
  function saveSetting(key, val, msg) {{
    window.webkit.messageHandlers.bridge.postMessage({{ cmd: 'save_setting', key: key, val: val }});
    showToast(msg);
  }}
  function onSearchPresetChange(val) {{
    saveSetting('search_engine_preset', val, 'Search engine updated');
    document.getElementById('custom-search-row').style.display = (val === 'custom') ? 'flex' : 'none';
  }}
  function onDoHChange(val) {{
    saveSetting('doh_provider', val, 'DNS provider updated');
    document.getElementById('custom-doh-row').style.display = (val === 'custom') ? 'flex' : 'none';
  }}
  function onUAModeChange(val) {{
    saveSetting('user_agent_mode', val, 'User-Agent profile updated');
    document.getElementById('custom-ua-row').style.display = (val === 'custom') ? 'flex' : 'none';
  }}
  function onStartupChange(val) {{
    saveSetting('startup_behavior', val, 'Startup behavior updated');
    document.getElementById('custom-startup-row').style.display = (val === 'custom_url') ? 'flex' : 'none';
  }}
  function browseDownloadFolder() {{
    window.webkit.messageHandlers.bridge.postMessage({{ cmd: 'choose_download_directory' }});
  }}
  function updateAdblockFilters() {{
    const b = document.getElementById('update-filters-btn');
    b.innerText = "Syncing Lists..."; b.disabled = true;
    window.webkit.messageHandlers.bridge.postMessage({{ cmd: 'update_adblock_filters' }});
  }}
  function searchDomain() {{
    const q = document.getElementById('inspector-query').value.trim();
    if (!q) return;
    const box = document.getElementById('inspector-results');
    box.style.display = 'block';
    box.innerHTML = 'Searching blocklists...';
    window.webkit.messageHandlers.bridge.postMessage({{ cmd: 'search_blocked_domains', query: q }});
  }}
  window.renderBlockedResults = function(jsonStr) {{
    let items = [];
    try {{ items = JSON.parse(jsonStr); }} catch(e) {{ items = []; }}
    const box = document.getElementById('inspector-results');
    if (!items || items.length === 0) {{
      box.innerHTML = '<span style="color:#D93025;">No matches found. Domain is NOT currently blocked.</span>';
      return;
    }}
    let out = '<div style="margin-bottom:6px;font-weight:600;color:var(--primary);">' + items.length + ' Matches Found:</div>';
    items.forEach(it => {{
      out += '<div><strong>[BLOCKED]</strong> ' + it.domain + ' <span style="opacity:0.6;">(' + it.source + ')</span></div>';
    }});
    box.innerHTML = out;
  }};
  function executeClearData() {{
    const targets = [];
    if (document.getElementById('clr-history').checked) targets.push('history');
    if (document.getElementById('clr-cookies').checked) targets.push('cookies_cache');
    if (document.getElementById('clr-favicons').checked) targets.push('favicons');
    if (document.getElementById('clr-dns').checked) targets.push('dns_cache');
    if (targets.length === 0) {{ showToast('Select at least one category'); return; }}
    window.webkit.messageHandlers.bridge.postMessage({{ cmd: 'clear_browsing_data', targets: targets }});
    showToast('Browsing data cleared');
  }}
  function executeResetDefaults() {{
    if (confirm("Restore all browser settings to factory defaults?")) {{
      window.webkit.messageHandlers.bridge.postMessage({{ cmd: 'reset_defaults' }});
    }}
  }}
  function jumpTo(id, navBtn) {{
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
    navBtn.classList.add('active');
    const target = document.getElementById(id);
    if (target) target.scrollIntoView({{ behavior: 'smooth' }});
  }}
</script></body></html>"""
