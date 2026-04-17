#!/usr/bin/env python3
# Local dev server for evers-menu. Serves index.html with aggressive no-cache
# so iPhone Safari stops clinging to stale HTML during live-edit sessions.
# Usage: python3 dev-server.py  (then open http://<mac-ip>:8000/?reset on phone)

import socket
from http.server import SimpleHTTPRequestHandler, HTTPServer

PORT = 8000

class NoCacheHandler(SimpleHTTPRequestHandler):
    # Strip conditional-request headers so SimpleHTTPRequestHandler never 304s.
    def do_GET(self):
        for h in ('If-Modified-Since', 'If-None-Match'):
            if h in self.headers:
                del self.headers[h]
        super().do_GET()

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    finally:
        s.close()

if __name__ == '__main__':
    ip = lan_ip()
    print(f'serving on http://{ip}:{PORT}')
    print(f'phone reset URL: http://{ip}:{PORT}/?reset')
    HTTPServer(('0.0.0.0', PORT), NoCacheHandler).serve_forever()
