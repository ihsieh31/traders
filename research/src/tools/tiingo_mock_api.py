"""Mock Tiingo API for offline testing. Never touches the network.

MOCK_SPEC  = {"limit_per_token": N, "tickers": {TICKER: "YYYY-MM-DD"}}
  A ticker's value is the last bar date the mock returns. Returning a date that
  differs from the symbol master's endDate is how ticker-reuse is simulated.
  Special values: "missing" -> 404, "empty" -> [].
MOCK_CALLS  = path to a jsonl log of every request, for assertions.
"""
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

SPEC = json.load(open(os.environ["MOCK_SPEC"]))
CALLS = os.environ.get("MOCK_CALLS", "/tmp/dl/_mock_calls.jsonl")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        auth = self.headers.get("Authorization", "").replace("Token ", "")
        with open(CALLS, "a") as f:
            f.write(json.dumps({"path": self.path, "token": auth[:8]}) + "\n")
        n = 0
        try:
            for line in open(CALLS):
                if line.strip() and json.loads(line)["token"] == auth[:8]:
                    n += 1
        except FileNotFoundError:
            pass
        if n > SPEC["limit_per_token"]:
            return self._send(429, b'{"detail":"You have run over your hourly request allocation."}')
        for t, kind in SPEC["tickers"].items():
            if f"/daily/{t}/" in self.path:
                if kind == "missing":
                    return self._send(404)
                if kind == "empty":
                    return self._send(200, b"[]")
                import datetime as _dt
                _end = _dt.date.fromisoformat(kind)
                row = []
                for _k in range(600):
                    _d = _end - _dt.timedelta(days=(599 - _k) * 2)   # ascending
                    row.append({"date": f"{_d.isoformat()}T00:00:00.000Z", "open": 1.0,
                                "high": 1.0, "low": 1.0, "close": 1.0, "volume": 100.0,
                                "divCash": 0.0, "splitFactor": 1.0})
                return self._send(200, json.dumps(row).encode())
        return self._send(404)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
