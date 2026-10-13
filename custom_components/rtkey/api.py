"""Asynchronous RTKey cloud API client."""
import asyncio
import json
import logging
import time
from urllib.parse import urlparse
import ipaddress
from aiohttp import ClientError, ClientResponseError, ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from .const import CAMERAS_URL, INTERCOMS_URL, AUTH_BASE

_LOGGER = logging.getLogger(__name__)

class RTKeyAPIError(Exception):
    """RTKey request failed."""

class RTKeyAuthError(RTKeyAPIError):
    """RTKey authentication failed."""

class RTKeyUpstreamBlocked(RTKeyAPIError):
    """Upstream protection rejected the request; do not retry immediately."""

def _trusted_host(url, *, service=False):
    """Validate external destinations before attaching any credential."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.port not in (None, 443)):
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
        if service:
            return host in ("keyapis.key.rt.ru", "household.key.rt.ru")
        # RTKey media endpoints use different Rostelecom subdomains.
        return host == "rt.ru" or host.endswith(".rt.ru")
    except (ValueError, TypeError):
        return False


class RTKeyAPI:
    def __init__(self, hass, token, device_id=None, entry_id=None):
        self.session = async_get_clientsession(hass)
        self._store = Store(hass, 1, f"rtkey_camera_cache_{entry_id}") if entry_id else None
        self._last_camera_failure = 0
        self._last_camera_success = 0
        self.token = token
        self.device_id = device_id
        self._cameras = []
        self._intercoms = []
        self._cameras_updated = 0
        self._intercoms_updated = 0
        self._images = {}
        self._image_locks = {}
        self._refresh_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._blocked_until = 0.0
        self._failure_count = 0

    async def load_camera_cache(self):
        """Restore last successful camera metadata after HA restart."""
        if self._store is None:
            return
        try:
            data = await self._store.async_load()
            items = data.get("cameras") if isinstance(data, dict) else None
            if isinstance(items, list) and all(isinstance(x, dict) for x in items):
                self._cameras = [{"uid": x["uid"], "title": x.get("title")}
                                 for x in items if x.get("uid") is not None]
                # Rewrite legacy cache immediately: older versions stored media credentials.
                await self._save_camera_cache()
                _LOGGER.info("RTKey restored %s cached cameras", len(self._cameras))
        except (ValueError, TypeError, OSError) as err:
            _LOGGER.warning("RTKey camera cache unreadable: %s", type(err).__name__)

    async def _save_camera_cache(self):
        if self._store is None:
            return
        try:
            await self._store.async_save({"cameras": [
                {"uid": x["uid"], "title": x.get("title")}
                for x in self._cameras if x.get("uid") is not None
            ]})
        except (ValueError, TypeError, OSError) as err:
            _LOGGER.warning("RTKey camera cache write failed: %s", type(err).__name__)

    async def request(self, method, url, *, auth=True, headers=None, json_body=None,
                      plain_json=False):
        """Rate-limited request with bounded retries and upstream cooldown.

        No URLs, response bodies, request headers or authentication secrets are logged.
        """
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        if not _trusted_host(url, service=auth or plain_json):
            raise RTKeyAPIError("RTKey refused an untrusted request destination")
        parsed = urlparse(url)
        # Log only the fixed operation type, never a URL or query parameters.
        endpoint = "cloud API" if auth or plain_json else "media endpoint"
        request_headers = dict(headers or {})
        if self.device_id and (auth or plain_json):
            request_headers["X-Device-Id"] = self.device_id
        if auth:
            request_headers["Authorization"] = f"Bearer {self.token}"
        if plain_json:
            request_headers.update({
                "Authorization": "Bearer", "Content-Type": "text/plain;charset=UTF-8",
                "Origin": "https://key.rt.ru", "Referer": "https://key.rt.ru/",
            })
        payload = json.dumps(json_body, ensure_ascii=False) if plain_json else None
        # Serialize requests to prevent bursts, including snapshot refreshes.
        async with self._request_lock:
            now = time.monotonic()
            if now < self._blocked_until:
                raise RTKeyUpstreamBlocked("RTKey upstream cooldown active")
            await asyncio.sleep(max(0, self._next_request_at - now))
            for attempt in range(3):
                self._next_request_at = time.monotonic() + 1.0
                try:
                    async with self.session.request(
                        method, url, headers=request_headers, data=payload,
                        json=None if plain_json else json_body,
                        timeout=ClientTimeout(total=20), allow_redirects=False,
                    ) as response:
                        if response.status in (301, 302, 303, 307, 308):
                            raise RTKeyAPIError(f"RTKey unexpected redirect at {endpoint}")
                        # 401 is an authentication failure; 403 may be a WAF rejection.
                        if response.status == 401:
                            raise RTKeyAuthError(f"RTKey HTTP 401 at {endpoint}")
                        if response.status == 403:
                            self._blocked_until = time.monotonic() + 900
                            raise RTKeyUpstreamBlocked(f"RTKey HTTP 403 at {endpoint}; cooldown 15 min")
                        # Do not allow an unexpectedly huge body to exhaust HA memory.
                        body = await response.content.read(16 * 1024 * 1024 + 1)
                        if len(body) > 16 * 1024 * 1024:
                            raise RTKeyAPIError("RTKey response exceeded size limit")
                        if (response.content_type == "text/html" and
                                (b"Request Rejected" in body or
                                 b"requested URL was rejected" in body)):
                            self._blocked_until = time.monotonic() + 1800
                            _LOGGER.warning("RTKey upstream protection rejected request at %s; cooldown 30 min", endpoint)
                            raise RTKeyUpstreamBlocked(f"RTKey upstream rejected {endpoint}")
                        if response.status in (429, 500, 502, 503, 504):
                            if attempt < 2:
                                retry_after = response.headers.get("Retry-After", "")
                                try:
                                    wait = float(retry_after)
                                except ValueError:
                                    try:
                                        wait = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                                    except (ValueError, TypeError, OverflowError):
                                        wait = 2 ** attempt * 3
                                wait = min(300, max(1, wait))
                                await asyncio.sleep(wait)
                                continue
                            if response.status == 429:
                                self._blocked_until = time.monotonic() + 300
                        response.raise_for_status()
                        self._failure_count = 0
                        return body
                except (RTKeyAPIError, RTKeyAuthError):
                    raise
                except (ClientError, asyncio.TimeoutError, OSError) as err:
                    self._failure_count += 1
                    if attempt < 2 and not isinstance(err, ClientResponseError):
                        await asyncio.sleep(2 ** attempt * 3)
                        continue
                    if self._failure_count >= 3:
                        self._blocked_until = time.monotonic() + 120
                    if isinstance(err, ClientResponseError):
                        raise RTKeyAPIError(f"RTKey HTTP {err.status} at {endpoint}") from None
                    raise RTKeyAPIError(f"RTKey network error at {endpoint}: {type(err).__name__}") from None

    async def _json(self, method, url, *, auth=True, json_body=None, plain_json=False, headers=None):
        payload = await self.request(
            method, url, auth=auth, json_body=json_body, plain_json=plain_json, headers=headers
        )
        try:
            return json.loads(payload)
        except (ValueError, UnicodeDecodeError) as err:
            endpoint = "cloud API" if auth or plain_json else "media endpoint"
            _LOGGER.warning(
                "RTKey invalid JSON at %s: response_bytes=%s (body hidden)",
                endpoint, len(payload),
            )
            raise RTKeyAPIError(
                f"RTKey invalid JSON at {endpoint} ({len(payload)} bytes)"
            ) from err

    async def cameras(self, force=False):
        async with self._refresh_lock:
            if not force and time.monotonic() - self._cameras_updated < 120:
                return self._cameras
            if self._last_camera_failure and time.monotonic() - self._last_camera_failure < 300 and self._cameras:
                return self._cameras
            try:
                result = await self._json(
                    "GET", CAMERAS_URL,
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Referer": "https://key.rt.ru/",
                        "Origin": "https://key.rt.ru",
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/154.0.0.0 Safari/537.36"
                        ),
                        "DNT": "1",
                    },
                )
            except RTKeyAPIError:
                self._last_camera_failure = time.monotonic()
                if self._cameras:
                    _LOGGER.warning("RTKey camera API unavailable; using cached camera metadata")
                    return self._cameras
                raise
            try:
                items = result["data"]
                if not isinstance(items, list):
                    raise TypeError()
            except (KeyError, TypeError) as err:
                raise RTKeyAPIError("Unexpected cameras response") from err
            self._cameras = items
            self._cameras_updated = time.monotonic()
            self._last_camera_success = time.monotonic()
            self._last_camera_failure = 0
            await self._save_camera_cache()
            return items

    async def intercoms(self, force=False):
        async with self._refresh_lock:
            if not force and time.monotonic() - self._intercoms_updated < 120:
                return self._intercoms
            result = await self._json("GET", INTERCOMS_URL)
            try:
                items = result["data"]["devices"]
                if not isinstance(items, list):
                    raise TypeError()
            except (KeyError, TypeError) as err:
                raise RTKeyAPIError("Unexpected intercoms response") from err
            self._intercoms = items
            self._intercoms_updated = time.monotonic()
            return items

    async def camera(self, camera_id, force=False):
        for item in await self.cameras(force):
            if str(item.get("uid")) == str(camera_id):
                return item
        return None

    async def snapshot(self, camera_id):
        now = time.monotonic()
        cached = self._images.get(str(camera_id))
        if cached and now - cached[0] < 3:
            return cached[1]
        lock = self._image_locks.setdefault(str(camera_id), asyncio.Lock())
        async with lock:
            cached = self._images.get(str(camera_id))
            if cached and time.monotonic() - cached[0] < 3:
                return cached[1]
            info = await self.camera(camera_id)
            if not info:
                return None
            if "screenshotToken" not in info:
                return None
            try:
                url = info["screenshotUrlTemplate"].format(
                    timestamp=int(time.time()), size="large",
                    cdn_token=info["screenshotToken"])
                headers = {"X-UTOKEN": info["userToken"]}
            except (KeyError, ValueError, IndexError) as err:
                raise RTKeyAPIError("Invalid screenshot parameters") from err
            if not _trusted_host(url):
                raise RTKeyAPIError("RTKey screenshot destination not trusted")
            try:
                image = await self.request("GET", url, auth=False, headers=headers)
            except RTKeyAuthError:
                info = await self.camera(camera_id, force=True)
                if not info:
                    return None
                url = info["screenshotUrlTemplate"].format(
                    timestamp=int(time.time()), size="large",
                    cdn_token=info["screenshotToken"])
                if not _trusted_host(url):
                    raise RTKeyAPIError("RTKey screenshot destination not trusted")
                image = await self.request("GET", url, auth=False,
                                           headers={"X-UTOKEN": info["userToken"]})
            self._images[str(camera_id)] = (time.monotonic(), image)
            return image

    async def stream_url(self, camera_id):
        info = await self.camera(camera_id)
        if not info:
            return None
        if "streamerToken" not in info:
            return None
        try:
            host = urlparse(info["streamerUrl"])
            if not _trusted_host(info["streamerUrl"]):
                raise ValueError("Invalid stream host")
            token = info["streamerToken"]
            from urllib.parse import quote
            return (f"https://{host.netloc}/stream/{quote(str(camera_id), safe='')}/live.mp4"
                    f"?mp4-fragment-length=0.5&mp4-use-speed=0&mp4-afiller=1&token={quote(token, safe='')}")
        except (KeyError, ValueError) as err:
            raise RTKeyAPIError("Invalid RTKey stream parameters") from err

    async def send_code(self, phone):
        return await self._json("POST", f"{AUTH_BASE}/send_code", auth=False, json_body={"phoneNumber": phone}, plain_json=True)

    async def login(self, code_id, code):
        return await self._json("POST", f"{AUTH_BASE}/login", auth=False, json_body={"codeId": code_id, "code": code}, plain_json=True)

    async def open_door(self, intercom_id):
        from urllib.parse import quote
        await self.request("POST",
            f"https://household.key.rt.ru/api/v2/app/devices/{quote(str(intercom_id), safe='')}/open")
