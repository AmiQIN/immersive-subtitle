"""SceneTalk 本地启动器
- 用 Python 标准库起一个只监听 127.0.0.1 的小服务，供页面读写（同时让语音识别拿到安全上下文）
- 用 Edge 的 --app 模式打开成独立窗口，看起来就是一个桌面小工具
- 页面每 8 秒发一次心跳；窗口关掉后心跳停止，本进程自动退出，不留后台常驻
"""

import http.server
import os
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PAGE = "SceneTalk.html"
PORT_RANGE = range(8791, 8811)

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]

last_beat = [0.0]
server_ref = [None]


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if self.path.split("?")[0] == "/api/beat":
            last_beat[0] = time.time()
            self.send_response(204)
            self.end_headers()
            return
        self.send_error(404)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/quit":
            self.send_response(204)
            self.end_headers()
            threading.Thread(target=shutdown, daemon=True).start()
            return
        super().do_GET()


def shutdown():
    time.sleep(0.2)
    if server_ref[0]:
        server_ref[0].shutdown()


def watchdog():
    # 有页面心跳之后，连续 45 秒没心跳就认为窗口已关闭
    while True:
        time.sleep(2)
        if last_beat[0] and (time.time() - last_beat[0]) > 45:
            os._exit(0)


def pick_port():
    for p in PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0


def find_edge():
    for path in EDGE_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def main():
    if not os.path.exists(os.path.join(ROOT, PAGE)):
        print("找不到 " + PAGE)
        return 1

    port = pick_port()
    if not port:
        print("8791-8810 端口都被占用")
        return 1

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server_ref[0] = httpd
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()

    url = "http://127.0.0.1:%d/%s" % (port, PAGE)
    edge = find_edge()
    if edge:
        subprocess.Popen([
            edge,
            "--app=" + url,
            "--window-size=1040,760",
            "--disable-features=EdgeSidebar",
        ])
    else:
        os.startfile(url)

    while True:
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
