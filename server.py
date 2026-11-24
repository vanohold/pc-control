"""PC CONTROL V2 backend for Windows.

Run locally by default: http://127.0.0.1:5000
For LAN access set PC_CONTROL_HOST=0.0.0.0 and PC_CONTROL_TOKEN to a secret.
"""

import asyncio
import ctypes
from ctypes import wintypes
import hashlib
import ipaddress
import logging
import multiprocessing
import os
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path

from flask import Flask, g, jsonify, make_response, request, send_from_directory
from werkzeug.exceptions import BadRequest

import psutil
import pythoncom
from pycaw.pycaw import AudioUtilities

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    )
    from winrt.windows.storage.streams import DataReader

    MEDIA_AVAILABLE = True
except Exception:
    MediaManager = None
    DataReader = None
    MEDIA_AVAILABLE = False


BASE_DIR = Path(__file__).resolve().parent
HOST = os.environ.get("PC_CONTROL_HOST", "127.0.0.1")
PORT = int(os.environ.get("PC_CONTROL_PORT", "5000"))
LAN_MODE = HOST not in {"127.0.0.1", "localhost", "::1"}
TOKEN = os.environ.get("PC_CONTROL_TOKEN", "").strip()
if LAN_MODE and len(TOKEN) < 24:
    raise RuntimeError(
        "LAN mode requires PC_CONTROL_TOKEN with at least 24 characters. "
        "Set a long random secret before starting the server."
    )

TRACK_TTL = 0.35
COMMAND_WAIT = 1.2
POLL_STEP = 0.08
COVER_MAX_SIZE = 4 * 1024 * 1024
COVER_CACHE_LIMIT = 8
COVER_READ_TIMEOUT = 10.0
COVER_RETRY_AFTER_TIMEOUT = 20.0
CPU_SAMPLE_INTERVAL = 1.0

# Discord has no unauthenticated local API for reading mute state. This button
# only sends the user's configured NumPad + key; it never claims to know state.
VK_ADD = 0x6B
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
_user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
_user32.MapVirtualKeyW.restype = wintypes.UINT


def _is_elevated():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None


def _send_input_events(inputs):
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    ctypes.set_last_error(0)
    sent = int(_user32.SendInput(n, ctypes.byref(arr[0]), ctypes.sizeof(INPUT)))
    return sent, int(ctypes.get_last_error())


def _send_vk(vk):
    vk = int(vk)
    scan = int(_user32.MapVirtualKeyW(vk, 0))

    # 1) SendInput with virtual key + scan.
    sent, err = _send_input_events([
        INPUT(type=INPUT_KEYBOARD, union=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, wScan=scan))),
        INPUT(type=INPUT_KEYBOARD, union=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=KEYEVENTF_KEYUP))),
    ])
    if sent == 2:
        return {"ok": True, "method": "SendInput(vk)", "sent": sent, "scan": scan}

    # 2) SendInput with hardware scan code (some apps ignore VK-only input).
    sent2, err2 = _send_input_events([
        INPUT(type=INPUT_KEYBOARD, union=_INPUTUNION(ki=KEYBDINPUT(wVk=0, wScan=scan, dwFlags=KEYEVENTF_SCANCODE))),
        INPUT(type=INPUT_KEYBOARD, union=_INPUTUNION(ki=KEYBDINPUT(wVk=0, wScan=scan, dwFlags=KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP))),
    ])
    if sent2 == 2:
        return {"ok": True, "method": "SendInput(scan)", "sent": sent2, "scan": scan, "sendinput_vk_error": err}

    log.warning(
        "SendInput blocked for vk=%s scan=%s vk_sent=%s vk_err=%s scan_sent=%s scan_err=%s; trying keybd_event fallback",
        vk, scan, sent, err, sent2, err2,
    )
    try:
        # Best-effort fallback; note: if UIPI blocks SendInput, keybd_event is
        # usually blocked too. We report this as not reliably delivered.
        ctypes.windll.user32.keybd_event(vk, scan, 0, 0)
        time.sleep(0.03)
        ctypes.windll.user32.keybd_event(vk, scan, KEYEVENTF_KEYUP, 0)
        blocked = (sent == 0 and sent2 == 0)
        return {
            "ok": not blocked,
            "method": "keybd_event_fallback",
            "sent": 2,
            "scan": scan,
            "sendinput_vk_error": err,
            "sendinput_scan_error": err2,
            "warning": (
                "SendInput был заблокирован Windows (sent=0). "
                "Запустите PC CONTROL и Discord с одинаковыми правами (оба обычные или оба от администратора)."
                if blocked else None
            ),
        }
    except Exception as exc:
        raise OSError(err2 or err, f"SendInput failed (vk {sent}/2 err {err}, scan {sent2}/2 err {err2}); keybd_event fallback failed: {exc}") from exc
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pc-control")

