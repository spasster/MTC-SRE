"""Локальный экран демонстрации с реальными данными Prometheus и Kubernetes."""

import argparse
import base64
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from experiment import api, cmd, metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default="mtc")
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    cmd("sh", "scripts/require-context.sh", args.cluster)
    secret = json.loads(cmd("kubectl", "get", "secret", "healer-webhook", "-n", "heal", "-o", "json"))
    token = base64.b64decode(secret["data"]["token"]).decode()
    forward = None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            nonlocal forward
            try:
                if self.path == "/":
                    payload = Path("docs/demo.html").read_bytes()
                    kind = "text/html; charset=utf-8"
                elif self.path == "/data":
                    data = metrics("http://127.0.0.1:19090")
                    try:
                        data["healer"] = api("http://127.0.0.1:18082/status", token=token)
                    except Exception:
                        if forward is not None and forward.poll() is None:
                            forward.terminate()
                            forward.wait(timeout=5)
                        forward = subprocess.Popen(
                            ["kubectl", "port-forward", "-n", "heal", "svc/healer", "18082:8080"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
                        data["healer"] = None
                    payload = json.dumps(data).encode()
                    kind = "application/json"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
            except Exception:
                self.send_error(503, "Данные временно недоступны")

    print(f"Экран демонстрации: http://127.0.0.1:{args.port}; токены не передаются браузеру.", flush=True)
    try:
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    finally:
        if forward is not None:
            forward.terminate()


if __name__ == "__main__":
    main()
