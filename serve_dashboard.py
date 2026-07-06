"""
Tiny local server for the dashboard (docs/index.html + docs/dashboard.json).

Browsers block fetch() of local JSON files opened directly (file://), so this
serves the docs/ folder over http://localhost instead. Run it, then open the
printed URL. Leave it running in a terminal tab; refresh the page any time
after you `git pull` to see the latest signals/charts/performance.

Usage:
    python3 serve_dashboard.py
    python3 serve_dashboard.py --port 8800
"""
import argparse
import http.server
import os
import socketserver
import webbrowser

DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    os.chdir(DOCS_DIR)
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("127.0.0.1", args.port), handler) as httpd:
        url = f"http://127.0.0.1:{args.port}/"
        print(f"Dashboard running at {url}  (Ctrl+C to stop)")
        if not args.no_browser:
            webbrowser.open(url)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