_track_lock = threading.Lock()
_track_cache = {"time": 0.0, "data": None}
_cover_lock = threading.Lock()
_cover_cache = OrderedDict()
_cover_read_lock = threading.Lock()
_cover_reader_state = {"stalled": False, "started_at": None, "retry_after": 0.0}
_command_lock = threading.Lock()
_cpu_lock = threading.Lock()
_cpu_cache = {"value": 0.0, "time": 0.0}

psutil.cpu_percent(interval=None)


def _update_cpu_cache():
    value = psutil.cpu_percent(interval=0.25)
    with _cpu_lock:
        _cpu_cache["value"] = value
        _cpu_cache["time"] = time.monotonic()


def _cpu_sampler():
    while True:
        try:
            _update_cpu_cache()
        except Exception:
            log.exception("CPU sampling failed")
        time.sleep(max(0.0, CPU_SAMPLE_INTERVAL - 0.25))


# Avoid side effects in spawned helper processes (e.g. cover reader).
if multiprocessing.parent_process() is None:
    threading.Thread(target=_cpu_sampler, name="cpu-sampler", daemon=True).start()
    _update_cpu_cache()


def _error(message, status):
    return jsonify({"error": message}), status


def _private_client():
    address = request.remote_addr
    if not address:
        return False
    try:
        ip = ipaddress.ip_address(address)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return False


@app.before_request
def protect_api():
    if request.endpoint == "home":
        return None
    if not _private_client():
        return _error("Local/private network access only", 403)

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != f"{request.scheme}://{request.host}".rstrip("/"):
            return _error("Invalid request origin", 403)

    if LAN_MODE:
        supplied = request.headers.get("X-PC-Control-Token", "")
        if not supplied or not secrets.compare_digest(supplied, TOKEN):
            return _error("Unauthorized", 401)
    return None


@app.before_request
def initialize_com():
    # Discord mute and static/home do not require COM; other API routes may use
    # WinRT/pycaw/COM-backed APIs on the request thread.
    if request.endpoint in {"home", "discord_mute"}:
        g.pc_control_com_initialized = False
        return None
    try:
        pythoncom.CoInitialize()
        g.pc_control_com_initialized = True
    except Exception:
        g.pc_control_com_initialized = False
        log.exception("COM initialization failed")
    return None


@app.teardown_appcontext
def uninitialize_com(_error_value=None):
    if getattr(g, "pc_control_com_initialized", False):
        try:
            pythoncom.CoUninitialize()
        except Exception:
            log.exception("COM uninitialization failed")


