"""Telegram sender."""
import html
import logging
import time

from . import config
from .http import get_json, post_json

log = logging.getLogger("hlbot.tg")

API = "https://api.telegram.org/bot{token}/{method}"

_last_send = [0.0]
MIN_SEND_GAP = 3.2   # channels tolerate ~20 msg/min; stay comfortably under

_UNSAFE = "\n\r\t<>\"'"


def _url(method: str) -> str:
    return API.format(token=config.BOT_TOKEN, method=method)


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def link(url, max_len: int = 44) -> str:
    """Anchor for a URL taken from token metadata.

    These strings are written by whoever deployed the token, so the scheme is
    whitelisted and both href and text are escaped. The visible text is the
    domain and path rather than a generic label: a link reading only "site"
    hides where it goes, and plenty of these point at somebody else's account.
    """
    u = str(url or "").strip()
    if not u or len(u) > 300:
        return ""
    if any(c in u for c in _UNSAFE):
        return ""

    low = u.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        # Bare domains are common here. Anything else (javascript:, data:, ...)
        # is dropped rather than guessed at.
        head = u.split("/")[0]
        if "." not in head or ":" in head or " " in u:
            return ""
        u = "https://" + u

    shown = u.split("://", 1)[1]
    if shown.startswith("www."):
        shown = shown[4:]
    if len(shown) > max_len:
        shown = shown[:max_len - 1] + "…"
    return '<a href="{}">{}</a>'.format(
        html.escape(u, quote=True), html.escape(shown, quote=False))


def get_me():
    return get_json(_url("getMe"))


def get_chat(chat_id: str = None):
    cid = chat_id or config.CHAT_ID
    return get_json(_url("getChat") + f"?chat_id={cid}")


def send(text: str, disable_preview: bool = True) -> bool:
    if not config.BOT_TOKEN or not config.CHAT_ID:
        log.error("BOT_TOKEN or CHAT_ID missing; cannot send")
        return False

    gap = time.time() - _last_send[0]
    if gap < MIN_SEND_GAP:
        time.sleep(MIN_SEND_GAP - gap)
    _last_send[0] = time.time()

    resp = post_json(_url("sendMessage"), {
        "chat_id": config.CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    })
    if not resp:
        return False
    if resp.get("ok"):
        return True
    log.error("telegram send failed: %s", resp.get("description") or resp)
    return False
