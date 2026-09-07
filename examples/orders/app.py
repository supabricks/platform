#!/usr/bin/env python3
"""Small local orders API. DATABASE_URL comes from `supabricks connect --uri`."""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import psycopg
from psycopg.rows import dict_row


class Orders(BaseHTTPRequestHandler):
    def reply(self, code, value):
        data = json.dumps(value, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path != "/orders":
            return self.reply(404, {"error": "not_found"})
        try:
            with psycopg.connect(
                os.environ["DATABASE_URL"], row_factory=dict_row
            ) as db:
                rows = db.execute(
                    "SELECT * FROM orders ORDER BY id LIMIT 100"
                ).fetchall()
            self.reply(200, {"orders": rows})
        except psycopg.Error:
            self.reply(503, {"error": "database_unavailable"})

    def do_POST(self):
        if self.path != "/orders":
            return self.reply(404, {"error": "not_found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError()
            value = json.loads(self.rfile.read(length))
            customer, cents = value["customer"], value["total_cents"]
            if not isinstance(customer, str) or not 1 <= len(customer) <= 200:
                raise ValueError()
            if type(cents) is not int or not 0 <= cents <= 9223372036854775807:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            return self.reply(
                400, {"error": "expected customer and nonnegative total_cents"}
            )
        try:
            with psycopg.connect(
                os.environ["DATABASE_URL"], row_factory=dict_row
            ) as db:
                row = db.execute(
                    "INSERT INTO orders(customer,total_cents) VALUES (%s,%s) RETURNING *",
                    (customer, cents),
                ).fetchone()
            self.reply(201, row)
        except psycopg.Error:
            self.reply(503, {"error": "database_unavailable"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if not os.environ.get("DATABASE_URL"):
        parser.error("set DATABASE_URL using supabricks connect --uri")
    server = HTTPServer(("127.0.0.1", args.port), Orders)
    print(f"Orders ready on http://127.0.0.1:{server.server_port}/orders", flush=True)
    server.serve_forever()
