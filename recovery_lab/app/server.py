"""Small, read-only application used by the synthetic recovery drill."""
import html
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/notes/1"):
            self.send_error(404)
            return
        try:
            with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
                row = conn.execute("SELECT title, body FROM notes WHERE id = 1").fetchone()
        except psycopg.Error:
            self.send_error(503, "Database unavailable")
            return
        if row is None:
            self.send_error(404)
            return
        title, body = map(html.escape, row)
        content = (f'<h1>Recovery Notes</h1><a href="/notes/1">{title}</a>'
                   if self.path == "/" else f"<h1>{title}</h1><article>{body}</article>")
        page = ("<!doctype html><html lang='en'><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width'><title>Recovery Notes</title>"
                "<style>body{max-width:48rem;margin:4rem auto;padding:1rem;font:20px system-ui;"
                "background:#f7f4ed;color:#182f36}a{color:#006b64}article{line-height:1.7}</style>"
                f"<main>{content}</main></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *_):
        pass


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
