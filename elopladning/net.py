"""Fælles HTTP-hjælper: JSON ind og ud, timeout, genforsøg og læsbare fejl."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "elopladning-til-biler"
RETRY_STATUS = {429, 500, 502, 503, 504}


class HTTPError(RuntimeError):
    def __init__(self, status, url, body=""):
        self.status = status
        self.body = body
        host = urllib.parse.urlsplit(url).netloc
        detail = f": {body.strip()[:200]}" if body and body.strip() else ""
        super().__init__(f"HTTP {status} fra {host}{detail}")


def _retry_after(value, default):
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return default


def request_json(url, method="GET", headers=None, body=None, form=None, timeout=30,
                 retries=2, backoff=2.0, context=None, sleep=time.sleep):
    """Send en forespørgsel og returnér JSON-svaret (tomt svar -> {}).

    Netværksfejl, 429 og 5xx forsøges igen `retries` gange med stigende pause.
    Andre HTTP-fejl kastes straks som HTTPError."""
    hdrs = {"Accept": "application/json", "User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")

    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
                raw = r.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            text = ""
            try:
                text = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code not in RETRY_STATUS or attempt >= retries:
                raise HTTPError(e.code, url, text) from None
            wait = _retry_after(e.headers.get("Retry-After") if e.headers else None,
                                backoff * 2 ** attempt)
        except OSError:  # URLError, timeout, forbindelse afbrudt
            if attempt >= retries:
                raise
            wait = backoff * 2 ** attempt
        attempt += 1
        sleep(min(wait, 60))
