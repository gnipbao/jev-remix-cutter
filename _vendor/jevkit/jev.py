"""Bounded Jev HTTPS transport, adapted from jev-highlight-cutter (MIT)."""
import json
import time
import urllib.error
import urllib.request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"

class JevError(ValueError):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise JevError("Jev redirect refused; credentials were not forwarded")

def call_api(payload, key, attempts=3):
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(min(3, max(1, attempts))):
        request = urllib.request.Request(ENDPOINT, data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(),
                                        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
        try:
            with opener.open(request, timeout=40) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise JevError("Jev response exceeded byte limit")
            return json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504, 529) and attempt + 1 < min(3, max(1, attempts)):
                time.sleep(min(4, 2 ** attempt))
                continue
            raise JevError(f"Jev HTTP {exc.code}; response bodies and credentials omitted") from None
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 < min(3, max(1, attempts)):
                time.sleep(min(4, 2 ** attempt))
                continue
            raise JevError("Jev network failure; completed caches preserved") from None
        except (ValueError, UnicodeError):
            raise JevError("Jev returned invalid JSON") from None
    raise JevError("Jev request failed")
