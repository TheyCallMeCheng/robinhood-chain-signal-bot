"""Minimal HTTP helper. Stdlib only, so the bot runs anywhere with python3."""
import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger("hlbot.http")

UA = "robinhood-chain-signal-bot/1.0"
JSON_HEADERS = {
    "Accept": "application/json;version=20230203",
    "User-Agent": UA,
}


def get_json(url: str, timeout: int = 25, retries: int = 3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=JSON_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            # 429/5xx are worth backing off on; 4xx generally are not.
            if (e.code == 429 or e.code >= 500) and attempt + 1 < retries:
                wait = 2 ** attempt * 3
                log.warning("GET %s -> %s, retrying in %ss", url[:90], e.code, wait)
                time.sleep(wait)
                continue
            log.warning("GET %s -> %s", url[:90], e.code)
            return None
        except Exception as e:  # noqa: BLE001 - network layer, keep the loop alive
            last = e
            time.sleep(2 ** attempt)
    log.warning("GET failed after %s attempts: %s (%s)", retries, url[:90], last)
    return None


def get_bytes(url: str, headers: dict = None, timeout: int = 20):
    """Raw fetch. IPFS trustless gateways need their own Accept header."""
    h = {"User-Agent": UA}
    h.update(headers or {})
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        log.debug("get_bytes %s -> %s", url[:80], e)
        return None


def post_json(url: str, payload: dict, timeout: int = 25, retries: int = 3):
    body = json.dumps(payload).encode("utf-8")
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "User-Agent": UA},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:  # noqa: BLE001
                pass
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt * 3)
                continue
            log.warning("POST %s -> %s %s", url[:90], e.code, detail)
            return {"_http_error": e.code, "_detail": detail}
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 ** attempt)
    log.warning("POST failed after %s attempts: %s (%s)", retries, url[:90], last)
    return None
