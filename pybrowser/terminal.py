"""
PyBrowser Embedded Terminal Session Manager.
Spawns native login shells over pseudo-terminals (PTY) and bridges I/O to WebKit.
"""

import os
import pty
import select
import subprocess
import termios
import fcntl
import struct
import threading
import base64
import time
import signal
import Cocoa

def dispatch_to_main(fn, *args):
    """Executes a function on the macOS main Cocoa run loop."""
    if Cocoa.NSThread.isMainThread():
        fn(*args)
    else:
        Cocoa.NSOperationQueue.mainQueue().addOperationWithBlock_(lambda: fn(*args))

class TerminalSession:
    """Non-blocking PTY session streaming raw I/O over Base64 to xterm.js."""
    def __init__(self, webview, cols=80, rows=24, on_close=None):
        self.webview = webview
        self.cols = max(10, int(cols))
        self.rows = max(5, int(rows))
        self.alive = True
        self.master_fd = -1
        self.proc = None
        self.lock = threading.Lock()
        self.on_close = on_close
        self.spawn()

    def spawn(self):
        try:
            self.master_fd, slave_fd = pty.openpty()
            winsize = struct.pack("HHHH", int(self.rows), int(self.cols), 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

            flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
            fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env["LANG"] = "en_US.UTF-8"
            env["SHELL"] = "/bin/zsh" if os.path.exists("/bin/zsh") else "/bin/bash"

            self.proc = subprocess.Popen(
                [env["SHELL"], "-l"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
                env=env,
                cwd=os.path.expanduser("~")
            )
            os.close(slave_fd)
            threading.Thread(target=self._read_loop, daemon=True).start()
        except Exception as e:
            print(f"[Terminal Spawn Error] {e}")

    def _read_loop(self):
        while self.alive and self.master_fd >= 0:
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.05)
                if r:
                    with self.lock:
                        if self.master_fd < 0:
                            break
                        try:
                            data = os.read(self.master_fd, 8192)
                        except (BlockingIOError, InterruptedError):
                            continue
                        except OSError:
                            break

                    if not data:
                        break
                    b64_str = base64.b64encode(data).decode('ascii')
                    wv = self.webview
                    if wv:
                        dispatch_to_main(lambda b=b64_str, w=wv: self._flush_to_webview(w, b))
            except (OSError, ValueError):
                break
        self.close()

    def _flush_to_webview(self, wv, b64_str):
        try:
            wv.evaluateJavaScript_completionHandler_(
                f"if (window.writeToTerm) window.writeToTerm('{b64_str}');", None
            )
        except Exception:
            pass

    def write(self, text):
        if not self.alive or self.master_fd < 0:
            return
        try:
            data = text.encode('utf-8')
            total = 0
            while total < len(data) and self.master_fd >= 0:
                try:
                    with self.lock:
                        if self.master_fd < 0:
                            break
                        n = os.write(self.master_fd, data[total:])
                    total += n
                except (BlockingIOError, InterruptedError):
                    time.sleep(0.001)
                except OSError:
                    break
        except Exception as e:
            print(f"[Terminal Write Error] {e}")

    def resize(self, cols, rows):
        self.cols = max(10, int(cols))
        self.rows = max(5, int(rows))
        if self.master_fd >= 0 and self.alive:
            try:
                winsize = struct.pack("HHHH", int(self.rows), int(self.cols), 0, 0)
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            except Exception as e:
                print(f"[Terminal Resize Error] {e}")

    def close(self):
        if not self.alive and self.master_fd < 0 and self.proc is None:
            return
        self.alive = False
        with self.lock:
            if self.master_fd >= 0:
                try:
                    os.close(self.master_fd)
                except Exception:
                    pass
                self.master_fd = -1

        if self.proc:
            try:
                pgid = os.getpgid(self.proc.pid)
                os.killpg(pgid, signal.SIGTERM)
                try:
                    self.proc.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    os.killpg(pgid, signal.SIGKILL)
                    self.proc.wait(timeout=0.2)
            except Exception:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=0.2)
                except Exception:
                    pass
            self.proc = None

        self.webview = None
        if self.on_close:
            try:
                self.on_close(self)
            except Exception:
                pass
            self.on_close = None

class TerminalManager:
    def __init__(self):
        self.sessions = {}
        self.lock = threading.Lock()

    def get_or_create(self, webview, cols=80, rows=24):
        with self.lock:
            session = self.sessions.get(webview)
            if session and session.alive:
                session.resize(cols, rows)
                return session
            if session:
                session.close()
            session = TerminalSession(
                webview, cols, rows,
                on_close=lambda s: self._remove_session(webview, s)
            )
            self.sessions[webview] = session
            return session

    def _remove_session(self, webview, session):
        with self.lock:
            if self.sessions.get(webview) is session:
                self.sessions.pop(webview, None)

    def write_active(self, webview, text):
        session = self.get_or_create(webview)
        session.write(text)

    def close_for_webview(self, webview):
        with self.lock:
            session = self.sessions.pop(webview, None)
        if session:
            session.close()

    def close_all(self):
        with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for s in sessions:
            s.close()

TERMINAL_MANAGER = TerminalManager()
