"""
PyBrowser DNS-over-HTTPS (DoH) Proxy Engine & StevenBlack Adblock Service.
Provides encrypted local DNS proxying and WebKit content rule compilation.
"""

import os
import json
import time
import threading
import urllib.parse
import urllib.request
import socket
import select
import socketserver
import http.server
import ssl
import ctypes
import re

import Cocoa
import WebKit
import objc

from pybrowser.settings import (
    SETTINGS, ADBLOCK_CACHE_FILE, DNS_CACHE_FILE, FILTER_LIST_URL,
    LOCAL_PROXY_PORT, BASE_ADBLOCK_DOMAINS
)
from pybrowser.terminal import dispatch_to_main

STEVENBLACK_DOMAINS = set()
ACTIVE_BLOCK_DOMAINS = set()
ADBLOCK_LOCK = threading.Lock()
WK_RULE_LIST = None
DNS_CACHE = {}
DNS_LOCK = threading.Lock()
DNS_CACHE_DIRTY = False

def rebuild_active_domains():
    global ACTIVE_BLOCK_DOMAINS
    with ADBLOCK_LOCK:
        s = set()
        if SETTINGS.settings.get("adblock_base_enabled", True):
            s.update(BASE_ADBLOCK_DOMAINS)
        if SETTINGS.settings.get("adblock_stevenblack_enabled", True):
            s.update(STEVENBLACK_DOMAINS)
        s.update(SETTINGS.get_custom_adblock_domains())
        ACTIVE_BLOCK_DOMAINS = s

def is_domain_blocked(host):
    if not host:
        return False
    host = host.strip().lower()
    if host in ACTIVE_BLOCK_DOMAINS:
        return True
    parts = host.split(".")
    for i in range(1, len(parts) - 1):
        if ".".join(parts[i:]) in ACTIVE_BLOCK_DOMAINS:
            return True
    return False

def search_blocked_domains(query, limit=100):
    query = (query or "").strip().lower()
    results = []
    custom = SETTINGS.get_custom_adblock_domains()
    with ADBLOCK_LOCK:
        sb = list(STEVENBLACK_DOMAINS)
        base = list(BASE_ADBLOCK_DOMAINS)

    for d in sorted(custom):
        if not query or query in d:
            results.append({"domain": d, "source": "Custom List"})
            if len(results) >= limit:
                return results

    for d in sorted(base):
        if not query or query in d:
            results.append({"domain": d, "source": "Built-in High Impact"})
            if len(results) >= limit:
                return results

    for d in sb:
        if not query or query in d:
            results.append({"domain": d, "source": "StevenBlack Unified"})
            if len(results) >= limit:
                return results

    return results

def load_dns_cache():
    global DNS_CACHE
    if os.path.exists(DNS_CACHE_FILE):
        try:
            with open(DNS_CACHE_FILE, 'r', encoding='utf-8') as f:
                DNS_CACHE = json.load(f)
        except Exception:
            DNS_CACHE = {}