def _volume_from_request():
    if not request.is_json:
        raise ValueError("Content-Type must be application/json")
    try:
        body = request.get_json(silent=False)
    except BadRequest as exc:
        raise ValueError("Malformed JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("JSON body must be an object")
    value = body.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("value must be a number from 0 to 100")
    if not 0 <= value <= 100:
        raise ValueError("value must be from 0 to 100")
    return float(value)


async def _read_track(with_cover=False):
    if not MEDIA_AVAILABLE:
        return {"playing": False, "state": "stopped", "error": "media_api_unavailable"}

    if with_cover:
        log.info("Cover worker: requesting GSMTC manager")
    manager = await MediaManager.request_async()
    if with_cover:
        log.info("Cover worker: GSMTC manager ready")
    session = manager.get_current_session()
    if with_cover:
        log.info("Cover worker: current session found=%s", session is not None)
    if session is None:
        return {"playing": False, "state": "stopped"}

    props = await session.try_get_media_properties_async()
    if with_cover:
        log.info("Cover worker: media properties received")
    playback = int(session.get_playback_info().playback_status)
    title = props.title or ""
    artist = props.artist or ""
    album = props.album_title or ""
    app_id = session.source_app_user_model_id or ""
    state = {4: "playing", 5: "paused"}.get(playback, "stopped")
    if not title and not artist:
        return {"playing": False, "state": state}

    identity = "\0".join((title, artist, album, app_id))
    track_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    result = {
        "playing": True,
        "title": title,
        "artist": artist,
        "album": album,
        "state": state,
        "id": track_id,
        "has_cover": props.thumbnail is not None,
        "app": app_id,
    }

    if with_cover and props.thumbnail is not None:
        stream = None
        reader = None
        message_pump = asyncio.create_task(_pump_sta_messages())
        try:
            log.info("Cover read: opening Windows thumbnail stream")
            stream = await props.thumbnail.open_read_async()
            size = int(stream.size)
            log.info("Cover read: stream opened, %s bytes", size)
            if 0 < size <= COVER_MAX_SIZE:
                reader = DataReader(stream.get_input_stream_at(0))
                log.info("Cover read: loading %s bytes", size)
                await reader.load_async(size)
                log.info("Cover read: bytes loaded")
                raw = bytearray(size)
                reader.read_bytes(raw)
                result["cover"] = bytes(raw)
                result["mime"] = stream.content_type or "image/jpeg"
                log.info("Cover read: thumbnail copied into response")
            else:
                log.warning("Windows thumbnail has unsupported size: %s bytes", size)
        finally:
            message_pump.cancel()
            try:
                await message_pump
            except asyncio.CancelledError:
                pass
            if reader is not None:
                reader.close()
            if stream is not None:
                stream.close()
    return result


async def _media_command(action):
    if not MEDIA_AVAILABLE:
        return False
    manager = await MediaManager.request_async()
    session = manager.get_current_session()
    if session is None:
        return False
    methods = {
        "play_pause": session.try_toggle_play_pause_async,
        "next": session.try_skip_next_async,
        "previous": session.try_skip_previous_async,
    }
    return bool(await methods[action]())


async def _pump_sta_messages():
    """Pump COM/WinRT callbacks while awaiting an operation in an STA thread."""
    while True:
        pythoncom.PumpWaitingMessages()
        await asyncio.sleep(0.01)


def get_track(force=False):
    now = time.monotonic()
    if not force:
        with _track_lock:
            cached = _track_cache["data"]
            if cached is not None and now - _track_cache["time"] < TRACK_TTL:
                return dict(cached)
    try:
        data = asyncio.run(_read_track(False))
    except Exception:
        log.exception("Reading media session failed")
        data = {"playing": False, "state": "stopped", "error": "media_read_failed"}
    with _track_lock:
        _track_cache["data"] = dict(data)
        _track_cache["time"] = time.monotonic()
    return data


def _wait_for_change(before, action):
    deadline = time.monotonic() + COMMAND_WAIT
    while time.monotonic() < deadline:
        time.sleep(POLL_STEP)
        current = get_track(force=True)
        if current.get("error"):
            continue
        if action == "play_pause":
            changed = current.get("state") != before.get("state")
        else:
            changed = current.get("id") != before.get("id")
        if changed:
            return current, True
    return get_track(force=True), False


def _cover_process_worker(track_id, q):
    try:
        pythoncom.CoInitialize()
        try:
            data = asyncio.run(_read_track(True))
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
        if not isinstance(data, dict):
            q.put({"ok": False, "error": "invalid_track_data"})
            return
        if data.get("id") != track_id:
            q.put({"ok": False, "error": "track_changed", "current_id": data.get("id")})
            return
        raw = data.get("cover")
        if not raw:
            q.put({"ok": False, "error": "no_cover_bytes"})
            return
        q.put({"ok": True, "mime": data.get("mime", "image/jpeg"), "cover": bytes(raw)})
    except Exception as exc:
        try:
            log.exception("Cover subprocess failed")
        except Exception:
            pass
        try:
            q.put({"ok": False, "error": str(exc)})
        except Exception:
            pass


def get_cover(track_id):
    with _cover_lock:
        item = _cover_cache.get(track_id)
        if item is not None:
            _cover_cache.move_to_end(track_id)
            return item
        if time.monotonic() < float(_cover_reader_state.get("retry_after") or 0.0):
            return None

    if not _cover_read_lock.acquire(blocking=False):
        return None

    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue(maxsize=1)
    proc = ctx.Process(target=_cover_process_worker, args=(track_id, q), name="cover-reader-proc", daemon=True)
    started = time.monotonic()
    with _cover_lock:
        _cover_reader_state.update({"stalled": False, "started_at": started, "retry_after": 0.0})

    try:
        proc.start()
        proc.join(COVER_READ_TIMEOUT)
        if proc.is_alive():
            log.warning(
                "Timed out reading Windows media thumbnail in subprocess after %.1fs; terminating",
                COVER_READ_TIMEOUT,
            )
            try:
                proc.terminate()
            except Exception:
                log.exception("Failed to terminate stalled cover subprocess")
            proc.join(1.0)
            with _cover_lock:
                _cover_reader_state.update({
                    "stalled": True,
                    "started_at": started,
                    "retry_after": time.monotonic() + COVER_RETRY_AFTER_TIMEOUT,
                })
            return None

        result = None
        try:
            result = q.get_nowait()
        except Exception:
            result = None

        if not result or not result.get("ok"):
            if result and result.get("error") == "track_changed":
                log.warning(
                    "Cover id changed during request (requested %s, current %s)",
                    track_id,
                    result.get("current_id"),
                )
            elif result and result.get("error") == "no_cover_bytes":
                log.warning("Windows returned no cover bytes for track %s", track_id)
            elif result:
                log.warning("Cover read failed for track %s: %s", track_id, result.get("error"))
            with _cover_lock:
                _cover_reader_state.update({"stalled": False, "started_at": None, "retry_after": 0.0})
            return None

        item = (result.get("mime", "image/jpeg"), result.get("cover"))
        with _cover_lock:
            _cover_cache[track_id] = item
            _cover_cache.move_to_end(track_id)
            while len(_cover_cache) > COVER_CACHE_LIMIT:
                _cover_cache.popitem(last=False)
            _cover_reader_state.update({"stalled": False, "started_at": None, "retry_after": 0.0})
        return item
    finally:
        try:
            q.close()
            q.join_thread()
        except Exception:
            pass
        _cover_read_lock.release()


def _get_volume_control():
    return AudioUtilities.GetSpeakers().EndpointVolume


@app.route("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/status")
def status():
    try:
        volume = round(_get_volume_control().GetMasterVolumeLevelScalar() * 100)
        memory = psutil.virtual_memory()
        with _cpu_lock:
            cpu = round(float(_cpu_cache.get("value", 0.0)), 1)
        with _cover_lock:
            cover_reader = dict(_cover_reader_state)
        return jsonify({
            "cpu": cpu,
            "ram": memory.percent,
            "ram_used": round(memory.used / (1024 ** 3), 2),
            "volume": volume,
            "track": get_track(),
            "cover_reader": cover_reader,
        })
    except Exception:
        log.exception("Reading system status failed")
        return _error("Unable to read system status", 503)


@app.route("/track")
def track():
    return jsonify({"track": get_track()})


@app.route("/discord/mute", methods=["POST"])
def discord_mute():
    """Send NumPad + to Discord's configured push-to-mute/deafen binding."""
    try:
        result = _send_vk(VK_ADD)
        elevated = _is_elevated()
        result["server_elevated"] = elevated
        ok = bool(result.get("ok"))
        log.info("Discord hotkey sent: vk=%s result=%s", VK_ADD, result)
        status = 200 if ok else 503
        payload = {
            "success": ok,
            "sent": bool(result.get("sent")),
            "state": "unknown",
            "vk": VK_ADD,
            **result,
        }
        if not ok:
            payload["error"] = "hotkey_blocked_by_windows"
        return jsonify(payload), status
    except Exception as exc:
        log.exception("Sending Discord mute hotkey failed")
        return jsonify({"success": False, "sent": False, "error": "hotkey_send_failed", "detail": str(exc)}), 503

@app.route("/cover/<track_id>")
def cover(track_id):
    if len(track_id) != 20 or not track_id.isalnum():
        return _error("Invalid track id", 400)
    item = get_cover(track_id)
    if item is None:
        return _error("Cover unavailable for current track", 404)
    mime, raw = item
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    response = make_response(raw)
    response.headers["Content-Type"] = mime
    response.headers["Cache-Control"] = "private, max-age=60"
    return response


@app.route("/media/<action>", methods=["POST"])
def media_control(action):
    if action not in {"previous", "play_pause", "next"}:
        return _error("Unknown command", 400)
    if not MEDIA_AVAILABLE:
        return jsonify({"success": False, "error": "media_api_unavailable"}), 503
    if not _command_lock.acquire(blocking=False):
        return _error("Another media command is in progress", 409)
    try:
        before = get_track(force=True)
        try:
            sent = bool(asyncio.run(_media_command(action)))
        except Exception:
            log.exception("Sending media command failed")
            sent = False
        if not sent:
            return jsonify({
                "success": False,
                "sent": False,
                "confirmed": False,
                "confirmation": "not_sent",
                "action": action,
                "track": get_track(force=True),
            }), 503
        current, confirmed = _wait_for_change(before, action)
        return jsonify({
            "success": bool(confirmed),
            "sent": True,
            "confirmed": bool(confirmed),
            "confirmation": "confirmed" if confirmed else "not_confirmed",
            "action": action,
            "track": current,
        }), 200
    finally:
        _command_lock.release()


@app.route("/volume", methods=["POST"])
def set_volume():
    try:
        value = _volume_from_request()
    except ValueError as exc:
        return _error(str(exc), 400)
    try:
        control = _get_volume_control()
        control.SetMasterVolumeLevelScalar(value / 100.0, None)
        actual = round(control.GetMasterVolumeLevelScalar() * 100)
        return jsonify({"success": True, "volume": actual})
    except Exception:
        log.exception("Setting volume failed")
        return _error("Unable to set system volume", 503)


if __name__ == "__main__":
    print(f"PC CONTROL listening on http://{HOST}:{PORT}")
    if LAN_MODE:
        print("LAN access is token protected. Use the same token in the dashboard.")
    app.run(host=HOST, port=PORT, threaded=True, debug=False)