def flush_dns_cache_to_disk():
    global DNS_CACHE_DIRTY
    with DNS_LOCK:
        if DNS_CACHE_DIRTY:
            try:
                with open(DNS_CACHE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(DNS_CACHE, f, indent=2)
                DNS_CACHE_DIRTY = False
            except Exception:
                pass

def doh_resolve(hostname):
    global DNS_CACHE_DIRTY
    now = time.time()
    with DNS_LOCK:
        if hostname in DNS_CACHE:
            record = DNS_CACHE[hostname]
            if isinstance(record, dict) and record.get("expires", 0) > now:
                return record["ip"]

    try:
        socket.inet_aton(hostname)
        return hostname
    except socket.error:
        pass

    provider = SETTINGS.settings.get("doh_provider", "cloudflare")
    doh_configs = {
        "cloudflare": ("1.1.1.1", "cloudflare-dns.com", "/dns-query"),
        "cloudflare_security": ("1.1.1.2", "security.cloudflare-dns.com", "/dns-query"),
        "google": ("8.8.8.8", "dns.google", "/resolve"),
        "quad9": ("9.9.9.9", "dns.quad9.net", "/dns-query"),
        "adguard": ("94.140.14.14", "dns.adguard-dns.com", "/dns-query"),
    }

    if provider == "custom":
        custom_url = SETTINGS.settings.get("doh_custom_url", "")
        if custom_url.startswith("https://"):
            try:
                parsed = urllib.parse.urlparse(custom_url)
                host_hdr = parsed.netloc
                path_base = parsed.path or "/dns-query"
                ip_target = host_hdr.split(":")[0]
            except Exception:
                ip_target, host_hdr, path_base = doh_configs["cloudflare"]
        else:
            ip_target, host_hdr, path_base = doh_configs["cloudflare"]
    else:
        ip_target, host_hdr, path_base = doh_configs.get(provider, doh_configs["cloudflare"])

    for _ in range(2):
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = socket.create_connection((ip_target, 443), timeout=3.5)
            ssock = ctx.wrap_socket(sock, server_hostname=host_hdr)
            sep = "&" if "?" in path_base else "?"
            query_path = f"{path_base}{sep}name={urllib.parse.quote(hostname)}&type=A"
            req = (
                f"GET {query_path} HTTP/1.1\r\n"
                f"Host: {host_hdr}\r\n"
                f"Accept: application/dns-json\r\n"
                f"User-Agent: PyBrowser-DoH/4.7\r\n"
                f"Connection: close\r\n\r\n"
            )
            ssock.sendall(req.encode('utf-8'))
            raw_resp = b""
            while True:
                chunk = ssock.recv(4096)
                if not chunk:
                    break
                raw_resp += chunk
            ssock.close()
            parts = raw_resp.split(b"\r\n\r\n", 1)
            if len(parts) == 2:
                data = json.loads(parts[1].decode("utf-8", errors="ignore"))
                for answer in data.get("Answer", []):
                    if answer.get("type") == 1:
                        ip = answer.get("data")
                        with DNS_LOCK:
                            DNS_CACHE[hostname] = {"ip": ip, "expires": now + 3600}
                            DNS_CACHE_DIRTY = True
                        return ip
        except Exception:
            time.sleep(0.05)

    return hostname

class DoHProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_CONNECT(self):
        try:
            host, port = (self.path.rsplit(":", 1) if ":" in self.path else (self.path, 443))
            port = int(port)
            host = host.strip("[]")
        except Exception:
            self.send_error(400, "Bad Request")
            return

        if SETTINGS.settings.get("adblock_enabled", True) and is_domain_blocked(host):
            self.send_error(403, "Blocked by PyBrowser uBlock Engine")
            return

        ip = doh_resolve(host)
        try:
            upstream = socket.create_connection((ip, port), timeout=6)
            self.send_response(200, "Connection Established")
            self.end_headers()
            self._tunnel(self.connection, upstream)
        except Exception:
            self.send_error(502, "Bad Gateway")

    def do_GET(self): self._proxy_plain_http()
    def do_POST(self): self._proxy_plain_http()
    def do_HEAD(self): self._proxy_plain_http()

    def _proxy_plain_http(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            host = parsed.hostname or self.headers.get("Host", "").split(":")[0]
            port = parsed.port or 80
            if SETTINGS.settings.get("adblock_enabled", True) and is_domain_blocked(host):
                self.send_error(403, "Blocked by PyBrowser")
                return

            ip = doh_resolve(host)
            upstream = socket.create_connection((ip, port), timeout=6)
            req_path = parsed.path or "/"
            if parsed.query:
                req_path += f"?{parsed.query}"

            raw_req = f"{self.command} {req_path} HTTP/1.1\r\n"
            for k, v in self.headers.items():
                if k.lower() not in ("proxy-connection",):
                    raw_req += f"{k}: {v}\r\n"
            raw_req += "\r\n"
            upstream.sendall(raw_req.encode("utf-8"))

            content_len = int(self.headers.get("Content-Length", 0))
            if content_len > 0:
                body = self.rfile.read(content_len)
                upstream.sendall(body)

            self._tunnel(self.connection, upstream)
        except Exception:
            try:
                self.send_error(502, "Bad Gateway")
            except Exception:
                pass

    def _tunnel(self, client, upstream):
        sockets = [client, upstream]
        try:
            while True:
                r, _, _ = select.select(sockets, [], [], 15)
                if not r:
                    break
                for sock in r:
                    other = upstream if sock is client else client
                    data = sock.recv(8192)
                    if not data:
                        return
                    other.sendall(data)
        except Exception:
            pass
        finally:
            try:
                client.close()
            except Exception:
                pass
            try:
                upstream.close()
            except Exception:
                pass

def start_proxy_server():
    class ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

        def server_bind(self):
            # Support SO_REUSEPORT on macOS / BSD for clean local restarts
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except Exception:
                    pass
            super().server_bind()

    try:
        server = ReusableTCPServer(("127.0.0.1", LOCAL_PROXY_PORT), DoHProxyHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        threading.Thread(target=dns_flush_worker, daemon=True).start()
    except Exception as e:
        print(f"[Proxy Warning] Could not start local proxy server: {e}")

def dns_flush_worker():
    while True:
        time.sleep(30)
        flush_dns_cache_to_disk()

def create_nw_proxy_config(host="127.0.0.1", port=LOCAL_PROXY_PORT):
    """Safely instantiates an NWProxyConfig instance via Network.framework."""
    try:
        # Import PyObjC Network module if available so runtime registers the class
        try:
            import Network
        except ImportError:
            pass

        libnet = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Network.framework/Network")
        libnet.nw_endpoint_create_host.restype = ctypes.c_void_p
        libnet.nw_endpoint_create_host.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        libnet.nw_proxy_config_create_http_connect.restype = ctypes.c_void_p
        libnet.nw_proxy_config_create_http_connect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        ep = libnet.nw_endpoint_create_host(host.encode("utf-8"), str(port).encode("utf-8"))
        if not ep:
            return None
        cfg = libnet.nw_proxy_config_create_http_connect(ep, None)
        if not cfg:
            return None
        
        # Bridge C pointer to PyObjC object
        return objc.objc_object(c_void_p=cfg)
    except Exception as e:
        print(f"[DoH Proxy Config Error] Failed to bind proxy to WebKit: {e}")
        return None

def load_adblock_filters_from_disk():
    global STEVENBLACK_DOMAINS
    domains = set()
    if os.path.exists(ADBLOCK_CACHE_FILE):
        try:
            with open(ADBLOCK_CACHE_FILE, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("0.0.0.0 ") or line.startswith("127.0.0.1 "):
                        parts = line.split()
                        if len(parts) >= 2:
                            d = parts[1].strip().lower()
                            if d and d not in ("localhost", "0.0.0.0", "broadcasthost", "local"):
                                domains.add(d)
        except Exception as e:
            print(f"[Adblock Disk Error] {e}")
    with ADBLOCK_LOCK:
        STEVENBLACK_DOMAINS = domains
    rebuild_active_domains()

def compile_webkit_adblock_rules():
    def compile_task():
        rebuild_active_domains()
        with ADBLOCK_LOCK:
            domains = list(ACTIVE_BLOCK_DOMAINS)[:48000]

        rules = []
        for d in domains:
            if not d:
                continue
            rules.append({
                "trigger": {"url-filter": f"^https?://([^/]+\\.)?{re.escape(d)}"},
                "action": {"type": "block"}
            })

        json_str = json.dumps(rules)

        def handler(rule_list, error):
            global WK_RULE_LIST
            if rule_list:
                WK_RULE_LIST = rule_list
                dispatch_to_main(apply_rules_to_all_tabs)
            elif error:
                print(f"[WebKit Rule Compile Error] {error}")

        dispatch_to_main(lambda: WebKit.WKContentRuleListStore.defaultStore().compileContentRuleListForIdentifier_encodedContentRuleList_completionHandler_(
            "PyBrowserAdblock", json_str, handler
        ))
    threading.Thread(target=compile_task, daemon=True).start()

def apply_rules_to_all_tabs():
    app_delegate = Cocoa.NSApp.delegate()
    if not app_delegate or not hasattr(app_delegate, 'controllers'):
        return
    for wc in getattr(app_delegate, "controllers", []):
        for tab in getattr(wc, "tabs", []):
            wv = tab.get("webview")
            if wv:
                uc = wv.configuration().userContentController()
                uc.removeAllContentRuleLists()
                if WK_RULE_LIST and SETTINGS.settings.get("adblock_enabled", True):
                    uc.addContentRuleList_(WK_RULE_LIST)

def update_adblock_filters_from_web():
    try:
        req = urllib.request.Request(FILTER_LIST_URL, headers={"User-Agent": "PyBrowser/4.7 Adblock"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
            with open(ADBLOCK_CACHE_FILE, "w", encoding="utf-8") as f:
                f.write(content)
        load_adblock_filters_from_disk()
        SETTINGS.settings["adblock_last_updated"] = time.strftime("%Y-%m-%d %H:%M")
        SETTINGS.save()
        compile_webkit_adblock_rules()
        return len(STEVENBLACK_DOMAINS)
    except Exception as e:
        print(f"[Adblock Web Update Error] {e}")
        return len(STEVENBLACK_DOMAINS)